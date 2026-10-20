"""Modern protocol behavior with actual SDK clients and disposable apps."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    ListToolsResult,
    PaginatedRequestParams,
    Request,
    Tool,
)
from test_elicitation import form
from test_integration import data

from universal_tools.continuations import Continuations, active_route
from universal_tools.framework import ApplicationHost
from universal_tools.identity import client_identity
from universal_tools.models import ServiceManifest, atomic_json
from universal_tools.skill_catalog import SkillCatalog, SkillGet, SkillList, SkillParams
from universal_tools.transport import list_all_tools


def publication(tmp_path, mode="2026-07-28"):
    worker = Path(__file__).with_name("fixtures") / "modern_mcp.py"
    path = tmp_path / "app.json"
    atomic_json(
        path,
        {
            "schema_version": 1,
            "id": "synthetic",
            "display_name": "Synthetic",
            "backends": [
                {
                    "revision": "v1",
                    "artifact": {
                        "path": str(worker),
                        "sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    },
                    "service": {
                        "id": "modern",
                        "display_name": "Modern",
                        "transport": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [str(worker)],
                        },
                        "protocol_mode": mode,
                        "session_mode": "exclusive",
                        "allow_writes": True,
                        "watch_catalog": True,
                        "discovery_cache_ttl_seconds": 10,
                    },
                }
            ],
        },
    )
    return path


@pytest.mark.parametrize("frontend_mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_both_eras_preserve_approvals_without_replaying_writes(
    tmp_path, frontend_mode, action
):
    calls = []

    async def answer(ctx, params):
        calls.append(params.message)
        return ElicitResult(action=action, content={"approved": True})

    async with ApplicationHost(tmp_path / "state", publication(tmp_path)) as host:
        server = host.create_server(frontend_identity="a" * 32)
        async with Client(
            server, mode=frontend_mode, elicitation_callback=answer, cache=None
        ) as client:
            context = data(await client.call_tool("acquire_service", {"service_id": "modern"}))[
                "context_id"
            ]
            description = data(await client.call_tool("describe_tool", {"tool_id": "modern/edit"}))
            args = {
                "tool_id": "modern/edit",
                "catalog_revision": description["catalog_revision"],
                "context_id": context,
                "arguments": {"expected_revision": 0},
                "request_id": "edit-one",
            }
            result = data(await client.call_tool("call_write_tool", args))
            assert result["revision"] == (1 if action == "accept" else 0)
            assert len(calls) == 1
            assert (await client.call_tool("call_write_tool", args)).is_error
            receipt = data(await client.call_tool("get_request_status", {"request_id": "edit-one"}))
            assert receipt["status"] == "completed"
            await client.call_tool(
                "release_service", {"service_id": "modern", "context_id": context}
            )
        assert not server.continuations.operations


async def test_backend_notification_refreshes_catalog_and_publishes_public_resource(tmp_path):
    async with ApplicationHost(tmp_path / "state", publication(tmp_path, "auto")) as host:
        async with Client(
            host.create_server(frontend_identity="a" * 32), mode="2026-07-28", cache=None
        ) as client:
            context = data(await client.call_tool("acquire_service", {"service_id": "modern"}))[
                "context_id"
            ]
            before = data(await client.call_tool("describe_tool", {"tool_id": "modern/publish"}))
            async with client.listen(resource_subscriptions=["broker://catalog"]) as changes:
                await client.call_tool(
                    "call_write_tool",
                    {
                        "tool_id": "modern/publish",
                        "catalog_revision": before["catalog_revision"],
                        "context_id": context,
                        "arguments": {},
                        "request_id": "publish-one",
                    },
                )
                async with asyncio.timeout(10):
                    await anext(changes)
                result = data(await client.call_tool("search_tools", {"query": "added"}))
                assert result["matches"][0]["tool_id"] == "modern/added"
                catalog = await client.read_resource("broker://catalog")
                assert json.loads(catalog.contents[0].text)["generation"] >= 2
            await client.call_tool(
                "release_service", {"service_id": "modern", "context_id": context}
            )


async def test_scoped_cache_copies_results_and_fresh_checks_bypass_hints():
    count = 0

    async def listing(**_):
        nonlocal count
        count += 1
        return ListToolsResult(
            tools=[Tool(name="read", input_schema={"type": "object"})],
            ttl_ms=10000,
            cache_scope="private",
        )

    first = SimpleNamespace(list_tools=listing, cache_ttl=10, tool_cache=None)
    other = SimpleNamespace(list_tools=listing, cache_ttl=10, tool_cache=None)
    (await list_all_tools(first))[0].name = "tampered"
    assert (await list_all_tools(first))[0].name == "read" and count == 1
    await list_all_tools(other)
    await list_all_tools(first, fresh=True)
    assert count == 3


async def test_expired_modern_approval_preserves_uncertain_write_receipt(tmp_path):
    async def answer(*_):
        raise AssertionError("manual round must not invoke a callback")

    async with ApplicationHost(tmp_path / "state", publication(tmp_path)) as host:
        server = host.create_server(frontend_identity="a" * 32)
        server.continuations.timeout = 0.1
        async with Client(
            server, mode="2026-07-28", elicitation_callback=answer, cache=None
        ) as client:
            context = data(await client.call_tool("acquire_service", {"service_id": "modern"}))[
                "context_id"
            ]
            tool = data(await client.call_tool("describe_tool", {"tool_id": "modern/edit"}))
            args = {
                "tool_id": "modern/edit",
                "catalog_revision": tool["catalog_revision"],
                "context_id": context,
                "arguments": {"expected_revision": 0},
                "request_id": "expire-one",
            }
            pending = await client.session.call_tool(
                "call_write_tool", args, allow_input_required=True
            )
            async with asyncio.timeout(10):
                while (
                    data(
                        await client.call_tool("get_request_status", {"request_id": "expire-one"})
                    )["status"]
                    != "uncertain"
                ):
                    await asyncio.sleep(0.05)
            with pytest.raises(MCPError):
                await client.session.call_tool(
                    "call_write_tool",
                    args,
                    request_state=pending.request_state,
                    input_responses={
                        "confirmation": ElicitResult(action="accept", content={"approved": True})
                    },
                    allow_input_required=True,
                )
            assert not server.continuations.operations


async def test_continuation_rejects_other_owner_changed_arguments_reuse_and_expiry():
    manager = Continuations(timeout=1)
    ctx = SimpleNamespace(
        request_id=1,
        meta={},
        session=SimpleNamespace(
            client_capabilities=ClientCapabilities(
                elicitation=ElicitationCapability(form=FormElicitationCapability())
            )
        ),
    )
    invocations = []

    async def invoke(*_):
        invocations.append(1)
        answer = await active_route.get().forward(form())
        assert answer.action == "accept"
        return CallToolResult(content=[])

    args = CallToolRequestParams(name="edit", arguments={"revision": 1})
    token = client_identity.set("owner")
    try:
        pending = await manager.run(ctx, args, invoke)
        assert isinstance(pending, InputRequiredResult)
        retry = args.model_copy(
            update={
                "request_state": pending.request_state,
                "input_responses": {
                    "confirmation": ElicitResult(action="accept", content={"approved": True})
                },
            }
        )
        client_identity.set("other")
        with pytest.raises(MCPError):
            await manager.run(ctx, retry, invoke)
        client_identity.set("owner")
        with pytest.raises(MCPError):
            await manager.run(ctx, retry.model_copy(update={"arguments": {"revision": 2}}), invoke)
        assert isinstance(await manager.run(ctx, retry, invoke), CallToolResult)
        with pytest.raises(MCPError):
            await manager.run(ctx, retry, invoke)
        assert len(invocations) == 1
        pending = await manager.run(ctx, args, invoke)
        await asyncio.sleep(1.1)
        with pytest.raises(MCPError):
            await manager.run(
                ctx, retry.model_copy(update={"request_state": pending.request_state}), invoke
            )
    finally:
        client_identity.reset(token)
        await manager.aclose()


def test_response_limits_do_not_create_a_distinct_native_resource():
    base = {
        "id": "app",
        "display_name": "App",
        "transport": {"type": "streamable-http", "url": "http://127.0.0.1:1234/mcp"},
    }
    first = ServiceManifest.model_validate(base)
    base["transport"]["max_sse_event_bytes"] = 1024
    second = ServiceManifest.model_validate(base)
    assert first.resource_keys == second.resource_keys and first.digest != second.digest


async def test_skill_snapshots_serve_exact_bytes_and_do_not_follow_later_disk_changes(tmp_path):
    package = tmp_path / "skill"
    package.mkdir()
    text = "---\nname: synthetic\ndescription: Synthetic fixture\n---\nRead the reference.\n"
    (package / "SKILL.md").write_bytes(text.encode("utf-8"))
    provider = SkillCatalog([package])
    (package / "SKILL.md").write_text("tampered", encoding="utf-8")
    path = tmp_path / "app.json"
    atomic_json(path, {"schema_version": 1, "id": "app", "display_name": "App"})
    async with ApplicationHost(tmp_path / "state", path) as host:
        async with Client(
            host.create_server(skills=provider), mode="2026-07-28", cache=None
        ) as client:
            listing = await client.session.send_request(
                Request(method="skills/list", params=PaginatedRequestParams()), SkillList
            )
            skill = listing.skills[0]
            entry = await client.session.send_request(
                Request(method="skills/get", params=SkillParams(uri=skill["uri"])), SkillGet
            )
            assert entry.skill == skill
            content = await client.read_resource(skill["uri"])
            assert content.contents[0].text == text
            assert (
                skill["resources"][0]["digest"]
                == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
            )
