"""滚动摘要与记忆块装配（SPEC-0006 T3，纯函数 + 注入缝）。"""

import unittest

from app.context_budget import assemble_with_summary


def _history(n: int) -> list[dict]:
    """n 条带 seq 的短消息，每条 9 token（8 开销 + ceil(2/3)=1）。"""
    return [
        {"seq": i + 1, "role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


class FakeSummarizer:
    """记录 summarize 调用，返回可预测的合并摘要。"""

    def __init__(self):
        self.calls: list[tuple[str | None, list[dict]]] = []

    def __call__(self, old_summary, absorbed):
        self.calls.append((old_summary, [dict(m) for m in absorbed]))
        joined = ",".join(m["content"] for m in absorbed)
        return f"{old_summary + ';' if old_summary else ''}{joined}"


class AssembleWithSummaryTest(unittest.TestCase):
    def test_no_overflow_means_no_summary(self):
        messages, meta = assemble_with_summary(
            "sys", _history(2), budget_tokens=10**6, recent_floor=2
        )
        self.assertFalse(meta["summary_generated"])
        self.assertIsNone(meta["compressed_upto"])
        self.assertEqual(len(messages), 3)  # system + 2 历史

    def test_overflow_absorbs_oldest_into_summary(self):
        summarize = FakeSummarizer()
        saved: list[tuple[int, str]] = []
        messages, meta = assemble_with_summary(
            "sys",
            _history(6),
            budget_tokens=45,
            recent_floor=2,
            summarize=summarize,
            on_summary=lambda seq, content: saved.append((seq, content)),
        )
        self.assertTrue(meta["summary_generated"])
        self.assertGreater(meta["compressed_upto"], 0)
        # 最旧的 m0 被吸收进摘要（记忆块，位于 system 之后）
        self.assertEqual(messages[0]["content"], "sys")
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("m0", messages[1]["content"])
        # 最新的 m5 逐字保留
        self.assertEqual(messages[-1]["content"], "m5")
        # 落库与 meta 一致、覆盖水位单调
        self.assertEqual(saved[-1][0], meta["compressed_upto"])
        seqs = [s for s, _ in saved]
        self.assertEqual(seqs, sorted(seqs))

    def test_summary_merges_with_old_summary(self):
        summarize = FakeSummarizer()
        assemble_with_summary(
            "sys",
            _history(6),
            budget_tokens=45,
            recent_floor=2,
            covered_seq=2,
            summary_text="旧行",
            summarize=summarize,
            on_summary=lambda s, c: None,
        )
        # 已有摘要覆盖 seq<=2，本轮回吸收的段合并进旧摘要
        self.assertTrue(summarize.calls)
        self.assertEqual(summarize.calls[0][0], "旧行")

    def test_without_summarize_degrades_but_marks(self):
        messages, meta = assemble_with_summary(
            "sys", _history(6), budget_tokens=45, recent_floor=2
        )
        self.assertFalse(meta["summary_generated"])
        self.assertTrue(meta["degraded"])  # 有历史被舍弃且无摘要兜底
        self.assertLess(len(messages), 7)

    def test_summary_failure_degrades_without_blocking(self):
        def broken(old, absorbed):
            raise RuntimeError("上游抖动")

        messages, meta = assemble_with_summary(
            "sys",
            _history(6),
            budget_tokens=45,
            recent_floor=2,
            summarize=broken,
            on_summary=lambda s, c: None,
        )
        self.assertFalse(meta["summary_generated"])
        self.assertTrue(meta["degraded"])  # 降级硬截断，不抛出
        self.assertEqual(messages[-1]["content"], "m5")

    def test_memory_blocks_extend_beyond_summary(self):
        summarize = FakeSummarizer()
        messages, meta = assemble_with_summary(
            "sys",
            _history(2),
            budget_tokens=10**6,
            recent_floor=2,
            memory_blocks=["长期记忆"],
        )
        contents = [m["content"] for m in messages]
        self.assertEqual(contents[0], "sys")
        self.assertIn("长期记忆", contents)

    def test_meta_shape(self):
        _, meta = assemble_with_summary("sys", _history(2), budget_tokens=100, recent_floor=2)
        self.assertEqual(
            sorted(meta.keys()),
            ["budget_tokens", "compressed_upto", "degraded", "estimated_tokens", "summary_generated"],
        )

    def test_early_constraint_survives_in_summary_block(self):
        """验收剧本：早期约束（第 3 条）在长对话后仍以摘要形式留在上下文。"""
        history = []
        for i in range(30):
            content = "约束：用红色" if i == 2 else f"填充{i}"
            history.append(
                {"seq": i + 1, "role": "user" if i % 2 == 0 else "assistant", "content": content}
            )
        summarize = FakeSummarizer()  # 回显被吸收内容，模拟忠实摘要
        messages, meta = assemble_with_summary(
            "sys", history, budget_tokens=200, recent_floor=4,
            summarize=summarize, on_summary=lambda s, c: None,
        )
        all_text = " ".join(m["content"] for m in messages)
        self.assertTrue(meta["summary_generated"])
        self.assertIn("约束：用红色", all_text)  # 早期约束经摘要存活
        self.assertEqual(messages[-1]["content"], "填充29")  # 最新逐字在场


if __name__ == "__main__":
    unittest.main()
