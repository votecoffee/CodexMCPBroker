"""Disposable stdio app with process-local documents/plans, for protocol tests only."""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--audit", type=Path)
parser.add_argument("--label", default="synthetic")
parser.add_argument("--port", type=int)
options = parser.parse_args()
instance = secrets.token_hex(16)
server = MCPServer(
    "Synthetic stateful app",
    instructions=f"Synthetic session guidance: {instance}. Inspect the target before applying.",
    log_level="WARNING",
)
server_http_options = {
    "host": "127.0.0.1",
    "port": options.port or 8765,
    "stateless_http": False,
    "json_response": False,
}
value = ""
plans = {}
commits = 0
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)


@server.tool(annotations=READ)
def inspect() -> dict:
    return {"instance": instance, "value": value}


@server.tool(annotations=WRITE)
def prepare(candidate: str) -> dict:
    plan = secrets.token_hex(16)
    plans[plan] = candidate
    return {"plan_id": plan, "instance": instance}


@server.tool(annotations=WRITE)
def apply(plan_id: str) -> dict:
    global value
    value = plans.pop(plan_id)
    return inspect()


@server.tool(annotations=WRITE)
async def slow_edit(candidate: str, delay: float) -> dict:
    global value
    value = candidate
    await asyncio.sleep(delay)
    return inspect()


@server.tool(annotations=WRITE)
def disconnect() -> dict:
    os._exit(23)


@server.tool(annotations=READ)
def confirmation_state(ctx: Context) -> dict:
    capabilities = ctx.session.client_params.capabilities.elicitation
    return {
        "commits": commits,
        "value": value,
        "form": capabilities is not None and capabilities.form is not None,
        "url": capabilities is not None and capabilities.url is not None,
    }


@server.tool(annotations=WRITE)
async def confirm(candidate: str, ctx: Context, repeats: int = 1) -> dict:
    global value, commits
    for _ in range(repeats):
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
    value = candidate
    commits += 1
    if options.audit is not None:
        with options.audit.open("a", encoding="utf-8") as audit:
            audit.write(json.dumps({"candidate": candidate, "commits": commits}) + "\n")
    return {"committed": True, "action": "accept", "commits": commits}


@server.tool(annotations=WRITE)
async def ask_url(ctx: Context) -> dict:
    await ctx.session.elicit_url(
        "Unsupported synthetic URL flow",
        "https://example.invalid/confirm",
        "synthetic-id",
        related_request_id=ctx.request_id,
    )
    return {"unexpected": True}


@server.tool(annotations=READ)
async def choose_label(ctx: Context) -> dict:
    result = await ctx.session.elicit_form(
        "Select a synthetic target label",
        {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]},
        related_request_id=ctx.request_id,
    )
    return {"action": result.action, "label": (result.content or {}).get("label")}


@server.tool(annotations=READ)
def transport_headers(ctx: Context) -> dict:
    request = ctx.request_context.request
    return {
        "frontend_identity": request.headers.get("x-universal-tools-client") if request else None
    }


server.run(
    transport="streamable-http" if options.port else "stdio",
    **(server_http_options if options.port else {}),
)
