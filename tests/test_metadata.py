"""Initialization guidance stays bounded, attributed and scoped to its session owner."""

import asyncio
from copy import deepcopy

import pytest
from mcp.types import InitializeResult
from test_core import fake as fake
from test_sessions import OWNER_A, OWNER_B, acquire, invoke, manifest
from test_sessions import stateful as stateful

import universal_tools.broker as broker_module
from universal_tools.metadata import initialization_metadata
from universal_tools.models import BrokerError, atomic_json


def initialization(**overrides):
    return InitializeResult.model_validate(
        {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "Synthetic app", "title": "Demo", "version": "1"},
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "experimental": {"example": {"feature": "test"}},
            },
            "instructions": "Inspect the explicit target.\nUnicode guidance: \u03b1\u03b2\u03b3.",
            **overrides,
        }
    )


def test_initialization_preserves_native_metadata_without_promoting_extensions():
    native = initialization(_meta={"private-extension": "not exported"})
    captured = initialization_metadata(native)
    assert captured == native.model_dump(mode="json", exclude_none=True, exclude={"meta"})
    assert set(captured) == {"protocolVersion", "serverInfo", "capabilities", "instructions"}
    assert captured["capabilities"]["resources"]["subscribe"] is True
    captured["capabilities"]["experimental"]["example"]["feature"] = "changed"
    assert native.capabilities.experimental["example"]["feature"] == "test"


@pytest.mark.parametrize("instructions", [None, ""])
def test_optional_instructions_preserve_absent_and_empty_values(instructions):
    captured = initialization_metadata(initialization(instructions=instructions))
    assert ("instructions" in captured) is (instructions is not None)
    if instructions is not None:
        assert captured["instructions"] == ""


def test_initialization_size_limit_counts_utf8_and_never_truncates():
    assert initialization_metadata(initialization(instructions="x" * 120_000))
    with pytest.raises(BrokerError, match="metadata exceeds 128 KiB"):
        initialization_metadata(initialization(instructions="\u03b1" * 70_000))


async def test_transient_metadata_is_a_detached_snapshot_without_app_contact(fake, monkeypatch):
    broker, session = fake
    before = await broker.describe_service("app", owner_id=None)
    assert before["metadata_source"] == "catalog"
    assert before["observed_generation"] == 1
    assert before["broker_support"]["tool_calls"] is True
    assert before["broker_support"]["server_requests"] is False
    session.initialization["instructions"] = "Changed guidance"

    with monkeypatch.context() as patch:
        patch.setattr(
            broker_module, "service_session", lambda _: pytest.fail("unexpected connection")
        )
        assert await broker.describe_service("app", owner_id=None) == before
    before["initialization"]["serverInfo"]["name"] = "Caller mutation"
    before["broker_support"]["resources"] = True
    again = await broker.describe_service("app", owner_id=None)
    assert again["initialization"]["serverInfo"]["name"] == "Synthetic app"
    assert again["broker_support"]["resources"] is False

    await broker.refresh()
    after = await broker.describe_service("app", owner_id=None)
    assert after["initialization"]["instructions"] == "Changed guidance"
    assert after["metadata_revision"] != again["metadata_revision"]
    assert after["observed_generation"] == 2


async def test_another_frontends_refresh_never_publishes_owned_guidance(stateful):
    broker, sessions = stateful
    public = await broker.describe_service("stateful", owner_id=None)
    context = await acquire(broker)
    expiry = broker._leases["stateful"].expires
    owned = await broker.describe_service("stateful", owner_id=OWNER_A)
    assert owned["metadata_source"] == "owned_session"
    assert owned["context_id"] == context
    assert owned["initialization"] == sessions[-1].initialization
    assert owned["initialization"] != public["initialization"]
    assert "observed_generation" not in owned
    for owner in (OWNER_B, None):
        assert await broker.describe_service("stateful", owner_id=owner) == public

    await broker.refresh()
    other = await broker.describe_service("stateful", owner_id=OWNER_B)
    assert other["initialization"] == public["initialization"]
    assert other["observed_generation"] == public["observed_generation"]
    assert other["metadata_revision"] == public["metadata_revision"]
    assert "context_id" not in other
    assert broker._leases["stateful"].owner == OWNER_A
    assert broker._leases["stateful"].expires == expiry
    assert len(sessions) == 2
    assert (await broker.describe_service("stateful", owner_id=OWNER_A))["initialization"] == owned[
        "initialization"
    ]

    await broker.release_service("stateful", context, owner_id=OWNER_A)
    await acquire(broker, OWNER_B)
    replacement = await broker.describe_service("stateful", owner_id=OWNER_B)
    assert replacement["metadata_revision"] != owned["metadata_revision"]
    assert (await broker.describe_service("stateful", owner_id=OWNER_A))[
        "initialization"
    ] == public["initialization"]


async def test_expired_owner_receives_only_catalog_metadata_without_renewal(stateful):
    broker, sessions = stateful
    await acquire(broker)
    expiry = broker._leases["stateful"].expires
    broker._clock = lambda: expiry + 1
    info = await broker.describe_service("stateful", owner_id=OWNER_A)
    assert info["metadata_source"] == "catalog" and "context_id" not in info
    assert broker._leases["stateful"].expires == expiry
    assert not sessions[-1].closed  # Description is observational; the reaper owns cleanup.


async def test_lost_session_blocks_metadata_without_reconnecting(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    sessions[-1].fail = True
    with pytest.raises(BrokerError):
        await invoke(broker, context)
    for owner in (OWNER_A, OWNER_B, None):
        with pytest.raises(BrokerError, match="recovery"):
            await broker.describe_service("stateful", owner_id=owner)
    await broker.refresh()
    assert not broker._metadata
    assert len(sessions) == 2


async def test_revoked_and_failed_registrations_do_not_serve_stale_guidance(stateful):
    broker, sessions = stateful
    config = manifest(enabled=False)
    atomic_json(broker.registry / "stateful.json", config.model_dump(mode="json"))
    with pytest.raises(BrokerError, match="registration changed"):
        await broker.describe_service("stateful", owner_id=None)
    await broker.refresh()
    with pytest.raises(BrokerError, match="unavailable"):
        await broker.describe_service("stateful", owner_id=None)
    assert not broker._metadata and len(sessions) == 1


async def test_refresh_rechecks_recovery_after_waiting_for_the_service_lock(stateful, monkeypatch):
    broker, sessions = stateful
    original = broker._recovery_blocker
    checked = asyncio.Event()

    def observe_discovery(config):
        result = original(config)
        checked.set()
        return result

    monkeypatch.setattr(broker, "_recovery_blocker", observe_discovery)
    previous = deepcopy(broker._metadata)
    async with broker.lock("stateful"):
        task = asyncio.create_task(broker.refresh())
        await asyncio.wait_for(checked.wait(), 2)
        assert not task.done()
        broker.ownership.reserve(manifest(), OWNER_A, "synthetic-lost-context")
        broker.ownership.lose("synthetic-lost-context", "session_lost")
    await asyncio.wait_for(task, 2)
    assert previous and not broker._metadata
    assert broker.services["stateful"]["status"] == "recovery_required"
    assert not broker.catalog and len(sessions) == 1
    with pytest.raises(BrokerError, match="recovery"):
        await broker.describe_service("stateful", owner_id=None)
