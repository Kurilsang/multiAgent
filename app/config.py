"""统一配置与厂商注册表。

三家厂商都提供 OpenAI 兼容 API，本模块维护各厂商的接入信息，
并负责从 .env 加载配置、解析出一次对话请求实际使用的 (厂商, 模型, Key)。
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 锚定到项目根目录，避免从其他目录启动时读取不到配置
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProviderInfo:
    """单个厂商的接入信息。"""

    base_url: str
    default_model: str


PROVIDERS: dict[str, ProviderInfo] = {
    "deepseek": ProviderInfo(
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    ),
    "glm": ProviderInfo(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4.6",
    ),
    "minimax": ProviderInfo(
        base_url="https://api.minimaxi.com/v1",
        default_model="MiniMax-M2",
    ),
}


class Settings(BaseSettings):
    """从环境变量 / .env 文件加载的全局配置。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str = ""
    glm_api_key: str = ""
    minimax_api_key: str = ""

    # 可选 base_url 覆盖（留空用厂商默认值）。
    # 例如 GLM Coding Plan 套餐 Key 需指向编码专用端点：
    #   GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
    deepseek_base_url: str = ""
    glm_base_url: str = ""
    minimax_base_url: str = ""

    llm_provider: str = "glm"
    llm_model: str = ""

    # 技能在线目录爬取服务地址（留空 = 关闭在线目录功能并隐藏入口）
    catalog_base_url: str = "http://127.0.0.1:8100"
    # 本机爬取服务托管：探活失败时自动拉起子进程（独立进程语义不变）；
    # 外部部署/自行管理时置 0
    catalog_autostart: bool = True

    max_context_messages: int = Field(default=20, ge=2)

    # Agent 任务最大思考轮数（引擎侧硬上限，超过则以 partial 终态收尾）
    agent_max_iterations: int = Field(default=8, ge=1)

    # 技能包目录（相对项目根）；清单注入条数上限（超出走 search_skills）
    skills_dir: str = "skills"
    skills_catalog_max: int = Field(default=30, ge=1)

    # 聊天通道迷你工具循环上限（技能激活与依赖工具调用的轮数）
    chat_max_tool_turns: int = Field(default=4, ge=1)

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    def api_key_for(self, provider: str) -> str:
        key = getattr(self, f"{provider}_api_key", "")
        return key.strip()


class ConfigError(Exception):
    """配置不完整或非法。"""


def provider_key_field(provider: str) -> str:
    return f"{provider.upper()}_API_KEY"


@dataclass(frozen=True)
class ResolvedTarget:
    """一次对话请求实际使用的目标。"""

    provider: str
    model: str
    base_url: str
    api_key: str


def resolve_target(
    settings: Settings, provider: str | None = None, model: str | None = None
) -> ResolvedTarget:
    """解析出对话目标：厂商缺省取配置默认值，模型缺省取厂商默认值。

    注意 LLM_MODEL 是全局模型覆盖，只对配置的默认厂商生效，
    避免切换厂商后把 A 厂商的模型名发给 B 厂商。
    """
    name = (provider or "").strip().lower() or settings.llm_provider.strip().lower()
    info = PROVIDERS.get(name)
    if info is None:
        raise ConfigError(
            f"未知厂商: {name!r}，可选: {', '.join(PROVIDERS)}"
        )

    api_key = settings.api_key_for(name)
    if not api_key:
        raise ConfigError(
            f"厂商 {name} 未配置 API Key，请在 .env 中设置 {provider_key_field(name)}"
        )

    base_url = info.base_url
    url_override = getattr(settings, f"{name}_base_url", "").strip()
    if url_override:
        base_url = url_override

    resolved_model = (model or "").strip()
    if not resolved_model and name == settings.llm_provider.strip().lower():
        resolved_model = settings.llm_model.strip()
    return ResolvedTarget(
        provider=name,
        model=resolved_model or info.default_model,
        base_url=base_url,
        api_key=api_key,
    )
