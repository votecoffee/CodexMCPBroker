"""Modern discovery and resolver forms against the supported SDK 2.2 floor."""

from mcp import Client
from mcp.types import ElicitResult
from test_integration import data
from test_modern import publication

from universal_tools.framework import ApplicationHost
from universal_tools.models import atomic_json


async def test_sdk22_modern_backend_preserves_one_reviewed_revision_change(tmp_path, sdk22_root):
    import json

    path = publication(tmp_path)
    value = json.loads(path.read_bytes())
    value["backends"][0]["service"]["transport"]["args"] += ["--sdk-root", str(sdk22_root)]
    atomic_json(path, value)

    async def answer(*_):
        return ElicitResult(action="accept", content={"approved": True})

    async with ApplicationHost(tmp_path / "state", path) as host:
        async with Client(
            host.create_server(frontend_identity="a" * 32),
            mode="2026-07-28",
            elicitation_callback=answer,
            cache=None,
        ) as client:
            context = data(await client.call_tool("acquire_service", {"service_id": "modern"}))[
                "context_id"
            ]
            tool = data(await client.call_tool("describe_tool", {"tool_id": "modern/edit"}))
            result = data(
                await client.call_tool(
                    "call_write_tool",
                    {
                        "tool_id": "modern/edit",
                        "catalog_revision": tool["catalog_revision"],
                        "context_id": context,
                        "arguments": {"expected_revision": 0},
                        "request_id": "sdk22-modern",
                    },
                )
            )
            assert result == {"status": "completed", "revision": 1}
            await client.call_tool(
                "release_service", {"service_id": "modern", "context_id": context}
            )
