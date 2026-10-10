# ADR-0003：多 Agent 编排拓扑

日期：2026-10-10 · 状态：已采纳 · 关联：[ADR-0001](0001-state-machine-for-react-loop.md)、[ADR-0002](0002-dual-layer-termination.md)、[SPEC-0004](../specs/0004-multi-agent.md)

## 决策

多 Agent 协作（SPEC-0004）在既有 ReAct 状态机上扩展，拓扑定为 **orchestrator/worker + critic 收尾**：

1. **编排器是一条普通的 ReAct 轨迹**。派发经引擎特判的特殊工具 `dispatch_task`（与 `finish` 同先例：不进 ToolRegistry、schema 随请求拼入），一次可批量派多个子任务（fan-out 并行）；子任务结果合成一条观察值回灌编排器轨迹，循环至 `finish`。
2. **状态机后门兑现**（ADR-0001）：新增状态 `DISPATCHING`（派发中）、`WAITING`（子任务收集中）与迁移边 `THINKING→DISPATCHING`、`DISPATCHING→WAITING`、`WAITING→OBSERVING`；引擎分派循环加分支，迁移表仍是数据。
3. **深度限一层**：子 agent 的请求 schema 硬性不含 `dispatch_task`（引擎约束，不靠 prompt）；子 agent 是同一引擎类的动态装配运行（persona / 工具子集 / 预激活技能 / 可选 provider/model），不做角色配置文件。
4. **critic 收尾 = 评审者/执行者分工**：编排器 `finish` 后跑一个审查子 agent（最小工具集），产出 pass / revise + 意见；revise 则意见并入任务整体重跑一次封顶（`REWORK_LIMIT=1`），再不过按现状交付并附意见。`AGENT_CRITIC_ENABLED` 默认开。
5. **上下文隔离**：子 agent 只见自包含任务书与自身轨迹，回传仅 answer + status（空答案回退关键观察值摘要）；完整子轨迹走事件流与审计日志，不进编排器上下文。
6. **工具继承闸门**：子 agent 默认下放技能元工具 + 占位工具 + `read_tool_result`；`mcp__*` 及其余注册表工具必须由编排器显式点名（外部动作面不随派发无声扩大）。
7. **编排层闸门不是终止层**：`AGENT_MAX_SUBTASKS`（子任务总数跨批累计含回炉）、`AGENT_MAX_PARALLEL`（并行度）、`REWORK_LIMIT`（回炉封顶）是**派发闸门**；ADR-0002 的双层终止（finish + 迭代硬上限 + 死循环止损）在每个 agent 内独立生效，不新增终止层、不跨 agent 共享预算。
8. **终态聚合**：任一子任务 failed / partial → 整体最多 `partial`，最终答案附子任务状态清单；被叫停的任务以 `partial` 收尾并展示已有进展（`POST /agent/stop` 置取消标志，agent 在迭代边界退出）。

## 被否决的替代

- **去中心化群聊 / 对等消息传递**：拓扑复杂度高、终止与预算语义难以定义，与「任务显式触发、低干预」的定位不符。
- **计划先行的固定管线**（PLANNING→DISPATCHING→AGGREGATING 一锤子计划）：中途无法调整，开放任务太脆；批量派发已覆盖并行收益。
- **每轮 critic**：已在 ADR-0002 否决（成本翻倍）；本决策只做收尾单次审查，回炉封顶一次。
- **嵌套编排（子 agent 再派发）**：爆炸面与预算语义失控，留待有真实需求再议。
