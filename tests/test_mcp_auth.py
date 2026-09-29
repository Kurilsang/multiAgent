"""动态 OAuth 授权流测试（issue #44）：token 存储 + 授权码贴码 + 设备码 + 管理面。

预约定缝（issue #44 测试决策）：授权协作对象（McpOAuth 注入）与 OAuth HTTP 缝
（FakeOAuthHttp）之外零真实网络/授权请求；时钟与睡眠同样注入（FakeClock），
杜绝真实等待。SDK 授权码 provider 只验我们注入的交互缝，不发真实请求。
"""

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.mcp import McpError, McpManager, McpServer
from app.mcp_auth import (
    DEFAULT_REDIRECT_URI,
    FileTokenStore,
    McpOAuth,
    OAuthHandlers,
    parse_callback,
)

from tests.fakes import FakeMcpOAuth, FakeMcpSession, FakeOAuthHttp, make_mcp_tool

SERVER_URL = "https://mcp.acme.dev/db/mcp"
AUTH_SERVER = "https://auth.example"
METADATA = {
    "token_endpoint": f"{AUTH_SERVER}/token",
    "device_authorization_endpoint": f"{AUTH_SERVER}/device",
    "registration_endpoint": f"{AUTH_SERVER}/register",
}
DEVICE_CODE_RESPONSE = (
    200,
    {
        "device_code": "dc-1",
        "user_code": "ABCD-1234",
        "verification_uri": f"{AUTH_SERVER}/activate",
        "expires_in": 300,
        "interval": 1,
    },
)
TOKEN_RESPONSE = (
    200,
    {
        "access_token": "at-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "rt-1",
    },
)


class FakeClock:
    """可拨快的假时钟（与 sleep 注入缝配合，杜绝真实等待）。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_server(**overrides) -> McpServer:
    values = dict(
        name="acme/remote",
        transport="streamable-http",
        url=SERVER_URL,
        auth="oauth",
    )
    values.update(overrides)
    return McpServer(**values)


def make_handlers(code_text=""):
    """记录式交互缝：open_url/notify 记录调用，wait_for_code 回放粘贴文本。"""
    record = {"opened": [], "notices": [], "wait_calls": 0}

    def open_url(url):
        record["opened"].append(url)

    def wait_for_code():
        record["wait_calls"] += 1
        return code_text

    def notify(message):
        record["notices"].append(message)

    handlers = OAuthHandlers(open_url=open_url, wait_for_code=wait_for_code, notify=notify)
    return handlers, record


def make_oauth(token_file, http=None, *, interactive=False, code_text="", clock=None):
    clock = clock or FakeClock()
    handlers, record = make_handlers(code_text)
    oauth = McpOAuth(
        token_file,
        handlers=handlers,
        http=http or FakeOAuthHttp(),
        sleep=clock.advance,
        time_fn=clock,
        interactive=interactive,
    )
    return oauth, record, clock


def device_script(*polls):
    """设备码全流程脚本：发现 → 注册 → 申请设备码 → 轮询若干次。"""
    return [
        (200, dict(METADATA)),
        (200, {"client_id": "cid-1"}),
        DEVICE_CODE_RESPONSE,
        *polls,
    ]


class TokenStoreTest(unittest.TestCase):
    """token 文件存储：分槽、状态三态、权限收紧、损坏文件兜底。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"
        self.clock = FakeClock()
        self.store = FileTokenStore(self.path, "acme/remote", time_fn=self.clock)

    def test_fresh_file_is_unauthorized(self):
        self.assertEqual(self.store.status(), "unauthorized")
        self.assertFalse(self.path.exists())  # 只读不落盘

    def test_tokens_round_trip_sync_and_async(self):
        self.store.write_tokens({"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600})
        entry = self.store.read_entry()
        self.assertEqual(entry["tokens"]["access_token"], "at-1")

        tokens = asyncio.run(self.store.get_tokens())
        self.assertEqual(tokens.access_token, "at-1")
        self.assertEqual(tokens.token_type, "Bearer")

        async def refresh():
            await self.store.set_tokens(tokens.model_copy(update={"access_token": "at-2"}))

        asyncio.run(refresh())
        self.assertEqual(self.store.read_entry()["tokens"]["access_token"], "at-2")

    def test_client_info_round_trip(self):
        from mcp.shared.auth import OAuthClientInformationFull

        self.store.write_client_info({"client_id": "cid-1"})
        info = asyncio.run(self.store.get_client_info())
        self.assertEqual(info.client_id, "cid-1")
        self.assertIsInstance(info, OAuthClientInformationFull)
        self.assertIsNone(FileTokenStore(self.path, "other/服务").read_entry())  # 分槽隔离

    def test_status_transitions(self):
        self.store.write_tokens({"access_token": "at-1", "expires_in": 3600})
        self.assertEqual(self.store.status(), "authorized")
        self.clock.advance(3600)  # 过期且无 refresh_token → 需人工重授权
        self.assertEqual(self.store.status(), "expired")
        self.store.write_tokens({"access_token": "at-2", "expires_in": 3600, "refresh_token": "rt-1"})
        self.clock.advance(3600)  # 过期但可刷新 → 仍算已授权
        self.assertEqual(self.store.status(), "authorized")
        self.store.clear()
        self.assertEqual(self.store.status(), "unauthorized")

    def test_file_permissions_tightened(self):
        if os.name == "nt":  # Windows 无 POSIX 权限位
            self.skipTest("POSIX 文件权限断言仅在类 UNIX 平台执行")
        self.store.write_tokens({"access_token": "at-1"})
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_degrades_to_empty(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.store.status(), "unauthorized")
        self.store.write_tokens({"access_token": "at-1"})  # 仍可覆盖写回
        self.assertEqual(self.store.status(), "authorized")

    def test_token_file_is_gitignored(self):
        """token 不落版本库：机械守住 .gitignore 的 mcp/tokens.json 条目。"""
        root = Path(__file__).resolve().parents[1]
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("mcp/tokens.json", ignored)


class CallbackParseTest(unittest.TestCase):
    """终端贴码解析：完整跳转 URL / 裸 query；缺 code 给可读错误。"""

    def test_parses_full_redirect_url(self):
        result = parse_callback(
            f"{DEFAULT_REDIRECT_URI}?code=abc123&state=st-1&iss={AUTH_SERVER}"
        )
        self.assertEqual(result.code, "abc123")
        self.assertEqual(result.state, "st-1")

    def test_parses_bare_query(self):
        self.assertEqual(parse_callback("code=abc&state=st").code, "abc")

    def test_missing_code_rejected(self):
        with self.assertRaises(McpError) as ctx:
            parse_callback(f"{DEFAULT_REDIRECT_URI}?state=st")
        self.assertIn("code", str(ctx.exception))
        with self.assertRaises(McpError):
            parse_callback("   ")

    def test_missing_state_rejected(self):
        with self.assertRaises(McpError) as ctx:
            parse_callback(f"{DEFAULT_REDIRECT_URI}?code=abc")
        self.assertIn("state", str(ctx.exception))


class DeviceFlowTest(unittest.TestCase):
    """设备码流程（RFC 8628）：注册/申请/轮询语义与失败可读性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"

    def test_full_flow_stores_tokens_and_notifies(self):
        http = FakeOAuthHttp(device_script((400, {"error": "authorization_pending"}), TOKEN_RESPONSE))
        oauth, record, _ = make_oauth(self.path, http)
        oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertEqual(oauth.status(make_server()), "authorized")
        self.assertTrue(any("ABCD-1234" in text for text in record["notices"]))
        self.assertTrue(any("完成" in text for text in record["notices"]))
        # 请求顺序：发现 → 注册 → 设备码 → 轮询 pending → 轮询成功
        self.assertEqual([req[0] for req in http.requests], ["GET", "POST", "POST", "POST", "POST"])
        self.assertEqual(http.requests[3][2]["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")

    def test_stored_client_id_skips_registration(self):
        FileTokenStore(self.path, "acme/remote").write_client_info({"client_id": "cid-9"})
        http = FakeOAuthHttp([(200, dict(METADATA)), DEVICE_CODE_RESPONSE, TOKEN_RESPONSE])
        oauth, _, _ = make_oauth(self.path, http)
        oauth.ensure_authorized(make_server(auth_flow="device_code"))
        # 发现 → 申请设备码 → 轮询换 token，全程复用已存 client_id、不再注册
        self.assertEqual([req[2].get("client_id") for req in http.requests[1:]], ["cid-9", "cid-9"])
        self.assertFalse(any(req[1].endswith("/register") for req in http.requests))

    def test_discovery_failure_is_readable(self):
        http = FakeOAuthHttp([(404, {}), (404, {}), (404, {})])
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("无法发现", str(ctx.exception))

    def test_missing_device_endpoint_rejected(self):
        http = FakeOAuthHttp([(200, {"token_endpoint": f"{AUTH_SERVER}/token"})])
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("设备码", str(ctx.exception))

    def test_access_denied_is_readable(self):
        http = FakeOAuthHttp(device_script((400, {"error": "access_denied"})))
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("拒绝", str(ctx.exception))

    def test_expired_device_code_is_readable(self):
        http = FakeOAuthHttp(device_script((400, {"error": "expired_token"})))
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("过期", str(ctx.exception))

    def test_poll_timeout_gives_up(self):
        # 设备码 3 秒过期、每秒轮询：pending 若干次后按 RFC 到期放弃
        http = FakeOAuthHttp(
            [
                (200, dict(METADATA)),
                (200, {"client_id": "cid-1"}),
                (
                    200,
                    {
                        "device_code": "dc-1",
                        "user_code": "ABCD-1234",
                        "verification_uri": f"{AUTH_SERVER}/activate",
                        "expires_in": 3,
                        "interval": 1,
                    },
                ),
                *[(400, {"error": "authorization_pending"})] * 3,
            ]
        )
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("过期", str(ctx.exception))

    def test_registration_failure_is_readable(self):
        http = FakeOAuthHttp(
            [(200, dict(METADATA)), (400, {"error": "invalid_client"})]
        )
        oauth, _, _ = make_oauth(self.path, http)
        with self.assertRaises(McpError) as ctx:
            oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertIn("注册", str(ctx.exception))


class EnsureAuthorizedTest(unittest.TestCase):
    """流程选择：无声明/已授权不动，授权码懒触发，无 TTY 自动落设备码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"

    def test_non_oauth_server_is_noop(self):
        http = FakeOAuthHttp()
        oauth, _, _ = make_oauth(self.path, http)
        oauth.ensure_authorized(make_server(auth="none", auth_flow="auto"))
        self.assertEqual(http.requests, [])
        self.assertEqual(oauth.status(make_server(auth="none")), "none")

    def test_authorized_server_skips_flow(self):
        FileTokenStore(self.path, "acme/remote").write_tokens({"access_token": "at-1", "expires_in": 3600})
        http = FakeOAuthHttp()
        oauth, _, _ = make_oauth(self.path, http)
        oauth.ensure_authorized(make_server(auth_flow="device_code"))
        self.assertEqual(http.requests, [])

    def test_authorization_code_flow_is_lazy(self):
        http = FakeOAuthHttp()
        oauth, record, _ = make_oauth(self.path, http, interactive=True)
        oauth.ensure_authorized(make_server(auth_flow="auto"))
        self.assertEqual(http.requests, [])  # 授权码 + PKCE 由连接期 SDK 授权流触发
        self.assertEqual(record["wait_calls"], 0)

    def test_auto_flow_without_tty_falls_to_device_code(self):
        http = FakeOAuthHttp(device_script(TOKEN_RESPONSE))
        oauth, _, _ = make_oauth(self.path, http, interactive=False)
        oauth.ensure_authorized(make_server(auth_flow="auto"))
        self.assertEqual(oauth.status(make_server()), "authorized")


class ProviderTest(unittest.TestCase):
    """SDK 授权码 provider：交互缝（打开 URL / 贴码）接到 OAuthClientProvider 上。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"

    def test_provider_wires_handlers_and_storage(self):
        from mcp.client.auth import OAuthClientProvider

        code_text = f"{DEFAULT_REDIRECT_URI}?code=abc&state=st"
        oauth, record, _ = make_oauth(self.path, interactive=True, code_text=code_text)
        provider = oauth.provider(make_server())
        self.assertIsInstance(provider, OAuthClientProvider)
        self.assertIs(provider.context.storage, oauth.store_for(make_server()))
        asyncio.run(provider.context.redirect_handler(f"{AUTH_SERVER}/authorize?state=st"))
        self.assertIn(f"{AUTH_SERVER}/authorize?state=st", record["opened"])
        self.assertTrue(record["notices"])
        result = asyncio.run(provider.context.callback_handler())
        self.assertEqual(result.code, "abc")
        self.assertEqual(record["wait_calls"], 1)


class ManagerOAuthTest(unittest.TestCase):
    """管理器接入：授权闸门、状态暴露、重授权（client factory + 授权协作对象双缝）。"""

    def setUp(self):
        self.sessions = []
        self.requested = []

    def make_manager(self, servers, oauth=None, session=None):
        session = session or FakeMcpSession(tools=[make_mcp_tool()])

        def factory(server):
            self.requested.append(server)
            self.sessions.append(session)
            return session

        return McpManager(servers, client_factory=factory, environ={}, oauth=oauth)

    def test_oauth_gate_runs_before_connect(self):
        oauth = FakeMcpOAuth(status="unauthorized")
        manager = self.make_manager([make_server()], oauth=oauth)
        self.assertEqual(manager.connect(), [])
        self.assertEqual(oauth.ensure_calls, ["acme/remote"])
        item = manager.listing()[0]
        self.assertEqual(item["auth"], "oauth")
        self.assertEqual(item["auth_status"], "unauthorized")

    def test_authorize_failure_rejects_server(self):
        oauth = FakeMcpOAuth(error=McpError("设备码已过期、授权未完成——请重新发起授权"))
        manager = self.make_manager([make_server()], oauth=oauth)
        errors = manager.connect()
        self.assertEqual(self.requested, [])  # 未建连
        self.assertIn("过期", errors[0])
        self.assertEqual(manager.listing()[0]["status"], "error")

    def test_missing_oauth_collaborator_fails_loud(self):
        manager = self.make_manager([make_server()], oauth=None)
        errors = manager.connect()
        self.assertEqual(self.requested, [])
        self.assertIn("OAuth 授权流未配置", errors[0])

    def test_non_oauth_listing_reports_none(self):
        manager = self.make_manager([make_server(auth="none")], oauth=FakeMcpOAuth())
        self.assertEqual(manager.connect(), [])
        item = manager.listing()[0]
        self.assertEqual(item["auth"], "none")
        self.assertEqual(item["auth_status"], "none")

    def test_reauthorize_clears_tokens_and_reconnects(self):
        oauth = FakeMcpOAuth()
        manager = self.make_manager([make_server()], oauth=oauth)
        manager.connect()
        item = manager.reauthorize("acme/remote")
        self.assertEqual(oauth.clear_calls, ["acme/remote"])
        self.assertEqual(len(self.requested), 2)  # 重连一次
        self.assertEqual(item["status"], "connected")

    def test_reauthorize_rejects_non_oauth(self):
        manager = self.make_manager([make_server(auth="none")], oauth=FakeMcpOAuth())
        manager.connect()
        with self.assertRaises(McpError):
            manager.reauthorize("acme/remote")


class ExplainErrorOAuthTest(unittest.TestCase):
    """OAuth 异常链上叶子展平后前置重授权提示（刷新失败降级语义）。"""

    def test_oauth_error_prompts_reauthorization(self):
        from mcp.client.auth import OAuthFlowError

        from app.mcp import explain_error

        leaf = OAuthFlowError("token refresh failed")
        inner = RuntimeError("initialize 失败")
        inner.__cause__ = leaf
        text = explain_error(ExceptionGroup("unhandled errors in a TaskGroup", [inner]))
        self.assertIn("重新授权", text)
        self.assertIn("token refresh failed", text)

    def test_non_oauth_error_keeps_shape(self):
        from app.mcp import explain_error

        self.assertEqual(explain_error(ValueError("连接被拒绝")), "连接被拒绝")
