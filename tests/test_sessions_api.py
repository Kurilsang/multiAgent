"""会话 HTTP 面（SPEC-0005 T2/T3 的接口部分）：落库时机、恢复、CRUD、归属。

TestClient 足够——这里断言的是请求/响应与库内容，不涉及「流中途断连」，
那个行为由 tests/test_tasks.py 用真实 uvicorn 覆盖。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.server as server
from app.agent import (
    ActionObserved,
    ActionStarted,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
)
from app.llm import LLMError, ReasoningDelta, TextDelta
from app.tasks import TaskHub

from tests.fakes import SessionStoreMixin, text_round
from tests.test_server_stream import (
    FakeAgentEngine,
    FakeErrorLLM,
    FakeStreamLLM,
    StubSettings,
)
from tests.test_tasks import _GatedEngine as GatedEngine


class SessionApiTest(SessionStoreMixin, unittest.TestCase):
    def setUp(self):
        self._orig_settings = server.settings
        self._orig_llm = server.llm
        server.settings = StubSettings()
        self.setup_session_store()
        self.client = TestClient(server.app)

    def tearDown(self):
        server.settings = self._orig_settings
        server.llm = self._orig_llm
        self.teardown_session_store()

    # ---- 落库时机与失败留痕 ----

    def test_user_message_is_persisted_before_model_call(self):
        """模型调用前就落库：调用中崩溃也不丢提问（SPEC-0005 Q7）。"""
        seen: list[list[dict]] = []

        class RecordingLLM:
            def chat_events(self, messages, provider=None, model=None, tools=None):
                seen.append([dict(m) for m in messages])
                # 此刻库里应已有用户消息
                stored = server.session_store.messages("default")
                yield TextDelta(f"库里已有{len(stored)}条")

        server.llm = RecordingLLM()
        resp = self.client.post("/chat/stream", json={"message": "提问"})
        self.assertIn("库里已有1条", resp.text)
        self.assertEqual(seen and seen[0][-1]["content"], "提问")

    def test_failed_turn_keeps_user_message_and_marks_failure(self):
        server.llm = FakeErrorLLM(LLMError("上游 500"))
        resp = self.client.post("/chat/stream", json={"message": "会失败"})
        self.assertIn("上游 500", resp.text)
        messages = self.session_messages()
        self.assertEqual(messages[0]["content"], "会失败")
        self.assertEqual(messages[1]["status"], "failed")

    def test_reasoning_and_steps_are_persisted(self):
        server.llm = FakeStreamLLM([ReasoningDelta("想想"), TextDelta("答案")])
        self.client.post("/chat/stream", json={"message": "问"})
        assistant = self.session_messages()[1]
        self.assertEqual(assistant["reasoning"], "想想")
        self.assertEqual(assistant["content"], "答案")

    def test_provider_attribution_recorded_per_answer(self):
        """每条回答记住实际厂商/模型（SPEC-0005 Q19）。"""
        server.llm = FakeStreamLLM([TextDelta("答案")])
        self.client.post("/chat/stream", json={"message": "问"})
        assistant = self.session_messages()[1]
        self.assertEqual(assistant["provider"], "glm")
        self.assertTrue(assistant["model"])

    def test_task_answer_also_records_provider(self):
        """任务通道的答案同样带归属（否则导出对任务轮次继续说谎）。"""
        from app.agent import TaskFinished, TaskStarted

        class FakeEngine:
            def run(self, task, provider=None, model=None, activated=(), **kwargs):
                yield TaskStarted(task=task)
                yield TaskFinished(status="completed", answer="任务答案", iterations=1)

        server.agent_engine = FakeEngine()
        try:
            resp = self.client.post("/agent/stream", json={"task": "干个活"})
            self.assertEqual(resp.status_code, 200)
            # 等终态回写完成（TestClient 会读完整个流）
            messages = self.session_messages()
            self.assertEqual([m["content"] for m in messages], ["干个活", "任务答案"])
            self.assertEqual(messages[1]["provider"], "glm")
            self.assertTrue(messages[1]["model"])
        finally:
            pass

    def test_session_named_from_first_message_by_server(self):
        """会话名由服务端按首条消息生成（前端不再复制截断规则）。"""
        created = self.client.post("/sessions", json={}).json()["session_id"]
        self.assertEqual(server.session_store.get_session(created)["name"], "")
        server.llm = FakeStreamLLM([TextDelta("答")])
        self.client.post(
            "/chat/stream", json={"message": "帮我看看这段代码", "session_id": created}
        )
        self.assertEqual(
            server.session_store.get_session(created)["name"], "帮我看看这段代码"
        )

    def test_export_marks_per_message_provider(self):
        """导出标注每条回答的实际厂商，而不是一律套默认厂商。"""
        server.llm = FakeStreamLLM([TextDelta("答案")])
        self.client.post("/chat/stream", json={"message": "问"})
        text = self.client.get("/export").text
        self.assertIn("（glm /", text)

    def test_chat_in_progress_rejects_new_task_in_same_session(self):
        """聊天轮次进行中（持会话锁）时，同会话发起任务必须被拒绝。

        只查 task_hub 的话，一次长聊天期间发起的任务会溜进来，
        同一会话就出现两个并发模型调用 + 交错的落库写入。
        """
        started = threading.Event()
        release = threading.Event()

        class SlowLLM:
            def chat_events(self, messages, provider=None, model=None, tools=None):
                started.set()
                release.wait(timeout=10)
                yield from text_round("慢回复")

        server.llm = SlowLLM()
        errors: list = []

        def chat():
            try:
                self.client.post("/chat/stream", json={"message": "慢问题"})
            except Exception as exc:  # pragma: no cover - 线程内异常回传
                errors.append(exc)

        thread = threading.Thread(target=chat)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=10))
            resp = self.client.post("/agent/stream", json={"task": "插队任务"})
            self.assertEqual(resp.status_code, 409, resp.text)
            self.assertIn("正有一轮对话在进行", resp.json()["detail"])
        finally:
            release.set()
            thread.join(timeout=15)
        self.assertEqual(errors, [])

    # ---- 恢复 ----

    def test_history_survives_store_reopen(self):
        """模拟重启：换一个 store 实例读同一文件，历史仍在。"""
        server.llm = FakeStreamLLM([TextDelta("第一答")])
        self.client.post("/chat/stream", json={"message": "第一问"})
        path = server.session_store.path
        server.reset_session_store(path)  # 重开同一文件
        messages = server.session_store.messages("default")
        self.assertEqual([m["content"] for m in messages], ["第一问", "第一答"])

    def test_messages_endpoint_returns_full_history(self):
        server.llm = FakeStreamLLM([ReasoningDelta("想"), TextDelta("答")])
        self.client.post("/chat/stream", json={"message": "问"})
        payload = self.client.get("/sessions/default/messages").json()
        self.assertEqual(payload["session"]["id"], "default")
        self.assertEqual([m["content"] for m in payload["messages"]], ["问", "答"])
        self.assertEqual(payload["messages"][1]["reasoning"], "想")

    def test_messages_endpoint_404_for_unknown_session(self):
        self.assertEqual(
            self.client.get("/sessions/nope/messages").status_code, 404
        )

    def test_context_sent_to_model_is_truncated_but_history_is_full(self):
        """展示全量、入模截断（SPEC-0005 Q8）。"""
        server.settings.max_context_messages = 4
        seen: list[list[dict]] = []

        class RecordingLLM:
            def chat_events(self, messages, provider=None, model=None, tools=None):
                seen.append([dict(m) for m in messages])
                yield TextDelta("ok")

        server.llm = RecordingLLM()
        for index in range(6):
            self.client.post("/chat/stream", json={"message": f"q{index}"})
        # 库里是全量（12 条：6 问 6 答）
        self.assertEqual(len(self.session_messages()), 12)
        # 入模 = 技能 system 提示 + 最近 4 条历史（本轮提问已先落库）
        sent = seen[-1]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(len(sent), 5)
        self.assertEqual(
            [m["content"] for m in sent[1:]], ["ok", "q4", "ok", "q5"]
        )

    # ---- 会话 CRUD ----

    def test_list_sessions_includes_default(self):
        payload = self.client.get("/sessions").json()
        self.assertEqual(payload["default_session_id"], "default")
        self.assertEqual([s["id"] for s in payload["sessions"]], ["default"])

    def test_create_session_and_use_it(self):
        created = self.client.post("/sessions", json={"name": "会话A"}).json()
        session_id = created["session_id"]
        server.llm = FakeStreamLLM([TextDelta("A 的答案")])
        self.client.post(
            "/chat/stream", json={"message": "A 的问题", "session_id": session_id}
        )
        self.assertEqual(
            [m["content"] for m in self.session_messages(session_id)],
            ["A 的问题", "A 的答案"],
        )
        # 默认会话不受影响（会话隔离）
        self.assertEqual(self.session_messages("default"), [])

    def test_unknown_session_id_rejected(self):
        resp = self.client.post(
            "/chat/stream", json={"message": "hi", "session_id": "nope"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_rename_and_delete_session(self):
        session_id = self.client.post("/sessions", json={"name": "旧名"}).json()[
            "session_id"
        ]
        renamed = self.client.post(
            f"/sessions/{session_id}/rename", json={"name": "新名"}
        ).json()
        self.assertEqual(renamed["session"]["name"], "新名")
        self.assertEqual(
            self.client.delete(f"/sessions/{session_id}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/sessions/{session_id}/messages").status_code, 404
        )

    def test_reset_creates_new_session_without_destroying_old(self):
        """「清空」= 新建会话（SPEC-0005 Q6）：旧记录留在列表里。"""
        server.llm = FakeStreamLLM([TextDelta("旧答案")])
        self.client.post("/chat/stream", json={"message": "旧问题"})
        new_id = self.client.post("/reset").json()["session_id"]
        self.assertNotEqual(new_id, "default")
        # 旧会话内容完好
        self.assertEqual(
            [m["content"] for m in self.session_messages("default")],
            ["旧问题", "旧答案"],
        )
        ids = [s["id"] for s in self.client.get("/sessions").json()["sessions"]]
        self.assertIn("default", ids)
        self.assertIn(new_id, ids)

    def test_session_name_defaults_from_first_message(self):
        """会话名缺省取首条消息截断（WebUI 建会话时使用）。"""
        from app.sessions import SESSION_NAME_CHARS, session_name_from

        short = "帮我看看这段代码"
        self.assertEqual(session_name_from(short), short)
        long_text = "帮我看看这段代码为什么一运行就报这个奇怪的错误"
        name = session_name_from(long_text)
        self.assertEqual(name, long_text[:SESSION_NAME_CHARS] + "…")

    def test_export_scoped_to_session(self):
        server.llm = FakeStreamLLM([TextDelta("答案")])
        self.client.post("/chat/stream", json={"message": "问题"})
        other = self.client.post("/sessions", json={"name": "空会话"}).json()[
            "session_id"
        ]
        text = self.client.get(f"/export?session_id={other}").text
        self.assertIn("消息数：0", text)
        text = self.client.get("/export?session_id=default").text
        self.assertIn("消息数：2", text)


class TaskReplayApiTest(SessionStoreMixin, unittest.TestCase):
    """#58 任务归属与轨迹回放：刷新后按会话读回轨迹，中断如实标注。"""

    def setUp(self):
        self._orig_settings = server.settings
        self._orig_engine = server.agent_engine
        self._orig_audit_dir = server.audit_log_dir
        self._orig_hub = server.task_hub
        self._audit_tmp = tempfile.TemporaryDirectory()
        server.audit_log_dir = Path(self._audit_tmp.name)
        server.settings = StubSettings()
        server.task_hub = TaskHub()
        self.setup_session_store()
        self.client = TestClient(server.app)

    def tearDown(self):
        for record in server.task_hub.running_tasks():
            record.cancel.cancel()
        server.task_hub = self._orig_hub
        server.settings = self._orig_settings
        server.agent_engine = self._orig_engine
        server.audit_log_dir = self._orig_audit_dir
        self._audit_tmp.cleanup()
        self.teardown_session_store()

    def test_completed_task_replays_with_events(self):
        server.agent_engine = FakeAgentEngine(
            [
                TaskStarted(task="干个活"),
                ThoughtDelta(text="想一下"),
                ActionStarted(tool_name="echo", arguments={}),
                ActionObserved(tool_name="echo", result="ok", is_error=False),
                TaskFinished(status="completed", answer="做完了", iterations=2),
            ]
        )
        self.client.post("/agent/stream", json={"task": "干个活"})
        payload = self.client.get("/sessions/default/tasks").json()
        self.assertEqual(len(payload["tasks"]), 1)
        entry = payload["tasks"][0]
        self.assertEqual(entry["task"], "干个活")
        self.assertEqual(entry["status"], "completed")
        self.assertFalse(entry["interrupted"])
        # 回放给的是服务端编码好的帧（前端直接重放，不再复刻编码逻辑）
        kinds = [f["type"] for f in entry["frames"]]
        self.assertEqual(
            kinds, ["task_started", "thought_delta", "action", "observation", "final"]
        )
        self.assertEqual([f["seq"] for f in entry["frames"]], [1, 2, 3, 4, 5])

    def test_interrupted_task_is_marked_not_faked(self):
        """索引说在跑、登记处没有它、日志无终态 = 已中断（进程重启）。"""
        session_store = server.session_store
        session_store.record_task(
            "deadbeef", "default", "半截任务", status="running", log_path=str(
                Path(server.audit_log_dir) / "20261010T120000-deadbeef.jsonl"
            )
        )
        # 造一份没有终态事件的日志（进程被杀的样子）
        log = Path(server.audit_log_dir) / "20261010T120000-deadbeef.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "TaskAuditHeader",
                    "ts": "2026-10-10T12:00:00",
                    "task_id": "deadbeef",
                    "task": "半截任务",
                    "session_id": "default",
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {"type": "TaskStarted", "task": "半截任务", "agent_id": "main",
                 "agent_name": "编排器", "role": "orchestrator", "parent_id": None},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        payload = self.client.get("/sessions/default/tasks").json()
        entry = payload["tasks"][0]
        self.assertEqual(entry["status"], "interrupted")
        self.assertTrue(entry["interrupted"])
        # 已有进展照常展示，不假装无事发生
        self.assertIn("task_started", [f["type"] for f in entry["frames"]])

    def test_orphan_log_without_session_not_in_any_timeline(self):
        """无会话归属的旧日志不进任何会话时间线。"""
        from app.audit import TaskAuditLog as Log

        orphan = Log("孤儿任务", directory=server.audit_log_dir)  # 不传 session_id
        orphan.record(TaskStarted(task="孤儿任务"))
        orphan.record(TaskFinished(status="completed", answer="x", iterations=1))
        payload = self.client.get("/sessions/default/tasks").json()
        self.assertEqual(payload["tasks"], [])

    def test_tasks_endpoint_404_for_unknown_session(self):
        self.assertEqual(self.client.get("/sessions/nope/tasks").status_code, 404)

    def test_running_task_is_marked_live(self):
        # 复用 test_tasks 的门控引擎（同一种「卡住的长任务」替身）
        gate = threading.Event()
        server.agent_engine = GatedEngine(gate)
        try:
            with self.client.stream("POST", "/agent/stream", json={"task": "长任务"}):
                pass  # TestClient 整流缓冲，出来时任务已结束
        finally:
            gate.set()
        payload = self.client.get("/sessions/default/tasks").json()
        self.assertEqual(len(payload["tasks"]), 1)
        # 已结束：live 为 False，状态是终态而非 interrupted
        self.assertFalse(payload["tasks"][0]["live"])
        self.assertEqual(payload["tasks"][0]["status"], "completed")

    # ---- #59 重启后的轨迹接续 ----

    def _write_dead_task_log(self, task_id: str, with_terminal: bool) -> Path:
        log = Path(server.audit_log_dir) / f"20261010T120000-{task_id}.jsonl"
        lines = [
            {
                "type": "TaskAuditHeader",
                "ts": "2026-10-10T12:00:00",
                "task_id": task_id,
                "task": "半截任务",
                "session_id": "default",
            },
            {
                "type": "TaskStarted",
                "task": "半截任务",
                "agent_id": "main",
                "agent_name": "编排器",
                "role": "orchestrator",
                "parent_id": None,
            },
            {
                "type": "ThoughtDelta",
                "text": "想一下",
                "agent_id": "main",
                "agent_name": "编排器",
                "role": "orchestrator",
                "parent_id": None,
            },
        ]
        if with_terminal:
            lines.append(
                {
                    "type": "TaskFinished",
                    "status": "completed",
                    "answer": "其实跑完了",
                    "iterations": 1,
                    "agent_id": "main",
                    "agent_name": "编排器",
                    "role": "orchestrator",
                    "parent_id": None,
                }
            )
        log.write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
            encoding="utf-8",
        )
        return log

    def test_restart_reattach_replays_from_audit_log(self):
        """进程重启后按 task_id 请求：从审计日志补历史并标注已中断。"""
        log = self._write_dead_task_log("deadbeef", with_terminal=False)
        server.session_store.record_task(
            "deadbeef", "default", "半截任务", status="running", log_path=str(log)
        )
        resp = self.client.get("/agent/tasks/deadbeef/stream?from_seq=0")
        self.assertEqual(resp.status_code, 200)
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertTrue(frames[0]["replayed"])
        kinds = [f["type"] for f in frames]
        self.assertIn("task_started", kinds)
        self.assertIn("thought_delta", kinds)
        # 明确标注中断，而不是假装任务正常结束
        self.assertEqual(kinds[-1], "failed")
        self.assertIn("已中断", frames[-1]["reason"])

    def test_restart_reattach_respects_from_seq(self):
        log = self._write_dead_task_log("deadbeef", with_terminal=True)
        server.session_store.record_task(
            "deadbeef", "default", "半截任务", status="running", log_path=str(log)
        )
        resp = self.client.get("/agent/tasks/deadbeef/stream?from_seq=2")
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        events = [f for f in frames if f.get("type") != "task_meta"]
        # 只补 seq > 2 的部分
        self.assertTrue(all(f["seq"] > 2 for f in events))

    def test_restart_task_status_reports_interrupted(self):
        log = self._write_dead_task_log("deadbeef", with_terminal=False)
        server.session_store.record_task(
            "deadbeef", "default", "半截任务", status="running", log_path=str(log)
        )
        payload = self.client.get("/agent/tasks/deadbeef").json()
        self.assertEqual(payload["status"], "interrupted")
        self.assertTrue(payload["finished"])
        self.assertTrue(payload["replayed"])
        self.assertGreater(payload["events"], 0)

    def test_restart_task_status_completed_is_not_interrupted(self):
        log = self._write_dead_task_log("cafebabe", with_terminal=True)
        server.session_store.record_task(
            "cafebabe", "default", "跑完的任务", status="completed", log_path=str(log)
        )
        payload = self.client.get("/agent/tasks/cafebabe").json()
        self.assertEqual(payload["status"], "completed")
        self.assertFalse(payload["replayed"] and payload["status"] == "interrupted")

    def test_unknown_task_still_404(self):
        self.assertEqual(self.client.get("/agent/tasks/nope").status_code, 404)
        self.assertEqual(
            self.client.get("/agent/tasks/nope/stream").status_code, 404
        )

    def test_session_list_exposes_interrupted_flag(self):
        """侧栏状态点：索引里留着 running 而登记处没有 = 已中断。"""
        log = self._write_dead_task_log("deadbeef", with_terminal=False)
        server.session_store.record_task(
            "deadbeef", "default", "半截任务", status="running", log_path=str(log)
        )
        payload = self.client.get("/sessions").json()
        entry = next(s for s in payload["sessions"] if s["id"] == "default")
        self.assertTrue(entry["interrupted"])
        self.assertIsNone(entry["running_task_id"])

    def test_replay_preserves_subagent_identity_and_rework(self):
        """回放要能重建子 agent 分区与回炉标记：事件顺序与身份字段必须完整。

        前端用同一套时间线渲染器消费这些帧——根 agent 的第二次
        task_started 即回炉标记，子 agent 帧带 agent_id/parent_id 才分得开。
        """
        server.agent_engine = FakeAgentEngine(
            [
                TaskStarted(task="主任务"),
                ActionStarted(tool_name="dispatch_task", arguments={"subtasks": []}),
                TaskStarted(
                    task="子任务",
                    agent_id="sub-1",
                    agent_name="子任务1",
                    role="worker",
                    parent_id="main",
                ),
                ThoughtDelta(
                    text="子思考",
                    agent_id="sub-1",
                    agent_name="子任务1",
                    role="worker",
                    parent_id="main",
                ),
                TaskFinished(
                    status="completed",
                    answer="子结果",
                    iterations=1,
                    agent_id="sub-1",
                    agent_name="子任务1",
                    role="worker",
                    parent_id="main",
                ),
                # 回炉：根 agent 再次 task_started
                TaskStarted(task="主任务（含审查意见）"),
                TaskFinished(status="completed", answer="总答案", iterations=2),
            ]
        )
        self.client.post("/agent/stream", json={"task": "主任务"})
        entry = self.client.get("/sessions/default/tasks").json()["tasks"][0]
        frames = entry["frames"]
        # 两个根 task_started = 回炉标记的判据
        roots = [
            f for f in frames
            if f["type"] == "task_started" and "agent_id" not in f
        ]
        self.assertEqual(len(roots), 2)
        # 子 agent 身份完整（分区靠它）
        worker = [f for f in frames if f.get("agent_id") == "sub-1"]
        self.assertTrue(worker)
        self.assertTrue(all(f.get("parent_id") == "main" for f in worker))
        self.assertTrue(all(f.get("role") == "worker" for f in worker))
        self.assertIn("final", [f["type"] for f in frames])

    def test_replay_frames_carry_identity_via_frame_encoder(self):
        """帧编码器要同时吃得下引擎事件与审计 dict（回放不另写一套渲染）。"""
        from app.agent import ActionObserved as Obs
        from app.server import _agent_event_frame

        live = _agent_event_frame(
            Obs(
                tool_name="echo",
                result="ok",
                is_error=False,
                agent_id="sub-1",
                agent_name="子任务1",
                role="worker",
                parent_id="main",
            )
        )
        replayed = _agent_event_frame(
            {
                "type": "ActionObserved",
                "tool_name": "echo",
                "result": "ok",
                "is_error": False,
                "agent_id": "sub-1",
                "agent_name": "子任务1",
                "role": "worker",
                "parent_id": "main",
            }
        )
        self.assertEqual(live, replayed)
        self.assertEqual(live["agent_id"], "sub-1")
        self.assertEqual(live["parent_id"], "main")

    def test_failed_handle_note_survives_replay(self):
        """旁存句柄失效的说明随事件落盘，回放时如实呈现（不伪造）。"""
        note = (
            "观察值句柄已失效或不存在：'r1'"
            "（可能已被内存池淘汰或服务重启清空）"
        )
        server.agent_engine = FakeAgentEngine(
            [
                TaskStarted(task="读大结果"),
                ActionStarted(tool_name="read_tool_result", arguments={"handle": "r1"}),
                ActionObserved(
                    tool_name="read_tool_result", result=note, is_error=True
                ),
                TaskFinished(status="partial", answer="只拿到部分", iterations=2),
            ]
        )
        self.client.post("/agent/stream", json={"task": "读大结果"})
        entry = self.client.get("/sessions/default/tasks").json()["tasks"][0]
        observed = [f for f in entry["frames"] if f["type"] == "observation"][0]
        self.assertTrue(observed["is_error"])
        self.assertIn("句柄已失效", observed["text"])


if __name__ == "__main__":
    unittest.main()
