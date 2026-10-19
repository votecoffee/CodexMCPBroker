"""Bounded synthetic bulk receiver. No native tools, files, targets or services."""

import argparse
import hashlib

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
options = parser.parse_args()
server = MCPServer(
    "Synthetic bulk receiver",
    log_level="WARNING",
)
server_http_options = {
    "host": "127.0.0.1",
    "port": options.port or 8765,
    "max_request_body_size": 128 * 1024 * 1024,
}
commits = 0


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def stage_candidate(candidate_xml: str) -> dict:
    global commits
    encoded = candidate_xml.encode("utf-8")
    commits += 1
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
        "utf16_units": len(candidate_xml.encode("utf-16-le")) // 2,
        "commits": commits,
    }


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def state() -> dict:
    return {"commits": commits}


server.run(
    transport="streamable-http" if options.port else "stdio",
    **(server_http_options if options.port else {}),
)
