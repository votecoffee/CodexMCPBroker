"""Disposable modern protocol app. No native engines or external targets."""

import sys
from typing import Annotated

if "--sdk-root" in sys.argv:
    sys.path.insert(0, sys.argv[sys.argv.index("--sdk-root") + 1])
    from importlib.metadata import version

    if version("mcp") != "2.2.0":
        raise RuntimeError("This compatibility fixture requires SDK 2.2.0")

from mcp.server import CacheHint, MCPServer  # noqa: E402
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, Resolve  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import BaseModel  # noqa: E402

server = MCPServer(
    "Synthetic modern app",
    version="1",
    cache_hints={"tools/list": CacheHint(ttl_ms=10000, scope="private")},
)
revision = 0
dispatches = 0


class Review(BaseModel):
    approved: bool


async def ask() -> Elicit[Review]:
    return Elicit("Approve this synthetic revision change?", Review)


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def state() -> dict:
    return {"revision": revision, "dispatches": dispatches}


@server.tool(annotations=ToolAnnotations(read_only_hint=False))
async def edit(
    expected_revision: int, review: Annotated[ElicitationResult[Review], Resolve(ask)]
) -> dict:
    global revision, dispatches
    dispatches += 1
    if not isinstance(review, AcceptedElicitation) or not review.data.approved:
        return {"status": "declined", "revision": revision}
    if expected_revision != revision:
        return {"status": "stale", "revision": revision}
    revision += 1
    return {"status": "completed", "revision": revision}


@server.tool(annotations=ToolAnnotations(read_only_hint=False))
async def publish(ctx: Context) -> dict:
    async def added() -> dict:
        return {"available": True}

    server.add_tool(added, annotations=ToolAnnotations(read_only_hint=True))
    await ctx.notify_tools_changed()
    return {"published": True}


if __name__ == "__main__":
    server.run()
