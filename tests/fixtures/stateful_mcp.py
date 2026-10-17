"""Disposable stdio app with process-local documents/plans, for protocol tests only."""

import asyncio
import os
import secrets

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

instance = secrets.token_hex(16)
server = FastMCP(
    "Synthetic stateful app",
    instructions=f"Synthetic session guidance: {instance}. Inspect the target before applying.",
    log_level="WARNING",
)
value = ""
plans = {}
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


server.run(transport="stdio")
