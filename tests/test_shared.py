"""Opt-in broker-owned workers; no installed app or native resources participate."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from test_capabilities import catalogs as catalogs
from test_sessions import OWNER_A, OWNER_B, invoke, manifest
from test_sessions import stateful as stateful

import universal_tools.broker as broker_module
from universal_tools.broker import Broker
from universal_tools.models import BrokerError, atomic_json


def test_shared_is_explicit_opt_in_and_included_in_the_published_schema():
    assert manifest().session_mode == "exclusive"  # Fixture explicitly opts in.
    raw = manifest().model_dump(mode="json", by_alias=True)
    raw.pop("session_mode")
    assert type(manifest()).model_validate(raw).session_mode == "transient"
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/service.schema.json").read_text()
    )
    assert schema["properties"]["session_mode"]["enum"] == ["transient", "exclusive", "shared"]


@pytest.fixture
async def shared(stateful):
    broker, sessions = stateful
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="shared").model_dump(mode="json", by_alias=True),
    )
    await broker.refresh()
    yield broker, sessions


async def test_shared_worker_is_lazy_and_reused_across_frontends_and_refresh(shared):
    broker, sessions = shared
    assert not broker._leases and not broker.ownership.entries()
    before = len(sessions)
    first = await invoke(broker, write=True, request_id="shared-edit", value="shared fixture")
    lease = broker._leases["stateful"]
    assert lease.owner is None
    for owner in (OWNER_B, None, OWNER_A):
        assert (await invoke(broker, owner=owner)).structured_content == first.structured_content
    await broker.refresh()
    assert (await invoke(broker, owner=OWNER_B)).structured_content == first.structured_content
    assert len(sessions) == before + 1 and not sessions[-1].closed
    assert broker.describe("stateful/inspect")["context_required"] is False
    assert broker.ownership.get("stateful")["state"] == "active"
    await broker.aclose()
    assert sessions[-1].closed and not broker.ownership.entries()


@pytest.mark.parametrize("owner", [OWNER_A, OWNER_B, None])
async def test_shared_context_cannot_be_acquired_released_or_exported(shared, owner):
    broker, _ = shared
    await invoke(broker)
    lease = broker._leases["stateful"]
    expires = lease.expires
    with pytest.raises(BrokerError, match="shared|identified"):
        await broker.acquire_service("stateful", owner_id=owner)
    with pytest.raises(BrokerError, match="another|identified"):
        await broker.release_service("stateful", lease.context_id, owner_id=owner)
    with pytest.raises(BrokerError, match="context_id"):
        await invoke(broker, lease.context_id, owner=owner)
    status = await broker.service_session_status("stateful", owner_id=owner)
    assert status["state"] == "shared_active" and "context_id" not in status
    metadata = await broker.describe_service("stateful", owner_id=owner)
    assert metadata["metadata_source"] == "catalog" and "context_id" not in metadata
    assert metadata["initialization"] != lease.worker.initialization
    assert lease.expires == expires


async def test_shared_calls_still_serialize_and_keep_write_policy_and_receipts(shared):
    broker, sessions = shared
    await invoke(broker)
    engine = sessions[-1]
    engine.started.clear()
    engine.unblock = asyncio.Event()
    first = asyncio.create_task(
        invoke(broker, write=True, request_id="shared-serial", value="first")
    )
    await engine.started.wait()
    second = asyncio.create_task(invoke(broker, owner=OWNER_B))
    await asyncio.sleep(0)
    assert not second.done() and engine.calls == 2
    engine.unblock.set()
    assert (await first).structured_content == (await second).structured_content
    count = engine.calls
    with pytest.raises(BrokerError, match="already"):
        await invoke(broker, owner=OWNER_B, write=True, request_id="shared-serial", value="first")
    assert engine.calls == count
    assert broker.request_status("shared-serial")["status"] == "completed"
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="shared", allowed_write_tools=[]).model_dump(
            mode="json", by_alias=True
        ),
    )
    with pytest.raises(BrokerError, match="registration changed"):
        await invoke(broker)
    await broker.refresh()
    assert engine.closed
    with pytest.raises(BrokerError, match="allowed_write_tools"):
        await invoke(broker, write=True, request_id="shared-policy")
    with pytest.raises(BrokerError, match="unknown"):
        broker.request_status("shared-policy")


async def test_clean_shared_idle_expiry_reopens_only_on_an_explicit_call(shared):
    broker, sessions = shared
    clock = [100.0]
    broker._clock = lambda: clock[0]
    first = await invoke(broker)
    engine = sessions[-1]
    clock[0] += 901
    assert (await broker.service_session_status("stateful", owner_id=OWNER_B))["state"] == (
        "shared_expired"
    )
    assert not engine.closed
    await broker._expire("stateful")
    count = len(sessions)
    assert engine.closed and not broker.ownership.entries()
    assert not broker._leases
    assert len(sessions) == count
    second = await invoke(broker, owner=OWNER_B)
    assert second.structured_content["generation"] != first.structured_content["generation"]
    assert len(sessions) == count + 1


async def test_normal_shared_app_error_preserves_worker_and_records_app_error(shared):
    broker, sessions = shared
    await invoke(broker)
    engine = sessions[-1]
    engine.app_error = True
    assert (await invoke(broker, write=True, request_id="shared-app-error")).is_error
    assert broker.request_status("shared-app-error")["status"] == "app_error"
    engine.app_error = False
    await invoke(broker, owner=OWNER_B)
    assert sessions[-1] is engine and not engine.closed


@pytest.mark.parametrize("write", [False, True])
async def test_shared_transport_failure_blocks_all_frontends_and_discovery(shared, write):
    broker, sessions = shared
    await invoke(broker)
    sessions[-1].fail = True
    with pytest.raises(BrokerError, match="app call failed"):
        await invoke(broker, write=write, request_id="shared-failure" if write else None)
    count = len(sessions)
    for owner in (OWNER_A, OWNER_B, None):
        with pytest.raises(BrokerError, match="operator recovery"):
            await invoke(broker, owner=owner)
        info = await broker.service_session_status("stateful", owner_id=owner)
        assert info["recovery_required"] and "context_id" not in info
    await broker.refresh()
    assert broker.services["stateful"]["status"] == "recovery_required"
    assert not broker.catalog and len(sessions) == count
    if write:
        assert broker.request_status("shared-failure")["status"] == "uncertain"


async def test_cancelled_shared_write_drains_before_rejecting_waiting_chat(shared):
    broker, sessions = shared
    await invoke(broker)
    engine = sessions[-1]
    engine.started.clear()
    engine.unblock = asyncio.Event()
    running = asyncio.create_task(invoke(broker, write=True, request_id="shared-cancel"))
    await engine.started.wait()
    running.cancel()
    other = asyncio.create_task(invoke(broker, owner=OWNER_B))
    await asyncio.sleep(0)
    assert not other.done() and not engine.closed
    engine.unblock.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    with pytest.raises(BrokerError, match="operator recovery"):
        await other
    assert engine.closed and broker.request_status("shared-cancel")["status"] == "uncertain"


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
async def test_failed_shared_initialization_retains_reservation_without_claim(
    shared, monkeypatch, failure
):
    broker, sessions = shared
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="shared", timeout_seconds=0.2).model_dump(mode="json", by_alias=True),
    )
    await broker.refresh()

    @asynccontextmanager
    async def unavailable(config):
        # Reservation must exist even before connection initialization starts.
        assert broker.ownership.get(config.id)["state"] == "opening"
        if failure == "disconnect":
            raise ConnectionError("synthetic initialization failure")
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(broker_module, "service_session", unavailable)
    with pytest.raises(BrokerError, match="operator recovery"):
        await invoke(broker, write=True, request_id="shared-no-dispatch")
    assert broker.ownership.get("stateful")["state"] == "lost"
    with pytest.raises(BrokerError, match="unknown"):
        broker.request_status("shared-no-dispatch")
    before = len(sessions)
    with pytest.raises(BrokerError, match="operator recovery"):
        await invoke(broker, owner=OWNER_B)
    assert len(sessions) == before


async def test_shared_restart_requires_recovery_and_does_not_restore_worker(shared):
    broker, sessions = shared
    await invoke(broker)
    await broker._leases["stateful"].worker.close()
    broker._leases.clear()  # Simulate broker death before clean retirement.
    replacement = Broker(broker.state)
    before = len(sessions)
    try:
        await replacement.start()
        await replacement.refresh()
        assert replacement.services["stateful"]["status"] == "recovery_required"
        assert len(sessions) == before
        for owner in (OWNER_A, OWNER_B, None):
            info = await replacement.service_session_status("stateful", owner_id=owner)
            assert info["state"] == "lost" and "context_id" not in info
    finally:
        await replacement.aclose()


async def test_shared_reservation_update_failure_after_dispatch_is_uncertain(shared, monkeypatch):
    broker, sessions = shared
    await invoke(broker)

    def fail(*args):
        raise BrokerError("synthetic journal failure")

    monkeypatch.setattr(broker.ownership, "activate", fail)
    with pytest.raises(BrokerError, match="recovery"):
        await invoke(broker, write=True, request_id="shared-renew-failure", value="applied")
    assert broker.request_status("shared-renew-failure")["status"] == "uncertain"
    assert sessions[-1].closed and broker.ownership.get("stateful")["state"] == "lost"


async def test_shared_cold_start_and_revalidation_share_one_budget_without_late_dispatch(
    shared, monkeypatch
):
    broker, sessions = shared
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="shared", timeout_seconds=1).model_dump(mode="json", by_alias=True),
    )
    await broker.refresh()
    connect = broker_module.service_session
    calls = []

    @asynccontextmanager
    async def delayed(config):
        await asyncio.sleep(0.3)
        async with connect(config) as session:
            list_tools = session.list_tools

            async def delayed_list(*, params=None):
                calls.append("list")
                await asyncio.sleep(0.3 if len(calls) == 1 else 0.5)
                return await list_tools(params=params)

            session.list_tools = delayed_list
            yield session

    monkeypatch.setattr(broker_module, "service_session", delayed)
    with pytest.raises(BrokerError, match="app call failed"):
        await invoke(broker, write=True, request_id="shared-deadline-no-dispatch")
    assert calls == ["list", "list"] and sessions[-1].calls == 0
    with pytest.raises(BrokerError, match="unknown"):
        broker.request_status("shared-deadline-no-dispatch")
    assert broker.ownership.get("stateful")["state"] == "lost"


async def test_failed_shared_teardown_keeps_barrier_when_registration_becomes_transient(
    shared, monkeypatch
):
    broker, sessions = shared
    await invoke(broker)
    worker = broker._leases["stateful"].worker
    close = worker.close

    async def failed_close():
        await close()
        worker.failed = True

    monkeypatch.setattr(worker, "close", failed_close)
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="transient").model_dump(mode="json", by_alias=True),
    )
    before = len(sessions)
    await broker.refresh()
    assert broker.services["stateful"]["status"] == "recovery_required"
    assert broker.ownership.get("stateful")["state"] == "lost" and len(sessions) == before


@pytest.mark.parametrize("mode", ["transient", "exclusive", "shared"])
@pytest.mark.parametrize("declared_alias", [False, True])
async def test_shared_resource_rejects_aliases_before_opening_another_session(
    shared, mode, declared_alias
):
    broker, sessions = shared
    if declared_alias:
        atomic_json(
            broker.registry / "stateful.json",
            manifest(session_mode="shared", resource_id="engine").model_dump(
                mode="json", by_alias=True
            ),
        )
        await broker.refresh()
    await invoke(broker)
    before = len(sessions)
    alias = manifest(
        id="alias",
        session_mode=mode,
        **(
            {
                "resource_id": "engine",
                "transport": {"type": "streamable-http", "url": "http://127.0.0.1:19992/mcp"},
            }
            if declared_alias
            else {}
        ),
    )
    atomic_json(broker.registry / "alias.json", alias.model_dump(mode="json", by_alias=True))
    with pytest.raises(BrokerError, match="conflicting"):
        await invoke(broker)
    await broker.refresh()
    assert not broker.catalog and len(sessions) == before
    assert all(item["status"] == "unavailable" for item in broker.services.values())


async def test_shared_capability_refresh_is_public_and_reuses_worker_without_handle(catalogs):
    broker, sessions, control = await catalogs(session_mode="shared")
    before = len(sessions)
    await broker.refresh_capabilities("app", owner_id=OWNER_A)
    worker = sessions[-1]
    for owner in (OWNER_B, None):
        await broker.refresh_capabilities("app", owner_id=owner)
        match = broker.search(kind="capability", owner_id=owner)["matches"][0]
        assert match["metadata_source"] == "catalog"
        assert match["description"].endswith(worker.scope)
    assert len(sessions) == before + 1
    calls = len(worker.calls)
    await broker.refresh()
    assert sessions[-1] is worker and len(worker.calls) == calls
    control.mode = "invalid"
    status = await broker.refresh_capabilities("app", owner_id=OWNER_B)
    assert status["status"] == "unavailable"
    assert broker.search(kind="capability")["total"] == 0
    assert not worker.closed


async def test_shared_capability_transport_loss_evicts_public_hints_and_blocks_reconnect(catalogs):
    broker, sessions, control = await catalogs(session_mode="shared")
    await broker.refresh_capabilities("app", owner_id=OWNER_A)
    control.mode = "disconnect"
    with pytest.raises(BrokerError, match="probe failed"):
        await broker.refresh_capabilities("app", owner_id=OWNER_B)
    count = len(sessions)
    for owner in (OWNER_A, OWNER_B, None):
        assert broker.search(kind="capability", owner_id=owner)["total"] == 0
        with pytest.raises(BrokerError, match="operator recovery"):
            await broker.refresh_capabilities("app", owner_id=owner)
    assert len(sessions) == count
