"""MCP 连接定义与运行时测试：模型层 + client factory 注入缝（零真实网络/进程）。

预约定缝（SPEC-0003 测试决策）：client factory 注入是唯一新缝；
真协议锚点另见 test_mcp_anchor.py。
"""

import sys
import unittest

from app.mcp import (
    McpManager,
    McpServer,
    load_servers,
    resolve_placeholders,
    short_server_name,
    tool_name,
)

from tests.fakes import FakeMcpSession, make_mcp_tool


class ParseServersTest(unittest.TestCase):
    """连接定义解析/校验：非法条目拒绝并给中文错误，互不牵连。"""

    def test_missing_file_gives_empty_config(self):
        servers, errors = load_servers(path=None)
        self.assertEqual(servers, [])
        self.assertEqual(errors, [])

    def test_parses_stdio_entry_with_optional_fields(self):
        servers, errors = load_servers(
            text="""
            {
              "servers": [
                {
                  "name": "io.github.acme/filesystem",
                  "transport": "stdio",
                  "command": ["npx", "-y", "@acme/mcp-fs@1.2.3"],
                  "env": {"ACME_TOKEN": "${MCP_ACME_TOKEN}"},
                  "enabled": true,
                  "source": "catalog:mcp-registry",
                  "max_observation_chars": 8000,
                  "enabled_tools": ["read_file"]
                }
              ]
            }
            """
        )
        self.assertEqual(errors, [])
        entry = servers[0]
        self.assertEqual(entry.name, "io.github.acme/filesystem")
        self.assertEqual(entry.transport, "stdio")
        self.assertEqual(entry.command, ("npx", "-y", "@acme/mcp-fs@1.2.3"))
        self.assertEqual(entry.env, {"ACME_TOKEN": "${MCP_ACME_TOKEN}"})
        self.assertEqual(entry.source, "catalog:mcp-registry")
        self.assertEqual(entry.max_observation_chars, 8000)
        self.assertEqual(entry.enabled_tools, ("read_file",))

    def test_stdio_entry_requires_command_argv_list(self):
        servers, errors = load_servers(
            text='{"servers": [{"name": "a/b", "transport": "stdio"}]}'
        )
        self.assertEqual(servers, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("command", errors[0])

    def test_unknown_transport_rejected(self):
        servers, errors = load_servers(
            text='{"servers": [{"name": "a/b", "transport": "sse", "command": ["x"]}]}'
        )
        self.assertEqual(servers, [])
        self.assertIn("transport", errors[0])

    def test_invalid_entry_does_not_kill_siblings(self):
        servers, errors = load_servers(
            text="""
            {"servers": [
              {"name": "a/good", "transport": "stdio", "command": ["x"]},
              {"transport": "stdio", "command": ["x"]}
            ]}
            """
        )
        self.assertEqual([s.name for s in servers], ["a/good"])
        self.assertEqual(len(errors), 1)

    def test_invalid_json_reported(self):
        servers, errors = load_servers(text="{ not json")
        self.assertEqual(servers, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("JSON", errors[0])


class PlaceholderTest(unittest.TestCase):
    """${VAR} 占位符：值来自 .env/环境变量；未解析即报错并禁用该服务。"""

    def test_resolves_env_values_and_url_template(self):
        resolved, missing = resolve_placeholders(
            env={"TOKEN": "${MY_TOKEN}"},
            url="https://mcp.example/${REGION}/mcp",
            environ={"MY_TOKEN": "s3cret", "REGION": "cn"},
        )
        self.assertEqual(resolved["env"]["TOKEN"], "s3cret")
        self.assertEqual(resolved["url"], "https://mcp.example/cn/mcp")
        self.assertEqual(missing, [])

    def test_unresolved_placeholder_reported(self):
        resolved, missing = resolve_placeholders(
            env={"TOKEN": "${NOPE}"}, url="", environ={}
        )
        self.assertEqual(missing, ["NOPE"])
        self.assertEqual(resolved["env"]["TOKEN"], "${NOPE}")


class NamingTest(unittest.TestCase):
    """命名清洗：reverse-DNS 全名 → 合法 function name 前缀；超长拒绝。"""

    def test_short_server_name_sanitizes_reverse_dns(self):
        self.assertEqual(short_server_name("io.github.acme/filesystem"), "io-github-acme-filesystem")

    def test_tool_name_shape(self):
        self.assertEqual(
            tool_name("io-github-acme-fs", "read_file"), "mcp__io-github-acme-fs__read_file"
        )

    def test_overlong_tool_name_rejected(self):
        from app.mcp import McpNamingError

        with self.assertRaises(McpNamingError):
            tool_name("x" * 60, "a_very_long_tool_name")


class ManagerConnectTest(unittest.TestCase):
    """运行时装配：tools/list 灌注册表、fail loud、闸门字段（client factory 缝）。"""

    def make_server(self, **overrides):
        values = dict(
            name="io.github.acme/filesystem",
            transport="stdio",
            command=(sys.executable, "-m", "fixture_mcp_server"),
        )
        values.update(overrides)
        return McpServer(**values)

    def make_manager(self, servers, session=None, **kw):
        session = session or FakeMcpSession(tools=[make_mcp_tool()])
        self.sessions = []
        self.requested = []

        def factory(server):
            self.requested.append(server)
            self.sessions.append(session)
            return session

        return McpManager(servers, client_factory=factory, environ={}, **kw)

    def test_registers_mcp_tool_and_routes_call(self):
        session = FakeMcpSession(
            tools=[make_mcp_tool()], results={"read_file": "文件内容"}
        )
        manager = self.make_manager([self.make_server()], session=session)
        self.assertEqual(manager.connect(), [])
        tools = manager.build_tools()
        self.assertEqual([t.name for t in tools], ["mcp__io-github-acme-filesystem__read_file"])
        self.assertEqual(tools[0].run({"path": "a.txt"}), "文件内容")
        self.assertEqual(session.calls, [("read_file", {"path": "a.txt"})])

    def test_unresolved_placeholder_disables_server(self):
        manager = self.make_manager(
            [self.make_server(env={"TOKEN": "${NOPE_TOKEN}"})],
        )
        errors = manager.connect()
        self.assertEqual(self.requested, [])  # 未建连
        self.assertIn("NOPE_TOKEN", errors[0])
        self.assertEqual(manager.build_tools(), [])
        self.assertEqual(manager.listing()[0]["status"], "error")

    def test_missing_runner_reported(self):
        manager = self.make_manager([self.make_server(command=("no-such-runner-xyz",))])
        errors = manager.connect()
        self.assertEqual(self.requested, [])
        self.assertIn("no-such-runner-xyz", errors[0])
        self.assertEqual(manager.build_tools(), [])

    def test_connect_failure_gives_load_error(self):
        session = FakeMcpSession(error=RuntimeError("握手超时"))
        manager = self.make_manager([self.make_server()], session=session)
        errors = manager.connect()
        self.assertIn("握手超时", errors[0])
        self.assertEqual(manager.build_tools(), [])
        self.assertEqual(manager.listing()[0]["status"], "error")

    def test_duplicate_short_name_rejected(self):
        session = FakeMcpSession(tools=[make_mcp_tool()])
        manager = self.make_manager(
            [
                self.make_server(name="io.github.acme/filesystem"),
                self.make_server(name="io.github.acme.filesystem"),
            ],
            session=session,
        )
        errors = manager.connect()
        self.assertEqual(len(manager.build_tools()), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("冲突", errors[0])

    def test_reserved_collision_rejects_server(self):
        session = FakeMcpSession(tools=[make_mcp_tool()])
        manager = self.make_manager([self.make_server()], session=session)
        errors = manager.connect(reserved={"mcp__io-github-acme-filesystem__read_file"})
        self.assertEqual(manager.build_tools(), [])
        self.assertIn("冲突", errors[0])

    def test_enabled_tools_whitelist_filters(self):
        session = FakeMcpSession(
            tools=[make_mcp_tool("read_file"), make_mcp_tool("write_file")]
        )
        manager = self.make_manager(
            [self.make_server(enabled_tools=("read_file",))], session=session
        )
        self.assertEqual(manager.connect(), [])
        self.assertEqual(
            [t.name for t in manager.build_tools()],
            ["mcp__io-github-acme-filesystem__read_file"],
        )

    def test_max_tools_cap_fails_loud(self):
        session = FakeMcpSession(
            tools=[make_mcp_tool("read_file"), make_mcp_tool("write_file")]
        )
        manager = self.make_manager([self.make_server()], session=session, max_tools=1)
        errors = manager.connect()
        self.assertIn("上限", errors[0])
        self.assertEqual(manager.build_tools(), [])
        self.assertTrue(session.closed)  # 拒载的会话要回收

    def test_disabled_server_not_connected(self):
        manager = self.make_manager([self.make_server(enabled=False)])
        self.assertEqual(manager.connect(), [])
        self.assertEqual(self.requested, [])
        self.assertEqual(manager.listing()[0]["status"], "disabled")

    def test_call_tool_failure_wrapped_with_context(self):
        session = FakeMcpSession(
            tools=[make_mcp_tool()], results={"read_file": RuntimeError("磁盘错误")}
        )
        manager = self.make_manager([self.make_server()], session=session)
        manager.connect()
        with self.assertRaises(Exception) as ctx:
            manager.build_tools()[0].run({"path": "a"})
        self.assertIn("磁盘错误", str(ctx.exception))

    def test_close_releases_sessions(self):
        session = FakeMcpSession(tools=[make_mcp_tool()])
        manager = self.make_manager([self.make_server()], session=session)
        manager.connect()
        manager.close()
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
