"""Independent example apps; no engineering applications or external systems are touched."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from .models import atomic_json

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def create_demo(kind: str, port: int, data: Path | None = None) -> FastMCP:
    app = FastMCP(
        f"Example {kind}",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
    )
    lock = asyncio.Lock()

    @app.tool(annotations=READ)
    async def ping() -> dict:
        """Identify this independent app server."""
        return {"app": kind, "ready": True}

    @app.tool(annotations=READ)
    async def echo(value: str, delay: float = 0) -> dict:
        """Return a value after an optional bounded delay, for concurrent-client checks."""
        if not 0 <= delay <= 5:
            raise ToolError("delay must be between zero and five seconds")
        await asyncio.sleep(delay)
        return {"app": kind, "value": value}

    if kind == "math":

        @app.tool(annotations=READ)
        async def add(a: float, b: float) -> dict:
            """Add two numbers."""
            return {"sum": a + b}

        @app.tool(annotations=READ, structured_output=False)
        async def sample_image() -> CallToolResult:
            """Return a tiny PNG to verify native MCP media forwarding."""
            return CallToolResult(
                content=[
                    ImageContent(
                        type="image",
                        mimeType="image/png",
                        data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aSucAAAAASUVORK5CYII=",
                    )
                ]
            )

        @app.tool(annotations=READ, structured_output=False)
        async def sample_error() -> CallToolResult:
            """Return an intentional app error to verify isError forwarding."""
            return CallToolResult(
                content=[TextContent(type="text", text="intentional example failure")], isError=True
            )
    else:
        if data is None:
            raise ValueError("notes demo requires a data file")

        def read():
            if data.exists():
                return json.loads(data.read_text(encoding="utf-8"))
            return {"revision": 0, "items": []}

        def target(notebook_id):
            if notebook_id != "demo":
                raise ToolError("the example only owns notebook_id='demo'")

        @app.tool(annotations=READ)
        async def list_items(notebook_id: str) -> dict:
            """Read the named example notebook, including its revision."""
            target(notebook_id)
            async with lock:
                return read()

        @app.tool(annotations=WRITE)
        async def add_item(notebook_id: str, expected_revision: int, text: str) -> dict:
            """Append to the named example notebook only if its revision still matches."""
            target(notebook_id)
            async with lock:
                state = read()
                if state["revision"] != expected_revision:
                    raise ToolError("notebook revision changed; read it again before writing")
                state["items"].append(text)
                state["revision"] += 1
                atomic_json(data, state)
                return state

        @app.tool()
        async def unclassified() -> str:
            """An intentionally unclassified operation; broker must never treat it as a read."""
            return "unclassified operation called"

    return app
