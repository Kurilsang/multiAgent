"""独立服务集合（与主服务 app/ 隔离）。

services/ 下的组件可独立进程部署；与主服务之间只存在窄接口合同
（见 services/catalog/README.md 的隔离标注），不共享状态文件。
"""
