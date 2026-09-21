"""HTTP SSE 流式接口测试：fake LLM / fake 引擎注入，不发起真实请求。

缝隙与引擎测试相同（LLM 客户端抽象），这里只断言 SSE 帧序列
与主对话回写，不触及流式实现细节。
"""

import json
import unittest

from fastapi.testclient import TestClient

import app.server as server
from app.llm import LLMError, LLMClient


class FakeStreamLLM:
    """模拟 chat_stream：按脚本吐文本块。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.seen_messages = []

    def chat_stream(self, messages, provider=None, model=None):
        self.seen_messages.append([dict(m) for m in messages])
        yield from self.chunks


class FakeErrorLLM:
    """模拟请求中途失败。"""

    def __init__(self, error):
        self.error = error

    def chat_stream(self, messages, provider=None, model=None):
        raise self.error
        yield  # pragma: no cover - 使本函数成为生成器


class StubSettings:
    """绕开真实 .env：任何厂商都视为已配置。"""

    llm_provider = "glm"
    llm_model = ""

    def api_key_for(self, provider: str) -> str:
        return "test-key"


class ChatStreamTest(unittest.TestCase):
    def setUp(self):
        self._orig_llm = server.llm
        self._orig_settings = server.settings
        server.settings = StubSettings()
        server.conversation.reset()
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        server.llm = self._orig_llm
        server.settings = self._orig_settings

    def _parse_frames(self, response) -> list[dict]:
        frames = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
        return frames

    def test_chat_stream_emits_deltas_then_done(self):
        fake = FakeStreamLLM(["你", "好"])
        server.llm = fake
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "hi"}
        ) as resp:
            self.assertTrue(
                resp.headers["content-type"].startswith("text/event-stream")
            )
            frames = self._parse_frames(resp)
        self.assertEqual(frames[0], {"type": "delta", "text": "你"})
        self.assertEqual(frames[1], {"type": "delta", "text": "好"})
        self.assertEqual(frames[2]["type"], "done")
        self.assertEqual(frames[2]["provider"], "glm")
        # 任务结束后「请求 + 回复」回写主对话
        self.assertEqual(len(server.conversation), 2)

    def test_llm_error_emits_error_frame_and_rolls_back(self):
        server.llm = FakeErrorLLM(LLMError("上游 500"))
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "hi"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "error")
        self.assertIn("上游 500", frames[0]["detail"])
        self.assertEqual(len(server.conversation), 0)

    def test_blank_message_rejected(self):
        resp = self.client.post("/chat/stream", json={"message": "   "})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
