"""Redis command/transaction contracts with an isolated Redis-compatible model."""

import asyncio
import hashlib

import fakeredis.aioredis
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ResourceUpdated, ToolsListChanged
from mcp.types import CallToolRequestParams
from test_tasks_extension import Jobs, context, host_form_ui, server

from universal_tools.models import BrokerError
from universal_tools.redis_tasks import RedisTaskStore
from universal_tools.subscription_bus import RedisSubscriptionBus
from universal_tools.tasks import TaskClient, TaskExtension, TaskRoute


async def durable_fixture():
    # This model has no disk; production uses the default same-connection WAITAOF barrier.
    await asyncio.sleep(0)


async def test_two_replicas_share_task_claims_exact_results_and_reject_foreign_owner():
    backend = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=backend) for _ in range(2)]
    stores = [RedisTaskStore(c, "fixture", durable=durable_fixture) for c in clients]
    jobs = Jobs()
    extensions = [
        TaskExtension(s, {"export": TaskRoute(jobs, "v1", lambda args: None)}) for s in stores
    ]
    tasks = TaskClient()
    try:
        async with Client(
            server(extensions[0]),
            mode="2026-07-28",
            extensions=[tasks],
            elicitation_callback=host_form_ui,
        ) as a:
            tid = (await tasks.create(a.session, "export", {"request_id": "shared-001"}))["taskId"]
        async with Client(
            server(extensions[1]),
            mode="2026-07-28",
            extensions=[tasks],
            elicitation_callback=host_form_ui,
        ) as b:
            duplicate = await tasks.create(b.session, "export", {"request_id": "shared-001"})
            assert duplicate["taskId"] == tid and jobs.starts == 1
            assert (await tasks.get(b.session, tid))["status"] == "input_required"
            await tasks.update(b.session, tid, {"review": {"action": "decline"}})
            assert (await tasks.get(b.session, tid))["status"] == "completed"
        with pytest.raises(BrokerError, match="unknown or expired"):
            await stores[0].load(tid, hashlib.sha256(b"foreign").hexdigest())
        owner = hashlib.sha256(("a" * 32).encode()).hexdigest()
        old = await stores[0].load(tid, owner)
        new = await stores[1].load(tid, owner)
        new["data"]["wire"]["result"]["largeInteger"] = 2**63 - 1
        await stores[1].save(new)
        with pytest.raises(BrokerError, match="changed"):
            await stores[0].save(old)
        assert (await stores[0].load(tid, owner))["data"]["wire"]["result"][
            "largeInteger"
        ] == 2**63 - 1
    finally:
        for client in clients:
            await client.aclose()


async def test_failed_durability_barrier_prevents_start_and_preserves_claim():
    client = fakeredis.aioredis.FakeRedis()

    async def failed():
        raise BrokerError("task persistence unconfirmed")

    store = RedisTaskStore(client, "fixture", durable=failed)
    with pytest.raises(BrokerError, match="unconfirmed"):
        await store.create("owner", "key", "export", "v1", {}, 10000)
    store.durable = durable_fixture
    _, fresh = await store.create("owner", "key", "export", "v1", {}, 10000)
    assert not fresh
    await client.aclose()


async def test_notification_bus_fans_out_between_replicas_and_cleans_up():
    backend = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=backend) for _ in range(2)]
    a, b = [RedisSubscriptionBus(c, "app-fixture") for c in clients]
    events, ready = [], asyncio.Event()
    try:
        async with a.lifespan(), b.lifespan():
            unsubscribe = b.subscribe(lambda event: (events.append(event), ready.set()))
            await a.publish(ResourceUpdated("demo://changed"))
            async with asyncio.timeout(3):
                await ready.wait()
            assert events == [ResourceUpdated("demo://changed")]
            unsubscribe()
            await a.publish(ToolsListChanged())
    finally:
        for client in clients:
            await client.aclose()
    assert not a.healthy and not b.healthy


async def test_other_replica_cannot_reconcile_or_start_while_creation_is_in_flight():
    backend = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=backend) for _ in range(2)]
    gate, entered = asyncio.Event(), asyncio.Event()

    async def delayed_barrier():
        entered.set()
        await gate.wait()

    stores = [
        RedisTaskStore(clients[0], "race", durable=delayed_barrier),
        RedisTaskStore(clients[1], "race", durable=durable_fixture),
    ]
    jobs = Jobs()
    extensions = [
        TaskExtension(
            s, {"export": TaskRoute(jobs, "v1", lambda args: None)}, identity=lambda ctx: ctx.owner
        )
        for s in stores
    ]
    params = CallToolRequestParams(name="export", arguments={"request_id": "raced-001"})
    pending = asyncio.create_task(extensions[0].intercept_tool_call(params, context(), None))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
        with pytest.raises(MCPError, match="busy"):
            await extensions[1].intercept_tool_call(params, context(), None)
        assert jobs.starts == 0
        gate.set()
        created = await pending
        duplicate = await extensions[1].intercept_tool_call(params, context(), None)
        assert created["taskId"] == duplicate["taskId"] and jobs.starts == 1
    finally:
        gate.set()
        await asyncio.gather(pending, return_exceptions=True)
        for client in clients:
            await client.aclose()


async def test_lost_repository_lease_is_fenced_and_cannot_unlock_new_owner():
    client = fakeredis.aioredis.FakeRedis()
    store = RedisTaskStore(client, "fencing", durable=durable_fixture)
    async with store.guard(1):
        await store.check_guard()
        await client.set(store.prefix + "operation-lease", "new-owner")
        with pytest.raises(BrokerError, match="lease lost"):
            await store.check_guard()
        with pytest.raises(BrokerError):
            await store.create("owner", "key", "export", "v1", {}, 10000)
    assert await client.get(store.prefix + "operation-lease") == b"new-owner"
    await client.aclose()
