"""真协议锚点用最小 MCP 服务（mcp 2.x MCPServer，stdio 传输）。"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="echo-fixture")


@mcp.tool()
def echo(text: str) -> str:
    """回显文本。"""
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
