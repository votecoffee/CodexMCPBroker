"""Disposable payload and policy fixture; no native applications or file staging."""

import argparse
import base64
import hashlib

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
options = parser.parse_args()
server = FastMCP(
    "Synthetic payload app",
    host="127.0.0.1",
    port=options.port or 8765,
    log_level="WARNING",
    max_request_body_size=10 * 1024 * 1024,
)
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)
commits = 0
reads = 0


@server.tool(annotations=READ)
def roundtrip(payload: dict) -> dict:
    global reads
    reads += 1
    return payload


@server.tool(annotations=WRITE)
def stage_payload(encoded: str, number: int) -> dict:
    global commits
    blob = base64.b64decode(encoded, validate=True)
    commits += 1
    return {
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "number": number,
        "commits": commits,
    }


@server.tool(annotations=WRITE)
def unapproved_edit() -> dict:
    global commits
    commits += 1
    return {"commits": commits}


@server.tool(annotations=READ)
def state() -> dict:
    return {"commits": commits, "reads": reads}


server.run(transport="streamable-http" if options.port else "stdio")
