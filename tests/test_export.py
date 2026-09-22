"""单元测试：对话导出格式化（markdown / json 纯函数，时间注入）。"""

import json
import unittest
from datetime import datetime

from app.export import to_json, to_markdown

FIXED_NOW = datetime(2026, 9, 22, 12, 0, 0)


class MarkdownExportTest(unittest.TestCase):
    def test_messages_rendered_with_roles_and_header(self):
        text = to_markdown(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮你？"},
            ],
            default_provider="glm",
            now=FIXED_NOW,
        )
        self.assertIn("# multiagent 对话导出", text)
        self.assertIn("2026-09-22 12:00:00", text)
        self.assertIn("默认厂商：glm", text)
        self.assertIn("消息数：2", text)
        self.assertIn("## 1. [用户]", text)
        self.assertIn("## 2. [助手]", text)
        self.assertIn("你好！有什么可以帮你？", text)

    def test_empty_history_still_valid(self):
        text = to_markdown([], default_provider="glm", now=FIXED_NOW)
        self.assertIn("消息数：0", text)


class JsonExportTest(unittest.TestCase):
    def test_json_payload_shape(self):
        text = to_json(
            [{"role": "user", "content": "hi"}],
            default_provider="glm",
            now=FIXED_NOW,
        )
        payload = json.loads(text)
        self.assertEqual(payload["exported_at"], "2026-09-22T12:00:00")
        self.assertEqual(payload["default_provider"], "glm")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
