"""单元测试：配置加载与对话目标解析（不依赖网络）。"""

import unittest

from pydantic import ValidationError

from app.config import ConfigError, Settings, resolve_target


class ResolveTargetTest(unittest.TestCase):
    def make_settings(self, **overrides) -> Settings:
        defaults = {
            "deepseek_api_key": "sk-deepseek",
            "glm_api_key": "sk-glm",
            "minimax_api_key": "",
            "llm_provider": "glm",
        }
        defaults.update(overrides)
        # _env_file=None: 不读取本地 .env，避免测试受真实配置影响
        return Settings(_env_file=None, **defaults)

    def test_default_provider_and_model(self):
        target = resolve_target(self.make_settings())
        self.assertEqual(target.provider, "glm")
        self.assertEqual(target.model, "glm-4.6")

    def test_explicit_provider(self):
        target = resolve_target(self.make_settings(), provider="deepseek")
        self.assertEqual(target.provider, "deepseek")
        self.assertEqual(target.model, "deepseek-chat")
        self.assertEqual(target.base_url, "https://api.deepseek.com/v1")

    def test_model_override(self):
        target = resolve_target(self.make_settings(), provider="glm", model="glm-4.5-air")
        self.assertEqual(target.model, "glm-4.5-air")

    def test_global_model_override_only_applies_to_default_provider(self):
        # 切换厂商后不能把默认厂商的模型名带给其他厂商
        settings = self.make_settings(llm_provider="glm", llm_model="glm-4.6")
        self.assertEqual(resolve_target(settings, "glm").model, "glm-4.6")
        self.assertEqual(resolve_target(settings, "deepseek").model, "deepseek-chat")

    def test_blank_provider_falls_back_to_default(self):
        target = resolve_target(self.make_settings(), provider="   ")
        self.assertEqual(target.provider, "glm")

    def test_blank_model_falls_back_to_provider_default(self):
        target = resolve_target(self.make_settings(), provider="deepseek", model="  ")
        self.assertEqual(target.model, "deepseek-chat")

    def test_unknown_provider(self):
        with self.assertRaises(ConfigError):
            resolve_target(self.make_settings(), provider="openai")

    def test_missing_key(self):
        with self.assertRaises(ConfigError):
            resolve_target(self.make_settings(), provider="minimax")

    def test_max_context_messages_has_lower_bound(self):
        with self.assertRaises(ValidationError):
            self.make_settings(max_context_messages=1)


if __name__ == "__main__":
    unittest.main()
