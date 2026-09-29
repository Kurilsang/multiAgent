"""MCP 动态 OAuth 授权（issue #44）：token 存储 + 授权码/PKCE + 设备码 + 自动刷新。

连接定义声明 `auth: "oauth"` 时的授权支撑层，与既有静态 ${VAR} 密钥路径共存：

- **token 存储**：独立 token 文件（默认 mcp/tokens.json，`MCP_TOKEN_FILE` 覆盖），
  不进版本库、不进 `.env` 明文；写入收紧文件权限（0600）并原子替换，按服务名分槽。
- **授权码 + PKCE**：接官方 mcp SDK 的 OAuthClientProvider（授权码 + PKCE + 过期自动刷新），
  交互回调走终端贴码——浏览器跳转后的完整 URL（含 code/state）整段粘贴回来。
- **设备码（RFC 8628）**：无头/网关部署路径。终端展示 user_code 与验证地址，
  用户在浏览器完成授权，终端轮询换 token（authorization_pending/slow_down 语义对位 RFC）。
- **授权状态**：已授权 / 过期 / 未授权（管理面 GET /mcp 暴露，POST /mcp/reauth 重授权）。

流程选择（连接定义 `auth_flow`）：`auto` = 交互终端取授权码、无 TTY 取设备码；
可显式钉死 `authorization_code` / `device_code`。刷新失败降级为连接错误并提示重授权
（错误文本经 app.mcp.explain_error 叶子穿透）。

所有网络与交互都经注入缝（http / handlers.open_url / handlers.wait_for_code /
handlers.notify / sleep / time_fn），测试零真实请求、零真实授权。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

from .mcp import McpError, resolve_config_path

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .mcp import McpServer

DEFAULT_REDIRECT_URI = "http://localhost:33800/callback"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_POLL_INTERVAL = 5.0
DEVICE_POLL_TIMEOUT = 600.0
TOKEN_EXPIRY_SKEW = 60.0

_FILE_LOCK = threading.Lock()


# ---- 交互缝：授权 URL 打开 / 终端贴码 / 进度通知 ----


@dataclass
class OAuthHandlers:
    """交互缝：授权 URL 打开、终端贴码、进度通知（测试注入假实现）。"""

    open_url: Callable[[str], None]
    wait_for_code: Callable[[], str]
    notify: Callable[[str], None]


def terminal_handlers() -> OAuthHandlers:
    """默认交互实现：打印并尽量拉起浏览器、终端一行贴码。"""
    import webbrowser

    def open_url(url: str) -> None:
        webbrowser.open(url)

    def wait_for_code() -> str:
        return input("粘贴浏览器跳转后的完整 URL（含 ?code=...&state=...）：")

    def notify(message: str) -> None:
        print(message, flush=True)

    return OAuthHandlers(open_url=open_url, wait_for_code=wait_for_code, notify=notify)


def parse_callback(text: str):
    """终端贴码 → SDK 授权回调结果（code/state/iss，RFC 9207）。

    接受完整跳转 URL 或裸 query 串；缺 code 即给可读错误（错误从用户视角写）。
    """
    from mcp.shared.auth import AuthorizationCodeResult

    text = (text or "").strip()
    if not text:
        raise McpError("未粘贴回调地址：请把浏览器跳转后的完整 URL（含 ?code=...&state=...）粘贴回来")
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    if "code" not in params or not params["code"]:
        raise McpError(
            "粘贴内容里没有授权码（code）：请复制浏览器跳转后的完整 URL（形如 "
            f"{DEFAULT_REDIRECT_URI}?code=...&state=...）"
        )
    if "state" not in params:
        raise McpError(
            "粘贴内容里没有 state 参数（防跨站伪造必需）：请复制浏览器跳转后的完整 URL，"
            "而不是只复制 code"
        )
    result: dict = {"code": params["code"], "state": params["state"]}
    if params.get("iss") is not None:
        result["iss"] = params["iss"]
    return AuthorizationCodeResult(**result)


# ---- HTTP 缝：设备码流程的元数据发现 / 注册 / 换 token ----


class OAuthHttp(Protocol):
    """OAuth HTTP 缝：返回 (status_code, JSON body)；非 2xx 也返回 body（内含 error）。"""

    def get_json(self, url: str) -> tuple[int, dict]: ...

    def post_json(self, url: str, data: dict) -> tuple[int, dict]: ...

    def post_form(self, url: str, data: dict) -> tuple[int, dict]: ...


class _UrllibOAuthHttp:
    """默认实现：stdlib urllib（零新增依赖；错误体照常解析返回）。"""

    def get_json(self, url: str) -> tuple[int, dict]:
        return self._request(urllib.request.Request(url, headers={"Accept": "application/json"}))

    def post_json(self, url: str, data: dict) -> tuple[int, dict]:
        body = json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._request(request)

    def post_form(self, url: str, data: dict) -> tuple[int, dict]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        return self._request(request)

    @staticmethod
    def _request(request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - 用户显式配置的端点
                return response.status, _load_json(response.read())
        except urllib.error.HTTPError as exc:  # 4xx/5xx：body 常含 error 描述，照常上抛给调用方判读
            return exc.code, _load_json(exc.read())
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"OAuth 端点请求失败：{exc}") from exc


def _load_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ---- token 存储：独立 token 文件（不进版本库、不进 .env） ----


class FileTokenStore:
    """按服务名分槽的 token 文件存储（SDK TokenStorage + 管理面状态读取）。

    单 JSON 文件承载全部服务：`{"servers": {"<服务名>": {tokens, client_info, stored_at}}}`。
    写入走临时文件 + os.replace 原子替换，权限收紧 0600（POSIX；Windows 尽力而为）。
    """

    def __init__(self, path: Path, server_name: str, *, time_fn: Callable[[], float] | None = None):
        self._path = Path(path)
        self._name = server_name
        self._time_fn = time_fn if time_fn is not None else time.time

    # ---- 同步读写（设备码流程 / 管理面状态 / 清除授权） ----

    def read_entry(self) -> dict | None:
        entry = self._load().get("servers", {}).get(self._name)
        return entry if isinstance(entry, dict) else None

    def write_tokens(self, tokens: dict) -> None:
        self._update(tokens={str(k): v for k, v in tokens.items() if v is not None})

    def write_client_info(self, client_info: dict) -> None:
        self._update(client_info=dict(client_info))

    def clear(self) -> None:
        with _FILE_LOCK:
            data = self._load()
            data.setdefault("servers", {}).pop(self._name, None)
        self._save(data)

    def status(self) -> str:
        """授权状态：unauthorized（无 token）/ expired（过期且无法刷新）/ authorized。"""
        entry = self.read_entry()
        tokens = (entry or {}).get("tokens") or {}
        if not tokens.get("access_token"):
            return "unauthorized"
        expires_in = tokens.get("expires_in")
        stored_at = float((entry or {}).get("stored_at") or 0)
        if expires_in and stored_at and stored_at + float(expires_in) - TOKEN_EXPIRY_SKEW <= self._time_fn():
            # 过期：有 refresh_token 即自动续期（仍算已授权），否则需要人工重授权
            return "authorized" if tokens.get("refresh_token") else "expired"
        return "authorized"

    # ---- SDK TokenStorage（授权码流 + 过期自动刷新走这里） ----

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        tokens = (self.read_entry() or {}).get("tokens") or {}
        if not tokens.get("access_token"):
            return None
        return OAuthToken.model_validate(_pick(tokens, OAuthToken))

    async def set_tokens(self, tokens) -> None:
        self.write_tokens(tokens.model_dump(mode="json"))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        info = (self.read_entry() or {}).get("client_info") or {}
        if not info.get("client_id"):
            return None
        return OAuthClientInformationFull.model_validate(_pick(info, OAuthClientInformationFull))

    async def set_client_info(self, client_info) -> None:
        self.write_client_info(client_info.model_dump(mode="json"))

    # ---- 内部：文件读写 ----

    def _update(self, **fields) -> None:
        with _FILE_LOCK:
            data = self._load()
            servers = data.setdefault("servers", {})
            entry = dict(servers.get(self._name) or {})
            entry.update(fields)
            entry["stored_at"] = self._time_fn()
            servers[self._name] = entry
        self._save(data)

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # Windows 只影响只读位，POSIX 收紧属主读写
        except OSError:  # pragma: no cover - 平台差异兜底
            pass
        with _FILE_LOCK:
            os.replace(tmp, self._path)


def _pick(raw: dict, model) -> dict:
    """只取模型认识的字段（存档里可能有旁路键）。"""
    return {key: raw[key] for key in model.model_fields if key in raw}


# ---- 授权协作对象：流程选择 + 设备码 + SDK 授权码 provider ----


class McpOAuth:
    """OAuth 授权协作对象：状态查询、交互授权、连接期 SDK provider、清除授权。"""

    def __init__(
        self,
        token_file: Path | None = None,
        *,
        handlers: OAuthHandlers | None = None,
        http: "OAuthHttp | None" = None,
        sleep: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
        interactive: bool | None = None,
    ):
        if token_file is None:
            from .config import PROJECT_ROOT, Settings

            token_file = resolve_config_path(Settings().mcp_token_file, PROJECT_ROOT)
        self._token_file = Path(token_file)
        self._handlers = handlers or terminal_handlers()
        self._http = http or _UrllibOAuthHttp()
        self._sleep = sleep if sleep is not None else time.sleep
        self._time_fn = time_fn if time_fn is not None else time.time
        self._interactive = sys.stdin.isatty() if interactive is None else interactive
        self._stores: dict[str, FileTokenStore] = {}

    # ---- 状态 / 存储 ----

    def store_for(self, server: "McpServer") -> FileTokenStore:
        store = self._stores.get(server.name)
        if store is None:
            store = FileTokenStore(self._token_file, server.name, time_fn=self._time_fn)
            self._stores[server.name] = store
        return store

    def status(self, server: "McpServer") -> str:
        if server.auth != "oauth":
            return "none"
        return self.store_for(server).status()

    def clear(self, server: "McpServer") -> None:
        self.store_for(server).clear()

    # ---- 授权入口（装配期调用） ----

    def ensure_authorized(self, server: "McpServer") -> None:
        """确保服务可用授权接入：已授权即通过；未授权按流程补授权。

        授权码 + PKCE 由连接期 SDK 授权流懒触发（OAuthClientProvider 401 时拉起）；
        设备码在这里就地完成（无头/网关路径）。
        """
        if server.auth != "oauth":
            return
        if self.status(server) == "authorized":
            return
        if self._resolve_flow(server) == "device_code":
            self._device_flow(server)

    def provider(self, server: "McpServer"):
        """连接期 SDK 授权对象（授权码 + PKCE + 过期自动刷新，挂在 httpx2 客户端上）。"""
        from mcp.client.auth import OAuthClientProvider

        return OAuthClientProvider(
            server_url=server.url,
            client_metadata=_client_metadata(),
            storage=self.store_for(server),
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )

    def _resolve_flow(self, server: "McpServer") -> str:
        if server.auth_flow != "auto":
            return server.auth_flow
        return "authorization_code" if self._interactive else "device_code"

    # ---- 授权码 + PKCE：终端贴码交互（SDK 授权流回调） ----

    async def _redirect(self, url: str) -> None:
        self._handlers.notify(f"请在浏览器打开以下地址完成 OAuth 授权：\n{url}")
        try:
            self._handlers.open_url(url)
        except Exception:  # pragma: no cover - 拉不起浏览器不阻断：URL 已通知
            pass

    async def _callback(self):
        import asyncio

        text = await asyncio.to_thread(self._handlers.wait_for_code)
        return parse_callback(text)

    # ---- 设备码（RFC 8628） ----

    def _device_flow(self, server: "McpServer") -> None:
        store = self.store_for(server)
        metadata = self._discover(server.url)
        device_endpoint = str(metadata.get("device_authorization_endpoint") or "")
        token_endpoint = str(metadata.get("token_endpoint") or "")
        if not device_endpoint or not token_endpoint:
            raise McpError(
                "该授权服务器不支持设备码流程（元数据缺 device_authorization_endpoint/token_endpoint）"
                "——请改用 auth_flow=authorization_code，或用长期 PAT 走静态 token"
            )
        client_id = self._client_id(store, metadata)
        status, data = self._http.post_form(device_endpoint, {"client_id": client_id})
        if status != 200 or not data.get("device_code"):
            raise McpError(f"设备码申请失败：{_oauth_error_text(status, data)}")
        interval = float(data.get("interval") or DEVICE_POLL_INTERVAL)
        expires_at = self._time_fn() + float(data.get("expires_in") or DEVICE_POLL_TIMEOUT)
        user_code = str(data.get("user_code") or "")
        verification_uri = str(data.get("verification_uri_complete") or data.get("verification_uri") or "")
        self._handlers.notify(
            f"OAuth 设备码授权：请在浏览器打开 {verification_uri} 并输入代码 {user_code} 完成授权"
            f"（本终端会自动轮询，勿关闭）"
        )
        self._poll_token(
            token_endpoint=token_endpoint,
            client_id=client_id,
            device_code=str(data.get("device_code") or ""),
            interval=interval,
            expires_at=expires_at,
            store=store,
        )

    def _poll_token(self, *, token_endpoint, client_id, device_code, interval, expires_at, store) -> None:
        interval = max(interval, 1.0)
        while True:
            if self._time_fn() >= expires_at:
                raise McpError("设备码已过期、授权未完成——请重新发起授权")
            self._sleep(interval)
            status, data = self._http.post_form(
                token_endpoint,
                {"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": client_id},
            )
            if status == 200 and data.get("access_token"):
                store.write_tokens(
                    {
                        "access_token": data.get("access_token"),
                        "token_type": data.get("token_type") or "Bearer",
                        "expires_in": data.get("expires_in"),
                        "refresh_token": data.get("refresh_token"),
                        "scope": data.get("scope"),
                    }
                )
                self._handlers.notify("OAuth 授权完成，token 已写入独立 token 文件")
                return
            error = str(data.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5.0
                continue
            if error == "expired_token":
                raise McpError("设备码已过期、授权未完成——请重新发起授权")
            if error == "access_denied":
                raise McpError("用户在浏览器拒绝了授权——可重新发起授权或改用长期 PAT 静态 token")
            raise McpError(f"设备码换 token 失败：{_oauth_error_text(status, data)}")

    def _discover(self, server_url: str) -> dict:
        for url in _discovery_urls(server_url):
            status, data = self._http.get_json(url)
            if status == 200 and (data.get("token_endpoint") or data.get("device_authorization_endpoint")):
                return data
        raise McpError(
            "无法发现 OAuth 授权服务器元数据（/.well-known/oauth-authorization-server 不可达）"
            "——请检查端点，或改用长期 PAT 静态 token"
        )

    def _client_id(self, store: FileTokenStore, metadata: dict) -> str:
        info = (store.read_entry() or {}).get("client_info") or {}
        if info.get("client_id"):
            return str(info["client_id"])
        registration_endpoint = str(metadata.get("registration_endpoint") or "")
        if not registration_endpoint:
            raise McpError(
                "授权服务器不支持动态客户端注册（缺 registration_endpoint），设备码流程无法继续"
                "——请改用长期 PAT 静态 token"
            )
        status, data = self._http.post_json(registration_endpoint, _registration_request())
        if status != 200 or not data.get("client_id"):
            raise McpError(f"OAuth 客户端注册失败：{_oauth_error_text(status, data)}")
        store.write_client_info(data)
        return str(data["client_id"])


# ---- 默认授权协作对象（sdk_client_factory 与入口层共用同一份 token 文件） ----

_DEFAULT: "McpOAuth | None" = None
_DEFAULT_LOCK = threading.Lock()


def default_oauth() -> McpOAuth:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = McpOAuth()
        return _DEFAULT


def set_default_oauth(oauth: "McpOAuth | None") -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = oauth


# ---- 纯函数拼装 ----


def _client_metadata():
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(
        client_name="multiagent MCP client",
        redirect_uris=[DEFAULT_REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token", DEVICE_GRANT],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _registration_request() -> dict:
    metadata = _client_metadata()
    return metadata.model_dump(mode="json", exclude_none=True)


def _discovery_urls(server_url: str) -> list[str]:
    """RFC 8414 元数据发现候选：带路径插段在前，随后 origin 级，再 OIDC 兜底。"""
    parts = urllib.parse.urlsplit(server_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    urls = [f"{origin}/.well-known/oauth-authorization-server{path}"] if path else []
    urls.append(f"{origin}/.well-known/oauth-authorization-server")
    urls.append(f"{origin}/.well-known/openid-configuration")
    return urls


def _oauth_error_text(status: int, data: dict) -> str:
    error = str(data.get("error") or "")
    description = str(data.get("error_description") or "")
    detail = " ".join(part for part in (error, description) if part)
    return f"HTTP {status}{('：' + detail) if detail else ''}"
