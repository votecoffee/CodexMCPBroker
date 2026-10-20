"""Bounded Tasks subscription streams above SDK 2.3's core notification driver."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from copy import deepcopy

from mcp.client.extension import NotificationBinding
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import (
    SUBSCRIPTION_ID_META_KEY,
    ResourceUpdated,
    event_matches,
    event_to_notification,
)
from mcp.types import (
    INVALID_PARAMS,
    Notification,
    NotificationParams,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
)
from pydantic import ConfigDict

from .budgets import DEFAULT_JSON_BUDGET
from .models import BrokerError, canonical_digest


class TaskStatusParams(NotificationParams):
    model_config = ConfigDict(extra="allow")


class TaskNotifications:
    """Server middleware; authorization is checked before every full-state delivery."""

    def __init__(self, extension, *, interval=1.0, capacity=128):
        if not 0.05 <= interval <= 300 or not 1 <= capacity <= 1024:
            raise ValueError("invalid task notification limits")
        self.extension, self.interval, self.capacity = extension, interval, capacity
        self.bus = None
        self.streams = 0

    async def changed(self, task_id):
        # Internal level trigger only: the bus never carries results or approvals.
        if self.bus is not None:
            await self.bus.publish(ResourceUpdated("broker-task://" + task_id))

    async def __call__(self, ctx, call_next):
        raw = ctx.params or {}
        requested = raw.get("notifications", {}) if isinstance(raw, dict) else {}
        if ctx.method != "subscriptions/listen" or "taskIds" not in requested:
            return await call_next(ctx)
        self.extension._principal(ctx)
        ids = requested["taskIds"]
        if (
            ctx.protocol_version != "2026-07-28"
            or ctx.request_id is None
            or not isinstance(ids, list)
            or not 1 <= len(ids) <= 64
            or any(not isinstance(i, str) or not 1 <= len(i) <= 128 for i in ids)
            or len(set(ids)) != len(ids)
            or self.streams >= self.capacity
            or self.bus is None
        ):
            raise MCPError(INVALID_PARAMS, "invalid or unavailable task subscription")
        from .tasks import TaskParams

        async def snapshot(task_id):
            value = await self.extension.get_task(ctx, TaskParams(taskId=task_id))
            value.pop("resultType", None)
            return value

        # Deny foreign, revoked and expired tasks before acknowledging any IDs.
        initial = {task_id: await snapshot(task_id) for task_id in ids}
        honored = SubscriptionFilter.model_validate(requested)
        for field in ("tools_list_changed", "prompts_list_changed", "resources_list_changed"):
            if getattr(honored, field) is not True:
                setattr(honored, field, None)
        metadata = {SUBSCRIPTION_ID_META_KEY: ctx.request_id}
        uris = frozenset(honored.resource_subscriptions or ())
        queue = asyncio.Queue(maxsize=128)
        wake = asyncio.Event()
        overflow = False

        def receive(event):
            nonlocal overflow
            if isinstance(event, ResourceUpdated) and event.uri.startswith("broker-task://"):
                if event.uri.removeprefix("broker-task://") in initial:
                    wake.set()
            elif event_matches(honored, uris, event):
                try:
                    queue.put_nowait(event)
                    wake.set()
                except asyncio.QueueFull:
                    overflow = True
                    wake.set()

        unsubscribe = self.bus.subscribe(receive)
        self.streams += 1
        try:
            await ctx.session.send_notification(
                SubscriptionsAcknowledgedNotification(
                    params=SubscriptionsAcknowledgedNotificationParams(
                        notifications=honored, _meta=metadata
                    )
                ),
                related_request_id=ctx.request_id,
            )
            previous = {}
            while not overflow:
                wake.clear()
                for task_id in ids:
                    # Recheck app authorization even when the stored result is terminal.
                    value = await snapshot(task_id)
                    digest = canonical_digest(value)
                    if previous.get(task_id) != digest:
                        await ctx.session.send_notification(
                            Notification[TaskStatusParams, str](
                                method="notifications/tasks",
                                params=TaskStatusParams(**value, _meta=metadata),
                            ),
                            related_request_id=ctx.request_id,
                        )
                        previous[task_id] = digest
                while not queue.empty():
                    await ctx.session.send_notification(
                        event_to_notification(queue.get_nowait(), metadata),
                        related_request_id=ctx.request_id,
                    )
                try:
                    await asyncio.wait_for(wake.wait(), self.interval)
                except TimeoutError:
                    pass
            return {"resultType": "complete", "_meta": metadata}
        finally:
            unsubscribe()
            self.streams -= 1


class TaskNotificationClient:
    """Mixin for explicit subscription use; no automatic reconnection or input answers."""

    def _subscriptions(self):
        if not hasattr(self, "_task_streams"):
            self._task_streams = {}
        return self._task_streams

    def notifications(self):
        return (
            NotificationBinding(
                method="notifications/tasks",
                params_type=TaskStatusParams,
                handler=self._receive_task,
            ),
        )

    async def _receive_task(self, params):
        value = params.model_dump(mode="json", by_alias=True, exclude_none=True)
        sid = value.get("_meta", {}).get(SUBSCRIPTION_ID_META_KEY)
        stream = self._subscriptions().get(sid)
        if stream is None or value.get("taskId") not in stream.ids:
            return
        try:
            DEFAULT_JSON_BUDGET.check(value)
            from .tasks import MAX_TASK_BYTES

            if len(params.model_dump_json().encode()) > MAX_TASK_BYTES:
                raise BrokerError("task notification exceeds budget")
            stream.offer(value)
        except Exception:
            stream.failure = BrokerError("invalid task notification; refetch task state")
            stream.ready.set()

    @asynccontextmanager
    async def listen(self, session, task_ids):
        if session.protocol_version != "2026-07-28":
            raise BrokerError("Task notifications require the modern protocol")
        ids = list(task_ids)
        if not 1 <= len(ids) <= 64 or any(not isinstance(i, str) for i in ids):
            raise ValueError("listen requires one to 64 task IDs")
        sid = "tasks-" + secrets.token_urlsafe(24)
        stream = TaskStream(ids)
        self._subscriptions()[sid] = stream
        # SDK 2.3 handles the core acknowledgment; its public listen driver does
        # not yet accept taskIds. Keep this pinned-SDK bridge in one module.
        route = session._register_listen_route(sid)
        payload = {"method": "subscriptions/listen", "params": {"notifications": {"taskIds": ids}}}
        options = {"request_id": sid}
        session._stamp(payload, options)

        async def drive():
            try:
                await session._dispatcher.send_raw_request(
                    payload["method"], payload["params"], options
                )
                stream.failure = BrokerError("task subscription ended; reconnect and refetch")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stream.failure = exc
            finally:
                route.acked.set()
                stream.ready.set()

        task = asyncio.create_task(drive())
        try:
            async with asyncio.timeout(session._session_read_timeout_seconds):
                await route.acked.wait()
            if stream.failure:
                raise stream.failure
            if route.honored is None or route.honored.model_extra.get("taskIds") != ids:
                raise BrokerError("server did not acknowledge the requested Tasks subscription")
            yield stream
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            session._unregister_listen_route(sid)
            self._subscriptions().pop(sid, None)


class TaskStream:
    def __init__(self, ids):
        self.ids = frozenset(ids)
        self.pending = {}
        self.ready = asyncio.Event()
        self.failure = None

    def offer(self, value):
        self.pending[value["taskId"]] = deepcopy(value)
        self.ready.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.pending:
            if self.failure:
                raise self.failure
            self.ready.clear()
            await self.ready.wait()
        key = next(iter(self.pending))
        return self.pending.pop(key)
