"""技能在线目录爬取服务（隔离区：唯一与外网交互的组件）。

⚠ 隔离边界（见 services/catalog/README.md）：
- 本服务是整个系统**唯一**与外网目录/仓库交互的组件；不持有 LLM Key、
  不持有技能包目录写权限，外网数据一律视为不可信输入（入库前清洗）
- `/internal/*` 仅对内网暴露（默认监听 127.0.0.1），不对公网开放
- 升级为独立部署单元（网关外）时本包原样搬出，主服务只改 CATALOG_BASE_URL

独立进程启动：python -m services.catalog（依赖见 requirements.txt，不回流主工程）。
"""
