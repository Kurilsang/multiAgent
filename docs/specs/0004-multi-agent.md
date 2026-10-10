# SPEC-0004：多 Agent 协作（编排派发 + 并行子任务 + critic 收尾）

状态：已定稿（grilling 会话逐条确认 + to-spec/to-tickets 落票）· 日期：2026-10-10
关联：[ADR-0001](../adr/0001-state-machine-for-react-loop.md)、[ADR-0002](../adr/0002-dual-layer-termination.md)、[SPEC-0001](0001-skill-market.md)（「多 Agent 协作下的技能共享与按 Agent 装配」伏笔）、CONTEXT.md「任务」「轨迹」「主对话」「终态」。

## 问题（Problem Statement）

当前任务通道是单 Agent ReAct 循环：一个复杂任务挤在一条轨迹、一个上下文窗口（`MAX_TRACE_MESSAGES=40`）里完成，长任务很快顶到截断与 `AGENT_MAX_ITERATIONS` 预算，产出 quality 无复核。README/ADR 早已伏笔「多 Agent 协作（评审者/执行者分工）」与「状态机后门」，但引擎还没有编排能力：无法拆解子任务、无法并行、无法让不同子任务装配不同工具/模型、无法对最终答案做一次审查。需要在**不重写引擎**的前提下（ADR-0001/0002 承诺兑现），把单 Agent 引擎升级为可编排的统一引擎。

## 方案（Solution）

任务通道统一走**编排引擎**：orchestrator 仍是一条 ReAct 轨迹，经特殊工具 `dispatch_task` 一次派发一个或多个**自包含子任务**给子 agent 并行执行；子任务结果作为观察值回灌，orchestrator 继续循环直到 `finish`。每个子 agent 就是同一个 `AgentEngine` 的一次动态装配运行（干净上下文 + 按需工具/技能子集 + 可选模型），深度限一层。`finish` 后由 critic 子 agent 做一次收尾审查（不合格自动回炉一次封顶）。全过程可随时叫停，全事件（含子 agent 轨迹）落 JSONL 审计日志；WebUI 任务时间线展示树状协作轨迹。

简单任务不派发即退化为今天的单 agent 行为——一套引擎、一套入口，零迁移成本。

## 用户故事（User Stories）

1. 作为内部用户，我想把复杂任务交给任务通道后由编排器自动拆解子任务、派发给子 agent 执行、最后汇总成一个答案，以便一句话完成跨多步骤的工作。
2. 作为内部用户，我想让一次派发的多个子任务并行执行，以便长任务耗时显著缩短。
3. 作为内部用户，我想在 WebUI 任务时间线里看到树状协作轨迹（编排器 → 每个子 agent 的思考/行动/观察），以便理解任务被怎样拆解执行。
4. 作为内部用户，我想随时点「停止」叫停整个编排（含在跑子 agent），并看到已完成子任务的部分产物汇总，以便控制成本与时间。
5. 作为内部用户，我想让每个任务默认带一次 critic 收尾审查（revise 自动回炉一次），以便交付答案多一道质量闸门；也可以配置关掉它。
6. 作为内部用户，我想让每个子任务在干净上下文里执行（只看到自包含任务书），以便子 agent 不被无关历史干扰、编排器上下文不被子轨迹撑爆。
7. 作为内部用户，我想让子 agent 按需装配工具与技能（编排器指定子集；MCP 工具必须显式点名），以便控制每个子任务的能力边界与外部动作面。
8. 作为内部用户，我想为子任务指定不同的厂商/模型（编排用强模型、子任务用便宜模型），以便平衡成本与质量。
9. 作为内部用户，我想让编排任务的全事件（含全部子 agent 轨迹）落成 JSONL 审计日志，并在导出时显式附上任务轨迹，以便回看与排障。
10. 作为内部用户，我想继续用 `/agent` 与 `/技能名` 触发任务，简单任务表现与现在完全一致（不强制拆解），以便平滑过渡。
11. 作为部署管理员，我想为编排配置并发度与子任务数上限，以便控制成本与动作面。
12. 作为内部用户，我想让任一子任务失败或超预算时整体最多「部分完成」并如实标注，以便终态可信。
13. 作为维护者，我想让多 agent 复用同一套状态机引擎与终止规则（每个 agent 内独立生效），以便 ADR-0001/0002 的承诺兑现、引擎不重写。
14. 作为维护者，我想让子 agent 只是引擎实例的动态装配（不引入角色配置文件），以便配置面不膨胀。

## 实现决策（Implementation Decisions）

**范围与交付切分**：分提交按序落地——① 可观测底座（事件 agent 标识 + SSE 帧扩展 + 审计日志 + 导出）→ ② 状态机扩展与 `dispatch_task`（串行派发闭环）→ ③ 子 agent 动态装配与工具继承闸门/预算/终态聚合 → ④ 并行派发 → ⑤ critic 收尾 → ⑥ 叫停语义 → ⑦ WebUI 轨迹树 + CLI 汇总 → ⑧ 文档与配置同步收口。

**编排拓扑**：orchestrator = 一条 ReAct 轨迹；`dispatch_task` 为**引擎特判的特殊工具**（与 `finish` 同先例：不在 ToolRegistry、schema 随请求拼入），支持一次批量派发多个子任务（fan-out 并行）；子任务结果合成一条观察值消息回灌 orchestrator 轨迹，循环至 `finish` 或终止规则触发。**深度限一层**：子 agent 的 schema 硬性不含 `dispatch_task`（引擎约束，不靠 prompt）。多 agent 恒可用不强制——不调 `dispatch_task` 即单 agent 退化特例。

**状态机扩展（ADR-0001 后门启用）**：新状态 `DISPATCHING`（派发中）、`WAITING`（子任务收集中）；新迁移边 `THINKING→DISPATCHING`、`DISPATCHING→WAITING`、`WAITING→OBSERVING`；`run()` 主循环加对应分派分支。普通工具「新增只改注册表」约定不变。

**子 agent 模型（动态装配，不做角色配置文件）**：worker = `AgentEngine` 实例的一次运行，差异只在装配参数：system 补充（persona + 自包含任务书）、工具子集、`activated` 技能、`max_iterations`、可选 `provider/model`。`dispatch_task` 的子任务条目形状：

```json
{
  "subtasks": [
    {
      "task": "自包含任务书（含必要背景，编排器负责写全）",
      "persona": "可选 system 补充",
      "tools": ["可选：下放的工具名列表"],
      "skills": ["可选：预激活技能名"],
      "provider": "可选，缺省沿用任务目标",
      "model": "可选，缺省沿用任务目标"
    }
  ]
}
```

派发返回观察值 = 各子任务 `id/status/answer`；answer 为空时回退关键观察值摘要；worker 哪些工具被下放也在观察值里披露。

**上下文隔离与回传**：子 agent 只见任务书与自身轨迹，**不注入**主对话与编排器轨迹；回传仅 answer + status（+ 空答案回退摘要）；完整子轨迹只进事件流（UI/审计日志），不进编排器上下文。编排器上下文 = 自身轨迹（`MAX_TRACE_MESSAGES=40` 截断不变）。

**工具继承闸门**：worker 默认工具集 = 技能元工具（`use_skill`/`search_skills`/`create_skill`）+ 占位工具 + `read_tool_result`；**`mcp__*` 及其余注册表工具必须由编排器显式点名（`tools`）才下放**——MCP 工具是外部动作面，不能随派发无声扩大授权。`skills` 参数指定预激活技能（对位 `/技能名` 的 `activated` 语义）。

**预算与终止**：每个 agent 独立生效 ADR-0002 双层终止（`finish` + `AGENT_MAX_ITERATIONS` 硬上限 + 死循环止损），预算不跨 agent 共享；编排层新增**派发闸门**（非终止层）：`AGENT_MAX_SUBTASKS`（默认 6，跨批累计、含回炉重派），超限拒绝派发并如实告知。终态聚合：任一 worker 为 failed/partial → 整体最多 `partial`，最终答案附子任务状态清单。

**并行执行**：`ThreadPoolExecutor` 并行跑 worker，`AGENT_MAX_PARALLEL`（默认 3）封顶；**执行器可注入**（默认线程池，测试换串行执行器保确定性）。`_chat_lock` 语义明确为「编排任务整体持锁独占会话」（单用户定位不变）；`LLMClient` 的 client 缓存加锁/预热保并发安全。worker 事件经队列汇入主事件流实时透传（带 agent 标识）。

**critic 收尾（评审者/执行者分工）**：orchestrator `finish` 后跑一个 critic 子 agent（审查 persona、最小工具集、缺省沿用任务模型），输入 = 任务 + 最终答案，输出 = pass/revise + 意见；revise → 意见并入任务描述**整体重跑一次**（回炉上限 1 次为常量），再不过则按现状交付并附审查意见。`AGENT_CRITIC_ENABLED` 默认 `true` 可关（关 = 完全不跑 critic）。critic 轨迹同样进事件流与审计日志。

**叫停语义**：`POST /agent/stop` 置当前任务取消标志（`_chat_lock` 保证单会话单任务，「当前任务」良定义）；worker 在迭代边界退出、编排器在事件边界退出；SSE 正常收尾 `final(partial)` 附已完成子任务汇总；客户端断连（生成器 `GeneratorExit`）为兜底同样置取消；CLI `Ctrl+C` 对齐既有 KeyboardInterrupt 语义并扩展到子 agent。停止后产物呈现 = 审计日志留全轨迹 + `final(partial)` 汇总已完成部分。

**模型路由**：派发子任务可带 `provider/model`，缺省沿用任务目标（`resolve_target` 逐调用解析已支持）；critic 缺省同任务模型。

**审计日志与导出**：每任务一份 `logs/tasks/<时间戳>-<任务id>.jsonl`，逐行一个事件（含 agent 标识、全部子 agent 轨迹、终态），失败/中断/叫停也留痕；`logs/` 进 `.gitignore`。`GET /export` 增「含任务轨迹」**显式选项**（markdown 渲染协作树），默认行为（只导主对话）不变。

**SSE/事件协议**：事件种类不增减，payload 增 `agent_id`、`agent_name`、`role`（orchestrator/worker/critic）、`parent_id`；树位置由标识字段表达。WebUI 任务时间线单卡片内树状嵌套（子 agent 分区展开），顺手把 `/chat/stream` 内嵌任务流与 `runAgent()` 两处重复的事件映射循环收敛为单一映射器；纯文本渲染（防注入）约定不变。CLI 只打汇总（最终答案 + 子任务状态清单），不逐事件渲染子轨迹。

**配置**：新增 `AGENT_MAX_PARALLEL`（默认 3）、`AGENT_MAX_SUBTASKS`（默认 6）、`AGENT_CRITIC_ENABLED`（默认 true）；同步 `.env.example` 与 README（项目约定）。回炉上限 1、编排深度上限 1 为引擎常量（对位 `DEAD_LOOP_THRESHOLD`）。

**多 agent 适用范围**：仅任务通道（`/agent`、`/agent/stream`、`/技能名` 前缀路由）；聊天通道迷你工具循环（`CHAT_MAX_TOOL_TURNS`）保持单 agent 不变。`/技能名` 预激活技能归编排器持有，可经 `skills` 参数下放给子任务。

## 测试决策（Testing Decisions）

**好测试的定义**：只测外部行为——事件序列（含 agent 标识与父子关系）、SSE 帧序列、审计日志 JSONL 内容、工具 schema 集合（worker 是否拿到 `dispatch_task`/MCP 工具）、HTTP 响应、终态与 status 聚合——不测内部调用次数与私有方法。

**测试缝方案**：
- **复用 `FakeLLMClient`**（tests/fakes.py 脚本化多轮）：编排器与每个 worker 各持独立脚本；fake 记录 `(messages, tools)` 断言上下文隔离（worker 消息不含主对话/编排轨迹）与工具下放。
- **新缝 1 = worker/LLM 工厂注入**：编排层构造子 agent 走工厂参数（默认 = 真引擎 + `resolve_target`），测试注入按子任务路由到独立 `FakeLLMClient`。
- **新缝 2 = 执行器注入**：默认 `ThreadPoolExecutor`，测试注入串行执行器保确定性；并行语义另用计数型 fake 单测（断言并发度受 `AGENT_MAX_PARALLEL` 约束）。
- 复用 `FakeAgentEngine`（test_server_stream.py）扩展多 agent 事件序列驱动帧断言；StubSettings + 临时目录（审计日志落盘、配置项）；停止测试用可控阻塞 fake + 取消标志断言。

**覆盖模块**：状态机新状态/迁移边合法性（非法迁移 fail loud）、`dispatch_task` 批量派发事件树与标识、子 agent 上下文隔离、工具继承默认与 MCP 显式点名、`AGENT_MAX_SUBTASKS` 超限拒派、终态聚合（worker 失败 → 整体 partial）、critic pass/revise/回炉一次封顶/开关关闭、叫停（stop → `final(partial)` + 已完成子任务汇总）、审计日志 JSONL 全事件与 `/export` 含轨迹选项、并发安全回归（client 缓存）。

**Prior art**：tests/test_agent.py 事件序列断言模式、tests/test_server_stream.py `FakeAgentEngine` 与帧断言、tests/fakes.py 脚本化 fake 基建、tests/test_mcp_auth.py 的注入缝哲学（网络与交互全走注入）。

## 非目标（Out of Scope）

- 子 agent 再派发（嵌套编排）、agent 间对等消息传递/群聊拓扑
- 角色配置文件/角色池（具名角色分工留给后续角色协作形态）
- 多会话持久化与任务恢复（重启清空、SSE 断连不恢复现状不变；审计日志只可回看）
- 逐步人工确认/审批流（只做随时叫停；MCP/技能安装确认卡语义不变）
- 聊天通道多 agent（迷你工具循环不变）
- 自动任务路由（显式触发与 `/技能名` 点名不变）
- 全面 asyncio 化
- 黑板/共享记忆模式、长期运行/定时编排
- critic 多投票/辩论、critic 独立模型默认配置
- 子任务级人工确认卡扩展

## 备注（Further Notes）

- **术语缺口（留给 /domain-modeling 补录 CONTEXT.md）**：编排任务、编排器（orchestrator）、子任务、worker agent、critic agent（审查 agent）、派发（批量派发）、回炉、审计日志（任务轨迹日志）。
- **ADR 一致性**：ADR-0001 后门兑现——新增 `DISPATCHING`/`WAITING` 状态与迁移边，引擎加分支但拓扑仍是数据表；ADR-0002 双层终止在**每个 agent 内独立生效**，编排层的子任务数/并发/回炉闸门是派发闸门**不是终止层**——建议 ADR-0002 增补一节或新立 ADR-0003 说明编排拓扑（收口票内定）。
- **成本提示**：critic 默认开 = 每任务多一次审查调用 + 最多一次整体重跑；`AGENT_CRITIC_ENABLED=false` 可关。这是 Q9 的显式取舍（默认质量优先于默认成本）。
- **WebUI 防分叉**：两处事件映射循环（`/chat/stream` 内嵌任务流与 `runAgent()`）借本步收敛为单一映射器，避免树渲染逻辑再次复制。
- **伏笔兑现**：SPEC-0001「多 Agent 协作下的技能共享与按 Agent 装配」→ 工具继承闸门 + `skills` 下放；ADR-0002「critic 留给多 Agent 阶段」→ critic 收尾；README「评审者/执行者分工」→ critic + worker；ADR-0001「状态机后门」→ DISPATCHING/WAITING。
