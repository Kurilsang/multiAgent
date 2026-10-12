"""装配器测试：预算分层装填与降级阶梯（SPEC-0006 T2，纯函数）。"""

import unittest

from app.context_budget import assemble_messages, estimate_messages


def _history(n: int) -> list[dict]:
    """n 条短消息，每条 8 结构开销 + ceil(2/3)=1 = 9 token。"""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


class AssembleMessagesTest(unittest.TestCase):
    """装填顺序：system → 记忆块 → 最近 N 条保底 → 更早对话（预算有剩余才进）。"""

    def test_floor_kept_and_older_dropped_marks_degraded(self):
        # system 9 + 4×9 = 45 恰好装下保底；6 条更早被舍弃 → degraded
        messages, meta = assemble_messages(
            "sys", _history(10), budget_tokens=45, recent_floor=4
        )
        self.assertEqual([m["content"] for m in messages], ["sys", "m6", "m7", "m8", "m9"])
        self.assertTrue(meta["degraded"])
        self.assertEqual(meta["estimated_tokens"], estimate_messages(messages))

    def test_older_turns_fill_when_budget_allows(self):
        # 预算 63 = system 9 + 6×9：保底 4 + 更早 2（与保底连续，取最靠近的）
        messages, meta = assemble_messages(
            "sys", _history(10), budget_tokens=63, recent_floor=4
        )
        self.assertEqual(
            [m["content"] for m in messages],
            ["sys", "m4", "m5", "m6", "m7", "m8", "m9"],
        )
        self.assertTrue(meta["degraded"])  # 仍有 4 条更早被舍弃

    def test_everything_fits_marks_not_degraded(self):
        messages, meta = assemble_messages(
            "sys", _history(4), budget_tokens=100, recent_floor=4
        )
        self.assertEqual(len(messages), 5)
        self.assertFalse(meta["degraded"])

    def test_floor_trims_to_newest_when_even_floor_overflows(self):
        # 预算 18 = system 9 + 1×9：保底 4 条装不下 → 硬截断到最新一条
        messages, meta = assemble_messages(
            "sys", _history(10), budget_tokens=18, recent_floor=4
        )
        self.assertEqual([m["content"] for m in messages], ["sys", "m9"])
        self.assertTrue(meta["degraded"])

    def test_newest_message_survives_even_below_budget(self):
        # 预算连一条都装不下：仍保 system + 最新一条（功能优先于预算）
        messages, meta = assemble_messages(
            "sys", _history(10), budget_tokens=5, recent_floor=4
        )
        self.assertEqual([m["content"] for m in messages], ["sys", "m9"])
        self.assertTrue(meta["degraded"])

    def test_memory_blocks_sit_between_system_and_history(self):
        # 记忆块 14（8+6）：head = 9 + 14 = 23，预算 59 = head + 4×9
        messages, meta = assemble_messages(
            "sys",
            _history(10),
            budget_tokens=59,
            recent_floor=4,
            memory_blocks=["记忆内容"],
        )
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "system", "user", "assistant", "user", "assistant"],
        )
        self.assertEqual(messages[1]["content"], "记忆内容")
        self.assertTrue(meta["degraded"])  # 仍有 6 条更早被舍弃（T3 前无摘要兜底）

    def test_meta_shape_is_stable(self):
        _, meta = assemble_messages("sys", _history(2), budget_tokens=100, recent_floor=4)
        self.assertEqual(
            sorted(meta.keys()),
            ["budget_tokens", "compressed_upto", "degraded", "estimated_tokens", "summary_generated"],
        )
        self.assertEqual(meta["budget_tokens"], 100)
        self.assertIsNone(meta["compressed_upto"])
        self.assertFalse(meta["summary_generated"])


if __name__ == "__main__":
    unittest.main()
