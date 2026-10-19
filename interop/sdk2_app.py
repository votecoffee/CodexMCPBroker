"""Synthetic MCP 2.3 server and same-SDK legacy reference; no native backend."""

import argparse
import asyncio
import json
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from interop_payloads import PNG, payload

if version("mcp") != "2.3.0":
    raise RuntimeError("This fixture requires its separate mcp==2.3.0 environment")

from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ImageContent, ToolAnnotations  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--reference", type=Path)
parser.add_argument("--audit", type=Path)
options = parser.parse_args()
server = MCPServer("Synthetic SDK2 app", version="2.3.0", instructions="Synthetic fixture only.")
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
value, revision = "", 0


@server.tool(annotations=READ)
def echo(payload: dict) -> dict:
    return payload


@server.tool(annotations=READ, structured_output=True)
def structured(payload: dict) -> dict[str, Any]:
    return payload


@server.tool(annotations=READ, structured_output=False)
def image() -> ImageContent:
    return ImageContent(
        data=PNG, mime_type="image/png", meta={"fixture": "sdk2", "serial": 2**63 - 1}
    )


@server.tool(annotations=READ)
def reject() -> dict:
    raise ToolError("Synthetic query rejected")


@server.tool(annotations=READ)
def state() -> dict:
    return {"value": value, "revision": revision, "pid": os.getpid()}


@server.tool(annotations=WRITE)
def edit(candidate: str, expected_revision: int, delay: float = 0) -> dict:
    global value, revision
    if expected_revision != revision:
        raise ToolError("Stale synthetic revision")
    value, revision = candidate, revision + 1
    if options.audit:
        with options.audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"revision": revision, "candidate": candidate}) + "\n")
    time.sleep(delay)
    return {"value": value, "revision": revision}


async def reference(path):
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve())])
    async with Client(params, mode="legacy", cache=None, read_timeout_seconds=20) as client:
        tools = (await client.list_tools()).tools
        results = {}
        for name, arguments in (
            ("echo", {"payload": payload()}),
            ("structured", {"payload": payload()}),
            ("image", {}),
            ("reject", {}),
        ):
            result = await client.call_tool(name, arguments)
            # Compare received fields, not defaults synthesized by a newer SDK.
            results[name] = result.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude_unset=True
            )
        path.write_text(
            json.dumps(
                {
                    "initialization": client.session.initialize_result.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "tools": [
                        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for tool in tools
                    ],
                    "results": results,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    if options.reference:
        asyncio.run(reference(options.reference))
    else:
        server.run(transport="stdio")
