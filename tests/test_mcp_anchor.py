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


if __name__ == "__main__":
    unittest.main()
