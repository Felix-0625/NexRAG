"""Disposable loopback MCP fixture for browser acceptance, with no business access.

Run with project Python and stop it after validation. It exposes integer addition only,
installs no credentials, executes no commands, opens no files, uses no network client,
and is never started automatically by the product.
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
