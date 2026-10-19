"""Synthetic MCP adapter; its disposable HTTP backend owns the job state."""

import argparse
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser()
parser.add_argument("--backend", required=True)
options = parser.parse_args()
server = FastMCP("Synthetic background jobs", log_level="WARNING")
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False)


def request(method, path, body=None):
    with httpx.Client(base_url=options.backend, timeout=40, trust_env=False) as client:
        response = client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()


@server.tool(annotations=READ)
def adapter_info() -> dict:
    return {"pid": os.getpid()}


@server.tool(annotations=WRITE)
def start_job(target: str, expected_revision: int, delay_reply: float = 0) -> dict:
    return request(
        "POST",
        "/jobs",
        {"target": target, "expected_revision": expected_revision, "delay_reply": delay_reply},
    )


@server.tool(annotations=READ)
def get_job(job_id: str) -> dict:
    return request("GET", "/jobs/" + job_id)


@server.tool(annotations=READ)
def list_jobs() -> dict:
    return request("GET", "/jobs")


@server.tool(annotations=WRITE)
def cancel_job(job_id: str) -> dict:
    return request("POST", "/jobs/" + job_id + "/cancel", {})


server.run(transport="stdio")
