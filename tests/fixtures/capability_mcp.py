"""Disposable inner catalog and revision checks; no CAD, device or project access."""

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--audit", type=Path)
options = parser.parse_args()
server = FastMCP(
    "Synthetic capability app",
    host="127.0.0.1",
    port=options.port or 8765,
    log_level="WARNING",
    stateless_http=False,
)
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)
instance = secrets.token_hex(16)
generation = 1
commits = catalog_calls = 0
invalid = confirmation = False
delay = 0.0


def revision(ctx):
    if not hasattr(ctx.session, "fixture_scope"):
        ctx.session.fixture_scope = secrets.token_hex(16)
    return f"opaque-native:{ctx.session.fixture_scope}:{generation}"


@server.tool(annotations=READ)
async def catalog(ctx: Context) -> dict:
    global catalog_calls
    catalog_calls += 1
    if options.audit:
        with options.audit.open("a", encoding="utf-8") as audit:
            audit.write(json.dumps({"catalog_call": catalog_calls, "delay": delay}) + "\n")
    await asyncio.sleep(delay)
    if confirmation:
        await ctx.session.elicit_form(
            "Invalid metadata-only confirmation",
            {"type": "object"},
            related_request_id=ctx.request_id,
        )
    names = ["holes.drill"] + (["fasteners.bolt"] if generation > 1 else [])
    return {
        "schema_version": 2 if invalid else 1,
        "catalog_revision": revision(ctx),
        "capabilities": [
            {
                "id": name,
                "description": "Counterbore machining"
                if name == "holes.drill"
                else "Insert a bolt",
                "tags": ["fixture"],
                "discovery": {"tool": "describe_feature", "arguments": {"capability": name}},
            }
            for name in names
        ],
    }


@server.tool(annotations=READ)
def describe_feature(capability: str, ctx: Context) -> dict:
    return {
        "capability": capability,
        "catalog_revision": revision(ctx),
        "input_schema": {
            "type": "object",
            "properties": {"diameter": {"type": "number"}},
            "required": ["diameter"],
        },
    }


@server.tool(annotations=WRITE)
def run_feature(
    document: str, capability: str, arguments: dict, catalog_revision: str, ctx: Context
) -> dict:
    global commits
    if document != "disposable-document" or catalog_revision != revision(ctx):
        raise ValueError("stale native revision or wrong fixture target")
    commits += 1
    return {
        "commits": commits,
        "instance": instance,
        "capability": capability,
        "arguments": arguments,
    }


@server.tool(annotations=WRITE)
def reload_fixture(
    make_invalid: bool = False, wait: float = 0, require_confirmation: bool = False
) -> dict:
    global generation, invalid, delay, confirmation
    generation += 1
    invalid, delay, confirmation = make_invalid, wait, require_confirmation
    return {"generation": generation}


@server.tool(annotations=READ)
def state() -> dict:
    return {
        "instance": instance,
        "generation": generation,
        "commits": commits,
        "catalog_calls": catalog_calls,
    }


server.run(transport="streamable-http" if options.port else "stdio")
