"""Coalesced expiry snapshots never coalesce reservation checks or transitions."""

import sqlite3
from contextlib import closing

import pytest
from test_sessions import OWNER_A, OWNER_B, acquire, invoke, manifest
from test_sessions import stateful as stateful

import universal_tools.ownership as ownership_module
from universal_tools.broker import Broker
from universal_tools.models import BrokerError
from universal_tools.ownership import OwnershipJournal

CONTEXT = "r" * 43


@pytest.fixture
def active(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ownership_module.time, "time", lambda: clock[0])
    journal = OwnershipJournal(tmp_path / "ownership.sqlite3")
    journal.reserve(manifest(), OWNER_A, CONTEXT)
    journal.activate(CONTEXT, 900)
    return journal, clock


def test_frequent_renewal_checks_do_not_write_unchanged_snapshot(active):
    journal, clock = active
    original = journal.get("stateful")
    with closing(sqlite3.connect(journal.path)) as observer:
        version = observer.execute("PRAGMA data_version").fetchone()[0]
        for stamp in (1000.1, 1001.0, 1004.99):
            clock[0] = stamp
            journal.activate(CONTEXT, 900)
            assert journal.get("stateful") == original
            assert observer.execute("PRAGMA data_version").fetchone()[0] == version
        clock[0] = 1005.0
        journal.activate(CONTEXT, 900)
        assert observer.execute("PRAGMA data_version").fetchone()[0] != version
    assert journal.get("stateful")["updated_at"] == 1005
    assert journal.get("stateful")["expires_at"] == 1905


@pytest.mark.parametrize("duration", [5.0, 10.0, 900.0])
def test_snapshot_window_is_bounded_by_a_quarter_of_the_lease(active, duration):
    journal, clock = active
    journal.activate(CONTEXT, duration)
    interval = min(5, duration / 4)
    clock[0] += interval - 0.01
    journal.activate(CONTEXT, duration)
    assert journal.get("stateful")["updated_at"] == 1000
    clock[0] = 1000 + interval
    journal.activate(CONTEXT, duration)
    assert journal.get("stateful")["updated_at"] == clock[0]
    assert journal.get("stateful")["expires_at"] == clock[0] + duration


@pytest.mark.parametrize("state", ["retiring", "lost", "missing", "wrong_context"])
def test_every_coalesced_renewal_rejects_changed_or_missing_reservation(active, state):
    journal, clock = active
    clock[0] += 0.1
    context = CONTEXT
    if state == "retiring":
        journal.retiring(CONTEXT)
    elif state == "lost":
        journal.lose(CONTEXT, "fixture_failure")
    elif state == "missing":
        journal.retiring(CONTEXT)
        journal.finish("stateful", CONTEXT, "fixture_close")
    else:
        context = "other" * 9
    with pytest.raises(BrokerError, match="recovery"):
        journal.activate(context, 900)
    if state in {"retiring", "lost"}:
        assert journal.get("stateful")["state"] == state


def test_clock_rollback_and_duration_change_refresh_the_snapshot(active):
    journal, clock = active
    clock[0] = 999.0
    journal.activate(CONTEXT, 900)
    assert journal.get("stateful")["updated_at"] == 999
    clock[0] = 999.1
    journal.activate(CONTEXT, 30)
    assert journal.get("stateful")["expires_at"] == pytest.approx(1029.1)


def test_opening_activation_and_retirement_are_immediately_durable(active):
    journal, clock = active
    journal.retiring(CONTEXT)
    journal.finish("stateful", CONTEXT, "fixture_close")
    journal.reserve(manifest(), OWNER_B, CONTEXT)
    assert journal.get("stateful")["state"] == "opening"
    journal.activate(CONTEXT, 900)
    assert journal.get("stateful")["state"] == "active"
    journal.retiring(CONTEXT)
    assert journal.get("stateful")["state"] == "retiring"
    journal.lose(CONTEXT, "fixture_crash")
    assert OwnershipJournal(journal.path).get("stateful")["state"] == "lost"


async def test_exact_live_deadline_renews_while_journal_snapshot_is_unchanged(
    stateful, monkeypatch
):
    broker, sessions = stateful
    live_clock = [100.0]
    monkeypatch.setattr(broker, "_clock", lambda: live_clock[0])
    journal_clock = [1000.0]
    monkeypatch.setattr(ownership_module.time, "time", lambda: journal_clock[0])
    context = await acquire(broker)
    original = broker.ownership.get("stateful")
    old_deadline = broker._leases["stateful"].expires
    for _ in range(3):
        live_clock[0] += 0.1
        journal_clock[0] += 0.1
        await invoke(broker, context)
        assert broker._leases["stateful"].expires == pytest.approx(live_clock[0] + 900)
        assert broker.ownership.get("stateful") == original
    assert broker._leases["stateful"].expires > old_deadline
    assert sessions[-1].calls == 3


async def test_crash_after_coalesced_renewal_still_blocks_other_owners(stateful, monkeypatch):
    broker, _ = stateful
    clock = [1000.0]
    monkeypatch.setattr(ownership_module.time, "time", lambda: clock[0])
    context = await acquire(broker)
    original = broker.ownership.get("stateful")
    clock[0] += 0.1
    await invoke(broker, context)
    assert broker.ownership.get("stateful") == original
    await broker._leases["stateful"].worker.close()
    broker._leases.clear()  # Simulate loss without clean retirement.
    restarted = Broker(broker.state)
    try:
        await restarted.start()
        await restarted.refresh()
        restarted._clock = lambda: 10**15
        with pytest.raises(BrokerError, match="operator recovery"):
            await acquire(restarted, OWNER_B)
        assert restarted.ownership.get("stateful")["state"] == "lost"
    finally:
        await restarted.aclose()
