"""真协议锚点用最小 MCP 服务（mcp 2.x MCPServer，stdio / streamable-http 传输）。"""

import sys

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="echo-fixture")


@mcp.tool()
def echo(text: str) -> str:
    """回显文本。"""
    return f"echo:{text}"


if __name__ == "__main__":
    if len(sys.argv) > 1:  # 带端口参数 = streamable-http 形态（真锚点用）
        mcp.run(transport="streamable-http", port=int(sys.argv[1]))
    else:
        mcp.run()
