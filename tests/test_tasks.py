"""任务生命周期与 HTTP 连接解耦（SPEC-0005 T1 / issue #55）。

断言的是外部行为：断连后任务继续跑、不置取消、不泄漏会话锁、可重连续流、
同会话并发被明确拒绝、停止在厂商流内生效——不测内部实现细节。

HTTP 层刻意起**真实 uvicorn**（回环随机端口）：Starlette 的 TestClient 对
同步生成器会整流缓冲（实测首帧要等流结束才返回），无法观察「断连后任务
仍在跑」，而那个行为正是本票的核心。真实套接字是唯一诚实的验证面，
先例见 tests/test_catalog_http.py 与 tests/test_mcp_anchor.py。
"""

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

import app.server as server
from app.agent import (
    CancelToken,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
)
from app.audit import read_task_logs
from app.tasks import SubscriberOverflow, TaskBusy, TaskHub

from tests.fakes import SessionStoreMixin
from tests.test_server_stream import StubSettings


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    """真实 uvicorn（回环）：可逐帧读、可在流中途断开。"""

    def __init__(self, app):
        self.port = _free_port()
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error"
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                status, _body = self.request("GET", "/health")
                if status == 200:
                    return self
            except OSError:
                time.sleep(0.05)
        raise AssertionError("测试用 uvicorn 未在 15s 内就绪")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def connect(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        return conn, conn.getresponse()

    def request(self, method: str, path: str, body: dict | None = None):
        conn, resp = self.connect(method, path, body)
        try:
            return resp.status, resp.read()
        finally:
            conn.close()

    def json(self, method: str, path: str, body: dict | None = None) -> dict:
        status, raw = self.request(method, path, body)
        return {"status": status, **json.loads(raw.decode("utf-8"))}


def read_frame(resp, buffer: list[bytes]) -> dict:
    """从流式响应里读出一帧（data: {...}），把剩余字节留在 buffer。"""
    while True:
        joined = b"".join(buffer)
        index = joined.find(b"\n\n")
        if index >= 0:
            frame = joined[:index]
            buffer.clear()
            rest = joined[index + 2 :]
            if rest:
                buffer.append(rest)
            for line in frame.split(b"\n"):
                if line.startswith(b"data: "):
                    return json.loads(line[len(b"data: ") :].decode("utf-8"))
        chunk = resp.read(1)
        if not chunk:
            raise AssertionError("流在读出完整帧之前结束")
        buffer.append(chunk)


def _slow_runner(steps=3, gate: threading.Event | None = None):
    """可控 runner：每步产出事件，可被 gate 卡住模拟长任务。"""

    def runner(cancel: CancelToken):
        yield TaskStarted(task="长任务")
        for index in range(steps):
            if gate is not None and index == 0:
                gate.wait(timeout=10)
            yield ThoughtDelta(text=f"第{index}步")
        yield TaskFinished(status="completed", answer="做完了", iterations=steps)

    return runner


class TaskHubTest(unittest.TestCase):
    """任务登记处：事件缓冲、订阅、停止（不经过 HTTP 层）。"""

    def setUp(self):
        self.hub = TaskHub()

    def test_task_runs_to_completion_without_any_subscriber(self):
        """没有任何订阅者时任务照常跑完（断连 = 退订，不是取消）。"""
        record = self.hub.start(session_id="s1", task="长任务", runner=_slow_runner())
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(record.finished)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.answer, "做完了")
        self.assertFalse(record.cancel.cancelled)

    def test_abandoned_subscriber_does_not_cancel_or_block_task(self):
        """订阅者被遗弃（模拟客户端断连）后任务继续，且不置取消标志。"""
        gate = threading.Event()
        record = self.hub.start(
            session_id="s1", task="长任务", runner=_slow_runner(gate=gate)
        )
        stream = record.subscribe(0)
        first = next(stream)  # 只读一帧就「断连」
        self.assertEqual(first[0], 1)
        stream.close()  # 生成器收尾 = 退订
        gate.set()
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(record.finished)
        self.assertEqual(record.status, "completed")
        # 关键断言：断连**没有**置取消标志（旧行为是断连即取消）
        self.assertFalse(record.cancel.cancelled)
        # 事件全部留在缓冲里，供重连补发
        self.assertGreaterEqual(record.event_count, 4)

    def test_reattach_replays_missing_events_without_gaps_or_dupes(self):
        record = self.hub.start(session_id="s1", task="长任务", runner=_slow_runner())
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        all_frames = list(record.subscribe(0))
        seqs = [seq for seq, _ts, _event in all_frames]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))  # 无缺口、无重复
        # 事件带时间戳（与 seq 一起构成可回放时间线）
        self.assertTrue(all(ts for _seq, ts, _event in all_frames))
        # 从第 2 条之后接回：只补缺口
        resumed = list(record.subscribe(2))
        self.assertEqual([seq for seq, _ts, _event in resumed], seqs[2:])

    def test_multiple_subscribers_each_get_full_stream(self):
        """多订阅者广播：多标签页都能看到同一任务的完整事件。"""
        record = self.hub.start(session_id="s1", task="长任务", runner=_slow_runner())
        collected: dict[int, list[int]] = {}
        lock = threading.Lock()

        def watch(key: int) -> None:
            frames = []
            for seq, _ts, _event in record.subscribe(0):
                frames.append(seq)
            with lock:
                collected[key] = frames

        threads = [threading.Thread(target=watch, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(collected), 3)
        for frames in collected.values():
            self.assertEqual(frames, list(range(1, len(frames) + 1)))

    def test_stop_marks_cancel_and_task_ends_partial(self):
        gate = threading.Event()

        def runner(cancel: CancelToken):
            yield TaskStarted(task="长任务")
            gate.wait(timeout=10)
            if cancel.cancelled:
                yield TaskFinished(status="partial", answer="停在这里", iterations=1)
                return
            yield TaskFinished(status="completed", answer="做完了", iterations=1)

        record = self.hub.start(session_id="s1", task="长任务", runner=runner)
        self.assertTrue(self.hub.stop(record.task_id))
        gate.set()
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(record.status, "partial")
        self.assertEqual(record.answer, "停在这里")

    def test_second_task_in_same_session_is_rejected(self):
        gate = threading.Event()
        record = self.hub.start(
            session_id="s1", task="长任务", runner=_slow_runner(gate=gate)
        )
        try:
            with self.assertRaises(TaskBusy) as ctx:
                self.hub.start(session_id="s1", task="另一个", runner=_slow_runner())
            self.assertEqual(ctx.exception.task_id, record.task_id)
        finally:
            gate.set()

    def test_different_sessions_run_concurrently(self):
        """不同会话互不阻塞（拆掉全局锁的收益）。"""
        gate = threading.Event()
        first = self.hub.start(
            session_id="s1", task="任务一", runner=_slow_runner(gate=gate)
        )
        try:
            second = self.hub.start(
                session_id="s2", task="任务二", runner=_slow_runner()
            )
            deadline = time.time() + 10
            while not second.finished and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(second.finished)
            self.assertFalse(first.finished)  # 第一个仍被 gate 卡着
        finally:
            gate.set()

    def test_runner_exception_becomes_failed_terminal(self):
        def boom(cancel: CancelToken):
            yield TaskStarted(task="坏任务")
            raise RuntimeError("引擎炸了")

        record = self.hub.start(session_id="s1", task="坏任务", runner=boom)
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(record.status, "failed")
        self.assertIn("引擎炸了", record.reason)

    def test_runner_without_terminal_is_marked_interrupted(self):
        """没有终态事件（进程中断的样子）= 如实标注，不假装成功。"""

        def truncated(cancel: CancelToken):
            yield TaskStarted(task="半截任务")
            yield ThoughtDelta(text="想到一半")

        record = self.hub.start(session_id="s1", task="半截任务", runner=truncated)
        deadline = time.time() + 10
        while not record.finished and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(record.status, "failed")
        self.assertIn("未产出终态", record.reason)

    def test_slow_subscriber_overflow_is_reported_not_silent(self):
        """消费过慢导致积压超限：抛 SubscriberOverflow 并带 last_seq（不静默截断）。"""
        hub = TaskHub(subscriber_max=2)
        gate = threading.Event()

        def runner(cancel: CancelToken):
            yield TaskStarted(task="多事件任务")
            gate.wait(timeout=10)
            for index in range(10):
                yield ThoughtDelta(text=f"第{index}步")
            yield TaskFinished(status="completed", answer="完", iterations=1)

        record = hub.start(session_id="s1", task="多事件任务", runner=runner)
        stream = record.subscribe(0)
        next(stream)  # 订阅但不再消费，让收件箱积压
        gate.set()
        deadline = time.time() + 10
        while record.event_count < 6 and time.time() < deadline:
            time.sleep(0.01)
        with self.assertRaises(SubscriberOverflow) as ctx:
            for _ in stream:
                pass
        self.assertGreaterEqual(ctx.exception.last_seq, 1)
        self.assertIn("消费过慢", ctx.exception.reason)


class TaskEndpointTest(SessionStoreMixin, unittest.TestCase):
    """HTTP 面（真实 uvicorn）：断连不取消、可重连、并发拒绝、管理面不被阻塞。"""

    def setUp(self):
        self._orig_engine = server.agent_engine
        self._orig_settings = server.settings
        self._orig_audit_dir = server.audit_log_dir
        self._orig_hub = server.task_hub
        self._audit_tmp = tempfile.TemporaryDirectory()
        server.audit_log_dir = Path(self._audit_tmp.name)
        server.settings = StubSettings()
        server.task_hub = TaskHub()
        self.setup_session_store()

    def tearDown(self):
        for record in server.task_hub.running_tasks():
            record.cancel.cancel()
        server.task_hub = self._orig_hub
        server.agent_engine = self._orig_engine
        server.settings = self._orig_settings
        server.audit_log_dir = self._orig_audit_dir
        self._audit_tmp.cleanup()
        self.teardown_session_store()

    def _wait_finished(self, live: LiveServer, task_id: str, timeout=15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = live.json("GET", f"/agent/tasks/{task_id}")
            if payload.get("finished"):
                return payload
            time.sleep(0.02)
        raise AssertionError(f"任务未在 {timeout}s 内结束: {task_id}")

    def test_client_disconnect_leaves_task_running_and_audit_complete(self):
        """核心回归：断连后任务照跑完、审计日志有终态（旧行为是冻住）。"""
        gate = threading.Event()
        server.agent_engine = _GatedEngine(gate)
        with LiveServer(server.app) as live:
            conn, resp = live.connect("POST", "/agent/stream", {"task": "长任务"})
            buffer: list[bytes] = []
            meta = read_frame(resp, buffer)
            task_id = meta["task_id"]
            conn.close()  # 断连（不读完）
            self.assertTrue(server.task_hub.running_tasks())  # 任务仍在跑
            gate.set()
            payload = self._wait_finished(live, task_id)
        self.assertEqual(payload["status"], "completed")
        logs = read_task_logs(server.audit_log_dir)
        self.assertEqual(len(logs), 1)
        types = [event["type"] for event in logs[0]["events"]]
        self.assertIn("TaskFinished", types)  # 断连不再截断留痕

    def test_disconnect_does_not_set_cancel_flag(self):
        gate = threading.Event()
        server.agent_engine = _GatedEngine(gate)
        with LiveServer(server.app) as live:
            conn, resp = live.connect("POST", "/agent/stream", {"task": "长任务"})
            buffer: list[bytes] = []
            task_id = read_frame(resp, buffer)["task_id"]
            conn.close()
            record = server.task_hub.get(task_id)
            self.assertIsNotNone(record)
            self.assertFalse(record.cancel.cancelled)
            gate.set()
            self._wait_finished(live, task_id)

    def test_other_endpoints_stay_responsive_while_task_runs(self):
        """任务在跑时 /export 与管理面不被阻塞（旧行为是全局锁被持有）。"""
        gate = threading.Event()
        server.agent_engine = _GatedEngine(gate)
        try:
            with LiveServer(server.app) as live:
                conn, resp = live.connect(
                    "POST", "/agent/stream", {"task": "长任务"}
                )
                buffer: list[bytes] = []
                read_frame(resp, buffer)
                # 任务仍在跑（gate 未放行），这些端点必须照常应答
                self.assertTrue(server.task_hub.running_tasks())
                self.assertEqual(live.request("GET", "/export")[0], 200)
                self.assertEqual(live.json("GET", "/skills")["status"], 200)
                self.assertEqual(live.json("GET", "/sessions")["status"], 200)
                conn.close()
        finally:
            gate.set()

    def test_second_task_in_same_session_rejected_with_409(self):
        gate = threading.Event()
        server.agent_engine = _GatedEngine(gate)
        try:
            with LiveServer(server.app) as live:
                conn, resp = live.connect(
                    "POST", "/agent/stream", {"task": "长任务"}
                )
                buffer: list[bytes] = []
                read_frame(resp, buffer)
                second = live.json("POST", "/agent/stream", {"task": "插队"})
                self.assertEqual(second["status"], 409)
                self.assertIn("已有任务在跑", second["detail"])
                # 同会话的聊天同样被明确拒绝（不排队、不阻塞）
                chat = live.json("POST", "/chat", {"message": "顺便问一句"})
                self.assertEqual(chat["status"], 409)
                conn.close()
        finally:
            gate.set()

    def test_reattach_stream_replays_from_seq(self):
        gate = threading.Event()
        server.agent_engine = _GatedEngine(gate)
        with LiveServer(server.app) as live:
            conn, resp = live.connect("POST", "/agent/stream", {"task": "长任务"})
            buffer: list[bytes] = []
            task_id = read_frame(resp, buffer)["task_id"]
            read_frame(resp, buffer)  # 再收一帧
            conn.close()
            gate.set()
            self._wait_finished(live, task_id)
            # 从 seq=1 之后接回：补发缺口，不重复
            status, raw = live.request(
                "GET", f"/agent/tasks/{task_id}/stream?from_seq=1"
            )
            self.assertEqual(status, 200)
            events = []
            for line in raw.decode("utf-8").splitlines():
                if line.startswith("data: "):
                    frame = json.loads(line[len("data: ") :])
                    if frame.get("type") != "task_meta":
                        events.append(frame)
        self.assertTrue(events)
        self.assertTrue(all(f["seq"] > 1 for f in events))
        seqs = [f["seq"] for f in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_task_status_endpoint_reports_state(self):
        server.agent_engine = _GatedEngine(threading.Event(), release=True)
        with LiveServer(server.app) as live:
            conn, resp = live.connect("POST", "/agent/stream", {"task": "短任务"})
            buffer: list[bytes] = []
            task_id = read_frame(resp, buffer)["task_id"]
            conn.close()
            payload = self._wait_finished(live, task_id)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["answer"], "完成")
        self.assertGreater(payload["events"], 0)

    def test_unknown_task_id_returns_404(self):
        with LiveServer(server.app) as live:
            self.assertEqual(live.json("GET", "/agent/tasks/nope")["status"], 404)

    def test_stop_effective_while_engine_blocked_in_provider_stream(self):
        """停止在厂商流内生效：引擎卡在一次长调用里也能停（SPEC-0005 T1）。"""
        started = threading.Event()
        release = threading.Event()
        server.agent_engine = _StuckInStreamEngine(started, release)
        try:
            with LiveServer(server.app) as live:
                conn, resp = live.connect(
                    "POST", "/agent/stream", {"task": "卡住的任务"}
                )
                buffer: list[bytes] = []
                task_id = read_frame(resp, buffer)["task_id"]
                self.assertTrue(started.wait(timeout=10))
                stopped = live.json("POST", "/agent/stop", {"task_id": task_id})
                self.assertEqual(stopped["stopped"], True)
                # 引擎在流内察觉取消并收尾，无需等厂商调用返回
                payload = self._wait_finished(live, task_id, timeout=10)
                conn.close()
        finally:
            release.set()
        self.assertEqual(payload["status"], "partial")


class _GatedEngine:
    """假引擎：产出一帧后卡在 gate 上，直到测试放行（模拟长任务）。"""

    def __init__(self, gate: threading.Event, release: bool = False):
        self._gate = gate
        self._release = release

    def run(self, task, provider=None, model=None, activated=(), **kwargs):
        yield TaskStarted(task=task)
        yield ThoughtDelta(text="想一下")
        if not self._release:
            self._gate.wait(timeout=10)
        yield TaskFinished(status="completed", answer="完成", iterations=1)


class _StuckInStreamEngine:
    """假引擎：卡在「厂商流」内部，只有取消标志能让它退出。"""

    def __init__(self, started: threading.Event, release: threading.Event):
        self._started = started
        self._release = release

    def run(self, task, provider=None, model=None, activated=(), cancel=None, **kwargs):
        yield TaskStarted(task=task)
        self._started.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            if cancel is not None and cancel.cancelled:
                yield TaskFinished(status="partial", answer="被叫停", iterations=1)
                return
            time.sleep(0.01)
        self._release.wait(timeout=1)
        yield TaskFinished(status="completed", answer="其实跑完了", iterations=1)


if __name__ == "__main__":
    unittest.main()
