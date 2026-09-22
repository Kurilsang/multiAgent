"""单元测试：思考链统一转写（reasoning_content 透传 + 内联 <think> 剥离）。

各厂商思考形态不同：GLM / DeepSeek 走 reasoning_content 独立字段，
MiniMax M 系列把 <think>…</think> 内联在 content 里。
转写是纯流式函数，直接喂事件序列断言输出。
"""

import unittest

from app.llm import (
    ReasoningDelta,
    ResponseToolCalls,
    TextDelta,
    ToolCall,
    _unify_thinking,
)


def unify(items) -> list:
    """str 视作 TextDelta 增量，其余按事件原样喂入转写器。"""
    events = [TextDelta(t) if isinstance(t, str) else t for t in items]
    return list(_unify_thinking(iter(events)))


class ReasoningPassthroughTest(unittest.TestCase):
    def test_reasoning_delta_passes_through(self):
        events = unify([ReasoningDelta("我想"), TextDelta("答案")])
        self.assertEqual(events, [ReasoningDelta("我想"), TextDelta("答案")])


class InlineThinkTagTest(unittest.TestCase):
    def test_inline_block_becomes_reasoning(self):
        events = unify(["<think>推理过程</think>最终答案"])
        self.assertEqual(events, [ReasoningDelta("推理过程"), TextDelta("最终答案")])

    def test_tag_split_across_chunks(self):
        events = unify(["<thi", "nk>推理", "过</th", "ink>答案"])
        self.assertEqual(
            events,
            [ReasoningDelta("推理"), ReasoningDelta("过"), TextDelta("答案")],
        )

    def test_text_without_tags_untouched(self):
        events = unify(["普通", "回复"])
        self.assertEqual(events, [TextDelta("普通"), TextDelta("回复")])

    def test_multiple_blocks(self):
        events = unify(["<think>a</think>x<think>b</think>y"])
        self.assertEqual(
            events,
            [
                ReasoningDelta("a"),
                TextDelta("x"),
                ReasoningDelta("b"),
                TextDelta("y"),
            ],
        )

    def test_unclosed_block_flushes_as_reasoning(self):
        events = unify(["<think>只有思考"])
        self.assertEqual(events, [ReasoningDelta("只有思考")])


class NonTextEventTest(unittest.TestCase):
    def test_tool_calls_event_passes_through(self):
        calls = ResponseToolCalls((ToolCall(id="c1", name="t", arguments="{}"),))
        events = unify([TextDelta("<think>a</think>"), calls])
        self.assertEqual(events, [ReasoningDelta("a"), calls])

    def test_pending_carry_flushed_before_non_text_event(self):
        """疑似标签前缀的尾巴不能被非文本事件吞掉。"""
        calls = ResponseToolCalls(())
        events = unify([TextDelta("答案<thi"), calls, TextDelta("nk>x</think>")])
        self.assertEqual(
            events,
            [TextDelta("答案"), calls, ReasoningDelta("x")],
        )


if __name__ == "__main__":
    unittest.main()
