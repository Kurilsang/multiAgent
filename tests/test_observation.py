"""观察值视图与旁存续读测试：FakeLLMClient 脚本 + 假工具（零真实网络）。

缝 = LLM 客户端抽象（脚本化 fake）+ 工具注册表公共接口 + 聊天 HTTP 面；
只断言外显观察值/帧内容与句柄行为，不测池内部结构。
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.agent import ActionObserved, AgentEngine, TaskFinished
from app.observation import (
    READ_TOOL_NAME,
    ResultPool,
    read_tool_result_tool,
)
from app.tools import Tool, ToolRegistry

from tests.fakes import FakeLLMClient, text_round, tool_round

FULL = "0123456789" * 5000  # 50000 字符


def make_tool_registry(pool, chunk=4000, big_tool_limit=None):
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="big_tool",
            description="返回大结果的假工具",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: FULL,
            max_observation_chars=big_tool_limit,
        )
    )
    registry.register(read_tool_result_tool(pool, chunk_size=chunk))
    return registry


def observed_texts(fake, round_index):
    messages = fake.calls[round_index][0]
    return [m["content"] for m in messages if m["role"] == "tool"]


class SpilloverRoundtripTest(unittest.TestCase):
    """视图进上下文、全文旁存、续读取回——完整返回与防打爆两全。"""

    def test_view_note_then_read_more_returns_rest(self):
        pool = ResultPool()
        fake = FakeLLMClient(
            [
                tool_round("big_tool", {}),
                tool_round("read_tool_result", {"handle": "r1", "offset": 4000}),
                tool_round("finish", {"answer": "完成"}),
            ]
        )
        engine = AgentEngine(fake, make_tool_registry(pool), observation_pool=pool)
        events = list(engine.run("取大结果"))

        # 第 2 轮看到的是视图 + 续读指引（全文旁存，句柄确定性可引用）
        view = observed_texts(fake, 1)[0]
        self.assertEqual(view[:4000], FULL[:4000])
        self.assertIn("全文共 50000 字符", view)
        self.assertIn('handle="r1"', view)

        # 第 3 轮看到的是续读分段 + 尾注
        piece = observed_texts(fake, 2)[1]
        self.assertEqual(piece[:4000], FULL[4000:8000])
        self.assertIn("已读至 8000", piece)

        read_more = [e for e in events if isinstance(e, ActionObserved)][1]
        self.assertFalse(read_more.is_error)
        self.assertIsInstance(events[-1], TaskFinished)

    def test_read_more_consumes_a_model_round(self):
        """续读走普通工具轮 = 计入迭代预算（与双层终止一致）。"""
        pool = ResultPool()
        fake = FakeLLMClient(
            [
                tool_round("big_tool", {}),
                tool_round("read_tool_result", {"handle": "r1", "offset": 4000}),
                tool_round("finish", {"answer": "完成"}),
            ]
        )
        list(AgentEngine(fake, make_tool_registry(pool), observation_pool=pool).run("x"))
        self.assertEqual(len(fake.calls), 3)  # 大结果 / 续读 / 收尾 各占一轮


class InvalidHandleTest(unittest.TestCase):
    def test_stale_handle_gives_error_observation(self):
        pool = ResultPool()
        fake = FakeLLMClient(
            [
                tool_round("read_tool_result", {"handle": "nope", "offset": 0}),
                tool_round("finish", {"answer": "完成"}),
            ]
        )
        engine = AgentEngine(fake, make_tool_registry(pool), observation_pool=pool)
        events = list(engine.run("读取"))
        observation = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertTrue(observation.is_error)
        self.assertIn("失效", observation.result)
        self.assertIn("nope", observed_texts(fake, 1)[0])


class ToolOverrideTest(unittest.TestCase):
    def run_with_limit(self, limit):
        pool = ResultPool()
        fake = FakeLLMClient(
            [
                tool_round("big_tool", {}),
                tool_round("finish", {"answer": "完成"}),
            ]
        )
        engine = AgentEngine(
            fake, make_tool_registry(pool, big_tool_limit=limit), observation_pool=pool
        )
        list(engine.run("取大结果"))
        return observed_texts(fake, 1)[0]

    def test_zero_means_full_inline(self):
        self.assertEqual(self.run_with_limit(0), FULL)

    def test_tool_level_view_size_overrides_default(self):
        text = self.run_with_limit(100)
        self.assertEqual(text[:100], FULL[:100])
        self.assertIn('offset=100', text)


class ResultPoolEvictionTest(unittest.TestCase):
    def test_oldest_evicted_when_byte_cap_exceeded(self):
        pool = ResultPool(max_bytes=100)
        h1 = pool.put("a" * 60)
        h2 = pool.put("b" * 60)  # 超上限：淘汰最旧
        self.assertIsNone(pool.get(h1))
        self.assertEqual(pool.get(h2), "b" * 60)

    def test_evicted_handle_reads_as_stale(self):
        pool = ResultPool(max_bytes=100)
        tool = read_tool_result_tool(pool, chunk_size=50)
        h1 = pool.put("a" * 60)
        pool.put("b" * 60)
        with self.assertRaises(ValueError) as ctx:
            tool.run({"handle": h1, "offset": 0})
        self.assertIn("失效", str(ctx.exception))


class ChatReadMoreTest(unittest.TestCase):
    """聊天通道：视图 + 续读全链路（技能依赖工具触发大结果）。"""

    def setUp(self):
        import app.server as server
        from tests.test_server_stream import StubSettings

        self.server = server
        self._orig = (
            server.settings,
            server.llm,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        )
        server.settings = StubSettings()
        server.conversation.reset()
        self._tmp = tempfile.TemporaryDirectory()
        (
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        ) = server._build_wiring(Path(self._tmp.name))
        pool = server.agent_engine.observation_pool
        server.tool_registry.register(
            Tool(
                name="big_tool",
                description="返回大结果的假工具",
                parameters={"type": "object", "properties": {}, "required": []},
                func=lambda: FULL,
            )
        )
        server.skill_registry.create("大结果", "取大结果", "调用 big_tool", ("big_tool",))
        self.client = __import__(
            "fastapi.testclient", fromlist=["TestClient"]
        ).TestClient(server.app)

    def tearDown(self):
        self.server.conversation.reset()
        (
            self.server.settings,
            self.server.llm,
            self.server.tool_registry,
            self.server.skill_registry,
            self.server.skill_load_errors,
            self.server.agent_engine,
            self.server.mcp_manager,
        ) = self._orig
        self._tmp.cleanup()

    def frames(self, response):
        result = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                result.append(json.loads(line[len("data: "):]))
        return result

    def test_view_and_read_more_flow_in_chat(self):
        fake = FakeLLMClient(
            [
                tool_round("use_skill", {"name": "大结果"}),
                tool_round("big_tool", {}),
                tool_round("read_tool_result", {"handle": "r1", "offset": 4000}),
                text_round("完成"),
            ]
        )
        self.server.llm = fake
        resp = self.client.post("/chat/stream", json={"message": "取一下"})
        frames = self.frames(resp)
        observations = [f for f in frames if f.get("type") == "observation"]
        self.assertEqual(len(observations), 3)
        self.assertIn("全文共 50000 字符", observations[1]["text"])  # 视图 + 续读指引
        self.assertEqual(observations[2]["text"][:4000], FULL[4000:8000])
        self.assertFalse(observations[2]["is_error"])  # 闸门放行 read_tool_result
        self.assertEqual(len(fake.calls), 4)  # 续读占一轮聊天预算


if __name__ == "__main__":
    unittest.main()
