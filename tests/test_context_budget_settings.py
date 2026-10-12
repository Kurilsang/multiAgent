"""单元测试：上下文预算配置（SPEC-0006，Settings 注入缝）。"""

import unittest

from app.config import Settings
from app.context_budget import context_budget_from_settings


class ContextBudgetSettingsTest(unittest.TestCase):
    def test_defaults_produce_conservative_budget(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.context_window_tokens, 32000)
        self.assertEqual(settings.context_output_reserve_tokens, 4096)
        self.assertEqual(context_budget_from_settings(settings), 27904)

    def test_budget_follows_injected_settings(self):
        settings = Settings(
            _env_file=None,
            context_window_tokens=16000,
            context_output_reserve_tokens=2000,
        )
        self.assertEqual(context_budget_from_settings(settings), 14000)

    def test_budget_rejects_reserve_not_smaller_than_window(self):
        settings = Settings(
            _env_file=None,
            context_window_tokens=1000,
            context_output_reserve_tokens=1000,
        )
        with self.assertRaises(ValueError):
            context_budget_from_settings(settings)


if __name__ == "__main__":
    unittest.main()
