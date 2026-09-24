"""测试替身:预约定缝隙的 fake 实现。

主缝隙 = LLM 客户端抽象:引擎与入口层的测试用脚本化 fake,
不发起真实请求;工具经注册表注册假工具或直接用占位工具集,
杜绝真实时钟与网络。
"""

import json
from pathlib import Path

from app.llm import ResponseToolCalls, TextDelta, ToolCall
from app.tools import Tool, ToolRegistry
from services.catalog.schema import CatalogEntry


def text_round(text: str) -> list:
    """一轮纯文本回复（模型未调用工具 → 直接作为最终答案）。"""
    return [TextDelta(text)]


def tool_round(name, arguments, call_id="call_1", thought=""):
    """一轮工具调用回复（可带思考文本）。"""
    events = []
    if thought:
        events.append(TextDelta(thought))
    events.append(
        ResponseToolCalls(
            (
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
            )
        )
    )
    return events


class FakeLLMClient:
    """按脚本逐轮响应 chat_events；记录每次收到的 messages/tools 供断言。

    脚本条目若为 Exception 实例，则在该轮直接抛出（模拟上游错误）。
    脚本耗尽后再被调用即失败，用于守住「计划外的模型调用」。
    """

    def __init__(self, script):
        self._script = [
            round_events if isinstance(round_events, Exception) else list(round_events)
            for round_events in script
        ]
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def chat_events(self, messages, provider=None, model=None, tools=None):
        self.calls.append(([dict(m) for m in messages], [dict(t) for t in (tools or [])]))
        if not self._script:
            raise AssertionError("脚本耗尽：引擎发起了计划外的模型调用")
        round_events = self._script.pop(0)
        if isinstance(round_events, Exception):
            raise round_events
        for event in round_events:
            yield event


def make_registry(name="echo", result="ok", error=None) -> ToolRegistry:
    """注册单个假工具的注册表；error 非 None 时执行即抛错。"""

    def run():
        if error is not None:
            raise error
        return result

    registry = ToolRegistry()
    registry.register(
        Tool(
            name=name,
            description="假工具",
            parameters={"type": "object", "properties": {}, "required": []},
            func=run,
        )
    )
    return registry


# ---- MCP 连接（app/mcp）测试替身：client factory 注入缝 ----


class FakeMcpSession:
    """脚本化 MCP 会话：tools/list 预设工具表、tools/call 按脚本返回/抛错。"""

    def __init__(self, tools=(), results=None, error=None):
        self.tools = list(tools)
        self.results = dict(results or {})
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def list_tools(self):
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.tools]

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.error is not None:
            raise self.error
        result = self.results.get(name, "ok")
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def make_mcp_tool(name="read_file", description="读文件", parameters=None):
    """构造 tools/list 形态的工具元数据。"""
    return {
        "name": name,
        "description": description,
        "parameters": parameters
        or {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }


# ---- 技能在线目录（services/catalog）测试替身 ----

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "catalog"


def load_catalog_fixture(name: str) -> str:
    """读取爬取适配器的 fixture（JSON/HTML/ZIP 文本形态），不发真实请求。"""
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def make_catalog_entries(count: int, source: str = "fake", prefix: str = "skill"):
    """构造 count 个统一条目（热度递减，保持榜单语义）。"""
    return [
        CatalogEntry(
            id=f"{source}/{prefix}-{i}",
            name=f"{prefix}-{i}",
            description=f"描述 {prefix}-{i}",
            source=source,
            origin="owner/repo",
            installs=(count - i) * 10,
        )
        for i in range(1, count + 1)
    ]


class FakeHttpClient:
    """脚本化 HTTP 客户端：按 URL 子串路由响应/异常（先匹配先生效），记录请求。"""

    def __init__(self, routes=None):
        self.routes = list((routes or {}).items())
        self.calls: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for needle, result in self.routes:
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                return result
        return FakeResponse("", 404)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class FakeResponse:
    """最小 HTTP 响应替身（适配器读 status_code / text / content）。"""

    def __init__(self, text="", status_code=200, content=None):
        self.text = text
        self.status_code = status_code
        self.content = content if content is not None else text.encode()


class FakeCatalogSource:
    """脚本化的目录源：crawl/detail/fetch_pack 可预设结果或错误。"""

    def __init__(self, name="fake", entries=None, detail=None, pack=None, error=None):
        self.name = name
        self.entries = entries or []
        self.detail_result = detail
        self.pack_result = pack
        self.error = error
        self.crawl_calls = 0
        self.detail_calls: list[str] = []
        self.pack_calls: list[str] = []

    def crawl(self, max_pages: int = 3):
        self.crawl_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.entries)

    def detail(self, ref: str):
        self.detail_calls.append(ref)
        if self.error is not None:
            raise self.error
        return self.detail_result

    def fetch_pack(self, ref: str):
        self.pack_calls.append(ref)
        if self.error is not None:
            raise self.error
        return self.pack_result
