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
