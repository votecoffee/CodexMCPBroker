"""Shared app host, independent of the optional universal daemon/plugin."""

import asyncio
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_integration import TOOLS, data, free_port

from universal_tools.cli import initialize, process_options
from universal_tools.framework import ApplicationDefinition, ApplicationHost
from universal_tools.models import BrokerError, atomic_json
from universal_tools.transport import http_session

ROOT = Path(__file__).resolve().parents[1]
publish = runpy.run_path(str(ROOT / "examples/framework_app.py"))["publish"]


def change(path, mutate):
    value = json.loads(path.read_bytes())
    mutate(value)
    atomic_json(path, value)


async def read(client, name="inspect_text", text="café 深 μ", context=None):
    description = data(await client.invoke("describe_tool", {"tool_id": "text-engine/" + name}))
    return await client.invoke(
        "call_read_tool",
        {
            "tool_id": "text-engine/" + name,
            "catalog_revision": description["catalog_revision"],
            "arguments": {"text": text},
            "context_id": context,
        },
    )


async def test_update_defers_owned_context_then_changes_backend_without_frontend_reload(tmp_path):
    publication = publish(tmp_path, 1, free_port())
    async with ApplicationHost(tmp_path, publication) as host:
        owner, other = host.client(), host.client()
        context = data(await owner.invoke("acquire_service", {"service_id": "text-engine"}))[
            "context_id"
        ]
        first = data(await read(owner, context=context))
        assert first["generation"] == 1
        assert (await read(other, context=context)).is_error
        publish(tmp_path, 2, json.loads((tmp_path / "settings.json").read_bytes())["port"])
        deferred = data(await other.invoke("refresh_services", {}))
        assert deferred["backend_publications"][0]["status"] == "deferred"
        assert data(await read(owner, context=context))["worker_pid"] == first["worker_pid"]
        old = data(await owner.invoke("describe_tool", {"tool_id": "text-engine/inspect_text"}))
        data(
            await owner.invoke(
                "release_service", {"service_id": "text-engine", "context_id": context}
            )
        )
        changed = data(await other.invoke("refresh_services", {}))
        assert changed["backend_publications"][0]["status"] == "applied"
        assert (
            await owner.invoke(
                "call_read_tool",
                {
                    "tool_id": "text-engine/inspect_text",
                    "catalog_revision": old["catalog_revision"],
                    "arguments": {"text": "old"},
                },
            )
        ).is_error
        context = data(await owner.invoke("acquire_service", {"service_id": "text-engine"}))[
            "context_id"
        ]
        second = data(await read(owner, context=context))
        assert second["generation"] == 2 and second["worker_pid"] != first["worker_pid"]
        assert data(await read(owner, "count_words", "one two", context))["words"] == 2
        data(
            await owner.invoke(
                "release_service", {"service_id": "text-engine", "context_id": context}
            )
        )
        assert data(await owner.invoke("broker_status", {}))["application_id"] == "example-text-app"
        change(publication, lambda v: v.update(backends=[]))
        assert (
            data(await owner.invoke("refresh_services", {}))["backend_publications"][0]["status"]
            == "applied"
        )
        assert data(await owner.invoke("search_tools", {"query": ""}))["total"] == 0


@pytest.mark.parametrize("failure", ["hash", "same-revision", "startup"])
async def test_invalid_candidates_keep_previous_backend_configuration(tmp_path, failure):
    publication = publish(tmp_path, 1, free_port())
    async with ApplicationHost(tmp_path, publication) as host:
        client = host.client()
        before = (host.root / "active.json").read_bytes()
        if failure != "same-revision":
            change(publication, lambda v: v["backends"][0].update(revision="rejected"))
        if failure == "hash":
            change(publication, lambda v: v["backends"][0]["artifact"].update(sha256="0" * 64))
        elif failure == "same-revision":
            change(
                publication, lambda v: v["backends"][0]["service"].update(display_name="Changed")
            )
        else:
            change(
                publication,
                lambda v: v["backends"][0]["service"]["transport"]["args"].append(
                    "--invalid-option"
                ),
            )
        result = data(await client.invoke("refresh_services", {}))
        assert result["backend_publications"][0]["status"] == "rejected"
        assert (host.root / "active.json").read_bytes() == before
        context = data(await client.invoke("acquire_service", {"service_id": "text-engine"}))[
            "context_id"
        ]
        assert data(await read(client, context=context))["generation"] == 1
        data(
            await client.invoke(
                "release_service", {"service_id": "text-engine", "context_id": context}
            )
        )


async def test_uncertain_write_blocks_reload_until_explicit_app_reconciliation(tmp_path):
    publication = publish(tmp_path, 1, free_port())
    async with ApplicationHost(tmp_path, publication) as host:
        host._engine.journal.claim("fixture-write-1", "fixture-fingerprint", "text-engine/write")
        host._engine.journal.finish("fixture-write-1", "uncertain")
        publish(tmp_path, 2, json.loads((tmp_path / "settings.json").read_bytes())["port"])
        client = host.client()
        result = data(await client.invoke("refresh_services", {}))
        assert result["backend_publications"][0]["reason"] == "app_write_reconciliation_required"
        row = host._engine.journal.status("fixture-write-1")
        with pytest.raises(BrokerError, match="receipt changed"):
            await host.acknowledge_reload_outcome("fixture-write-1", expected_updated_at="old")
        await host.acknowledge_reload_outcome(
            "fixture-write-1", expected_updated_at=row["updated_at"]
        )
        assert (
            data(await client.invoke("refresh_services", {}))["backend_publications"][0]["status"]
            == "applied"
        )
        assert host._engine.journal.status("fixture-write-1")["status"] == "uncertain"
        with pytest.raises(BrokerError, match="already claimed"):
            host._engine.journal.claim(
                "fixture-write-1", "fixture-fingerprint", "text-engine/write"
            )


async def test_original_app_write_policy_and_duplicate_receipts_survive_framework_routing(tmp_path):
    publication = publish(tmp_path, 1, free_port())
    worker = ROOT / "tests/fixtures/stateful_mcp.py"

    def stateful(value):
        backend = value["backends"][0]
        backend["artifact"] = {
            "path": str(worker),
            "sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        }
        backend["service"]["transport"]["args"] = [str(worker)]
        backend["service"].update(allow_writes=True, allowed_write_tools=["prepare", "apply"])

    change(publication, stateful)
    async with ApplicationHost(tmp_path, publication) as host:
        client = host.client()
        context = data(await client.invoke("acquire_service", {"service_id": "text-engine"}))[
            "context_id"
        ]

        async def write(name, arguments, request_id):
            description = data(
                await client.invoke("describe_tool", {"tool_id": "text-engine/" + name})
            )
            return await client.invoke(
                "call_write_tool",
                {
                    "tool_id": "text-engine/" + name,
                    "catalog_revision": description["catalog_revision"],
                    "arguments": arguments,
                    "request_id": request_id,
                    "context_id": context,
                },
            )

        blocked = await write(
            "slow_edit", {"candidate": "not allowed", "delay": 0}, "framework-blocked-1"
        )
        assert blocked.is_error
        plan = data(
            await write("prepare", {"candidate": "reviewed fixture"}, "framework-prepare-1")
        )
        applied = data(await write("apply", {"plan_id": plan["plan_id"]}, "framework-apply-1"))
        assert applied["value"] == "reviewed fixture"
        assert (await write("apply", {"plan_id": plan["plan_id"]}, "framework-apply-1")).is_error
        assert (
            data(await client.invoke("get_request_status", {"request_id": "framework-apply-1"}))[
                "status"
            ]
            == "completed"
        )
        data(
            await client.invoke(
                "release_service", {"service_id": "text-engine", "context_id": context}
            )
        )


async def test_cancelled_candidate_publication_never_leaves_a_dispatchable_staged_route(
    tmp_path, monkeypatch
):
    publication = publish(tmp_path, 1, free_port())
    async with ApplicationHost(tmp_path, publication) as host:
        before = (host.root / "active.json").read_bytes()
        old_registry = host._engine.registry
        port = json.loads((tmp_path / "settings.json").read_bytes())["port"]
        publish(tmp_path, 2, port)
        real_refresh = host._engine.refresh
        entered = asyncio.Event()

        async def pause_after_candidate_discovery():
            result = await real_refresh()
            entered.set()
            await asyncio.Event().wait()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(host._engine, "refresh", pause_after_candidate_discovery)
            task = asyncio.create_task(host.refresh())
            try:
                async with asyncio.timeout(30):
                    await entered.wait()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert (host.root / "active.json").read_bytes() == before
        assert host._engine.registry == old_registry
        client = host.client()
        assert (
            await client.invoke("describe_tool", {"tool_id": "text-engine/count_words"})
        ).is_error
        assert (
            data(await client.invoke("refresh_services", {}))["backend_publications"][0]["status"]
            == "applied"
        )


async def test_lost_context_blocks_backend_replacement_across_host_restart(tmp_path):
    port = free_port()
    publication = publish(tmp_path, 1, port)
    async with ApplicationHost(tmp_path, publication) as host:
        context = data(
            await host.client().invoke("acquire_service", {"service_id": "text-engine"})
        )["context_id"]
        host._engine._leases["text-engine"].lost = True
        host._engine.ownership.lose(context, "fixture_lost")
    publish(tmp_path, 2, port)
    async with ApplicationHost(tmp_path, publication) as host:
        status = data(await host.client().invoke("broker_status", {}))
        assert status["services"][0]["status"] == "recovery_required"
        assert status["backend_publications"][0]["reason"] == "operator_recovery_required"
        assert host._selected["text-engine"].revision == "text-1"


async def test_framework_restart_keeps_publication_and_rejects_foreign_state(tmp_path):
    publication = publish(tmp_path, 1, free_port())
    async with ApplicationHost(tmp_path, publication):
        pass
    change(publication, lambda v: v.update(instructions="Updated app startup guidance"))
    async with ApplicationHost(tmp_path, publication) as host:
        assert len(host._selected) == 1
    change(publication, lambda v: v.update(id="other-app"))
    with pytest.raises(BrokerError, match="different application"):
        async with ApplicationHost(tmp_path, publication):
            pytest.fail("must not start a foreign app")


async def test_shared_reload_requires_explicit_app_quiescence_and_keeps_frontend(tmp_path):
    port = free_port()
    publication = publish(tmp_path, 1, port)
    change(publication, lambda v: v["backends"][0]["service"].update(session_mode="shared"))
    async with ApplicationHost(tmp_path, publication) as host:
        client = host.client()
        first = data(await read(client))
        publish(tmp_path, 2, port)
        change(publication, lambda v: v["backends"][0]["service"].update(session_mode="shared"))
        assert (
            data(await client.invoke("refresh_services", {}))["backend_publications"][0]["status"]
            == "deferred"
        )
        with pytest.raises(BrokerError, match="quiescence"):
            await host.retire_shared_backend("text-engine", expected_revision="text-1")
        with pytest.raises(BrokerError, match="publication changed"):
            await host.retire_shared_backend(
                "text-engine", expected_revision="old", app_quiescent=True
            )
        assert (
            await host.retire_shared_backend(
                "text-engine", expected_revision="text-1", app_quiescent=True
            )
        )["closed"]
        assert (
            data(await client.invoke("refresh_services", {}))["backend_publications"][0]["status"]
            == "applied"
        )
        second = data(await read(client))
        assert second["generation"] == 2 and second["worker_pid"] != first["worker_pid"]


async def test_changed_artifact_cannot_be_dispatched_or_probed(tmp_path):
    publication = publish(tmp_path, 1, free_port())
    original = ROOT / "examples/framework_worker.py"
    copy = tmp_path / "worker.py"
    copy.write_bytes(original.read_bytes())
    change(
        publication,
        lambda v: (
            v["backends"][0]["artifact"].update(path=str(copy)),
            v["backends"][0]["service"]["transport"]["args"].__setitem__(0, str(copy)),
        ),
    )
    async with ApplicationHost(tmp_path, publication) as host:
        copy.write_bytes(copy.read_bytes() + b"\n# changed after publication\n")
        client = host.client()
        result = await client.invoke("acquire_service", {"service_id": "text-engine"})
        assert result.is_error and "artifact changed" in result.content[0].text
        assert (await client.invoke("refresh_services", {})).is_error
        assert not host._engine._leases


async def test_unrelated_backend_dispatch_is_not_locked_by_pending_reload(tmp_path):
    publication = publish(tmp_path, 1, free_port())

    def second(value):
        backend = json.loads(json.dumps(value["backends"][0]))
        backend["service"]["id"] = "independent"
        backend["service"]["transport"]["args"] += ["--unused"]
        # Use a different harmless endpoint identity without creating a conflicting alias.
        backend["service"]["enabled"] = False
        value["backends"].append(backend)

    change(publication, second)
    async with ApplicationHost(tmp_path, publication) as host:
        client = host.client()
        async with host.gate("text-engine"):
            change(publication, lambda v: v["backends"][0].update(revision="text-2"))
            pending = asyncio.create_task(host.refresh())
            try:
                await asyncio.sleep(0)
                async with asyncio.timeout(1):
                    result = await client.invoke(
                        "service_session_status", {"service_id": "independent"}
                    )
                    assert data(result)["state"] == "unowned"  # answered during reload wait
                assert not pending.done()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_same_native_mcp_sessions_discover_new_backend_tools_without_reinitialize(tmp_path):
    port = free_port()
    publication = publish(tmp_path, 1, port)
    settings = initialize(tmp_path, port)
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "universal_tools",
            "--state",
            str(tmp_path),
            "app-serve",
            "--publication",
            str(publication),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_options(),
    )
    try:
        async with httpx.AsyncClient(trust_env=False) as health:
            for _ in range(300):
                try:
                    response = await health.get(
                        settings.url + "/health",
                        headers={"Authorization": "Bearer " + settings.token},
                    )
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert child.poll() is None
                await asyncio.sleep(0.1)
            else:
                pytest.fail("framework reference host did not start")
        # Native clients omit the broker-specific identity header. Ownership binds to MCP sessions.
        async with http_session(settings.url + "/mcp", settings.token) as first:
            async with http_session(settings.url + "/mcp", settings.token) as second:
                definitions = [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in (await first.list_tools()).tools
                ]
                assert {tool["name"] for tool in definitions} == TOOLS
                assert first.initialization["serverInfo"]["name"] == "Example Text App"
                context = data(
                    await first.call_tool("acquire_service", {"service_id": "text-engine"})
                )["context_id"]
                assert (
                    await second.call_tool(
                        "release_service", {"service_id": "text-engine", "context_id": context}
                    )
                ).is_error
                data(
                    await first.call_tool(
                        "release_service", {"service_id": "text-engine", "context_id": context}
                    )
                )
                publish(tmp_path, 2, port)
                data(await second.call_tool("refresh_services", {}))
                matches = data(await first.call_tool("search_tools", {"query": "count_words"}))[
                    "matches"
                ]
                assert matches[0]["tool_id"] == "text-engine/count_words"
                assert [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in (await first.list_tools()).tools
                ] == definitions
    finally:
        if child.poll() is None:
            async with httpx.AsyncClient(trust_env=False) as client:
                try:
                    await client.post(
                        settings.url + "/shutdown",
                        headers={"Authorization": "Bearer " + settings.token},
                    )
                except httpx.HTTPError:
                    pass
            try:
                await asyncio.to_thread(child.wait, 15)
            except subprocess.TimeoutExpired:
                child.terminate()
                await asyncio.to_thread(child.wait, 10)


def test_application_schema_matches_export_and_requires_pinned_stdio_entrypoint():
    assert (
        json.loads((ROOT / "examples/application.schema.json").read_bytes())
        == ApplicationDefinition.model_json_schema()
    )
    with pytest.raises(ValueError):
        ApplicationDefinition.model_validate(
            {"schema_version": True, "id": "app", "display_name": "App"}
        )
