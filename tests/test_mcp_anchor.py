"""真协议锚点：官方 mcp SDK stdio client ↔ 最小 MCPServer（当前解释器直起）。

只锚「SDK 用法没接错」这一个风险点（SPEC-0003 测试决策）；
其余行为断言走 fake client 缝（test_mcp.py / test_mcp_api.py）。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

from app.mcp import McpManager, McpServer, sdk_client_factory

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(MCP_AVAILABLE, "需要官方 mcp SDK")
class StdioAnchorTest(unittest.TestCase):
    def test_initialize_list_and_call_over_real_stdio(self):
        server = McpServer(
            name="test/echo",
            transport="stdio",
            command=(sys.executable, str(FIXTURE)),
        )
        manager = McpManager([server], client_factory=sdk_client_factory)
        try:
            self.assertEqual(manager.connect(), [])
            tools = manager.build_tools()
            self.assertEqual([t.name for t in tools], ["mcp__test-echo__echo"])
            self.assertEqual(tools[0].run({"text": "你好"}), "echo:你好")
        finally:
            manager.close()


@unittest.skipUnless(MCP_AVAILABLE, "需要官方 mcp SDK")
class StreamableHttpAnchorTest(unittest.TestCase):
    """远程传输真锚点：真实 HTTP 传输层也被执行过（防依赖漂移教训复发）。"""

    def test_initialize_list_and_call_over_real_http(self):
        import socket
        import subprocess
        import time

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen([sys.executable, str(FIXTURE), str(port)])
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.2)
            server = McpServer(
                name="test/remote-echo",
                transport="streamable-http",
                url=f"http://127.0.0.1:{port}/mcp",
            )
            manager = McpManager([server], client_factory=sdk_client_factory)
            try:
                self.assertEqual(manager.connect(), [])
                tools = manager.build_tools()
                self.assertEqual(
                    [t.name for t in tools], ["mcp__test-remote-echo__echo"]
                )
                self.assertEqual(tools[0].run({"text": "你好"}), "echo:你好")
            finally:
                manager.close()
        finally:
            proc.terminate()


if __name__ == "__main__":
    unittest.main()
