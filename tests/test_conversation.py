"""单元测试：上下文截断与会话状态（不依赖网络）。"""

import unittest

from app.conversation import Conversation


class ConversationTest(unittest.TestCase):
    def test_messages_for_api_keeps_system_and_history(self):
        conv = Conversation(system_prompt="你是助手", max_messages=4)
        conv.add("user", "hi")
        conv.add("assistant", "hello")
        messages = conv.messages_for_api()
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(len(messages), 3)

    def test_truncation_keeps_system_and_recent_messages(self):
        conv = Conversation(system_prompt="sys", max_messages=4)
        for i in range(10):
            conv.add("user", f"u{i}")
            conv.add("assistant", f"a{i}")
        messages = conv.messages_for_api()
        # system + 最近 4 条（u8,a8,u9,a9）
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["content"], "sys")
        self.assertEqual(messages[1]["content"], "u8")
        self.assertEqual(messages[-1]["content"], "a9")

    def test_messages_for_api_returns_copies(self):
        conv = Conversation(max_messages=4)
        conv.add("user", "hi")
        messages = conv.messages_for_api()
        messages[0]["content"] = "mutated"
        self.assertEqual(conv.messages_for_api()[0]["content"], "hi")

    def test_pop_last_rolls_back(self):
        conv = Conversation(max_messages=4)
        conv.add("user", "hi")
        self.assertEqual(conv.pop_last()["content"], "hi")
        self.assertEqual(len(conv), 0)
        self.assertIsNone(conv.pop_last())

    def test_reset_clears_history(self):
        conv = Conversation(system_prompt="sys", max_messages=4)
        conv.add("user", "hi")
        conv.reset()
        self.assertEqual(conv.messages_for_api(), [{"role": "system", "content": "sys"}])

    def test_invalid_role_rejected(self):
        conv = Conversation(max_messages=4)
        with self.assertRaises(ValueError):
            conv.add("system", "nope")

    def test_max_messages_too_small_rejected(self):
        with self.assertRaises(ValueError):
            Conversation(max_messages=1)


if __name__ == "__main__":
    unittest.main()
