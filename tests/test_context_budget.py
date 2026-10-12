"""单元测试：token 估算与上下文预算（SPEC-0006，纯函数，不依赖网络）。"""

import unittest

from app.context_budget import context_budget, estimate_messages, estimate_tokens


class TokenEstimateTest(unittest.TestCase):
    """估算器规格：CJK 1 token/字，非 CJK 1 token/3 字符，向上取整。"""

    def test_cjk_text_counts_one_token_per_char(self):
        self.assertEqual(estimate_tokens("上下文预算"), 5)

    def test_non_cjk_text_rounds_up_per_three_chars(self):
        self.assertEqual(estimate_tokens("hello"), 2)  # ceil(5/3)
        self.assertEqual(estimate_tokens("hi"), 1)  # ceil(2/3)
        self.assertEqual(estimate_tokens(""), 0)

    def test_mixed_text_counts_each_script(self):
        # "hi" → 1（ceil(2/3)），"你好" → 2
        self.assertEqual(estimate_tokens("hi你好"), 3)

    def test_fullwidth_punctuation_counts_as_cjk(self):
        self.assertEqual(estimate_tokens("你好，世界！"), 6)

    def test_message_carries_structure_overhead(self):
        messages = [{"role": "user", "content": "hi"}]
        # 8 结构开销 + ceil(2/3)=1
        self.assertEqual(estimate_messages(messages), 9)

    def test_message_estimate_sums_over_all_messages(self):
        messages = [
            {"role": "user", "content": "上下文"},
            {"role": "assistant", "content": "ok"},
        ]
        # (8+3) + (8+1)
        self.assertEqual(estimate_messages(messages), 20)


class ContextBudgetTest(unittest.TestCase):
    def test_budget_is_window_minus_output_reserve(self):
        self.assertEqual(
            context_budget(window_tokens=32000, output_reserve_tokens=4096), 27904
        )


if __name__ == "__main__":
    unittest.main()
