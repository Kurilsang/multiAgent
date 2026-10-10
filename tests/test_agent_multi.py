"""单元测试：多 Agent 编排（派发/装配/预算/聚合/critic/取消）。

缝隙 = 脚本化 LLM fake（编排器/子 agent/critic 各自脚本）+ 执行器注入
（InlineExecutor 保确定性）+ llm_factory 注入缝；只断言外显事件序列、
观察值内容、工具 schema 集合与终态，不触及内部实现细节。
"""

import threading
import time
import unittest

from app.agent import (
    _CRITIC_PROTOCOL,
    ActionObserved,
    ActionStarted,
    AgentEngine,
    AgentState,
    CancelToken,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
    TRANSITIONS,
)
from app.llm import LLMError, TextDelta
from app.tools import default_registry

from tests.fakes import InlineExecutor, FakeLLMClient, make_registry, text_round, tool_round


def dispatch_round(subtasks, call_id="call_d", thought="拆解子任务"):
    return tool_round("dispatch_task", {"subtasks": subtasks}, call_id=call_id, thought=thought)


def event_types(events) -> list[str]:
    return [type(event).__name__ for event in events]


def names(events) -> set[str]:
    return {t["function"]["name"] for _, tools in events for t in tools}


class SubtaskDispatchTest(unittest.TestCase):
    """串行派发闭环：事件树、结果回灌、终态聚合。"""

    def make_engine(self, orchestrator_script, workers, **kwargs):
        self.worker_llms = {task: FakeLLMClient(script) for task, script in workers.items()}
        return AgentEngine(
            FakeLLMClient(orchestrator_script),
            kwargs.pop("registry", default_registry()),
            executor=InlineExecutor(),
            llm_factory=lambda spec: self.worker_llms[spec.task],
            critic_enabled=False,
            **kwargs,
        )

    def test_dispatch_runs_subagents_and_composes_report(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": "子任务A"}, {"task": "子任务B"}]),
                tool_round("finish", {"answer": "汇总答案"}, call_id="call_f"),
            ],
            {"子任务A": [text_round("结果A")], "子任务B": [text_round("结果B")]},
        )
        events = list(engine.run("主任务"))
        self.assertEqual(
            event_types(events),
            [
                "TaskStarted",
                "ThoughtDelta",
                "ActionStarted",
                # InlineExecutor：子任务按派发顺序整段透传
                "TaskStarted",
                "ThoughtDelta",
                "TaskFinished",
                "TaskStarted",
                "ThoughtDelta",
                "TaskFinished",
                "ActionObserved",
                "TaskFinished",
            ],
        )
        dispatch_action = events[2]
        self.assertEqual(dispatch_action.tool_name, "dispatch_task")
        report = events[9]
        self.assertFalse(report.is_error)
        self.assertIn("结果A", report.result)
        self.assertIn("结果B", report.result)
        self.assertIn("下放工具：", report.result)
        finished = events[-1]
        self.assertEqual(finished.status, "completed")
        self.assertIn("汇总答案", finished.answer)
        self.assertIn("子任务状态：", finished.answer)
        self.assertIn("子任务1：完成", finished.answer)

    def test_subagent_events_carry_identity(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": "子任务A"}]),
                tool_round("finish", {"answer": "done"}, call_id="call_f"),
            ],
            {"子任务A": [text_round("结果A")]},
        )
        events = list(engine.run("主任务"))
        worker_events = [e for e in events if getattr(e, "role", "") == "worker"]
        self.assertTrue(worker_events)
        for event in worker_events:
            self.assertTrue(event.agent_id.startswith("sub-1"))
            self.assertEqual(event.agent_name, "子任务1")
            self.assertEqual(event.parent_id, "main")
        root_events = [e for e in events if getattr(e, "role", "") == "orchestrator"]
        self.assertTrue(root_events)
        self.assertTrue(all(e.agent_id == "main" for e in root_events))

    def test_subagent_context_isolated_from_orchestrator_trace(self):
        """子 agent 只见自包含任务书：主对话/编排轨迹不进其上下文。"""
        registry = make_registry(result="秘密情报")
        engine = self.make_engine(
            [
                tool_round("echo", {}),
                dispatch_round([{"task": "独立子任务", "tools": ["echo"]}]),
                tool_round("finish", {"answer": "done"}, call_id="call_f"),
            ],
            {"独立子任务": [text_round("子结果")]},
            registry=registry,
        )
        list(engine.run("主任务背景：暗号 007"))
        worker_messages = self.worker_llms["独立子任务"].calls[0][0]
        blob = str(worker_messages)
        self.assertNotIn("秘密情报", blob)
        self.assertNotIn("007", blob)
        self.assertNotIn("dispatch_task", blob)

    def test_worker_schema_excludes_dispatch_task(self):
        """深度限一层：子 agent 的 schema 不含 dispatch_task，编排器含。"""
        engine = self.make_engine(
            [
                dispatch_round([{"task": "子任务A"}]),
                tool_round("finish", {"answer": "done"}, call_id="call_f"),
            ],
            {"子任务A": [text_round("结果A")]},
        )
        list(engine.run("主任务"))
        self.assertIn("dispatch_task", names(engine.llm.calls))
        self.assertNotIn("dispatch_task", names(self.worker_llms["子任务A"].calls))

    def test_worker_result_failure_aggregates_partial(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": "子任务A"}, {"task": "子任务B"}]),
                tool_round("finish", {"answer": "尽力而为"}, call_id="call_f"),
            ],
            {"子任务A": [text_round("结果A")], "子任务B": [LLMError("子任务炸了")]},
        )
        events = list(engine.run("主任务"))
        finished = events[-1]
        self.assertEqual(finished.status, "partial")
        self.assertIn("子任务2：失败", finished.answer)

    def test_rejected_dispatch_reports_error_observation(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": ""}]),
                tool_round("finish", {"answer": "补救"}, call_id="call_f"),
            ],
            {},
        )
        events = list(engine.run("主任务"))
        observed = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertTrue(observed.is_error)
        self.assertIn("task", observed.result)
        self.assertEqual(events[-1].status, "completed")


class ToolInheritanceGateTest(unittest.TestCase):
    """工具继承闸门：默认最小集，MCP 等外部工具必须显式点名。"""

    def make_engine(self, subtasks, workers):
        registry = default_registry()
        self.mcp_calls = []
        registry.register(
            _fake_mcp_tool("mcp__x__y", self.mcp_calls)
        )
        self.worker_llms = {task: FakeLLMClient(script) for task, script in workers.items()}
        return AgentEngine(
            FakeLLMClient(
                [
                    dispatch_round(subtasks),
                    tool_round("finish", {"answer": "done"}, call_id="call_f"),
                ]
            ),
            registry,
            executor=InlineExecutor(),
            llm_factory=lambda spec: self.worker_llms[spec.task],
            critic_enabled=False,
        )

    def test_mcp_tool_not_granted_by_default(self):
        engine = self.make_engine([{"task": "子任务A"}], {"子任务A": [text_round("ok")]})
        events = list(engine.run("主任务"))
        report = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertNotIn("mcp__x__y", report.result)
        self.assertNotIn("mcp__x__y", names(self.worker_llms["子任务A"].calls))

    def test_mcp_tool_granted_when_named(self):
        engine = self.make_engine(
            [{"task": "子任务A", "tools": ["mcp__x__y"]}], {"子任务A": [text_round("ok")]}
        )
        events = list(engine.run("主任务"))
        report = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertIn("mcp__x__y", report.result)
        self.assertIn("mcp__x__y", names(self.worker_llms["子任务A"].calls))


class SubtaskBudgetTest(unittest.TestCase):
    """派发闸门 AGENT_MAX_SUBTASKS：跨批累计、含回炉重派。"""

    def make_engine(self, script, workers, **kwargs):
        self.worker_llms = {task: FakeLLMClient(s) for task, s in workers.items()}
        return AgentEngine(
            FakeLLMClient(script),
            default_registry(),
            executor=InlineExecutor(),
            llm_factory=lambda spec: self.worker_llms[spec.task],
            critic_enabled=False,
            **kwargs,
        )

    def test_over_limit_dispatch_rejected(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": "A"}, {"task": "B"}, {"task": "C"}]),
                tool_round("finish", {"answer": "done"}, call_id="call_f"),
            ],
            {},
            max_subtasks=2,
        )
        events = list(engine.run("主任务"))
        observed = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertTrue(observed.is_error)
        self.assertIn("子任务数已达上限", observed.result)

    def test_budget_counts_across_batches(self):
        engine = self.make_engine(
            [
                dispatch_round([{"task": "A"}], call_id="call_d1"),
                dispatch_round([{"task": "B"}, {"task": "C"}], call_id="call_d2"),
                tool_round("finish", {"answer": "done"}, call_id="call_f"),
            ],
            {"A": [text_round("a")]},
            max_subtasks=2,
        )
        events = list(engine.run("主任务"))
        observations = [e for e in events if isinstance(e, ActionObserved)]
        self.assertFalse(observations[0].is_error)
        self.assertTrue(observations[1].is_error)
        self.assertIn("子任务数已达上限", observations[1].result)

    def test_budget_shared_with_rework(self):
        """回炉重派同样计数：critic 判 revise 后的重跑不能突破上限。"""

        def critic_aware(spec):
            if spec.persona == _CRITIC_PROTOCOL:
                return self.critic_llm
            return self.worker_llms[spec.task]

        self.worker_llms = {"A": FakeLLMClient([text_round("a")])}
        self.critic_llm = FakeLLMClient(
            [text_round("返工：补充数据"), text_round("通过")]
        )
        engine = AgentEngine(
            FakeLLMClient(
                [
                    dispatch_round([{"task": "A"}], call_id="call_d1"),
                    tool_round("finish", {"answer": "v1"}, call_id="call_f1"),
                    dispatch_round([{"task": "A"}], call_id="call_d2"),
                    tool_round("finish", {"answer": "v2"}, call_id="call_f2"),
                ]
            ),
            default_registry(),
            executor=InlineExecutor(),
            llm_factory=critic_aware,
            critic_enabled=True,
            max_subtasks=1,
        )
        events = list(engine.run("主任务"))
        observations = [e for e in events if isinstance(e, ActionObserved)]
        self.assertFalse(observations[0].is_error)
        self.assertTrue(observations[1].is_error)
        self.assertIn("子任务数已达上限", observations[1].result)
        mains = [e for e in events if isinstance(e, TaskFinished) and e.role == "orchestrator"]
        self.assertEqual(mains[-1].status, "completed")
        self.assertIn("v2", mains[-1].answer)


class CriticReviewTest(unittest.TestCase):
    """critic 收尾：pass / revise 回炉一次封顶 / 二次仍不过交付附意见。"""

    def make_engine(self, script):
        return AgentEngine(
            FakeLLMClient(script),
            default_registry(),
            executor=InlineExecutor(),
            critic_enabled=True,
        )

    def test_pass_delivers_answer_unchanged(self):
        engine = self.make_engine([text_round("最终答案"), text_round("通过：没问题")])
        events = list(engine.run("主任务"))
        mains = [e for e in events if isinstance(e, TaskFinished) and e.role == "orchestrator"]
        critics = [e for e in events if getattr(e, "role", "") == "critic"]
        self.assertEqual(len(mains), 1)
        self.assertEqual(mains[0].answer, "最终答案")
        self.assertTrue(critics)

    def test_revise_reruns_once_then_passes(self):
        engine = self.make_engine(
            [
                text_round("初版"),
                text_round("返工：补充数据来源"),
                text_round("改进版"),
                text_round("通过"),
            ]
        )
        events = list(engine.run("主任务"))
        mains = [e for e in events if isinstance(e, TaskFinished) and e.role == "orchestrator"]
        self.assertEqual(len(mains), 2)
        self.assertEqual(mains[-1].answer, "改进版")
        starts = [e for e in events if isinstance(e, TaskStarted) and e.role == "orchestrator"]
        self.assertEqual(len(starts), 2)
        self.assertIn("审查意见", starts[1].task)

    def test_second_revise_delivers_with_opinion(self):
        engine = self.make_engine(
            [
                text_round("A 版"),
                text_round("返工：改 1"),
                text_round("B 版"),
                text_round("返工：改 2"),
            ]
        )
        events = list(engine.run("主任务"))
        mains = [e for e in events if isinstance(e, TaskFinished) and e.role == "orchestrator"]
        self.assertEqual(len(mains), 3)  # A 版 / B 版 / 附审查意见的最终交付
        self.assertIn("B 版", mains[-1].answer)
        self.assertIn("（审查意见：返工：改 2", mains[-1].answer)

    def test_critic_skipped_when_disabled(self):
        engine = AgentEngine(
            FakeLLMClient([text_round("答案")]),
            default_registry(),
            executor=InlineExecutor(),
            critic_enabled=False,
        )
        events = list(engine.run("主任务"))
        self.assertEqual(event_types(events), ["TaskStarted", "ThoughtDelta", "TaskFinished"])

    def test_critic_not_applied_to_workers(self):
        """子 agent（role=worker）交付不进 critic：审查只收编排器产物。"""
        worker_llm = FakeLLMClient([text_round("子结果")])
        critic_llm = FakeLLMClient([text_round("通过")])

        def factory(spec):
            if spec.persona == _CRITIC_PROTOCOL:
                return critic_llm
            return worker_llm

        engine = AgentEngine(
            FakeLLMClient(
                [
                    dispatch_round([{"task": "子任务A"}]),
                    tool_round("finish", {"answer": "done"}, call_id="call_f"),
                ]
            ),
            default_registry(),
            executor=InlineExecutor(),
            llm_factory=factory,
            critic_enabled=True,
        )
        events = list(engine.run("主任务"))
        mains = [e for e in events if isinstance(e, TaskFinished) and e.role == "orchestrator"]
        self.assertEqual(len(mains), 1)  # 无回炉
        self.assertIn("done", mains[-1].answer)
        self.assertEqual(len(worker_llm.calls), 1)  # worker 只跑自己一轮
        self.assertEqual(len(critic_llm.calls), 1)  # 只有一次审查（对编排器）


class CancelTest(unittest.TestCase):
    """叫停：取消标志在迭代边界收尾为 partial。"""

    def test_cancel_before_run_returns_partial(self):
        cancel = CancelToken()
        cancel.cancel()
        engine = AgentEngine(
            FakeLLMClient([text_round("不该跑到这")]),
            default_registry(),
            critic_enabled=False,
        )
        events = list(engine.run("主任务", cancel=cancel))
        self.assertEqual(event_types(events), ["TaskStarted", "TaskFinished"])
        self.assertEqual(events[-1].status, "partial")
        self.assertIn("叫停", events[-1].answer)

    def test_cancel_mid_run_stops_at_iteration_boundary(self):
        cancel = CancelToken()

        def cancelling_tool():
            cancel.cancel()
            return "已请求叫停"

        registry = default_registry()
        from app.tools import Tool

        registry.register(
            Tool(
                name="stop_now",
                description="请求叫停",
                parameters={"type": "object", "properties": {}, "required": []},
                func=cancelling_tool,
            )
        )
        engine = AgentEngine(
            FakeLLMClient(
                [
                    tool_round("stop_now", {}),
                    tool_round("finish", {"answer": "不该到这"}, call_id="call_f"),
                ]
            ),
            registry,
            critic_enabled=False,
        )
        events = list(engine.run("主任务", cancel=cancel))
        self.assertEqual(events[-1].status, "partial")
        self.assertIn("叫停", events[-1].answer)
        self.assertEqual(len(engine.llm.calls), 1)  # 第二轮 LLM 调用未发生


class ParallelBoundTest(unittest.TestCase):
    def test_parallelism_bounded_by_max_parallel(self):
        """并行度受 AGENT_MAX_PARALLEL 约束（真实线程 + 探针 fake）。"""
        tracker = {"active": 0, "peak": 0}
        lock = threading.Lock()

        class ProbeLLM:
            def chat_events(self, messages, provider=None, model=None, tools=None):
                with lock:
                    tracker["active"] += 1
                    tracker["peak"] = max(tracker["peak"], tracker["active"])
                time.sleep(0.05)
                with lock:
                    tracker["active"] -= 1
                yield TextDelta("ok")

        engine = AgentEngine(
            FakeLLMClient(
                [
                    dispatch_round([{"task": "A"}, {"task": "B"}, {"task": "C"}]),
                    tool_round("finish", {"answer": "done"}, call_id="call_f"),
                ]
            ),
            default_registry(),
            max_parallel=2,
            llm_factory=lambda spec: ProbeLLM(),
            critic_enabled=False,
        )
        events = list(engine.run("主任务"))
        self.assertEqual(events[-1].status, "completed")
        self.assertLessEqual(tracker["peak"], 2)


class StateMachineTest(unittest.TestCase):
    """状态机后门（ADR-0001/0003）：新状态与迁移边合法、非法迁移 fail loud。"""

    def test_dispatch_edges_registered(self):
        self.assertIn(AgentState.DISPATCHING, TRANSITIONS[AgentState.THINKING])
        self.assertIn(AgentState.WAITING, TRANSITIONS[AgentState.DISPATCHING])
        self.assertIn(AgentState.OBSERVING, TRANSITIONS[AgentState.WAITING])

    def test_illegal_transition_raises(self):
        with self.assertRaises(RuntimeError):
            AgentEngine._goto(AgentState.WAITING, AgentState.FINISHED)


class WorkerAssemblyTest(unittest.TestCase):
    def test_worker_without_any_tool_fails_assembly(self):
        """默认工具集为空且未点名 tools → 子任务装配失败进报告，不炸编排。"""
        worker_llm = FakeLLMClient([])
        engine = AgentEngine(
            FakeLLMClient(
                [
                    dispatch_round([{"task": "A"}]),
                    tool_round("finish", {"answer": "done"}, call_id="call_f"),
                ]
            ),
            make_registry(),  # 只有 echo：不属于默认下放集
            executor=InlineExecutor(),
            llm_factory=lambda spec: worker_llm,
            critic_enabled=False,
        )
        events = list(engine.run("主任务"))
        report = [e for e in events if isinstance(e, ActionObserved)][0]
        self.assertIn("装配失败", report.result)
        self.assertEqual(events[-1].status, "partial")  # 子任务失败 → 整体 partial
        self.assertEqual(worker_llm.calls, [])  # 未发起计划外的模型调用


def _fake_mcp_tool(name: str, calls: list):
    from app.tools import Tool

    def run(**kwargs):
        calls.append(kwargs)
        return "mcp ok"

    return Tool(
        name=name,
        description="假 MCP 工具",
        parameters={"type": "object", "properties": {}, "required": []},
        func=run,
    )


if __name__ == "__main__":
    unittest.main()
