"""单元测试：ReAct 引擎的任务循环（思考→行动→观察→判断→终态）。

缝隙 = LLM 客户端抽象（脚本化 fake）与工具注册表（假工具），
只断言外显事件序列、终止原因与回灌内容，不触及内部状态枚举。
"""

import tempfile
import unittest
from pathlib import Path

from app.agent import (
    ActionObserved,
    ActionStarted,
    AgentEngine,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
)
from app.llm import LLMError, ResponseToolCalls, ToolCall
from app.skills import Skill, SkillRegistry
from app.tools import default_registry, skill_tools

from tests.fakes import FakeLLMClient, make_registry, text_round, tool_round


def make_engine(script, registry=None, **kwargs) -> AgentEngine:
    return AgentEngine(
        FakeLLMClient(script), registry or make_registry(), **kwargs
    )


def event_types(events) -> list[str]:
    return [type(event).__name__ for event in events]


class DirectAnswerTest(unittest.TestCase):
    def test_text_only_response_completes_immediately(self):
        engine = make_engine([text_round("直接答案")])
        events = list(engine.run("测试任务"))
        self.assertEqual(
            event_types(events), ["TaskStarted", "ThoughtDelta", "TaskFinished"]
        )
        finished = events[-1]
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.answer, "直接答案")
        self.assertEqual(finished.iterations, 1)


class ToolLoopTest(unittest.TestCase):
    def test_tool_call_then_finish_delivers_answer(self):
        engine = make_engine(
            [
                tool_round("calculator", {"expression": "3*7"}, thought="我需要计算 3*7"),
                tool_round("finish", {"answer": "3 乘 7 等于 21"}, call_id="call_2"),
            ],
            registry=default_registry(),
        )
        events = list(engine.run("3 乘 7 等于几"))
        self.assertEqual(
            event_types(events),
            [
                "TaskStarted",
                "ThoughtDelta",
                "ActionStarted",
                "ActionObserved",
                "TaskFinished",
            ],
        )
        action = events[2]
        self.assertEqual(action.tool_name, "calculator")
        self.assertEqual(action.arguments, {"expression": "3*7"})
        observed = events[3]
        self.assertEqual(observed.result, "21")
        self.assertFalse(observed.is_error)
        finished = events[4]
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.answer, "3 乘 7 等于 21")
        self.assertEqual(finished.iterations, 2)

    def test_trace_messages_follow_openai_tool_protocol(self):
        engine = make_engine(
            [
                tool_round("calculator", {"expression": "3*7"}),
                tool_round("finish", {"answer": "21"}, call_id="call_2"),
            ],
            registry=default_registry(),
        )
        list(engine.run("3 乘 7 等于几"))
        first_messages, first_tools = engine.llm.calls[0]
        self.assertEqual([m["role"] for m in first_messages], ["system", "user"])
        self.assertIn("finish", first_tools[-1]["function"]["name"])
        second_messages, _ = engine.llm.calls[1]
        self.assertEqual(
            [m["role"] for m in second_messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(second_messages[2]["tool_calls"][0]["function"]["name"], "calculator")
        self.assertEqual(second_messages[3]["tool_call_id"], "call_1")
        self.assertEqual(second_messages[3]["content"], "21")


class ErrorRecoveryTest(unittest.TestCase):
    def test_unknown_tool_becomes_error_observation(self):
        engine = make_engine(
            [
                tool_round("nope", {}),
                tool_round("finish", {"answer": "已恢复"}, call_id="call_2"),
            ],
            registry=default_registry(),
        )
        events = list(engine.run("测试任务"))
        observed = events[2]
        self.assertTrue(observed.is_error)
        self.assertIn("未注册", observed.result)
        self.assertEqual(events[-1].status, "completed")

    def test_tool_exception_becomes_error_observation(self):
        engine = make_engine(
            [
                tool_round("echo", {}),
                tool_round("finish", {"answer": "换个办法"}, call_id="call_2"),
            ],
            registry=make_registry(error=ValueError("炸了")),
        )
        events = list(engine.run("测试任务"))
        observed = events[2]
        self.assertTrue(observed.is_error)
        self.assertIn("炸了", observed.result)
        self.assertEqual(events[-1].status, "completed")

    def test_llm_request_error_fails_task(self):
        engine = make_engine([LLMError("请求 glm 失败: 上游 500")])
        events = list(engine.run("测试任务"))
        self.assertEqual(event_types(events), ["TaskStarted", "TaskFailed"])
        self.assertIn("上游 500", events[-1].reason)


class TerminationGuardTest(unittest.TestCase):
    def test_budget_exhaustion_returns_partial_with_summary(self):
        engine = make_engine(
            [tool_round("echo", {}), tool_round("echo", {})],
            registry=make_registry(result="进展数据"),
            max_iterations=2,
        )
        events = list(engine.run("测试任务"))
        finished = events[-1]
        self.assertEqual(finished.status, "partial")
        self.assertIn("2 轮", finished.answer)
        self.assertIn("进展数据", finished.answer)

    def test_dead_loop_aborts_with_failed(self):
        engine = make_engine(
            [tool_round("echo", {"q": 1}), tool_round("echo", {"q": 1}), tool_round("echo", {"q": 1})]
        )
        events = list(engine.run("测试任务"))
        failed = events[-1]
        self.assertIsInstance(failed, TaskFailed)
        self.assertIn("死循环", failed.reason)
        self.assertEqual(failed.iterations, 3)

    def test_unparseable_arguments_fails_task(self):
        script = [[ResponseToolCalls((ToolCall(id="c1", name="echo", arguments="{bad json"),))]]
        engine = make_engine(script)
        events = list(engine.run("测试任务"))
        failed = events[-1]
        self.assertIsInstance(failed, TaskFailed)
        self.assertIn("无法解析", failed.reason)

    def test_empty_response_fails_task(self):
        engine = make_engine([[]])
        events = list(engine.run("测试任务"))
        self.assertIsInstance(events[-1], TaskFailed)
        self.assertIn("响应为空", events[-1].reason)

    def test_finish_with_empty_answer_falls_back_to_thought(self):
        engine = make_engine(
            [
                tool_round("finish", {"answer": ""}, thought="最终结论是 X"),
            ],
            registry=default_registry(),
        )
        events = list(engine.run("测试任务"))
        self.assertEqual(events[-1].answer, "最终结论是 X")


class ObservationLimitTest(unittest.TestCase):
    def test_oversized_observation_becomes_view_with_read_more(self):
        """超限观察值 = 视图 + 旁存续读指引（全文不丢弃，read_tool_result 取回）。"""
        engine = make_engine(
            [
                tool_round("echo", {}),
                tool_round("finish", {"answer": "done"}, call_id="call_2"),
            ],
            registry=make_registry(result="x" * 500),
            max_observation_chars=100,
        )
        events = list(engine.run("测试任务"))
        observed = events[2]
        self.assertTrue(observed.result.startswith("x" * 100))  # 视图进上下文
        self.assertIn("全文共 500 字符", observed.result)  # 旁存续读指引
        self.assertIn("read_tool_result", observed.result)


class SkillInjectionTest(unittest.TestCase):
    def make_skill(self) -> Skill:
        return Skill(
            name="时间报告",
            description="日期推算",
            guide="先取时间，再换算日期",
            tools=("get_current_time", "calculator"),
        )

    def test_activated_skill_guide_injected_into_system_prompt(self):
        """/技能名 显式点名的预激活：guide 直接进 system，不等 use_skill。"""
        engine = make_engine([text_round("答案")], registry=default_registry())
        list(engine.run("测试任务", activated=(self.make_skill(),)))
        messages, _ = engine.llm.calls[0]
        system = messages[0]["content"]
        self.assertIn("时间报告", system)
        self.assertIn("先取时间，再换算日期", system)
        self.assertIn("get_current_time", system)

    def test_dynamic_catalog_injected_from_registry(self):
        """动态清单只带名称 + 描述；正文按需由 use_skill 展开。"""
        with tempfile.TemporaryDirectory() as tmp:
            tools = default_registry()
            skills = SkillRegistry(Path(tmp), tools=tools)
            for tool in skill_tools(skills):
                tools.register(tool)
            skills.create("时间报告", "日期推算", "先取时间，再换算日期", ("calculator",))
            engine = make_engine([text_round("答案")], registry=tools, skills=skills)
            list(engine.run("测试任务"))
            system = engine.llm.calls[0][0][0]["content"]
        self.assertIn("技能清单", system)
        self.assertIn("时间报告", system)
        self.assertIn("日期推算", system)
        self.assertIn("use_skill", system)
        self.assertNotIn("先取时间，再换算日期", system)

    def test_base_protocol_present_without_skills(self):
        engine = make_engine([text_round("答案")], registry=default_registry())
        list(engine.run("测试任务"))
        system = engine.llm.calls[0][0][0]["content"]
        self.assertIn("finish", system)
        self.assertNotIn("技能清单", system)


class SkillToolLoopTest(unittest.TestCase):
    """元工具走普通工具调用通道：use_skill 按需展开，create_skill 自愈。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tools = default_registry()
        self.skills = SkillRegistry(Path(self._tmp.name), tools=tools)
        for tool in skill_tools(self.skills):
            tools.register(tool)
        self.tools = tools
        # 200 字正文，远超 max_observation_chars=50 的截断线
        self.skills.create("时间报告", "日期推算", "x" * 200, ("calculator",))

    def tearDown(self):
        self._tmp.cleanup()

    def test_use_skill_guide_not_truncated(self):
        engine = AgentEngine(
            FakeLLMClient(
                [
                    tool_round("use_skill", {"name": "时间报告"}),
                    tool_round("finish", {"answer": "done"}, call_id="call_2"),
                ]
            ),
            self.tools,
            max_observation_chars=50,
            skills=self.skills,
        )
        events = list(engine.run("测试任务"))
        observed = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertFalse(observed.is_error)
        self.assertIn("x" * 200, observed.result)

    def test_create_skill_validation_error_fed_back_for_self_heal(self):
        engine = AgentEngine(
            FakeLLMClient(
                [
                    tool_round(
                        "create_skill",
                        {"name": "新技能", "description": "d", "guide": "g", "tools": ["missing"]},
                    ),
                    tool_round(
                        "create_skill",
                        {"name": "新技能", "description": "d", "guide": "g"},
                        call_id="call_2",
                    ),
                    tool_round("finish", {"answer": "done"}, call_id="call_3"),
                ]
            ),
            self.tools,
            skills=self.skills,
        )
        events = list(engine.run("测试任务"))
        observations = [e for e in events if isinstance(e, ActionObserved)]
        self.assertTrue(observations[0].is_error)
        self.assertIn("未注册的工具", observations[0].result)
        self.assertFalse(observations[1].is_error)
        self.assertIsNotNone(self.skills.get("新技能"))
        self.assertEqual(events[-1].status, "completed")


class EngineGuardTest(unittest.TestCase):
    def test_engine_without_tools_rejected(self):
        from app.tools import ToolRegistry

        with self.assertRaises(ValueError):
            AgentEngine(FakeLLMClient([]), ToolRegistry())

    def test_non_positive_iterations_rejected(self):
        with self.assertRaises(ValueError):
            make_engine([], max_iterations=0)


if __name__ == "__main__":
    unittest.main()
