"""Explicit optional profile: SDK 2.3 broker/frontends against an SDK 2.3 server."""

import asyncio
import base64
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from interop_payloads import PNG, payload
from test_elicitation_integration import frontends
from test_integration import BROKER_TEST_OPERATIONS, data
from test_integration import stack as stack

from universal_tools.cli import process_options
from universal_tools.models import atomic_json
from universal_tools.ownership import OwnershipJournal
from universal_tools.surface import RemoteBackend


@pytest.fixture(scope="module")
def reference(sdk2_python, tmp_path_factory):
    path = tmp_path_factory.mktemp("sdk2-reference") / "reference.json"
    result = subprocess.run(
        [str(sdk2_python), str(Path(__file__).with_name("sdk2_app.py")), "--reference", str(path)],
        capture_output=True,
        timeout=45,
        **process_options(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return json.loads(path.read_text(encoding="utf-8"))


@asynccontextmanager
async def sdk2_app(stack, python, *, timeout=20, audit=None, sdk22_root=None):
    path = stack[0] / "services/sdk2.json"
    args = [str(Path(__file__).with_name("sdk2_app.py"))]
    if sdk22_root is not None:
        args += ["--sdk-root", str(sdk22_root)]
    if audit is not None:
        args += ["--audit", str(audit)]
    atomic_json(
        path,
        dict(
            id="sdk2",
            display_name="Synthetic SDK2 app",
            transport=dict(type="stdio", command=str(python), args=args),
            session_mode="exclusive",
            allow_writes=True,
            allowed_write_tools=["edit"],
            max_argument_bytes=2 * 1024 * 1024,
            timeout_seconds=timeout,
        ),
    )
    async with RemoteBackend(stack[1]) as admin:
        try:
            services = data(await admin.invoke("refresh_services", {}))["services"]
            status = next(s for s in services if s["service_id"] == "sdk2")
            assert status["status"] == "available", status
            yield
        finally:
            path.unlink()
            data(await admin.invoke("refresh_services", {}))
            reservation = OwnershipJournal(stack[0] / "ownership.sqlite3").get("sdk2")
            if reservation:
                # Reconcile only this disposable fake process, under the stopped
                # fixture broker's OS lock. Never clear a normal app reservation.
                await asyncio.to_thread(
                    BROKER_TEST_OPERATIONS[stack[0]]["recover"],
                    "sdk2",
                    reservation["context_id"],
                )


async def acquire(client):
    return data(await client.invoke("acquire_service", {"service_id": "sdk2"}))["context_id"]


async def call(client, context, name, arguments=None, *, request_id=None):
    description = data(await client.invoke("describe_tool", {"tool_id": "sdk2/" + name}))
    args = dict(
        tool_id="sdk2/" + name,
        catalog_revision=description["catalog_revision"],
        arguments=arguments or {},
        context_id=context,
    )
    if request_id is not None:
        args["request_id"] = request_id
    return await client.invoke("call_write_tool" if request_id else "call_read_tool", args)


async def test_initialize_and_tool_definitions_match_the_sdk2_legacy_reference(
    stack, sdk2_python, reference
):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None) as (owner,):
        info = data(await owner.invoke("describe_service", {"service_id": "sdk2"}))
        assert info["initialization"] == reference["initialization"]
        assert info["initialization"]["protocolVersion"] == "2025-11-25"
        for tool in reference["tools"]:
            description = data(
                await owner.invoke("describe_tool", {"tool_id": "sdk2/" + tool["name"]})
            )
            assert description["definition"] == tool
            assert description["classification"] == (
                "write_or_unknown" if tool["name"] == "edit" else "read"
            )


@pytest.mark.parametrize("name", ["echo", "structured"])
async def test_full_results_and_large_payloads_match_the_same_sdk_reference(
    stack, sdk2_python, reference, name
):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None) as (owner,):
        result = await call(owner, await acquire(owner), name, {"payload": payload()})
        assert (
            result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
            == reference["results"][name]
        )
        assert data(result) == payload()


async def test_image_bytes_and_mime_type_survive_both_broker_hops(stack, sdk2_python, reference):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None) as (owner,):
        result = await call(owner, await acquire(owner), "image")
        assert (
            result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
            == reference["results"]["image"]
        )
        assert result.content[0].mime_type == "image/png"
        assert base64.b64decode(result.content[0].data) == base64.b64decode(PNG)


async def test_application_error_is_preserved_without_losing_the_session(
    stack, sdk2_python, reference
):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        before = data(await call(owner, context, "state"))
        result = await call(owner, context, "reject")
        assert (
            result.is_error
            and result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
            == reference["results"]["reject"]
        )
        assert data(await call(owner, context, "state")) == before


async def test_owner_revisions_refresh_and_write_receipts_remain_independent(stack, sdk2_python):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None, None) as (owner, other):
        context = await acquire(owner)
        args = {"candidate": "reviewed", "expected_revision": 0}
        assert (await call(other, context, "edit", args, request_id="sdk2-stolen")).is_error
        assert (await owner.invoke("get_request_status", {"request_id": "sdk2-stolen"})).is_error
        assert (
            data(await call(owner, context, "edit", args, request_id="sdk2-edit"))["revision"] == 1
        )
        assert (await call(owner, context, "edit", args, request_id="sdk2-edit")).is_error
        before = data(await call(owner, context, "state"))
        data(await other.invoke("refresh_services", {}))
        assert data(await call(owner, context, "state")) == before
        assert (await call(owner, context, "edit", args, request_id="sdk2-stale")).is_error
        assert (
            data(await owner.invoke("get_request_status", {"request_id": "sdk2-stale"}))["status"]
            == "app_error"
        )
        assert data(await call(owner, context, "state")) == before


async def test_invalid_input_is_not_dispatched_or_claimed(stack, sdk2_python):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        assert (
            await call(owner, context, "edit", {"candidate": "invalid"}, request_id="sdk2-invalid")
        ).is_error
        assert (await owner.invoke("get_request_status", {"request_id": "sdk2-invalid"})).is_error
        assert data(await call(owner, context, "state"))["revision"] == 0


async def test_clean_release_starts_a_new_synthetic_process_and_empty_state(stack, sdk2_python):
    async with sdk2_app(stack, sdk2_python), frontends(stack[0], None, None) as (owner, other):
        context = await acquire(owner)
        before = data(await call(owner, context, "state"))
        data(
            await call(
                owner,
                context,
                "edit",
                {"candidate": "old", "expected_revision": 0},
                request_id="sdk2-retire",
            )
        )
        data(await owner.invoke("release_service", {"service_id": "sdk2", "context_id": context}))
        replacement = await acquire(other)
        after = data(await call(other, replacement, "state"))
        assert replacement != context and after["pid"] != before["pid"]
        assert after["revision"] == 0 and after["value"] == ""


async def test_timed_out_synchronous_write_keeps_uncertain_status_and_recovery_barrier(
    stack, sdk2_python, tmp_path
):
    audit = tmp_path / "sdk2-writes.jsonl"
    async with (
        # The service budget also covers cold SDK imports and initialization.
        # Keep enough startup margin, then prove a dispatched 30-second write
        # exceeds the deadline through its audit record and uncertain receipt.
        sdk2_app(stack, sdk2_python, timeout=15, audit=audit),
        frontends(stack[0], None, None) as (owner, other),
    ):
        context = await acquire(owner)
        result = await call(
            owner,
            context,
            "edit",
            {"candidate": "maybe", "expected_revision": 0, "delay": 30},
            request_id="sdk2-timeout",
        )
        assert result.is_error
        assert (
            data(await owner.invoke("get_request_status", {"request_id": "sdk2-timeout"}))["status"]
            == "uncertain"
        )
        status = data(await owner.invoke("service_session_status", {"service_id": "sdk2"}))
        assert status["state"] == "lost" and status["recovery_required"]
        assert (await other.invoke("acquire_service", {"service_id": "sdk2"})).is_error
        assert len(audit.read_text(encoding="utf-8").splitlines()) == 1


async def test_sdk23_framework_preserves_sdk22_app_data_and_revision_guards(
    stack,
    sdk2_python,
    sdk22_root,
):
    async with (
        sdk2_app(stack, sdk2_python, sdk22_root=sdk22_root),
        frontends(stack[0], None) as (owner,),
    ):
        context = await acquire(owner)
        assert data(await call(owner, context, "echo", {"payload": payload()})) == payload()
        image = await call(owner, context, "image")
        assert not image.is_error
        assert image.content[0].mime_type == "image/png"
        assert image.content[0].meta == {"fixture": "sdk2", "serial": 2**63 - 1}
        assert base64.b64decode(image.content[0].data) == base64.b64decode(PNG)
        assert (
            data(
                await call(
                    owner,
                    context,
                    "edit",
                    {"candidate": "caf\u00e9 \u6df1 \u03bc", "expected_revision": 0},
                    request_id="sdk22-edit-one",
                )
            )["revision"]
            == 1
        )
        assert (
            await call(
                owner,
                context,
                "edit",
                {"candidate": "stale", "expected_revision": 0},
                request_id="sdk22-stale-one",
            )
        ).is_error
        state = data(await call(owner, context, "state"))
        assert isinstance(state.pop("pid"), int)
        assert state == {
            "value": "caf\u00e9 \u6df1 \u03bc",
            "revision": 1,
        }
        data(await owner.invoke("release_service", {"service_id": "sdk2", "context_id": context}))
