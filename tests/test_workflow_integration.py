"""One fixed plugin surface discovers lazy app workflow contributions without app plugins."""

import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import jsonschema
import pytest
from test_elicitation_integration import frontends
from test_integration import TOOLS, data
from test_integration import stack as stack

from universal_tools.models import atomic_json
from universal_tools.surface import RemoteBackend

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "examples/workflow.schema.json").read_text(encoding="utf-8"))


@asynccontextmanager
async def workflow_app(stack, tmp_path, mode):
    publication = tmp_path / "publication.json"
    atomic_json(publication, {"generation": 1})
    descriptor = stack[0] / "services/workflow-example.json"
    atomic_json(
        descriptor,
        {
            "id": "workflow-example",
            "display_name": "Synthetic workflow contributions",
            "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(ROOT / "examples/workflow_mcp.py"), "--publication", str(publication)],
            },
            "session_mode": mode,
            "allow_writes": False,
            "allowed_write_tools": [],
            "capability_catalog": {"tool": "list_workflow_capabilities"},
            "timeout_seconds": 30,
        },
    )
    async with RemoteBackend(stack[1]) as admin:
        try:
            services = data(await admin.invoke("refresh_services", {}))["services"]
            row = next(row for row in services if row["service_id"] == "workflow-example")
            assert row["status"] == "available", row
            yield publication
        finally:
            descriptor.unlink()
            data(await admin.invoke("refresh_services", {}))


async def read(client, tool, arguments, context=None):
    description = data(
        await client.invoke("describe_tool", {"tool_id": "workflow-example/" + tool})
    )
    request = {
        "tool_id": "workflow-example/" + tool,
        "catalog_revision": description["catalog_revision"],
        "arguments": arguments,
    }
    if context:
        request["context_id"] = context
    return await client.invoke("call_read_tool", request)


async def search(client):
    return data(await client.invoke("search_tools", {"query": "workflow", "kind": "capability"}))[
        "matches"
    ]


@pytest.mark.parametrize("mode", ["transient", "exclusive"])
async def test_lazy_workflow_guidance_and_publication_keep_fixed_frontend_surface(
    stack, tmp_path, mode
):
    async with workflow_app(stack, tmp_path, mode) as publication:
        async with frontends(stack[0], None, None) as (owner, other):
            assert {tool.name for tool in (await owner.session.list_tools()).tools} == TOOLS
            context = None
            if mode == "exclusive":
                context = data(
                    await owner.invoke("acquire_service", {"service_id": "workflow-example"})
                )["context_id"]
                data(
                    await owner.invoke(
                        "refresh_capabilities",
                        {"service_id": "workflow-example", "context_id": context},
                    )
                )
            matches = await search(owner)
            assert len(matches) == 1 and matches[0]["callable"] is False
            capability = data(
                await owner.invoke(
                    "describe_capability",
                    {
                        "service_id": "workflow-example",
                        "capability_id": matches[0]["capability_id"],
                        "capability_revision": matches[0]["capability_revision"],
                    },
                )
            )
            discovery = capability["discovery"]
            arguments = discovery["arguments"]
            guide = data(await read(owner, "read_workflow", arguments, context))
            jsonschema.Draft202012Validator(SCHEMA).validate(guide)
            assert (
                guide["instructions"]["sha256"]
                == hashlib.sha256(guide["instructions"]["text"].encode("utf-8")).hexdigest()
            )
            reference = guide["references"][0]
            result = data(
                await read(
                    owner, reference["read"]["tool"], reference["read"]["arguments"], context
                )
            )
            assert hashlib.sha256(result["content"].encode()).hexdigest() == reference["sha256"]
            bad = {**reference["read"]["arguments"], "expected_sha256": "0" * 64}
            assert (await read(owner, reference["read"]["tool"], bad, context)).isError
            if context:
                assert (await read(other, "read_workflow", arguments, context)).isError
            result = data(await read(owner, "summarize_text", {"text": "café 深 μ"}, context))
            assert result["text"] == "café 深 μ"
            definitions = {
                tool: data(
                    await owner.invoke("describe_tool", {"tool_id": "workflow-example/" + tool})
                )["catalog_revision"]
                for tool in ("read_workflow", "summarize_text")
            }
            atomic_json(publication, {"generation": 2})
            data(await other.invoke("refresh_services", {}))
            if context:
                # Global refresh preserves the owner's prior snapshot; only the
                # owner explicitly refreshes its private capability index.
                assert len(await search(owner)) == 1
                data(
                    await owner.invoke(
                        "refresh_capabilities",
                        {"service_id": "workflow-example", "context_id": context},
                    )
                )
            assert len(await search(owner)) == 2
            assert (await read(owner, "read_workflow", arguments, context)).isError
            for tool, revision in definitions.items():
                assert (
                    data(
                        await owner.invoke("describe_tool", {"tool_id": "workflow-example/" + tool})
                    )["catalog_revision"]
                    == revision
                )
            assert {tool.name for tool in (await owner.session.list_tools()).tools} == TOOLS
            if context:
                data(
                    await owner.invoke(
                        "release_service", {"service_id": "workflow-example", "context_id": context}
                    )
                )
