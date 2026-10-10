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
from app.llm import LLMError, ReasoningDelta, TextDelta

from tests.fakes import SessionStoreMixin, text_round
from tests.test_server_stream import FakeErrorLLM, FakeStreamLLM, StubSettings


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


if __name__ == "__main__":
    unittest.main()
