"""Synthetic state and review lifetimes; no installed app or native project access."""

import asyncio
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

import universal_tools.broker as broker_module
from universal_tools.broker import Broker
from universal_tools.models import BrokerError, ServiceManifest, atomic_json

OWNER_A = "a" * 48
OWNER_B = "b" * 48


def manifest(**overrides):
    return ServiceManifest.model_validate(
        {
            "id": "stateful",
            "display_name": "Synthetic stateful app",
            "transport": {"type": "streamable-http", "url": "http://127.0.0.1:19991/mcp"},
            "session_mode": "exclusive",
            "allow_writes": True,
            **overrides,
        }
    )


class StatefulSession:
    def __init__(self, generation):
        self.generation = generation
        self.value = ""
        self.calls = 0
        self.closed = False
        self.enter_task = asyncio.current_task()
        self.fail = False
        self.app_error = False
        self.started = asyncio.Event()
        self.unblock = None

    async def list_tools(self, cursor=None):
        assert asyncio.current_task() is self.enter_task
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description="Synthetic document state",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    annotations=ToolAnnotations(readOnlyHint=name == "inspect"),
                )
                for name in ("inspect", "edit")
            ]
        )

    async def call_tool(self, name, arguments):
        assert asyncio.current_task() is self.enter_task
        self.calls += 1
        self.started.set()
        if self.unblock:
            await self.unblock.wait()
        if self.fail:
            raise ConnectionError("PRIVATE-TRANSPORT-DETAIL")
        if name == "edit":
            self.value = arguments.get("value", "")
        value = {"generation": self.generation, "value": self.value}
        return CallToolResult(
            content=[TextContent(type="text", text=self.value)],
            structuredContent=value,
            isError=self.app_error,
        )


@pytest.fixture
async def stateful(tmp_path, monkeypatch):
    sessions = []

    @asynccontextmanager
    async def connect(config):
        # An actual AnyIO task group catches the invalid cross-task context-manager pattern.
        async with anyio.create_task_group():
            session = StatefulSession(len(sessions) + 1)
            sessions.append(session)
            try:
                yield session
            finally:
                assert asyncio.current_task() is session.enter_task
                session.closed = True

    monkeypatch.setattr(broker_module, "service_session", connect)
    broker = Broker(tmp_path)
    atomic_json(broker.registry / "stateful.json", manifest().model_dump(mode="json"))
    await broker.refresh()
    try:
        yield broker, sessions
    finally:
        await broker.aclose()
        assert all(session.closed for session in sessions)


async def invoke(broker, context=None, owner=OWNER_A, *, write=False, request_id=None, value=""):
    name = "stateful/edit" if write else "stateful/inspect"
    return await broker.call(
        name,
        broker.describe(name)["catalog_revision"],
        {"value": value},
        write=write,
        request_id=request_id,
        context_id=context,
        owner_id=owner,
    )


async def acquire(broker, owner=OWNER_A):
    return (await broker.acquire_service("stateful", owner_id=owner))["context_id"]


async def test_state_survives_calls_refresh_and_owner_renewal(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    generation = sessions[-1].generation
    await invoke(broker, context, write=True, request_id="persistent-edit-001", value="draft")
    await broker.refresh()
    assert await acquire(broker) == context
    result = await invoke(broker, context)
    assert result.structuredContent == {"generation": generation, "value": "draft"}
    assert len(sessions) == 2  # Temporary discovery + one owned worker, never a refresh worker.
    assert sessions[0].closed and not sessions[1].closed


async def test_context_and_frontend_identity_are_both_required(stateful):
    broker, sessions = stateful
    with pytest.raises(BrokerError, match="identified"):
        await acquire(broker, None)
    with pytest.raises(BrokerError, match="missing"):
        await invoke(broker)
    context = await acquire(broker)
    with pytest.raises(BrokerError, match="owned by another"):
        await invoke(broker, context, OWNER_B)
    with pytest.raises(BrokerError, match="another frontend"):
        await acquire(broker, OWNER_B)
    with pytest.raises(BrokerError, match="owned by another"):
        await broker.release_service("stateful", context, owner_id=OWNER_B)
    info = await broker.service_session_status("stateful", owner_id=OWNER_B)
    assert info == {"service_id": "stateful", "state": "owned_by_another"}
    assert sessions[-1].calls == 0


async def test_acquisition_race_has_one_owner_and_one_worker(stateful):
    broker, sessions = stateful
    results = await asyncio.gather(
        acquire(broker), acquire(broker, OWNER_B), return_exceptions=True
    )
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, BrokerError) for result in results) == 1
    assert len(sessions) == 2


async def test_release_closes_old_engine_before_new_owner_gets_clean_context(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    await invoke(broker, context, write=True, request_id="persistent-edit-002", value="private")
    old_engine = sessions[-1]
    await broker.release_service("stateful", context, owner_id=OWNER_A)
    assert old_engine.closed
    replacement = await acquire(broker, OWNER_B)
    assert replacement != context
    assert (await invoke(broker, replacement, OWNER_B)).structuredContent["value"] == ""
    with pytest.raises(BrokerError, match="missing"):
        await invoke(broker, context, OWNER_B)


async def test_expiry_closes_context_and_does_not_implicitly_reacquire(stateful):
    broker, sessions = stateful
    clock = [100.0]
    broker._clock = lambda: clock[0]
    context = await acquire(broker)
    clock[0] += 901
    with pytest.raises(BrokerError, match="expired"):
        await invoke(broker, context)
    assert sessions[-1].closed
    assert len(sessions) == 2
    assert (await broker.service_session_status("stateful", owner_id=OWNER_A))["state"] == "unowned"
    assert await acquire(broker, OWNER_B) != context


async def test_transport_failure_is_lost_and_write_uncertain_without_reconnect(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    sessions[-1].fail = True
    with pytest.raises(BrokerError, match="uncertain") as error:
        await invoke(broker, context, write=True, request_id="persistent-failure-001")
    assert "PRIVATE" not in str(error.value)
    assert broker.request_status("persistent-failure-001")["status"] == "uncertain"
    assert sessions[-1].closed
    assert (await broker.service_session_status("stateful", owner_id=OWNER_A))["state"] == "lost"
    with pytest.raises(BrokerError, match="lost"):
        await invoke(broker, context)
    with pytest.raises(BrokerError, match="lost"):
        await acquire(broker)
    assert len(sessions) == 2
    await broker.release_service("stateful", context, owner_id=OWNER_A)
    assert await acquire(broker) != context


async def test_app_error_preserves_context_for_inspection(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    sessions[-1].app_error = True
    result = await invoke(broker, context, write=True, request_id="persistent-app-error")
    assert result.isError
    assert broker.request_status("persistent-app-error")["status"] == "app_error"
    assert (await broker.service_session_status("stateful", owner_id=OWNER_A))["state"] == "active"


async def test_timeout_retires_worker_and_claim_remains_uncertain(stateful):
    broker, sessions = stateful
    atomic_json(
        broker.registry / "stateful.json",
        manifest(timeout_seconds=0.2).model_dump(mode="json"),
    )
    await broker.refresh()
    context = await acquire(broker)
    sessions[-1].unblock = asyncio.Event()
    with pytest.raises(BrokerError, match="uncertain"):
        await invoke(broker, context, write=True, request_id="persistent-timeout")
    assert sessions[-1].closed
    assert broker.request_status("persistent-timeout")["status"] == "uncertain"


async def test_cancellation_cannot_transfer_a_running_context(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    engine = sessions[-1]
    engine.unblock = asyncio.Event()
    running = asyncio.create_task(
        invoke(broker, context, write=True, request_id="persistent-cancel", value="maybe-applied")
    )
    await engine.started.wait()
    running.cancel()
    contender = asyncio.create_task(acquire(broker, OWNER_B))
    await asyncio.sleep(0)
    assert not contender.done()
    engine.unblock.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    with pytest.raises(BrokerError, match="another frontend"):
        await contender
    assert engine.closed
    assert broker.request_status("persistent-cancel")["status"] == "uncertain"


async def test_registration_change_revokes_calls_and_refresh_closes_context(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    atomic_json(broker.registry / "stateful.json", manifest(enabled=False).model_dump(mode="json"))
    with pytest.raises(BrokerError, match="registration changed"):
        await invoke(broker, context)
    await broker.refresh()
    assert sessions[-1].closed
    assert not broker._leases
    assert not broker.catalog


async def test_alias_cannot_bypass_exclusive_resource_ownership(stateful):
    broker, sessions = stateful
    await acquire(broker)
    atomic_json(
        broker.registry / "alias.json",
        manifest(id="alias", session_mode="transient").model_dump(mode="json"),
    )
    with pytest.raises(BrokerError, match="conflicting"):
        await invoke(broker, broker._leases["stateful"].context_id)
    await broker.refresh()
    assert not broker.catalog
    assert all(item["status"] == "unavailable" for item in broker.services.values())
    assert sessions[-1].closed


async def test_explicit_resource_id_blocks_distinct_routes_to_same_native_resource(stateful):
    broker, _ = stateful
    atomic_json(
        broker.registry / "stateful.json",
        manifest(resource_id="shared-engine").model_dump(mode="json"),
    )
    atomic_json(
        broker.registry / "alias.json",
        manifest(
            id="alias",
            resource_id="shared-engine",
            transport={"type": "streamable-http", "url": "http://127.0.0.1:19992/mcp"},
        ).model_dump(mode="json"),
    )
    await broker.refresh()
    assert not broker.catalog


async def test_transient_policy_still_opens_and_closes_each_call(stateful):
    broker, sessions = stateful
    atomic_json(
        broker.registry / "stateful.json",
        manifest(session_mode="transient").model_dump(mode="json"),
    )
    await broker.refresh()
    count = len(sessions)
    await invoke(broker)
    await invoke(broker)
    assert len(sessions) == count + 2
    assert all(session.closed for session in sessions)
    with pytest.raises(BrokerError, match="transient"):
        await acquire(broker)


async def test_broker_restart_does_not_restore_or_accept_old_context(stateful):
    broker, _ = stateful
    context = await acquire(broker)
    await broker.aclose()
    replacement = Broker(broker.state)
    try:
        await replacement.refresh()
        with pytest.raises(BrokerError, match="missing"):
            await invoke(replacement, context)
        assert await acquire(replacement) != context
    finally:
        await replacement.aclose()


async def test_background_expiry_reaps_abandoned_worker_without_another_tool_call(stateful):
    broker, sessions = stateful
    clock = [100.0]
    broker._clock = lambda: clock[0]
    await broker.start()
    await acquire(broker)
    worker = sessions[-1]
    clock[0] += 901
    async with asyncio.timeout(3):
        while not worker.closed:
            await asyncio.sleep(0.02)
    assert not broker._leases


async def test_status_is_observational_and_does_not_renew_or_destroy_context(stateful):
    broker, sessions = stateful
    clock = [100.0]
    broker._clock = lambda: clock[0]
    await acquire(broker)
    clock[0] += 901
    info = await broker.service_session_status("stateful", owner_id=OWNER_A)
    assert info["state"] == "expired"
    assert not sessions[-1].closed  # Cleanup belongs to expiry maintenance/dispatch paths.


async def test_hung_initialization_is_bounded_and_creates_no_lease(stateful, monkeypatch):
    broker, _ = stateful

    @asynccontextmanager
    async def hang(config):
        await asyncio.Event().wait()
        yield

    atomic_json(
        broker.registry / "stateful.json",
        manifest(timeout_seconds=0.2).model_dump(mode="json"),
    )
    await broker.refresh()
    monkeypatch.setattr(broker_module, "service_session", hang)
    async with asyncio.timeout(2):
        with pytest.raises(BrokerError, match="could not be acquired"):
            await acquire(broker)
    assert not broker._leases


async def test_queued_call_cannot_run_in_a_replacement_context(stateful):
    broker, sessions = stateful
    old = await acquire(broker)
    lock = broker.lock("stateful")
    await lock.acquire()
    queued = asyncio.create_task(invoke(broker, old))
    await asyncio.sleep(0)
    # An administratively retired context must not turn an already queued request into a new one.
    await broker._retire("stateful")
    lock.release()
    with pytest.raises(BrokerError, match="missing"):
        await queued
    assert len(sessions) == 2 and sessions[-1].calls == 0


async def test_bad_service_names_do_not_allocate_locks(stateful):
    broker, _ = stateful
    count = len(broker._service_locks)
    with pytest.raises(BrokerError, match="invalid service_id"):
        await broker.service_session_status("../outside", owner_id=OWNER_A)
    assert len(broker._service_locks) == count


async def test_sdk_style_cancellation_shields_cleanup_and_marks_write_uncertain(stateful):
    broker, sessions = stateful
    context = await acquire(broker)
    engine = sessions[-1]
    engine.unblock = asyncio.Event()
    scopes = []
    finished = asyncio.Event()

    async def request():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await invoke(broker, context, write=True, request_id="persistent-anyio-cancel")
        finally:
            finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(request)
        await engine.started.wait()
        scopes[0].cancel()
        await asyncio.sleep(0.02)
        assert not finished.is_set()
        contender = asyncio.create_task(acquire(broker, OWNER_B))
        await asyncio.sleep(0)
        assert not contender.done()
        engine.unblock.set()
        await finished.wait()
        with pytest.raises(BrokerError, match="another frontend"):
            await contender
    assert engine.closed
    assert broker.request_status("persistent-anyio-cancel")["status"] == "uncertain"
