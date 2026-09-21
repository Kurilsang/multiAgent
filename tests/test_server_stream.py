"""HTTP SSE 流式接口测试：fake LLM / fake 引擎注入，不发起真实请求。

缝隙与引擎测试相同（LLM 客户端抽象），这里只断言 SSE 帧序列
与主对话回写，不触及流式实现细节。
"""

import json
import unittest

from fastapi.testclient import TestClient

import app.server as server
from app.agent import (
    ActionObserved,
    ActionStarted,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
)
from app.llm import LLMError


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


class FakeAgentEngine:
    """模拟引擎：按脚本产出事件流。"""

    def __init__(self, events):
        self.events = events
        self.calls = []

    def run(self, task, provider=None, model=None):
        self.calls.append((task, provider, model))
        yield from self.events


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


class AgentStreamTest(unittest.TestCase):
    def setUp(self):
        self._orig_engine = server.agent_engine
        self._orig_settings = server.settings
        server.settings = StubSettings()
        server.conversation.reset()
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        server.agent_engine = self._orig_engine
        server.settings = self._orig_settings

    def _parse_frames(self, response) -> list[dict]:
        frames = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
        return frames

    def test_agent_stream_emits_step_frames(self):
        server.agent_engine = FakeAgentEngine(
            [
                TaskStarted(task="现在几点"),
                ThoughtDelta("我需要"),
                ThoughtDelta("查时间"),
                ActionStarted(tool_name="get_current_time", arguments={}),
                ActionObserved(tool_name="get_current_time", result="2026-09-21 星期一", is_error=False),
                TaskFinished(status="completed", answer="今天是星期一", iterations=2),
            ]
        )
        with self.client.stream(
            "POST", "/agent/stream", json={"task": "现在几点"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(
            frames,
            [
                {"type": "task_started"},
                {"type": "thought_delta", "text": "我需要"},
                {"type": "thought_delta", "text": "查时间"},
                {"type": "action", "tool": "get_current_time", "arguments": {}},
                {
                    "type": "observation",
                    "tool": "get_current_time",
                    "text": "2026-09-21 星期一",
                    "is_error": False,
                },
                {
                    "type": "final",
                    "status": "completed",
                    "answer": "今天是星期一",
                    "iterations": 2,
                },
            ],
        )
        # 任务结束后「请求 + 最终答案」回写主对话（与 CLI 路径一致）
        self.assertEqual(len(server.conversation), 2)
        messages = server.conversation.messages_for_api()
        self.assertEqual(messages[0]["content"], "现在几点")
        self.assertEqual(messages[1]["content"], "今天是星期一")

    def test_agent_stream_emits_failed_frame(self):
        server.agent_engine = FakeAgentEngine(
            [TaskStarted(task="x"), TaskFailed(reason="死循环", iterations=3)]
        )
        with self.client.stream(
            "POST", "/agent/stream", json={"task": "x"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(
            frames[-1], {"type": "failed", "reason": "死循环", "iterations": 3}
        )
        # 失败的任务不回写主对话
        self.assertEqual(len(server.conversation), 0)

    def test_blank_task_rejected(self):
        resp = self.client.post("/agent/stream", json={"task": "   "})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
