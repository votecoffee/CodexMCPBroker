"""Bounded frontend reuse, independent channels, and no replay after failure."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

import universal_tools.surface as surface_module
from universal_tools.models import BrokerError, Settings
from universal_tools.sessions import SessionPool
from universal_tools.surface import RemoteBackend


@dataclass(frozen=True)
class Config:
    id: str = "synthetic-broker"
    timeout_seconds: float = 1


class Channel:
    initialization = {}

    def __init__(self):
        self.task = asyncio.current_task()
        self.calls = []
        self.started = asyncio.Event()
        self.unblock = asyncio.Event()
        self.closed = False

    async def call_tool(self, name, arguments):
        assert asyncio.current_task() is self.task
        self.calls.append((name, arguments))
        self.started.set()
        if name == "wait":
            await self.unblock.wait()
        if name == "fail":
            raise ConnectionError("private transport detail")
        return CallToolResult(
            content=[TextContent(type="text", text=arguments["value"])],
            isError=name == "app_error",
        )


@pytest.fixture
async def channels():
    opened = []
    pools = []
    initialization_gate = asyncio.Event()
    initialization_gate.set()

    @asynccontextmanager
    async def connect(_):
        async with anyio.create_task_group():
            await initialization_gate.wait()
            channel = Channel()
            opened.append(channel)
            try:
                yield channel
            finally:
                assert asyncio.current_task() is channel.task
                channel.closed = True

    def create(*, size=2, timeout=1, initialization_timeout=1):
        pool = SessionPool(
            Config(timeout_seconds=timeout),
            connect,
            size=size,
            initialization_timeout=initialization_timeout,
        )
        pools.append(pool)
        return pool

    yield create, opened, initialization_gate
    for channel in opened:
        channel.unblock.set()
    for pool in pools:
        await pool.aclose()
    assert all(channel.closed for channel in opened)


async def call(pool, name="echo", value="test"):
    return await pool.execute(lambda session: session.call_tool(name, {"value": value}))


async def wait_started(opened, count):
    async with asyncio.timeout(2):
        while len(opened) < count or not all(
            channel.started.is_set() for channel in opened[:count]
        ):
            await asyncio.sleep(0)


async def test_sequential_calls_reuse_one_actor_and_application_errors_keep_the_channel(channels):
    create, opened, _ = channels
    pool = create()
    for value in range(20):
        assert (await call(pool, value=str(value))).content[0].text == str(value)
    assert (await call(pool, "app_error")).is_error
    assert not (await call(pool)).is_error
    assert len(opened) == 1 and len(opened[0].calls) == 22 and not opened[0].closed


async def test_concurrent_calls_are_bounded_and_a_cancelled_waiter_never_dispatches(channels):
    create, opened, _ = channels
    pool = create(size=2)
    first = asyncio.create_task(call(pool, "wait", "one"))
    second = asyncio.create_task(call(pool, "wait", "two"))
    await wait_started(opened, 2)
    queued = asyncio.create_task(call(pool, value="never dispatched"))
    await asyncio.sleep(0)
    assert not queued.done() and len(opened) == 2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    for channel in opened:
        channel.unblock.set()
    results = await asyncio.gather(first, second)
    assert [result.content[0].text for result in results] == ["one", "two"]
    assert sum(len(channel.calls) for channel in opened) == 2
    await call(pool)
    assert len(opened) == 2


async def test_failed_operation_is_not_replayed_and_only_a_later_call_creates_a_channel(channels):
    create, opened, _ = channels
    pool = create()
    with pytest.raises(ConnectionError):
        await call(pool, "fail", "intended write")
    assert len(opened) == 1 and opened[0].closed and len(opened[0].calls) == 1
    assert not pool._workers
    assert (await call(pool, value="explicit later operation")).content[
        0
    ].text == "explicit later operation"
    assert len(opened) == 2 and len(opened[0].calls) == 1


async def test_operation_timeout_retires_its_channel_without_harming_another(channels):
    create, opened, _ = channels
    pool = create(timeout=0.05)
    slow = asyncio.create_task(call(pool, "wait"))
    await wait_started(opened, 1)
    assert not (await call(pool, value="independent")).is_error
    with pytest.raises(TimeoutError):
        await slow
    assert opened[0].closed and not opened[1].closed
    await call(pool)
    assert len(opened) == 2


async def test_asyncio_cancellation_keeps_channel_borrowed_until_operation_cleanup(channels):
    create, opened, _ = channels
    pool = create(size=1)
    abandoned = asyncio.create_task(call(pool, "wait"))
    await wait_started(opened, 1)
    abandoned.cancel()
    queued = asyncio.create_task(call(pool, value="next"))
    await asyncio.sleep(0)
    assert not abandoned.done() and not queued.done() and len(opened) == 1
    opened[0].unblock.set()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    assert (await queued).content[0].text == "next"
    assert opened[0].closed and len(opened) == 2


async def test_sdk_cancellation_does_not_strand_a_borrowed_channel(channels):
    create, opened, _ = channels
    pool = create(size=1)
    scopes = []

    async def caller():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await call(pool, "wait")

    task = asyncio.create_task(caller())
    await wait_started(opened, 1)
    scopes[0].cancel()
    await asyncio.sleep(0)
    assert not task.done()
    opened[0].unblock.set()
    await asyncio.wait_for(task, 2)
    assert opened[0].closed and not pool._workers
    assert not (await call(pool)).is_error
    assert len(opened) == 2


async def test_close_wakes_waiters_and_drains_dispatched_work_before_context_exit(channels):
    create, opened, _ = channels
    pool = create(size=1)
    running = asyncio.create_task(call(pool, "wait", "already dispatched"))
    await wait_started(opened, 1)
    queued = asyncio.create_task(call(pool, value="never dispatched"))
    closing = asyncio.create_task(pool.aclose())
    with pytest.raises(BrokerError, match="closed"):
        await asyncio.wait_for(queued, 2)
    assert not closing.done() and not opened[0].closed
    opened[0].unblock.set()
    assert (await running).content[0].text == "already dispatched"
    await asyncio.wait_for(closing, 2)
    assert opened[0].closed and len(opened[0].calls) == 1 and not pool._workers
    await pool.aclose()
    with pytest.raises(BrokerError, match="closed"):
        await call(pool)


async def test_initialization_has_its_own_deadline_and_cannot_dispatch_an_operation(channels):
    create, opened, gate = channels
    gate.clear()
    pool = create(initialization_timeout=0.03)
    with pytest.raises(BrokerError, match="initialization failed"):
        await asyncio.wait_for(call(pool), 2)
    assert not opened and not pool._workers
    gate.set()
    assert not (await call(pool)).is_error
    assert len(opened) == 1


async def test_two_frontend_pools_never_share_workers(channels):
    create, opened, _ = channels
    first, second = create(), create()
    await call(first)
    await call(second)
    assert len(opened) == 2
    await first.aclose()
    assert opened[0].closed and not opened[1].closed
    await call(second)
    assert len(opened) == 2


async def test_shutdown_cleanup_survives_an_already_cancelled_sdk_scope(channels):
    create, opened, _ = channels
    pool = create()
    await call(pool)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await pool.aclose()
    assert opened[0].closed and not pool._workers


async def test_frontend_redacts_failed_write_without_replay_or_identity_change(monkeypatch):
    connections, attempts = [], []

    class LostReply:
        initialization = {}

        async def call_tool(self, name, arguments):
            attempts.append((name, arguments))
            if name == "call_write_tool":
                raise ConnectionError("PRIVATE-BROKER-CONNECTION-DETAIL")
            return CallToolResult(content=[TextContent(type="text", text="explicit status check")])

    @asynccontextmanager
    async def connect(*args, **kwargs):
        connections.append(kwargs["client_id"])
        async with anyio.create_task_group():
            yield LostReply()

    monkeypatch.setattr(surface_module, "http_session", connect)
    async with RemoteBackend(Settings(token="s" * 48)) as backend:
        result = await backend.invoke("call_write_tool", {"request_id": "intended-write-001"})
        assert result.is_error and "get_request_status" in result.content[0].text
        assert "PRIVATE" not in result.content[0].text
        assert attempts == [("call_write_tool", {"request_id": "intended-write-001"})]
        assert connections == [backend.client_id] and not backend.pool._workers
        await backend.invoke("get_request_status", {"request_id": "intended-write-001"})
        assert connections == [backend.client_id, backend.client_id]
        assert len(attempts) == 2 and attempts[1][0] == "get_request_status"
