"""Disposable progress app, separate from the stateful fixture's startup budget."""

import argparse
import asyncio
import secrets

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--label", default="synthetic")
parser.add_argument("--port", type=int)
options = parser.parse_args()
instance = secrets.token_hex(16)
server = FastMCP(
    "Synthetic progress app",
    log_level="WARNING",
    host="127.0.0.1",
    port=options.port or 8765,
    stateless_http=False,
    json_response=False,
)
commits = 0
previous_progress_token = None


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def progress_work(ctx: Context, label: str, delay: float = 0.15, steps: int = 3) -> dict:
    global previous_progress_token
    token = ctx.request_context.meta.progressToken if ctx.request_context.meta else None
    if token is not None:
        for stray in ("unrequested-token", previous_progress_token):
            if stray is not None:
                await ctx.session.send_progress_notification(
                    stray, 9000, message="stray or previous call", related_request_id=ctx.request_id
                )
    previous_progress_token = token
    for index in range(steps):
        await ctx.report_progress(index + 1, steps, label)
        await ctx.report_progress(index + 1, steps, "duplicate")
        await ctx.report_progress(index, steps, "backwards")
        await asyncio.sleep(delay)
    return {"label": label, "progress_requested": token is not None, "instance": instance}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
async def progress_edit(
    ctx: Context,
    candidate: str,
    delay: float = 0.15,
    steps: int = 3,
    fail: bool = False,
    confirmation: bool = False,
) -> dict:
    global commits
    await ctx.report_progress(0, steps + 1, "before synthetic edit")
    if confirmation:
        result = await ctx.session.elicit_form(
            f"Confirm synthetic candidate: {candidate}",
            {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
                "additionalProperties": False,
            },
            related_request_id=ctx.request_id,
        )
        if (
            result.action != "accept"
            or not result.content
            or result.content.get("approved") is not True
        ):
            return {"committed": False, "action": result.action, "commits": commits}
    commits += 1
    result = await progress_work(ctx, candidate, delay, steps)
    if fail:
        raise ValueError("synthetic error after edit")
    return {**result, "commits": commits}


server.run(transport="streamable-http" if options.port else "stdio")
