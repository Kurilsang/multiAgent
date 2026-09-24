# services/catalog — 技能在线目录爬取服务

> **⚠ 隔离边界（外网唯一通道）**
>
> - 本服务是整个系统**唯一**与外网目录/仓库交互的组件。主服务默认零外网（仅既有
>   Git URL 手装除外），一切目录浏览、SKILL.md 预览、目录包下载都经本服务。
> - 本服务**零密钥、零技能包写权限**：不持有任何 LLM API Key，不写 `skills/`
>   技能包目录；技能落盘永远发生在主服务的校验闸门之后。
> - 外网数据**一律不可信**：入库前经 `schema.clean_text` 清洗（去控制字符、
>   截断上限），UI 只允许 textContent 渲染。
> - `/internal/*` 仅对内网暴露（默认监听 `127.0.0.1:8100`），**不对公网开放**。
>
> **独立部署升级路径**：本包可原样搬出为独立部署单元（置于网关之外），两服务
> 之间只存在 `/internal/*` 窄接口合同、无共享状态文件——主服务仅需把
> `CATALOG_BASE_URL` 指向新地址。

## 运行

```bash
pip install -r services/catalog/requirements.txt   # 独立依赖，不回流主工程
python -m services.catalog                          # 默认 127.0.0.1:8100
```

配置（环境变量）：`CATALOG_HOST` / `CATALOG_PORT` / `CATALOG_DB` /
`CATALOG_REFRESH_HOURS` / `CATALOG_SOURCES`（详见 docs/specs/0002-skill-catalog.md）。

## 接口（/internal/* 窄合同）

```
GET  /health                  健康检查（app 身份标识 multiagent-catalog）
GET  /internal/sources        各源状态（last_refresh / entry_count / last_error / stale）
GET  /internal/search         ?q=&source=&page=&page_size= 统一条目分页（读缓存；条目含 kind：skill|mcp）
GET  /internal/detail         ?id=&source= 确认卡预览（manifest_path/manifest_text + 审计明细）
GET  /internal/pack/{ref}     ?source= 目录包：manifest 文件集（manifest_path 指定清单文件）+ 附带文件清单
POST /internal/refresh        {"source"?: "..."} 一次爬取（逐源降级，单源失败不毁全局）
```

## 目录

- `schema.py` — 统一条目/目录包 schema + 不可信输入清洗
- `store.py` — 条目缓存与源状态 + 平台凭证仓（MemoryStore / SqliteStore，后者见 SqliteStore 类）
- `sources/` — 平台适配器（skills.sh / LobeHub），差异在此归一
- `app.py` — /internal/* 路由；`config.py` — 环境变量配置
