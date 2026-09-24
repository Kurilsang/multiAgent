"""MCP 连接定义（模型层）。

连接定义是单一声明式文件（默认 mcp/servers.json）里的条目：名称 +
transport + 命令 argv / endpoint + `${VAR}` 环境变量占位 + 启用位/来源/
观察值视图大小/工具白名单。文件态是唯一事实源（对位技能包约定，SPEC-0003）。

运行时侧（McpManager，client factory 注入缝）见本模块后半部分：
启用服务暴露的工具以 `mcp__<服务短名>__<工具名>` 灌入工具注册表，引擎零改动。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

TRANSPORTS = ("stdio", "streamable-http")
MAX_NAME_CHARS = 200
FUNCTION_NAME_MAX = 64

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class McpError(Exception):
    """连接定义非法或 MCP 服务不可用，携带面向用户的可读信息。"""


class McpNamingError(McpError):
    """工具名无法落入 function name 约束（超长或清洗后为空）。"""


@dataclass(frozen=True)
class McpServer:
    """一条 MCP 服务连接定义。"""

    name: str  # 全名（reverse-DNS 或自拟），展示用
    transport: str  # stdio | streamable-http
    command: tuple[str, ...] = ()  # stdio：argv 列表，不经 shell
    url: str = ""  # streamable-http：endpoint（支持 ${VAR} 模板变量）
    env: Mapping[str, str] = field(default_factory=dict)  # ${VAR} 占位原文
    headers: Mapping[str, str] = field(default_factory=dict)  # 远程请求头（密钥占位）
    enabled: bool = True
    source: str = "local"  # local | catalog:<平台> | import:<path>
    max_observation_chars: int | None = None  # 观察值视图大小覆盖；None=全局默认
    enabled_tools: tuple[str, ...] = ()  # 工具白名单；空 = 全量


def load_servers(
    *, path: Path | None = None, text: str | None = None
) -> tuple[list[McpServer], list[str]]:
    """读取连接定义文件：返回 (有效条目, 中文错误列表)。

    文件缺失 = 空配置（不算错误）；单条非法只拒该条，不牵连其他条目。
    占位符不在解析期展开（连接期解析，见 resolve_placeholders）。
    """
    if text is None:
        if path is None or not Path(path).is_file():
            return [], []
        raw_text = Path(path).read_text(encoding="utf-8")
    else:
        raw_text = text
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [], [f"连接定义文件不是有效 JSON：{exc.msg}"]
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        return [], ["连接定义文件须为 {\"servers\": [...]} 形状"]

    servers: list[McpServer] = []
    errors: list[str] = []
    for index, raw in enumerate(data["servers"], start=1):
        try:
            servers.append(_parse_entry(raw))
        except McpError as exc:
            errors.append(f"第 {index} 条连接定义无效：{exc}")
    return servers, errors


def _parse_entry(raw) -> McpServer:
    if not isinstance(raw, dict):
        raise McpError("条目须为对象")
    name = _clean_str(raw.get("name"), MAX_NAME_CHARS)
    if not name:
        raise McpError("缺 name（服务名，如 io.github.acme/filesystem）")

    transport = _clean_str(raw.get("transport"), 32)
    if transport not in TRANSPORTS:
        raise McpError(f"transport 必为 {' | '.join(TRANSPORTS)} 之一，收到 {transport!r}")

    command: tuple[str, ...] = ()
    url = ""
    if transport == "stdio":
        argv = raw.get("command")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item.strip() for item in argv)
        ):
            raise McpError("stdio 条目需要 command（非空 argv 字符串列表，不经 shell）")
        command = tuple(item.strip() for item in argv)
    else:
        url = _clean_str(raw.get("url"), 500)
        if not url:
            raise McpError("streamable-http 条目需要 url（endpoint 地址）")

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise McpError("env 须为 {变量名: 值} 对象（值可用 ${VAR} 占位）")
    env = {str(key): str(value) for key, value in env_raw.items()}

    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise McpError("headers 须为 {请求头名: 值} 对象（值可用 ${VAR} 占位）")
    headers = {str(key): str(value) for key, value in headers_raw.items()}

    max_observation_chars = raw.get("max_observation_chars")
    if max_observation_chars is not None:
        try:
            max_observation_chars = int(max_observation_chars)
        except (TypeError, ValueError):
            raise McpError("max_observation_chars 须为整数（0 = 全量直灌）") from None
        if max_observation_chars < 0:
            raise McpError("max_observation_chars 不能为负数")

    tools_raw = raw.get("enabled_tools") or ()
    if not isinstance(tools_raw, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in tools_raw
    ):
        raise McpError("enabled_tools 须为工具名列表（可省略 = 全量）")

    return McpServer(
        name=name,
        transport=transport,
        command=command,
        url=url,
        env=env,
        headers=headers,
        enabled=bool(raw.get("enabled", True)),
        source=_clean_str(raw.get("source"), 64) or "local",
        max_observation_chars=max_observation_chars,
        enabled_tools=tuple(item.strip() for item in tools_raw),
    )


def resolve_placeholders(
    *,
    env: Mapping[str, str],
    url: str = "",
    headers: Mapping[str, str] | None = None,
    environ: Mapping[str, str],
) -> tuple[dict, list[str]]:
    """解析 ${VAR} 占位（值来自 .env / 环境变量），覆盖 env 值、url 模板与请求头。

    返回 ({"env": 解析后, "url": 解析后, "headers": 解析后}, 未解析变量名列表)；
    未解析的占位保留原文，由调用方按「报错并禁用该服务」处置。
    """
    missing: list[str] = []

    def resolve(text: str) -> str:
        def substitute(match: re.Match) -> str:
            var = match.group(1)
            if var in environ:
                return str(environ[var])
            if var not in missing:
                missing.append(var)
            return match.group(0)

        return _PLACEHOLDER_RE.sub(substitute, text)

    return (
        {
            "env": {k: resolve(v) for k, v in env.items()},
            "url": resolve(url),
            "headers": {k: resolve(v) for k, v in (headers or {}).items()},
        },
        missing,
    )


def build_environ(dotenv_path: Path | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """占位符取值环境：进程环境优先，.env 文件兜底（KEY=VALUE 简单格式）。"""
    values: dict[str, str] = {}
    if dotenv_path is not None and Path(dotenv_path).is_file():
        for line in Path(dotenv_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    values.update(dict(environ if environ is not None else os.environ))
    return values


def _redact_url(url: str) -> str:
    """展示脱敏：去掉 userinfo/query/fragment（密钥可能藏于其中），保留域名与路径。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url or "")
    if not parts.scheme:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def short_server_name(name: str) -> str:
    """全名清洗为 function name 安全前缀（非法字符 → -，如 io.github.a/b → io-github-a-b）。"""
    return _UNSAFE_CHARS.sub("-", name)


def tool_name(server_short: str, tool: str) -> str:
    """组合 MCP 工具全名：mcp__<服务短名>__<工具名>；超长/清洗后为空即拒绝（fail loud）。"""
    short_tool = _UNSAFE_CHARS.sub("-", tool)
    if not short_tool:
        raise McpNamingError(f"工具名 {tool!r} 清洗后为空，无法注册")
    full = f"mcp__{server_short}__{short_tool}"
    if len(full) > FUNCTION_NAME_MAX:
        raise McpNamingError(
            f"工具全名超长（{len(full)} > {FUNCTION_NAME_MAX} 字符）：{full!r}"
        )
    return full


def _clean_str(value, limit: int) -> str:
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    return " ".join(text.split())[:limit]


def runner_problem(server: McpServer) -> str | None:
    """宿主运行器预检：命令不在 PATH 且非可执行路径 → 中文问题描述。"""
    if server.transport != "stdio":
        return None
    argv0 = server.command[0] if server.command else ""
    if shutil.which(argv0) or Path(argv0).exists():
        return None
    return f"未找到命令 {argv0!r}（缺运行器？请安装 npx/uvx/docker 等或改用完整路径）"


class McpSession(Protocol):
    """一个已建连 MCP 服务的会话（client factory 注入缝的产物）。

    默认实现由官方 mcp SDK 撑起（见 sdk_client_factory）；测试注入脚本化 fake。
    """

    def list_tools(self) -> list[dict]:
        """工具表：[{"name", "description", "parameters"}]。"""
        ...

    def call_tool(self, name: str, arguments: dict) -> str:
        """调用工具（name 为 MCP 原始工具名），返回文本结果。"""
        ...

    def close(self) -> None: ...


@dataclass
class _ToolEntry:
    server: McpServer
    meta: dict
    session: McpSession
    full_name: str


class McpManager:
    """MCP 连接管理器：建连、工具灌名、调用路由、启停状态（内存态）。

    部装配期 fail loud：命名冲突/超长、缺 runner、占位符未解析、
    连接失败、超工具总数上限——都拒绝加载该服务并给中文错误，不静默改名。
    """

    def __init__(
        self,
        servers: list[McpServer],
        *,
        client_factory,
        environ: Mapping[str, str] | None = None,
        max_tools: int = 0,
        config_path: Path | None = None,
        registry=None,
    ):
        self._servers = list(servers)
        self._factory = client_factory
        self._environ = dict(environ if environ is not None else build_environ())
        self._max_tools = max_tools
        self._config_path = Path(config_path) if config_path is not None else None
        self._registry = registry
        self._sessions: dict[str, McpSession] = {}
        self._entries: dict[str, _ToolEntry] = {}
        self._statuses: dict[str, tuple[str, str]] = {}
        self._resolved_servers: dict[str, McpServer] = {}
        self.load_errors: list[str] = []

    def connect(self, reserved=()) -> list[str]:
        """对全部启用条目建连并灌工具表；返回本轮中文加载错误（累积于 load_errors）。"""
        reserved = set(reserved)
        seen_names: dict[str, str] = {}
        seen_short: dict[str, str] = {}
        errors: list[str] = []
        for server in self._servers:
            if not server.enabled:
                self._statuses.setdefault(server.name, ("disabled", ""))
                continue
            try:
                self._connect_one(server, seen_names, seen_short, reserved)
            except McpError as exc:
                self._reject(server.name, str(exc), errors)
            finally:
                seen_names.setdefault(server.name, server.name)
                seen_short.setdefault(short_server_name(server.name), server.name)
        self.load_errors.extend(errors)
        return errors

    def _connect_one(self, server, seen_names, seen_short, reserved) -> None:
        if server.name in seen_names:
            raise McpError(f"服务名重复：{server.name!r}")
        short = short_server_name(server.name)
        if short in seen_short:
            raise McpError(
                f"工具名前缀冲突：与服务 {seen_short[short]!r} 清洗后同为 {short!r}"
            )
        problem = runner_problem(server)
        if problem:
            raise McpError(problem)
        resolved, missing = resolve_placeholders(
            env=server.env, url=server.url, headers=server.headers, environ=self._environ
        )
        if missing:
            raise McpError(
                f"未解析的占位符：{'、'.join('${' + var + '}' for var in missing)}"
                f"——请在 .env 或环境变量中配置"
            )
        session = self._factory(
            replace(
                server,
                env=resolved["env"],
                url=resolved["url"],
                headers=resolved["headers"],
            )
        )
        try:
            metas = session.list_tools()
        except Exception as exc:
            try:
                session.close()
            except Exception:
                pass
            raise McpError(f"连接失败/工具清单拉取失败：{exc}") from exc
        if server.enabled_tools:
            allowed = set(server.enabled_tools)
            metas = [meta for meta in metas if meta.get("name") in allowed]
        entries: list[_ToolEntry] = []
        taken = set(reserved)
        try:
            for meta in metas:
                full = tool_name(short, meta.get("name", ""))
                if full in taken:
                    raise McpNamingError(f"工具全名与已有工具冲突：{full!r}")
                taken.add(full)
                entries.append(_ToolEntry(server=server, meta=meta, session=session, full_name=full))
        except McpNamingError as exc:
            try:
                session.close()
            except Exception:
                pass
            raise McpError(f"工具命名失败，已拒绝加载该服务：{exc}") from exc
        if self._max_tools and len(self._entries) + len(entries) > self._max_tools:
            try:
                session.close()
            except Exception:
                pass
            raise McpError(
                f"工具总数将超上限（{self._max_tools}），已拒绝加载该服务"
                f"——请用 enabled_tools 收窄或提高 MCP_MAX_TOOLS"
            )
        self._sessions[server.name] = session
        self._resolved_servers[server.name] = replace(
            server,
            env=resolved["env"],
            url=resolved["url"],
            headers=resolved["headers"],
        )
        for entry in entries:
            self._entries[entry.full_name] = entry
            if self._registry is not None:
                self._registry.register(self._tool_for(entry))
        self._statuses[server.name] = ("connected", "")

    def _reject(self, name: str, message: str, errors: list[str]) -> None:
        self._statuses[name] = ("error", message)
        errors.append(f"MCP 服务 {name!r}：{message}")

    def build_tools(self) -> list["Tool"]:
        """把已建连工具包成注册表工具（func 路由回本管理器）。"""
        return [self._tool_for(entry) for entry in self._entries.values()]

    def _tool_for(self, entry: _ToolEntry) -> "Tool":
        from .tools import Tool

        return Tool(
            name=entry.full_name,
            description=entry.meta.get("description")
            or f"MCP 工具 {entry.meta.get('name')}（来自 {entry.server.name}）",
            parameters=entry.meta.get("parameters")
            or {"type": "object", "properties": {}, "required": []},
            func=_make_tool_func(self, entry.full_name),
            max_observation_chars=entry.server.max_observation_chars,
        )

    def call_tool(self, full_name: str, arguments: dict) -> str:
        entry = self._entries.get(full_name)
        if entry is None:
            raise McpError(f"未注册的 MCP 工具: {full_name!r}")
        try:
            return self._invoke(entry, arguments)
        except McpError:  # 工具语义错误：不重试，交给模型决断
            raise
        except Exception as exc:  # 传输断裂：自动重连一次并重试该次调用
            self._reconnect(entry.server.name, cause=exc)
            entry = self._entries.get(full_name)
            if entry is None:
                raise McpError(f"重连后工具已不存在: {full_name!r}") from exc
            try:
                return self._invoke(entry, arguments)
            except McpError:
                raise
            except Exception as retry_exc:
                raise McpError(
                    f"MCP 工具 {full_name} 调用失败（重连后仍失败）：{retry_exc}"
                ) from retry_exc

    @staticmethod
    def _invoke(entry: "_ToolEntry", arguments: dict) -> str:
        return str(entry.session.call_tool(entry.meta.get("name", ""), arguments))

    def _reconnect(self, name: str, cause: Exception) -> None:
        """调用中断连后重建会话（恰好一次重试的支撑）；失败译成中文 McpError。"""
        old = self._sessions.pop(name, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        resolved = self._resolved_servers.get(name)
        if resolved is None:
            raise McpError(f"MCP 服务 {name!r} 重连失败：连接定义已不存在（原错误：{cause}）")
        try:
            session = self._factory(resolved)
        except Exception as exc:
            self._statuses[name] = ("error", f"重连失败：{exc}")
            raise McpError(f"MCP 服务 {name!r} 重连失败：{exc}（原错误：{cause}）") from exc
        self._sessions[name] = session
        for entry in self._entries.values():
            if entry.server.name == name:
                entry.session = session

    def listing(self) -> list[dict]:
        """GET /mcp 列表载荷：服务 + 状态 + 工具全名（env 只列变量名，值脱敏）。"""
        items = []
        for server in self._servers:
            status, detail = self._statuses.get(server.name, ("disabled", ""))
            items.append(
                {
                    "name": server.name,
                    "transport": server.transport,
                    "command": list(server.command),
                    "url": _redact_url(server.url),
                    "source": server.source,
                    "enabled": server.enabled,
                    "env_keys": sorted(server.env),
                    "header_keys": sorted(server.headers),
                    "tools": sorted(
                        full for full, entry in self._entries.items()
                        if entry.server.name == server.name
                    ),
                    "status": status,
                    "error": detail,
                }
            )
        return items

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()
        self._entries.clear()

    # ---- 管理面：启用/禁用/删除/重载（文件态唯一事实源 + 即时注册表同步） ----

    def set_enabled(self, name: str, enabled: bool) -> dict:
        """启用/禁用服务：启用即建连注册、禁用即摘除；变更落盘。返回状态条目。"""
        server = self._find(name)
        if server is None:
            raise McpError(f"未找到 MCP 服务: {name!r}")
        if bool(server.enabled) != bool(enabled):
            self._replace_server(replace(server, enabled=bool(enabled)))
            self._persist()
        if not enabled:
            self._drop_server(name, status=("disabled", ""))
            return self.listing_item(name)
        if name not in self._sessions:
            errors: list[str] = []
            try:
                self._connect_one(self._find(name), {}, {}, self._reserved())
            except McpError as exc:
                self._reject(name, str(exc), errors)
                self.load_errors.extend(errors)
        return self.listing_item(name)

    def remove(self, name: str) -> None:
        """删除服务：摘除工具、关会话、出清单并落盘。"""
        if self._find(name) is None:
            raise McpError(f"未找到 MCP 服务: {name!r}")
        self._drop_server(name, status=("disabled", ""))
        self._statuses.pop(name, None)
        self._servers = [item for item in self._servers if item.name != name]
        self._persist()

    def reload(self) -> list[str]:
        """重读连接定义文件并整体重连（外部手改文件后的生效入口）。"""
        for name in [item.name for item in self._servers]:
            self._drop_server(name, status=("disabled", ""))
        self._statuses.clear()
        servers, parse_errors = (
            load_servers(path=self._config_path)
            if self._config_path is not None
            else ([], [])
        )
        self._servers = servers
        self.load_errors = list(parse_errors)
        self.connect(reserved=self._reserved())
        return list(self.load_errors)

    def install(
        self, server: McpServer, environ_update: Mapping[str, str] | None = None
    ) -> tuple[str, str]:
        """安装一条连接定义：落盘 + 立即建连注册（同名跳过）。返回 (状态, 说明)。"""
        if self._find(server.name) is not None:
            return "skipped", "同名服务已存在"
        if environ_update:  # 新写入的密钥并入解析环境：装了即用，不等重启
            self._environ.update(
                {str(key): str(value) for key, value in environ_update.items()}
            )
        self._servers.append(server)
        self._persist()
        errors: list[str] = []
        try:
            self._connect_one(server, {}, {}, self._reserved())
        except McpError as exc:
            self._reject(server.name, str(exc), errors)
            self.load_errors.extend(errors)
            return "installed", f"已写入连接定义；连接失败：{exc}"
        return "installed", "已写入连接定义并生效"

    def listing_item(self, name: str) -> dict:
        for item in self.listing():
            if item["name"] == name:
                return item
        raise McpError(f"未找到 MCP 服务: {name!r}")

    def _find(self, name: str) -> McpServer | None:
        for server in self._servers:
            if server.name == name:
                return server
        return None

    def _replace_server(self, server: McpServer) -> None:
        self._servers = [
            server if item.name == server.name else item for item in self._servers
        ]

    def _reserved(self) -> set:
        return set(self._registry.names()) if self._registry is not None else set()

    def _drop_server(self, name: str, *, status: tuple[str, str]) -> None:
        session = self._sessions.pop(name, None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        for full in [key for key, entry in self._entries.items() if entry.server.name == name]:
            del self._entries[full]
            if self._registry is not None:
                self._registry.unregister(full)
        self._resolved_servers.pop(name, None)
        self._statuses[name] = status

    def _persist(self) -> None:
        save_servers(self._config_path, self._servers)


def _make_tool_func(manager: McpManager, full_name: str):
    def run(**arguments) -> str:
        return manager.call_tool(full_name, arguments)

    return run


def resolve_config_path(value: str, root: Path) -> Path:
    """连接定义文件路径：相对路径锚定项目根（对位 resolve_skills_dir 约定）。"""
    path = Path(value)
    return path if path.is_absolute() else Path(root) / path


def save_servers(path: Path | None, servers: list[McpServer]) -> None:
    """把连接定义写回文件（文件态唯一事实源的落盘面；管理面变更用）。"""
    if path is None:
        return
    entries = []
    for server in servers:
        entry: dict = {"name": server.name, "transport": server.transport}
        if server.command:
            entry["command"] = list(server.command)
        if server.url:
            entry["url"] = server.url
        if server.env:
            entry["env"] = dict(server.env)
        if server.headers:
            entry["headers"] = dict(server.headers)
        entry["enabled"] = server.enabled
        if server.source and server.source != "local":
            entry["source"] = server.source
        if server.max_observation_chars is not None:
            entry["max_observation_chars"] = server.max_observation_chars
        if server.enabled_tools:
            entry["enabled_tools"] = list(server.enabled_tools)
        entries.append(entry)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps({"servers": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_dotenv(path: Path | None, values: Mapping[str, str]) -> None:
    """把密钥值写入 .env（连接定义只留 ${VAR} 占位；已存在键原地更新）。"""
    if path is None or not values:
        return
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    updated = {str(key): str(value) for key, value in values.items()}
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key and key in updated:
            out.append(f"{key}={updated[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updated.items():
        if key not in seen:
            out.append(f"{key}={value}")
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


def parse_install_text(
    text: str, *, source: str
) -> tuple[list[tuple[str, "McpServer | None", str]], list[str]]:
    """安装内容 → 连接定义候选：兼容 server.json（Registry ServerJSON）与 mcp.json。

    返回 ([(名称, 条目或 None, 错误说明)], 警告列表)；逐条语义对位技能安装报告。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"安装内容不是有效 JSON：{exc.msg}") from exc
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        return _entries_from_mcp_json(data, source=source), []
    if isinstance(data, dict) and isinstance(data.get("servers"), list):
        items = data["servers"]
    elif isinstance(data, list):
        items = data
    else:
        items = [data]
    candidates: list[tuple[str, "McpServer | None", str]] = []
    warnings: list[str] = []
    for item in items:
        raw = item if isinstance(item, dict) else {}
        name = str(raw.get("name") or "") or "(无名条目)"
        server, error, item_warnings = _entry_from_server_json(raw, source=source)
        warnings.extend(item_warnings)
        candidates.append((name, server, error))
    return candidates, warnings


def _entry_from_server_json(
    data: dict, *, source: str
) -> tuple["McpServer | None", str, list[str]]:
    """ServerJSON → 连接定义：包坐标合成 runner 命令模板，或取远程 endpoint。"""
    warnings: list[str] = []
    name = str(data.get("name") or "").strip()
    if not name:
        return None, "缺 name（server 全名）", warnings
    packages = [item for item in data.get("packages") or [] if isinstance(item, dict)]
    package = next(
        (item for item in packages if item.get("registryType") != "mcpb"), None
    )
    if package is not None:
        command = _command_from_package(package, warnings)
        if not command:
            return None, "无法合成命令模板（缺包坐标）", warnings
        env = {
            str(decl.get("name")): f"${{{decl.get('name')}}}"
            for decl in package.get("environmentVariables") or []
            if isinstance(decl, dict) and decl.get("name")
        }
        return (
            McpServer(
                name=name,
                transport="stdio",
                command=tuple(command),
                env=env,
                source=source,
            ),
            "",
            warnings,
        )
    remotes = [item for item in data.get("remotes") or [] if isinstance(item, dict)]
    remote = next(
        (item for item in remotes if item.get("type") == "streamable-http"), None
    )
    if remote is not None:
        url = str(remote.get("url") or "").strip()
        if not url:
            return None, "远程条目缺 url", warnings
        headers = {}
        for decl in remote.get("headers") or []:
            header = str(decl.get("name") or "")
            var = re.sub(r"[^A-Za-z0-9_]", "_", header).upper()
            if header and var:
                headers[header] = f"${{{var}}}"
        return (
            McpServer(
                name=name,
                transport="streamable-http",
                url=url,
                headers=headers,
                source=source,
            ),
            "",
            warnings,
        )
    if packages:
        return (
            None,
            "mcpb 单文件包暂不支持安装",
            warnings + [f"{name}: 附带可执行物的 mcpb 包本期不落盘"],
        )
    if remotes:
        return None, "仅 sse 远程（协议已废弃），暂不支持", warnings
    return None, "条目无可安装形态（无 packages/remotes）", warnings


def _command_from_package(package: dict, warnings: list[str]) -> list[str]:
    """包坐标 → runner 命令模板（官方 schema 无现成命令串，客户端拼装；不经 shell）。"""
    argv: list[str] = []
    hint = str(package.get("runtimeHint") or "").strip()
    if hint:
        argv.append(hint)
    for arg in package.get("runtimeArguments") or []:
        value = str(arg.get("value") or "") if isinstance(arg, dict) else ""
        if value:
            argv.append(value)
    identifier = str(package.get("identifier") or "").strip()
    if not identifier:
        return []
    version = str(package.get("version") or "").strip()
    argv.append(f"{identifier}@{version}" if version else identifier)
    for arg in package.get("packageArguments") or []:
        if not isinstance(arg, dict):
            continue
        value = str(arg.get("value") or "")
        if "{" in value and "}" in value:
            warnings.append(f"参数含变量占位已跳过：{value}")
            continue
        if arg.get("type") == "named" and arg.get("name"):
            argv.append(str(arg["name"]))
        if value:
            argv.append(value)
    return argv


def _entries_from_mcp_json(data: dict, *, source: str):
    """mcp.json（{"mcpServers": {...}} 客户端惯用格式）→ 连接定义候选。"""
    candidates = []
    for name, config in (data.get("mcpServers") or {}).items():
        config = config if isinstance(config, dict) else {}
        command = config.get("command")
        args = config.get("args") or []
        if isinstance(command, list):
            argv = [str(item) for item in command]
        elif command:
            argv = [str(command), *[str(item) for item in args]]
        else:
            argv = []
        env = {str(k): str(v) for k, v in (config.get("env") or {}).items()}
        headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
        url = str(config.get("url") or "")
        if argv:
            server = McpServer(
                name=str(name),
                transport="stdio",
                command=tuple(argv),
                env=env,
                headers=headers,
                source=source,
            )
            candidates.append((str(name), server, ""))
        elif url:
            server = McpServer(
                name=str(name),
                transport="streamable-http",
                url=url,
                env=env,
                headers=headers,
                source=source,
            )
            candidates.append((str(name), server, ""))
        else:
            candidates.append((str(name), None, "缺 command 与 url"))
    return candidates


# ---- 默认 client factory：官方 mcp SDK 会话（stdio） ----

_CONNECT_TIMEOUT = 30.0
_CALL_TIMEOUT = 60.0
_CLOSE_TIMEOUT = 10.0


class _LoopThread:
    """常驻事件循环线程：同步入口经 run_coroutine_threadsafe 桥接 async SDK。"""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="mcp-client", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro) -> "concurrent.futures.Future":
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, fn) -> None:
        self._loop.call_soon_threadsafe(fn)


_LOOP_THREAD: _LoopThread | None = None


def _loop_thread() -> _LoopThread:
    global _LOOP_THREAD
    if _LOOP_THREAD is None:
        _LOOP_THREAD = _LoopThread()
    return _LOOP_THREAD


class _SdkSessionBase:
    """官方 mcp SDK 会话的同步适配基类（stdio / streamable-http 共用生命周期）。

    会话生命周期全程驻留在同一协程任务里——anyio 的退出栈要求
    enter/exit 同任务，close 只是把关闭事件置位后等任务收尾。
    传输差异由子类 _enter_transport 消化。
    """

    def __init__(self, server: McpServer):
        self._server = server
        self._closing = asyncio.Event()
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._done: concurrent.futures.Future = concurrent.futures.Future()
        self._session = None
        self._closed = False
        _loop_thread().submit(self._lifecycle())
        try:
            self._ready.result(timeout=_CONNECT_TIMEOUT)
        except Exception as exc:
            self.close()
            raise McpError(f"MCP 服务连接失败：{exc}") from exc

    async def _lifecycle(self) -> None:
        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession

            async with AsyncExitStack() as stack:
                read, write = await self._enter_transport(stack)
                session = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._session = session
                self._ready.set_result(True)
                await self._closing.wait()
                self._session = None
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            if not self._done.done():
                self._done.set_result(True)

    def list_tools(self) -> list[dict]:
        session = self._require_session()
        result = _loop_thread().submit(session.list_tools()).result(_CALL_TIMEOUT)
        return [
            {
                "name": item.name,
                "description": item.description or "",
                "parameters": getattr(item, "inputSchema", None)
                or {"type": "object", "properties": {}},
            }
            for item in result.tools
        ]

    def call_tool(self, name: str, arguments: dict) -> str:
        session = self._require_session()
        try:
            result = _loop_thread().submit(
                session.call_tool(name, arguments)
            ).result(_CALL_TIMEOUT)
        except McpError:
            raise
        except Exception as exc:  # SDK 语义错误译成应用错误（不触发断连重试）
            error = _as_app_error(exc)
            if error is exc:
                raise
            raise error from exc
        texts = [
            getattr(block, "text", "")
            for block in (getattr(result, "content", None) or [])
            if getattr(block, "type", "") == "text"
        ]
        text = "\n".join(item for item in texts if item)
        if getattr(result, "isError", False):
            raise McpError(text or "工具返回错误")
        return text or str(result.content)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        thread = _loop_thread()
        thread.call_soon(self._closing.set)
        try:
            self._done.result(timeout=_CLOSE_TIMEOUT)
        except Exception:
            pass

    def _require_session(self):
        if self._session is None:
            raise McpError("MCP 会话已关闭或未就绪")
        return self._session


    async def _enter_transport(self, stack):
        """进入传输上下文，返回 (read, write) 流（子类实现）。"""
        raise NotImplementedError


class _SdkStdioSession(_SdkSessionBase):
    """stdio 会话：argv 直起子进程（不经 shell），env 合并默认环境。"""

    async def _enter_transport(self, stack):
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=self._server.command[0],
            args=list(self._server.command[1:]),
            env={**_default_environment(), **dict(self._server.env)},
        )
        streams = await stack.enter_async_context(stdio_client(params))
        return streams[0], streams[1]


class _SdkStreamableHttpSession(_SdkSessionBase):
    """streamable-http 会话：远程 endpoint 直连，密钥经请求头传递（展示脱敏）。"""

    async def _enter_transport(self, stack):
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        client = await stack.enter_async_context(
            httpx2.AsyncClient(headers=dict(self._server.headers) or None)
        )
        streams = await stack.enter_async_context(
            streamable_http_client(self._server.url, http_client=client)
        )
        return streams[0], streams[1]


def _default_environment() -> dict:
    try:
        from mcp.client.stdio import get_default_environment

        return dict(get_default_environment())
    except ImportError:  # pragma: no cover - SDK 版本差异兜底
        return dict(os.environ)


def _as_app_error(exc: Exception) -> Exception:
    """SDK 侧语义错误（RPC 拒绝/参数错误）译成应用 McpError——不属断连，不重试。"""
    try:
        from mcp.shared.exceptions import MCPError as _SdkMcpError
    except ImportError:  # pragma: no cover - SDK 版本差异兜底
        return exc
    if isinstance(exc, _SdkMcpError):
        return McpError(f"工具调用被服务端拒绝：{exc}")
    return exc


def sdk_client_factory(server: McpServer) -> McpSession:
    """默认 client factory：官方 mcp SDK 会话（stdio / streamable-http）。"""
    try:
        if server.transport == "stdio":
            return _SdkStdioSession(server)
        return _SdkStreamableHttpSession(server)
    except McpError:
        raise
    except Exception as exc:  # pragma: no cover - 防御：保证错误都译成 McpError
        raise McpError(f"MCP 服务连接失败：{exc}") from exc
