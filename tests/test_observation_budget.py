"""观察值预算降级：共享原语单测（SPEC-0006 T4）。"""

import unittest

from app.observation import (
    OBSERVATION_MIN_CHARS,
    ResultPool,
    format_observation,
    observation_budget_chars,
)


class ObservationBudgetLimitTest(unittest.TestCase):
    def test_budget_chars_scales_with_remaining_tokens(self):
        # 3 字符/token（ASCII 口径）：2000 token 剩余 → 6000 字符额度
        self.assertEqual(observation_budget_chars(2000), 6000)

    def test_budget_chars_split_across_multiple_observations(self):
        self.assertEqual(observation_budget_chars(2000, count=2), 3000)

    def test_budget_chars_never_negative(self):
        self.assertEqual(observation_budget_chars(-50), 0)


class FormatObservationBudgetTest(unittest.TestCase):
    def setUp(self):
        self.pool = ResultPool()

    def test_no_budget_keeps_configured_limit(self):
        text = "x" * 10000
        out = format_observation(text, limit=None, pool=self.pool, default_limit=4000)
        self.assertEqual(len(out.splitlines()[0]), 4000)

    def test_budget_limit_shrinks_view(self):
        text = "x" * 10000
        out = format_observation(
            text, limit=None, pool=self.pool, default_limit=4000, budget_limit=1200
        )
        self.assertEqual(len(out.splitlines()[0]), 1200)

    def test_budget_limit_respects_floor(self):
        text = "x" * 10000
        out = format_observation(
            text, limit=None, pool=self.pool, default_limit=4000, budget_limit=10
        )
        self.assertEqual(len(out.splitlines()[0]), OBSERVATION_MIN_CHARS)

    def test_zero_limit_still_full_passthrough(self):
        # 0 = 全量直灌，语义不变（预算不改变这一点）
        text = "x" * 10000
        out = format_observation(
            text, limit=0, pool=self.pool, default_limit=4000, budget_limit=100
        )
        self.assertEqual(out, text)

    def test_tool_level_limit_still_caps(self):
        # 工具级上限 3000，预算给 9999 → 取更严的 3000
        text = "x" * 10000
        out = format_observation(
            text, limit=3000, pool=self.pool, default_limit=4000, budget_limit=9999
        )
        self.assertEqual(len(out.splitlines()[0]), 3000)

    def test_compressed_view_still_spillover_readable(self):
        text = "y" * 10000
        out = format_observation(
            text, limit=None, pool=self.pool, default_limit=4000, budget_limit=500
        )
        self.assertIn("全文共 10000 字符", out)
        # 视图被压到 500，但全文仍可经句柄取回
        import re

        handle = re.search(r'handle="(\w+)"', out).group(1)
        self.assertEqual(self.pool.get(handle), text)


if __name__ == "__main__":
    unittest.main()
