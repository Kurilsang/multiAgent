"""上下文预算与 token 估算（SPEC-0006）。

零依赖估算：CJK/全角字形按 1 token/字，非 CJK 按 1 token/3 字符
（对主流 BPE 偏保守），每条消息固定加结构开销。目的不是精确，
是一致且保守——宁可少装不可爆窗。
"""


def estimate_tokens(text: str) -> int:
    """估算单段文本的 token 占用（向上取整，偏保守）。"""
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 2) // 3


def _is_cjk(ch: str) -> bool:
    """CJK 表意文字、假名、谚文与全角标点/字符按 1 token/字计。"""
    code = ord(ch)
    return (
        0x2E80 <= code <= 0x9FFF  # CJK 部首/表意文字（含扩展 A）
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0x3040 <= code <= 0x30FF  # 假名
        or 0x3400 <= code <= 0x4DBF  # 扩展 A（并入上方区间，冗余保留语义）
        or 0xAC00 <= code <= 0xD7AF  # 谚文
        or 0xF900 <= code <= 0xFAFF  # 兼容表意文字
        or 0xFF00 <= code <= 0xFFEF  # 全角字符
    )


# 每条消息的固定结构开销（role、分隔符等，随消息数线性累积）
MESSAGE_OVERHEAD_TOKENS = 8


def estimate_messages(messages: list[dict]) -> int:
    """估算消息数组的 token 占用：逐条内容估算 + 每条固定结构开销。"""
    total = 0
    for message in messages:
        content = message.get("content") or ""
        total += MESSAGE_OVERHEAD_TOKENS + estimate_tokens(content)
    return total


def context_budget(window_tokens: int, output_reserve_tokens: int) -> int:
    """入模预算 = 上下文窗口 − 输出预留。"""
    return window_tokens - output_reserve_tokens


def context_budget_from_settings(settings) -> int:
    """从 Settings 计算入模预算；预留不小于窗口即配置非法。"""
    window = settings.context_window_tokens
    reserve = settings.context_output_reserve_tokens
    if reserve >= window:
        raise ValueError(
            f"CONTEXT_OUTPUT_RESERVE_TOKENS({reserve}) 必须小于 "
            f"CONTEXT_WINDOW_TOKENS({window})"
        )
    return context_budget(window, reserve)


def context_meta(
    messages: list[dict],
    settings,
    *,
    degraded: bool = False,
    compressed_upto: int | None = None,
    summary_generated: bool = False,
) -> dict:
    """装配元数据：本次入模的账目（SPEC-0006）。

    estimated_tokens 以实际发给模型的消息为口径估算，保证与
    入模消息边界（测试主缝）的断言可直接对账。
    """
    return {
        "estimated_tokens": estimate_messages(messages),
        "budget_tokens": context_budget_from_settings(settings),
        "compressed_upto": compressed_upto,
        "summary_generated": summary_generated,
        "degraded": degraded,
    }


def assemble_messages(
    system: str | None,
    history: list[dict],
    *,
    budget_tokens: int,
    recent_floor: int,
    memory_blocks: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """预算分层装配（SPEC-0006 T2）：唯一入模实现点。

    装填顺序（占预算顺序）：system → 记忆块 → 最近 recent_floor 条保底
    → 更早对话（预算有剩余才逐字入模，与保底连续取最靠近的）。
    降级阶梯：更早对话装不下即舍弃（degraded，T3 起由滚动摘要兜底）；
    连保底都装不下时硬截断到 system + 最新一条（degraded）；
    预算低于单条消息也仍保 system + 最新一条（功能优先于预算）。
    """
    head: list[dict] = []
    if system:
        head.append({"role": "system", "content": system})
    for block in memory_blocks or []:
        head.append({"role": "system", "content": block})

    if recent_floor < 1:
        recent_floor = 1
    tail: list[dict] = list(history[-recent_floor:]) if history else []
    older = list(history[: len(history) - len(tail)])

    # 保底装不下：从最旧开始逐条舍弃，最终保 system + 最新一条
    while len(tail) > 1 and estimate_messages(head + tail) > budget_tokens:
        tail.pop(0)
    if not tail and history:
        tail = [dict(history[-1])]

    # 更早对话：预算有剩余才进，与保底连续（从最靠近保底的开始）
    while older:
        candidate = [dict(older[-1])] + tail
        if estimate_messages(head + candidate) > budget_tokens:
            break
        tail = candidate
        older.pop()

    # degraded = 有历史没逐字入模（T3 前无摘要兜底）或预算被击穿
    degraded = len(tail) < len(history) or bool(
        history and estimate_messages(head + tail) > budget_tokens
    )

    messages = head + [dict(m) for m in tail]
    meta = {
        "estimated_tokens": estimate_messages(messages),
        "budget_tokens": budget_tokens,
        "compressed_upto": None,
        "summary_generated": False,
        "degraded": degraded,
    }
    return messages, meta


def _fill_budget(
    head: list[dict], fresh: list[dict], budget_tokens: int, recent_floor: int
) -> tuple[list[dict], list[dict], bool]:
    """预算内装填 fresh：返回 (逐字入模, 被舍弃的最旧段, 是否硬截断保底)。"""
    if recent_floor < 1:
        recent_floor = 1
    tail = list(fresh[-recent_floor:]) if fresh else []
    older = list(fresh[: len(fresh) - len(tail)])
    floor_truncated = False
    while len(tail) > 1 and estimate_messages(head + tail) > budget_tokens:
        tail.pop(0)
        floor_truncated = True
    while older:
        candidate = [dict(older[-1])] + tail
        if estimate_messages(head + candidate) > budget_tokens:
            break
        tail = candidate
        older.pop()
    return tail, older, floor_truncated


def assemble_with_summary(
    system: str | None,
    history: list[dict],
    *,
    budget_tokens: int,
    recent_floor: int,
    covered_seq: int = 0,
    summary_text: str | None = None,
    summarize=None,
    on_summary=None,
    memory_blocks: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """滚动摘要 + 记忆块装配（SPEC-0006 T3）。

    history 条目须带 ``seq``（会话库消息序号）。装不下的**最旧连续段**不是
    丢弃，而是吸收进滚动摘要（记忆块注入 system 之后）；摘要合并旧摘要、
    覆盖水位单调推进。summarize(old_summary, absorbed) -> str 为注入的摘要
    生成（现有对话通道）；on_summary(covered_seq, content) 为落库回调。
    无 summarize 或仍装不下则降级硬截断（degraded），对话不阻塞。
    """
    extra_blocks = list(memory_blocks or [])
    summary_generated = False
    floor_truncated = False
    kept: list[dict] = []
    dropped: list[dict] = list(history)

    for _ in range(len(history) + 2):  # 有界：每轮 covered_seq 必然推进
        blocks = ([summary_text] if summary_text else []) + extra_blocks
        head = (
            [{"role": "system", "content": system}] if system else []
        ) + [{"role": "system", "content": b} for b in blocks]
        fresh = [m for m in history if m["seq"] > covered_seq]
        kept, dropped, truncated = _fill_budget(
            head, fresh, budget_tokens, recent_floor
        )
        floor_truncated = floor_truncated or truncated
        if not dropped or summarize is None:
            break
        new_covered = dropped[-1]["seq"]
        try:
            new_summary = summarize(summary_text, dropped)
        except Exception:  # 摘要失败 → 降级硬截断，对话不阻塞
            break
        summary_text = new_summary
        covered_seq = new_covered
        summary_generated = True
        if on_summary is not None:
            on_summary(covered_seq, summary_text)

    blocks = ([summary_text] if summary_text else []) + extra_blocks
    head = (
        [{"role": "system", "content": system}] if system else []
    ) + [{"role": "system", "content": b} for b in blocks]
    messages = head + [dict(m) for m in kept]
    degraded = bool(dropped) or floor_truncated
    meta = {
        "estimated_tokens": estimate_messages(messages),
        "budget_tokens": budget_tokens,
        "compressed_upto": covered_seq if summary_text else None,
        "summary_generated": summary_generated,
        "degraded": degraded,
    }
    return messages, meta
