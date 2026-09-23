"""HTTP SSE 流式接口测试：fake LLM / fake 引擎注入，不发起真实请求。

缝隙与引擎测试相同（LLM 客户端抽象），这里只断言 SSE 帧序列
与主对话回写，不触及流式实现细节。
"""

import json
import tempfile
import unittest
from pathlib import Path

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
from app.llm import LLMError, ReasoningDelta, TextDelta

from tests.fakes import FakeLLMClient, text_round, tool_round


class FakeStreamLLM:
    """模拟 chat_events：按脚本吐事件。"""

    def __init__(self, events):
        self.events = events
        self.seen_messages = []

    def chat_events(self, messages, provider=None, model=None, tools=None):
        self.seen_messages.append([dict(m) for m in messages])
        yield from self.events


class FakeErrorLLM:
    """模拟请求中途失败。"""

    def __init__(self, error):
        self.error = error

    def chat_events(self, messages, provider=None, model=None, tools=None):
        raise self.error
        yield  # pragma: no cover - 使本函数成为生成器


class StubSettings:
    """绕开真实 .env：任何厂商都视为已配置。"""

    llm_provider = "glm"
    llm_model = ""
    agent_max_iterations = 8
    skills_catalog_max = 30
    chat_max_tool_turns = 4
    catalog_base_url = "http://catalog.test"

    def api_key_for(self, provider: str) -> str:
        return "test-key"


class FakeAgentEngine:
    """模拟引擎：按脚本产出事件流。"""

    def __init__(self, events):
        self.events = events
        self.calls = []

    def run(self, task, provider=None, model=None, activated=()):
        self.calls.append((task, provider, model, activated))
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
        fake = FakeStreamLLM([TextDelta("你"), TextDelta("好")])
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

    def test_reasoning_forwarded_but_not_stored(self):
        """思考链以 reasoning_delta 帧透传展示，主对话只保留正式回复。"""
        server.llm = FakeStreamLLM([ReasoningDelta("我先想一想"), TextDelta("答案")])
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "hi"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(
            frames[:-1],
            [
                {"type": "reasoning_delta", "text": "我先想一想"},
                {"type": "delta", "text": "答案"},
            ],
        )
        self.assertEqual(frames[-1]["type"], "done")
        messages = server.conversation.messages_for_api()
        self.assertEqual(
            [m["content"] for m in messages], ["hi", "答案"]
        )

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


class WiringSwapMixin:
    """把整套技能装配换到临时目录（测试间互不污染，可安全热重载）。"""

    def setUp(self):
        self._orig = (
            server.llm,
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        )
        server.settings = StubSettings()
        server.conversation.reset()
        self._tmp = tempfile.TemporaryDirectory()
        server.llm = FakeLLMClient([])
        (
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        ) = server._build_wiring(Path(self._tmp.name))
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        (
            server.llm,
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        ) = self._orig
        self._tmp.cleanup()

    def _parse_frames(self, response) -> list[dict]:
        frames = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
        return frames


class ChatSkillLoopTest(WiringSwapMixin, unittest.TestCase):
    """聊天迷你工具循环：技能自主激活、工具闸门、轮数上限。"""

    def test_use_skill_activates_dependency_tools_within_turn(self):
        server.skill_registry.create("时间报告", "日期推算", "先取时间，再换算", ("calculator",))
        fake = FakeLLMClient(
            [tool_round("use_skill", {"name": "时间报告"}), text_round("答案是周三")]
        )
        server.llm = fake
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "3 天后是几号"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(
            [f["type"] for f in frames], ["action", "observation", "delta", "done"]
        )
        self.assertEqual(frames[0]["tool"], "use_skill")
        self.assertIn("先取时间", frames[1]["text"])
        # 第二轮把激活技能声明的 calculator 加入了工具集
        second_tools = {t["function"]["name"] for t in fake.calls[1][1]}
        self.assertIn("calculator", second_tools)
        # 仅最终文本入主对话，技能正文与工具中间态不落历史
        self.assertEqual(
            [m["content"] for m in server.conversation.messages_for_api()],
            ["3 天后是几号", "答案是周三"],
        )

    def test_unactivated_dependency_tool_rejected(self):
        server.skill_registry.create("时间报告", "日期推算", "先取时间", ("calculator",))
        fake = FakeLLMClient(
            [tool_round("calculator", {"expression": "1+1"}), text_round("好吧")]
        )
        server.llm = fake
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "算一下"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertTrue(frames[1]["is_error"])
        self.assertIn("未激活", frames[1]["text"])
        second_tools = {t["function"]["name"] for t in fake.calls[1][1]}
        self.assertNotIn("calculator", second_tools)

    def test_turn_limit_ends_with_error_frame_and_rolls_back(self):
        server.skill_registry.create("时间报告", "日期推算", "先取时间")
        rounds = [
            tool_round("use_skill", {"name": "时间报告"})
            for _ in range(StubSettings.chat_max_tool_turns)
        ]
        server.llm = FakeLLMClient(rounds)
        with self.client.stream("POST", "/chat/stream", json={"message": "嗨"}) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertIn("上限", frames[-1]["detail"])
        self.assertEqual(len(server.conversation), 0)


class SkillPrefixRoutingTest(WiringSwapMixin, unittest.TestCase):
    """/技能名 显式点名：确定性路由任务通道并预激活。"""

    def test_prefix_routes_to_agent_with_activated_skill(self):
        server.skill_registry.create("时间报告", "日期推算", "先取时间")
        fake_engine = FakeAgentEngine(
            [
                TaskStarted(task="/时间报告 3 天后是几号"),
                TaskFinished(status="completed", answer="周三", iterations=1),
            ]
        )
        server.agent_engine = fake_engine
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "/时间报告 3 天后是几号"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual([f["type"] for f in frames], ["task_started", "final"])
        task, _provider, _model, activated = fake_engine.calls[0]
        self.assertEqual(task, "/时间报告 3 天后是几号")
        self.assertEqual(activated[0].name, "时间报告")
        self.assertEqual(len(server.conversation), 2)

    def test_unknown_prefix_falls_back_to_chat(self):
        server.llm = FakeStreamLLM([TextDelta("你好")])
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "/nope 嗨"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(frames[0]["type"], "delta")

    def test_disabled_skill_prefix_falls_back_to_chat(self):
        server.skill_registry.create("时间报告", "日期推算", "先取时间")
        server.skill_registry.disable("时间报告")
        server.llm = FakeStreamLLM([TextDelta("你好")])
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "/时间报告 3 天后"}
        ) as resp:
            frames = self._parse_frames(resp)
        self.assertEqual(frames[0]["type"], "delta")


class ExportEndpointTest(unittest.TestCase):
    def setUp(self):
        self._orig_settings = server.settings
        server.settings = StubSettings()
        server.conversation.reset()
        server.conversation.add("user", "问题")
        server.conversation.add("assistant", "回答")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        server.settings = self._orig_settings

    def test_markdown_export_contains_messages(self):
        resp = self.client.get("/export")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/markdown"))
        self.assertIn("1. [用户]", resp.text)
        self.assertIn("问题", resp.text)
        self.assertIn("2. [助手]", resp.text)
        self.assertIn("回答", resp.text)

    def test_json_export_returns_history(self):
        resp = self.client.get("/export?format=json")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["default_provider"], "glm")
        self.assertEqual(
            payload["messages"],
            [
                {"role": "user", "content": "问题"},
                {"role": "assistant", "content": "回答"},
            ],
        )

    def test_unknown_format_rejected(self):
        resp = self.client.get("/export?format=xml")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不支持的导出格式", resp.json()["detail"])


class HealthEndpointTest(unittest.TestCase):
    """/health 的 app 标识是 run.bat 判定端口占用者的依据，不能丢。"""

    def test_health_reports_app_identity(self):
        client = TestClient(server.app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "app": "multiagent"})


if __name__ == "__main__":
    unittest.main()
