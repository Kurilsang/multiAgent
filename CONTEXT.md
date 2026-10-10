# multiagent

内部网关场景的多厂商 LLM 对话服务。当前演进到第七步:多会话持久化与断连续流(进行中,SPEC-0005;前六步三厂商对话、ReAct 循环、技能市场、技能在线目录、MCP 市场、多 Agent 协作已落地)。

## Language

### Agent 循环

**任务(Task)**:
Agent 为完成一个用户请求而发起的一次状态机执行,从接收请求到进入终态。
_Avoid_: 运行、会话(易与主对话混淆)

**轨迹(Trace)**:
一次任务的完整执行记录,含全部思考、动作与观察;独立于主对话保存,按任务专属策略截断。
_Avoid_: scratchpad、草稿区、上下文

**主对话**:
用户与系统的常驻多轮聊天历史;任务结束后只回写用户请求与最终答案;现持久化在会话库(见「会话」),页面展示全量而发给模型时按上限截断。
_Avoid_: 历史记录、缓存

**会话(Session)**:
一段独立的主对话记录及其归属的任务轨迹;有标识、可新建/切换/重命名/删除,持久化在会话库(`SESSION_DB`,默认 `data/sessions.db`),服务重启后仍在。
_Avoid_: 上下文、聊天室、线程

**思考(THINKING)**:
模型决定下一步的状态:发起工具调用、宣告完成,或直接给出答案。

**行动(ACTING)**:
引擎实际执行模型所选工具的状态。

**派发(DISPATCHING)**:
编排器把子任务交给子 agent 执行的状态(`dispatch_task` 特殊工具触发);随后进入 WAITING 收集子任务结果。
_Avoid_: 调用子函数

**等待(WAITING)**:
子任务收集中:并行子 agent 的事件实时透传,结果合成一条观察值回灌编排器轨迹。

**观察(OBSERVING)**:
工具执行结果(观察值)被回灌为模型输入的状态。

**判断(JUDGING)**:
引擎依据双层终止规则决定继续思考或进入终态的状态;不发起额外模型调用。

**完成动作(finish)**:
模型显式宣告任务达成并交付最终答案的工具调用,是语义层面的终止信号。

**终态**:
FINISHED(任务结束,完成度分 completed / partial)与 FAILED(任务失败,如死循环止损、模型响应无法解析)。

**部分完成(partial)**:
未达目标但已有进展的终态:预算耗尽、被用户叫停、或有子任务未达成;向用户返回轨迹摘要(编排任务附子任务状态清单)。

### 多 Agent 编排

**编排任务(Orchestrated Task)**:
经编排器拆解为子任务执行的任务;不派发子任务时即退化为单 Agent 任务,一套引擎一套入口。
_Avoid_: 工作流、流水线

**编排器(Orchestrator)**:
编排任务的主 agent:一条 ReAct 轨迹,经 `dispatch_task` 特殊工具派发子任务并汇总;事件身份 agent_id=main。
_Avoid_: 主控、调度器

**子任务(Subtask)**:
`dispatch_task` 派发的一个自包含任务书(写清背景、目标与验收要求);子 agent 看不到编排器上下文。
_Avoid_: 分片、工作项

**子 agent(Sub-agent)**:
执行一个子任务的引擎运行(动态装配:persona / 工具子集 / 预激活技能 / 可选模型);深度限一层,不能再派发。
_Avoid_: 从 agent、工人

**审查 agent(critic)**:
编排器交付前的收尾审查者:输入任务与最终答案,产出 pass / revise + 意见;revise 触发一次整体回炉(封顶一次)。
_Avoid_: 质检员、第二个主 agent

**派发闸门(Dispatch Gate)**:
编排层的预算闸门(非终止层,见 ADR-0003):子任务总数上限 `AGENT_MAX_SUBTASKS`(跨批累计含回炉)、并行度 `AGENT_MAX_PARALLEL`、回炉封顶 `REWORK_LIMIT`;ADR-0002 双层终止仍在每个 agent 内独立生效。
_Avoid_: 终止层、预算

**回炉(Rework)**:
critic 判 revise 后把审查意见并入任务描述整体重跑一次;二次仍不过则按现状交付并附审查意见。
_Avoid_: 重试(易与工具重试混淆)

**叫停(Stop)**:
用户随时终止整个编排任务(含在跑子 agent):`POST /agent/stop` 或 CLI Ctrl+C 置取消标志,agent 在迭代边界退出,以 partial 收尾并展示已有进展。
_Avoid_: 取消会话

**审计日志(Audit Log)**:
每任务一份 JSONL 留痕(`logs/tasks/*.jsonl`),含全部 agent 事件与终态及会话归属,失败/中断/叫停同样留痕;是任务事件的唯一权威副本,可回看、可重放渲染成轨迹,但不提供续跑。
_Avoid_: 运行日志、备份

**断连续流(Reattach)**:
客户端断开后任务继续在后台跑(断连=退订,不置取消);凭任务标识与已收序号重连时,服务端先补发缺口事件再转实时。
_Avoid_: 重试、重跑、恢复

### 能力

**工具(Tool)**:
通过 function calling 通道暴露给模型的原子可调用函数,带名称、描述与参数 schema。
_Avoid_: 插件、技能、命令

**MCP 工具(MCP Tool)**:
启用 MCP 服务暴露、经唯一适配器灌入工具注册表的工具,命名 `mcp__<服务短名>__<工具名>`(清洗后冲突或超长即拒绝加载该服务,fail loud);对模型就是普通工具,观察值默认走视图截断;禁用服务即整组摘除。
_Avoid_: 外部工具、远程工具、插件

**技能(Skill)**:
以提示词形式注入上下文的能力包,声明其依赖的工具;经激活展开正文,能力本体不占用函数调用通道。
_Avoid_: 插件、宏、工具包

**技能包(Skill Pack)**:
技能的文件形态:`skills/<名称>/SKILL.md`(frontmatter:name / description / tools + Markdown 正文),纯提示词,不含可执行脚本;文件态是唯一事实源。
_Avoid_: 插件包、安装包

**清单(Catalog)**:
每次请求现场生成的已启用技能索引(名称 + 一句话描述),紧凑注入 system prompt;条数上限外的技能经 search_skills 检索。
_Avoid_: 索引、菜单

**激活(Activation)**:
use_skill 元工具把技能正文展开进上下文的动作;`/技能名` 显式点名走预激活(正文直接注入,不等模型激活)。
_Avoid_: 调用技能(易与工具调用混淆)

**技能市场(Market)**:
技能的安装与管理面(安装/启停/删除/查看/自产);货源为 Git 仓库中的 SKILL.md 生态与本地导入。
_Avoid_: 插件商店

**在线目录(Online Catalog)**:
技能平台的可浏览条目流(名称/描述/安装坐标/热度/审计徽标),由爬取服务(services/catalog,隔离区)爬取缓存,主服务经 /market/* 代理消费;外网元数据交互集中在隔离区、可独立部署升级(MCP 运行时连接是另一条用户显式配置的通路,见「MCP 服务」)。
_Avoid_: 应用商店、推荐流

**目录包(Catalog Pack)**:
在线目录条目对应的可安装内容:manifest 文件集(技能 = SKILL.md 纯提示词 / MCP = server.json 连接定义),由爬取服务归一交付主服务校验闸门;技能侧附带脚本/资源文件丢弃不落盘。
_Avoid_: 安装包、压缩包

**MCP 服务(MCP Server)**:
经 MCP 协议暴露工具的外部进程(stdio)或远程端点;其暴露的工具见「MCP 工具」,启用即注册、禁用即摘除。
_Avoid_: 插件、远程工具

**连接定义(Connection Entry)**:
一条 MCP 服务的声明式配置(名称/transport/命令 argv 或 endpoint/${VAR} 环境变量与请求头占位/启用位/工具白名单/认证声明 auth、auth_flow),存于 MCP_CONFIG 文件;文件态是唯一事实源,密钥只以占位符引用 .env。
_Avoid_: 配置项、安装包

**动态授权流(OAuth Flow)**:
`auth: oauth` 连接定义的动态授权方式:授权码 + PKCE(浏览器授权、终端贴码)或设备码(RFC 8628,无头/网关路径),token 过期自动刷新;刷新失败降级为连接错误并提示重新授权。
_Avoid_: 单点登录、SSO、密钥

**token 文件(Token File)**:
OAuth 授权产物的独立存储(`MCP_TOKEN_FILE`,默认 mcp/tokens.json),按服务名分槽、权限收紧;不进版本库、不进 .env 明文。
_Avoid_: 密钥文件、凭据库、.env

**授权状态(Authorization Status)**:
管理面对 OAuth 服务的三态标注:已授权(authorized)/过期(expired,过期且无 refresh_token,需人工重授权)/未授权(unauthorized);重授权 = 清 token 并重连触发授权流。
_Avoid_: 登录状态

**观察值视图(Observation View)**:
工具结果进入上下文的截断窗口(默认 4000 字符,全局/工具级可配,0=全量直灌);尾注标注全文大小与续读指引。
_Avoid_: 摘要

**旁存续读(Spillover Read)**:
被截断观察值的完整文本暂存内存句柄池(LRU+字节上限),模型经 read_tool_result(handle, offset) 分段取回;不落盘,重启即清。
_Avoid_: 缓存、分页

**思考链(Reasoning)**:
模型在正式回答之外的推理过程文本,随流式输出透传给界面展示;不写入主对话,也不进入任务轨迹。
_Avoid_: `<think>` 标签、reasoning_content(均为上游形态,不是概念)
