"""Progress is disposable telemetry, never flow control or a write receipt."""

import asyncio

import anyio
import pytest

from universal_tools import progress as module
from universal_tools.elicitation import ElicitationRouter, ElicitationTarget, routed_call
from universal_tools.progress import ProgressRelay


class Receiver:
    def __init__(self):
        self.events = []
        self.started = asyncio.Event()
        self.gate = None
        self.error = False

    async def send_progress_notification(self, **event):
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise ValueError("private app text must not reach logs")
        self.events.append(event)


async def settled(route):
    if route.task is not None:
        await asyncio.wait_for(asyncio.shield(route.task), 1)


@pytest.mark.parametrize("token", [0, 123, "client-progress"])
async def test_exact_upstream_token_and_request_attribution(token):
    receiver = Receiver()
    route = ProgressRelay(receiver, "request-17", token)
    route.label = "App fixture/work progress:\n"
    await route.offer(1.5, 7.5, "Working")
    await settled(route)
    assert receiver.events == [
        dict(
            progress_token=token,
            progress=1.5,
            total=7.5,
            message="App fixture/work progress:\nWorking",
            related_request_id="request-17",
        )
    ]
    await route.aclose()


@pytest.mark.parametrize(
    "values",
    [
        (float("nan"), None, None),
        (float("inf"), None, None),
        (True, None, None),
        (None, None, None),
        ("1", None, None),
        (1, float("inf"), None),
        (1, False, None),
        (1, None, {}),
        (1, None, "x" * 4097),
        (1, None, "界" * 1366),
        (1, None, "\ud800"),
        (10**1000, None, None),
    ],
)
async def test_invalid_or_oversized_updates_are_dropped_without_breaking_the_call(values):
    receiver = Receiver()
    route = ProgressRelay(receiver, 1, 0)
    await route.offer(*values)
    assert route.task is None and not receiver.events
    await route.offer(2, None, "valid")
    await settled(route)
    assert receiver.events[0]["progress"] == 2
    await route.aclose()


async def test_updates_increase_without_inventing_total_or_completion():
    receiver = Receiver()
    route = ProgressRelay(receiver, 1, 0)
    for value in (1, 1, 0, -1, 2):
        await route.offer(value)
        await settled(route)
    assert [event["progress"] for event in receiver.events] == [1, 2]
    assert all(event["total"] is None and event["message"] is None for event in receiver.events)
    await route.aclose()
    await route.offer(3, 3, "late")
    assert len(receiver.events) == 2


async def test_flood_coalesces_one_pending_update_and_caps_per_call_work():
    receiver = Receiver()
    receiver.gate = asyncio.Event()
    route = ProgressRelay(receiver, 1, 0)
    await route.offer(0)
    await receiver.started.wait()
    task = route.task
    for value in range(1, 2 * module.MAX_UPDATES):
        await route.offer(value)
    assert route.task is task and route.pending == (module.MAX_UPDATES - 1, None, None)
    assert route.received == module.MAX_UPDATES
    receiver.gate.set()
    await settled(route)
    assert [event["progress"] for event in receiver.events] == [0, module.MAX_UPDATES - 1]
    await route.aclose()


async def test_utf8_budget_includes_attribution_and_preserves_an_exact_boundary():
    receiver = Receiver()
    route = ProgressRelay(receiver, 1, 0)
    route.label = "App fixture/work progress:\n"
    size = module.MAX_MESSAGE_BYTES - len(route.label.encode("utf-8"))
    message = "界" * (size // 3) + "x" * (size % 3)
    await route.offer(1, message=message)
    await settled(route)
    await route.offer(2, message=message + "x")
    await settled(route)
    assert len(receiver.events) == 1
    assert receiver.events[0]["message"] == route.label + message
    assert len(receiver.events[0]["message"].encode("utf-8")) == module.MAX_MESSAGE_BYTES
    await route.aclose()


@pytest.mark.parametrize("mode", ["slow", "disconnected"])
async def test_unavailable_receiver_disables_only_telemetry(mode, monkeypatch, caplog):
    monkeypatch.setattr(module, "SEND_TIMEOUT", 0.01)
    receiver = Receiver()
    receiver.gate = asyncio.Event() if mode == "slow" else None
    receiver.error = mode == "disconnected"
    route = ProgressRelay(receiver, 1, 0)
    await route.offer(1)
    await settled(route)
    assert route.closed and not receiver.events and not caplog.records
    await route.offer(2)
    assert route.task is None


async def test_close_cancels_pending_delivery_even_under_an_sdk_cancel_scope():
    receiver = Receiver()
    receiver.gate = asyncio.Event()
    target = ElicitationTarget(receiver, 1, progress_token=0)
    await target.progress.offer(1)
    await receiver.started.wait()
    with anyio.CancelScope() as scope:
        scope.cancel()
        await target.aclose()
    receiver.gate.set()
    await target.progress.offer(2)
    assert target.closed.is_set() and target.progress.task is None and not receiver.events


async def test_routed_call_opts_in_only_for_this_request_and_closes_before_result():
    receiver = Receiver()

    class App:
        elicitation_router = ElicitationRouter()

        async def call_tool(self, name, arguments, **options):
            callback = options.get("progress_callback")
            if callback:
                self.old_callback = callback
                await callback(1, 5, "active")
                await settled(target.progress)
            return (name, arguments, callback is not None)

    app = App()
    target = ElicitationTarget(receiver, 1, progress_token=0)
    assert await routed_call(app, "work", {}, target) == ("work", {}, True)
    assert target.progress.closed and app.elicitation_router.target is None
    next_target = ElicitationTarget(receiver, 2)
    assert await routed_call(app, "work", {}, next_target) == ("work", {}, False)
    await app.old_callback(2, 5, "late")
    assert len(receiver.events) == 1


async def test_failed_call_cannot_leave_a_progress_pump_alive():
    receiver = Receiver()
    receiver.gate = asyncio.Event()
    target = ElicitationTarget(receiver, 1, progress_token="failed")

    class App:
        async def call_tool(self, name, arguments, progress_callback):
            await progress_callback(1)
            await receiver.started.wait()
            raise RuntimeError("app connection failed")

    with pytest.raises(RuntimeError):
        await routed_call(App(), "work", {}, target)
    assert target.progress.closed and target.progress.task is None and not receiver.events
