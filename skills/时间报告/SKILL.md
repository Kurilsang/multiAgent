---
name: 时间报告
description: 涉及当前日期时间推算时的多步推理流程
tools:
  - get_current_time
  - calculator
---

当用户询问涉及当前日期时间的推算（如『3 天后是几号』）时：先调用 get_current_time 获取当前时间，再基于它推理目标日期，需要数值计算时用 calculator，最后用 finish 交付完整结论。
