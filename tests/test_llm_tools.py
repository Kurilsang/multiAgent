"""单元测试:tool_calls 流式分片聚合(纯函数,不依赖网络)。

各厂商对 tool_calls 的流式分片习惯不同(首片全量 / arguments 拆多段),
聚合逻辑是 function calling 通道可靠性的核心,直测纯函数。
"""

import unittest
from types import SimpleNamespace

from app.llm import ToolCall, _materialize_tool_calls, _accumulate_tool_calls


def fn_chunk(index, id=None, name=None, arguments=None):
    """构造一个模仿 OpenAI delta.tool_calls 条目的对象。"""
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=function)


class AccumulateToolCallsTest(unittest.TestCase):
    def test_fragments_concatenated_in_order(self):
        pending = {}
        _accumulate_tool_calls(
            pending,
            [
                fn_chunk(0, "a", "calculator", '{"expr'),
                fn_chunk(0, None, None, 'ession": "1+1"}'),
            ],
        )
        calls = list(_materialize_tool_calls(pending))
        self.assertEqual(calls, [ToolCall(id="a", name="calculator", arguments='{"expression": "1+1"}')])

    def test_multiple_calls_split_by_index(self):
        pending = {}
        _accumulate_tool_calls(
            pending,
            [
                fn_chunk(0, "a", "t1", "{}"),
                fn_chunk(1, "b", "t2", '{"x":'),
                fn_chunk(1, None, None, "1}"),
            ],
        )
        calls = list(_materialize_tool_calls(pending))
        self.assertEqual(
            calls,
            [
                ToolCall(id="a", name="t1", arguments="{}"),
                ToolCall(id="b", name="t2", arguments='{"x":1}'),
            ],
        )

    def test_missing_id_falls_back_to_index(self):
        pending = {}
        _accumulate_tool_calls(pending, [fn_chunk(2, None, "t", "{}")])
        call = list(_materialize_tool_calls(pending))[0]
        self.assertEqual(call.id, "call_2")

    def test_none_chunks_ignored(self):
        pending = {}
        _accumulate_tool_calls(pending, None)
        self.assertEqual(pending, {})


if __name__ == "__main__":
    unittest.main()
