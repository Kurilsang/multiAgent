"""单元测试：上下文截断与会话状态（不依赖网络）。"""

import unittest

from app.conversation import Conversation


class ConversationTest(unittest.TestCase):
    def test_assemble_keeps_system_and_history(self):
        conv = Conversation(system_prompt="你是助手", max_messages=4)
        conv.add("user", "hi")
        conv.add("assistant", "hello")
        messages, _ = conv.assemble(budget_tokens=10**6)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(len(messages), 3)

    def test_floor_is_minimum_not_ceiling(self):
        """MAX_CONTEXT_MESSAGES 是保底：预算够时旧对话也入模（SPEC-0006）。"""
        conv = Conversation(system_prompt="sys", max_messages=2)
        for i in range(10):
            conv.add("user", f"u{i}")
            conv.add("assistant", f"a{i}")
        messages, _ = conv.assemble(budget_tokens=10**6)
        # 20 条历史全在预算内 → 一字不丢（旧语义只保留最近 2 条）
        self.assertEqual(len(messages), 21)
        self.assertEqual(messages[0]["content"], "sys")
        self.assertEqual(messages[1]["content"], "u0")
        self.assertEqual(messages[-1]["content"], "a9")

    def test_tight_budget_keeps_floor_and_drops_older(self):
        conv = Conversation(system_prompt="sys", max_messages=2)
        for i in range(10):
            conv.add("user", f"u{i}")
            conv.add("assistant", f"a{i}")
        # 每条 8+ceil(len/3)；u{i}/a{i} 2 字符 = 9 token。预算 36 = sys(9) + 保底 2 + 更早 1
        messages, meta = conv.assemble(budget_tokens=45)
        self.assertEqual(messages[0]["content"], "sys")
        self.assertLessEqual(len(messages), 21)
        self.assertTrue(meta["degraded"])

    def test_assemble_returns_copies(self):
        conv = Conversation(max_messages=4)
        conv.add("user", "hi")
        messages, _ = conv.assemble(budget_tokens=10**6)
        messages[0]["content"] = "mutated"
        again, _ = conv.assemble(budget_tokens=10**6)
        self.assertEqual(again[0]["content"], "hi")

    def test_history_returns_full_snapshot_copies(self):
        conv = Conversation(max_messages=2)
        for i in range(5):
            conv.add("user", f"u{i}")
            conv.add("assistant", f"a{i}")
        history = conv.history()
        # 导出用完整快照，不受入模装配影响
        self.assertEqual(len(history), 10)
        self.assertEqual(history[0]["content"], "u0")
        self.assertEqual(history[-1]["content"], "a4")
        history[0]["content"] = "mutated"
        self.assertEqual(conv.history()[0]["content"], "u0")

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
        messages, _ = conv.assemble(budget_tokens=10**6)
        self.assertEqual(messages, [{"role": "system", "content": "sys"}])

    def test_invalid_role_rejected(self):
        conv = Conversation(max_messages=4)
        with self.assertRaises(ValueError):
            conv.add("system", "nope")

    def test_max_messages_too_small_rejected(self):
        with self.assertRaises(ValueError):
            Conversation(max_messages=1)


if __name__ == "__main__":
    unittest.main()
