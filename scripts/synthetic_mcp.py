"""供浏览器验收使用的一次性本地 MCP fixture，不访问业务数据。

用已安装的项目 Python 运行。该 fixture 只监听回环地址，仅提供一个整数加法工具，
验证后请关闭其终端。它不安装凭据、不执行命令、不打开文件，也不使用网络客户端，
产品不会自动启动它。
"""

import argparse

import uvicorn
from mcp.server.mcpserver import MCPServer

server = MCPServer("NexRAG synthetic acceptance")


@server.tool()
def public_sum(left: int, right: int) -> dict:
    """Add two artificial public integers; no files, database or external network."""
    if abs(left) > 1_000_000 or abs(right) > 1_000_000:
        raise ValueError("Synthetic inputs must be at most one million")
    return {"total": left + right, "source": "synthetic-local-mcp"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    uvicorn.run(
        server.streamable_http_app(json_response=True, stateless_http=True),
        host="127.0.0.1",
        port=args.port,
    )
