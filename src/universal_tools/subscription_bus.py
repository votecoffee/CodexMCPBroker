"""Optional Redis-backed SDK event fan-out; the app supplies its Redis client."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    ToolsListChanged,
)

from .models import BrokerError


class RedisSubscriptionBus(InMemorySubscriptionBus):
    """One app namespace/channel per trust boundary, using redis.asyncio's API.

    Only invalidation hints cross the bus. No replay, results, credentials or
    approval answers are stored. Redis ACL/TLS and durable job state are app
    deployment concerns. Monitor healthy; a failed worker must be replaced.
    """

    def __init__(self, client, channel):
        super().__init__()
        if not isinstance(channel, str) or not 1 <= len(channel) <= 256:
            raise ValueError("a bounded app-specific Redis channel is required")
        self.client, self.channel = client, channel
        self.healthy = False
        self._running = False
        self.failure = asyncio.Event()

    @asynccontextmanager
    async def lifespan(self, server=None):
        if self._running:
            raise BrokerError("subscription bus is already running")
        self._running = True
        pubsub = self.client.pubsub()
        task = None
        try:
            await pubsub.subscribe(self.channel)
            async with asyncio.timeout(20):
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=1)
                    if message and message["type"] == "subscribe":
                        break
            self.failure.clear()
            self.healthy = True

            async def receive():
                try:
                    while True:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if message and message["type"] == "message":
                            raw = message["data"]
                            if len(raw) > 8192:
                                continue
                            try:
                                value = json.loads(raw)
                                if value["kind"] == "ResourceUpdated":
                                    uri = value["uri"]
                                    if not isinstance(uri, str) or len(uri) > 4096:
                                        continue
                                    event = ResourceUpdated(uri)
                                else:
                                    kind = {
                                        c.__name__: c
                                        for c in (
                                            ToolsListChanged,
                                            PromptsListChanged,
                                            ResourcesListChanged,
                                        )
                                    }
                                    event = kind[value["kind"]]()
                            except (TypeError, ValueError, KeyError):
                                continue
                            await super(RedisSubscriptionBus, self).publish(event)
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.healthy = False
                    self.failure.set()

            task = asyncio.create_task(receive(), name="framework-redis-subscriptions")
            yield self
        finally:
            self.healthy = False
            self._running = False
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pubsub.aclose()

    async def publish(self, event):
        if not self.healthy:
            raise BrokerError("shared subscription bus is unavailable")
        if not isinstance(
            event, ToolsListChanged | PromptsListChanged | ResourcesListChanged | ResourceUpdated
        ):
            raise ValueError("unsupported SDK subscription event")
        value = {"kind": type(event).__name__}
        if isinstance(event, ResourceUpdated):
            if len(event.uri) > 4096:
                raise ValueError("resource invalidation URI exceeds its budget")
            value["uri"] = event.uri
        try:
            await self.client.publish(self.channel, json.dumps(value, separators=(",", ":")))
        except Exception:
            self.healthy = False
            self.failure.set()
            raise BrokerError("shared subscription bus is unavailable") from None

    def subscribe(self, listener):
        if not self.healthy:
            raise BrokerError("shared subscription bus is unavailable")
        return super().subscribe(listener)
