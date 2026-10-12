"""主缝测试：入模消息边界——LLM fake 收到的 messages 与 context_meta 一致（SPEC-0006）。"""

import unittest

from fastapi.testclient import TestClient

from app import server
from app.context_budget import estimate_messages
from tests.fakes import SessionStoreMixin


class RecordingLLM:
    """记录每次收到的 messages，返回固定回复。"""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def chat(self, messages, provider=None, model=None):
        self.calls.append([dict(m) for m in messages])

        class Result:
            content = "ok"
            provider = "glm"
            model = "test-model"

        return Result()

    def chat_events(self, messages, provider=None, model=None, tools=None):
        self.calls.append([dict(m) for m in messages])
        from app.llm import TextDelta

        yield TextDelta("ok")


class ContextMetaApiTest(SessionStoreMixin, unittest.TestCase):
    def setUp(self):
        self.setup_session_store()
        self._orig_llm = server.llm
        self._orig_settings_fields = (
            server.settings.context_window_tokens,
            server.settings.context_output_reserve_tokens,
        )
        server.settings.context_window_tokens = 1000
        server.settings.context_output_reserve_tokens = 100
        self.recording = RecordingLLM()
        server.llm = self.recording
        self.client = TestClient(server.app)

    def tearDown(self):
        server.llm = self._orig_llm
        (
            server.settings.context_window_tokens,
            server.settings.context_output_reserve_tokens,
        ) = self._orig_settings_fields
        self.teardown_session_store()

    def test_chat_response_carries_context_meta_matching_sent_messages(self):
        """主缝断言：meta 的估算量 = fake 实际收到 messages 的估算量。"""
        resp = self.client.post("/chat", json={"message": "上下文预算测试"})
        self.assertEqual(resp.status_code, 200)
        meta = resp.json()["context_meta"]
        sent = self.recording.calls[0]
        self.assertEqual(meta["estimated_tokens"], estimate_messages(sent))
        self.assertEqual(meta["budget_tokens"], 900)
        self.assertFalse(meta["degraded"])

    def test_context_meta_tracks_message_growth(self):
        first = self.client.post("/chat", json={"message": "第一条消息内容"}).json()[
            "context_meta"
        ]
        second = self.client.post("/chat", json={"message": "第二条消息内容"}).json()[
            "context_meta"
        ]
        self.assertEqual(first["estimated_tokens"], estimate_messages(self.recording.calls[0]))
        self.assertEqual(second["estimated_tokens"], estimate_messages(self.recording.calls[1]))
        self.assertGreater(second["estimated_tokens"], first["estimated_tokens"])

    def test_chat_stream_done_frame_carries_context_meta(self):
        """SSE 通道与 HTTP 通道的账目口径一致（主缝：fake 收到的 messages）。"""
        import json as jsonlib

        resp = self.client.post("/chat/stream", json={"message": "流式账目测试"})
        frames = [
            jsonlib.loads(line[len("data: ") :])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        done = [f for f in frames if f["type"] == "done"][-1]
        sent = self.recording.calls[-1]
        self.assertEqual(done["context_meta"]["estimated_tokens"], estimate_messages(sent))
        self.assertEqual(done["context_meta"]["budget_tokens"], 900)
        self.assertFalse(done["context_meta"]["degraded"])


if __name__ == "__main__":
    unittest.main()
