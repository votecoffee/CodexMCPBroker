"""Durable barriers and stopped-broker recovery using only synthetic app state."""

import asyncio

import pytest
from test_sessions import OWNER_A, OWNER_B, acquire, invoke, manifest
from test_sessions import stateful as stateful

from universal_tools.broker import Broker
from universal_tools.cli import initialize, list_contexts, recover_context
from universal_tools.daemon import InstanceLock
from universal_tools.models import BrokerError, atomic_json
from universal_tools.ownership import OwnershipJournal, owner_digest

CONTEXT_A = "c" * 43
CONTEXT_B = "d" * 43


def test_opening_reservation_survives_restart_without_plaintext_owner(tmp_path):
    path = tmp_path / "ownership.sqlite3"
    journal = OwnershipJournal(path)
    journal.reserve(manifest(), OWNER_A, CONTEXT_A)
    assert OWNER_A.encode() not in path.read_bytes()
    assert journal.get("stateful")["owner_digest"] == owner_digest(OWNER_A)
    restarted = OwnershipJournal(path)
    restarted.orphaned(set())
    assert restarted.get("stateful")["state"] == "lost"
    assert restarted.get("stateful")["reason"] == "broker_restart"
    with pytest.raises(BrokerError, match="recovery"):
        restarted.activate(CONTEXT_A, 900)
    with pytest.raises(BrokerError, match="recovery"):
        restarted.finish("stateful", CONTEXT_A, "closed")


def test_resource_reservations_are_atomic_across_aliases(tmp_path):
    path = tmp_path / "ownership.sqlite3"
    journal = OwnershipJournal(path)
    journal.reserve(manifest(), OWNER_A, CONTEXT_A)
    competitor = OwnershipJournal(path)
    with pytest.raises(BrokerError, match="already reserved"):
        competitor.reserve(manifest(id="alias"), OWNER_B, CONTEXT_B)
    assert competitor.get("alias") is None
    assert len(competitor.entries()) == 1
    assert {row["context_id"] for row in competitor.blockers(manifest().resource_keys)} == {
        CONTEXT_A
    }


def test_recovery_requires_acknowledgement_stopped_broker_and_exact_handle(tmp_path):
    initialize(tmp_path, 19995)
    journal = OwnershipJournal(tmp_path / "ownership.sqlite3")
    journal.reserve(manifest(), OWNER_A, CONTEXT_A)
    with pytest.raises(BrokerError, match="acknowledge-outcome"):
        recover_context(tmp_path, "stateful", CONTEXT_A, acknowledged=False)
    with InstanceLock(tmp_path / "broker.lock"):
        with pytest.raises(BrokerError, match="already owns"):
            recover_context(tmp_path, "stateful", CONTEXT_A, acknowledged=True)
    with pytest.raises(BrokerError, match="stale context_id"):
        recover_context(tmp_path, "stateful", CONTEXT_B, acknowledged=True)
    visible = list_contexts(tmp_path)["contexts"]
    assert len(visible) == 1 and "owner_digest" not in visible[0]
    assert recover_context(tmp_path, "stateful", CONTEXT_A, acknowledged=True)["recovered"]
    assert not journal.entries()
    journal.reserve(manifest(), OWNER_B, CONTEXT_B)
    with pytest.raises(BrokerError, match="stale context_id"):
        recover_context(tmp_path, "stateful", CONTEXT_A, acknowledged=True)
    assert journal.get("stateful")["context_id"] == CONTEXT_B


def test_crash_during_retirement_keeps_an_unknown_context_reserved(tmp_path):
    journal = OwnershipJournal(tmp_path / "ownership.sqlite3")
    journal.reserve(manifest(), OWNER_A, CONTEXT_A)
    journal.activate(CONTEXT_A, 900)
    journal.retiring(CONTEXT_A)
    restarted = OwnershipJournal(tmp_path / "ownership.sqlite3")
    restarted.orphaned(set())
    assert restarted.get("stateful")["state"] == "lost"
    assert restarted.blockers(manifest().resource_keys)


async def test_restart_blocks_even_metadata_and_never_expires_the_lost_reservation(stateful):
    broker, sessions = stateful
    initialize(broker.state, 19995)
    context = await acquire(broker)
    # Simulate abrupt owner loss: engine ends without the broker's retirement transaction.
    await broker._leases["stateful"].worker.close()
    broker._leases.clear()
    count = len(sessions)
    restarted = Broker(broker.state)
    await restarted.start()
    try:
        result = await restarted.refresh()
        assert result["tool_count"] == 0
        assert result["services"][0]["status"] == "recovery_required"
        for owner in (OWNER_A, OWNER_B):
            with pytest.raises(BrokerError, match="operator recovery"):
                await acquire(restarted, owner)
        restarted._clock = lambda: 10**15
        await restarted._expire("stateful")
        await restarted.refresh()
        assert restarted.ownership.get("stateful")["state"] == "lost"
        assert len(sessions) == count
        own = await restarted.service_session_status("stateful", owner_id=OWNER_A)
        assert own["context_id"] == context and own["recovery_required"]
        other = await restarted.service_session_status("stateful", owner_id=OWNER_B)
        assert "context_id" not in other and other["recovery_required"]
    finally:
        await restarted.aclose()
    assert restarted.ownership.get("stateful") is not None
    recover_context(broker.state, "stateful", context, acknowledged=True)
    clean = Broker(broker.state)
    try:
        await clean.start()
        await clean.refresh()
        assert await acquire(clean, OWNER_B) != context
    finally:
        await clean.aclose()


@pytest.mark.parametrize("mode", ["transient", "exclusive"])
async def test_registry_changes_cannot_bypass_an_unresolved_service(stateful, mode):
    broker, sessions = stateful
    old = manifest(resource_id="native-engine")
    broker.ownership.reserve(old, OWNER_A, CONTEXT_A)
    broker.ownership.lose(CONTEXT_A, "synthetic_crash")
    atomic_json(
        broker.registry / "stateful.json",
        manifest(
            session_mode=mode,
            transport={"type": "streamable-http", "url": "http://127.0.0.1:19993/mcp"},
        ).model_dump(mode="json"),
    )
    count = len(sessions)
    await broker.refresh()
    assert broker.services["stateful"]["status"] == "recovery_required"
    assert len(sessions) == count
    assert not broker.catalog


async def test_lost_resource_also_blocks_a_renamed_transient_registration(stateful):
    broker, sessions = stateful
    broker.ownership.reserve(manifest(resource_id="engine"), OWNER_A, CONTEXT_A)
    broker.ownership.lose(CONTEXT_A, "synthetic_crash")
    (broker.registry / "stateful.json").unlink()
    atomic_json(
        broker.registry / "alias.json",
        manifest(id="alias", session_mode="transient", resource_id="engine").model_dump(
            mode="json"
        ),
    )
    count = len(sessions)
    await broker.refresh()
    assert broker.services["alias"]["status"] == "recovery_required"
    assert len(sessions) == count and not broker.catalog


async def test_reservation_update_failure_after_write_is_uncertain_and_closes_worker(
    stateful, monkeypatch
):
    broker, sessions = stateful
    context = await acquire(broker)

    def fail(*args):
        raise BrokerError("synthetic journal failure")

    monkeypatch.setattr(broker.ownership, "activate", fail)
    with pytest.raises(BrokerError, match="recovery"):
        await invoke(broker, context, write=True, request_id="renew-failure-001", value="applied")
    assert broker.request_status("renew-failure-001")["status"] == "uncertain"
    assert broker.ownership.get("stateful")["state"] == "lost"
    assert sessions[-1].closed
    assert sessions[-1].calls == 1


async def test_reservation_is_not_cleared_until_downstream_cleanup_finishes(stateful, monkeypatch):
    broker, _ = stateful
    context = await acquire(broker)
    worker = broker._leases["stateful"].worker
    close = worker.close
    closing = asyncio.Event()
    allow_finish = asyncio.Event()

    async def delayed_close():
        await close()
        closing.set()
        await allow_finish.wait()

    monkeypatch.setattr(worker, "close", delayed_close)
    release = asyncio.create_task(broker.release_service("stateful", context, owner_id=OWNER_A))
    try:
        await closing.wait()
        assert broker.ownership.get("stateful")["state"] == "retiring"
        with pytest.raises(BrokerError, match="already reserved"):
            broker.ownership.reserve(manifest(), OWNER_B, CONTEXT_B)
    finally:
        allow_finish.set()
        await release
    assert broker.ownership.get("stateful") is None


async def test_normal_shutdown_clears_only_clean_reservations(stateful):
    broker, sessions = stateful
    await acquire(broker)
    assert broker.ownership.entries()
    await broker.aclose()
    assert sessions[-1].closed
    assert not broker.ownership.entries()
