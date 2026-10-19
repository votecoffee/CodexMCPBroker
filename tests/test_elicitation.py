"""Approval routing and rejected paths use synthetic clients, never real app approval."""

import asyncio

import anyio
import pytest
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    ErrorData,
    FormElicitationCapability,
    Implementation,
    InitializeRequestParams,
    UrlElicitationCapability,
)
from test_pool import channels as channels

from universal_tools.elicitation import ElicitationRouter, ElicitationTarget
from universal_tools.transport import RoutingDispatcher


def form(**changes):
    return ElicitRequestFormParams(
        message="Review the explicit target",
        requestedSchema={
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
            "additionalProperties": False,
        },
        **changes,
    )


class Client:
    def __init__(self, capability=None, *, supported=True):
        self.client_params = InitializeRequestParams(
            protocolVersion="2025-11-25",
            clientInfo=Implementation(name="synthetic", version="1"),
            capabilities=ClientCapabilities(
                elicitation=(capability or ElicitationCapability(form=FormElicitationCapability()))
                if supported
                else None,
            ),
        )
        self.calls = []
        self.started = asyncio.Event()
        self.gate = None
        self.reply = ElicitResult(action="accept", content={"approved": True})

    async def elicit_form(self, message, requestedSchema, related_request_id):
        self.calls.append((message, requestedSchema, related_request_id))
        self.started.set()
        if self.gate:
            await self.gate.wait()
        return self.reply


@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_actions_preserve_origin_and_only_accepted_schema_valid_content(action):
    client = Client()
    client.reply = ElicitResult(action=action, content={"approved": True})
    route = ElicitationTarget(client, 23, label="App fixture/confirm:\n")
    result = await route.forward(form())
    assert result.action == action
    assert result.content == ({"approved": True} if action == "accept" else None)
    assert client.calls == [
        ("App fixture/confirm:\nReview the explicit target", form().requested_schema, 23)
    ]


@pytest.mark.parametrize(
    "capability", [None, ElicitationCapability(url=UrlElicitationCapability())]
)
async def test_unsupported_clients_are_not_prompted_or_assumed_to_approve(capability):
    client = Client(capability, supported=capability is not None)
    result = await ElicitationTarget(client, 1).forward(form())
    assert isinstance(result, ErrorData) and not client.calls


async def test_legacy_empty_elicitation_capability_means_form_support():
    client = Client(ElicitationCapability())
    assert (await ElicitationTarget(client, 1).forward(form())).action == "accept"


@pytest.mark.parametrize("content", [{}, {"approved": "true"}, {"approved": True, "extra": 1}])
async def test_invalid_accepted_answers_never_reach_the_app_as_approval(content):
    client = Client()
    client.reply = ElicitResult(action="accept", content=content)
    assert isinstance(await ElicitationTarget(client, 1).forward(form()), ErrorData)


async def test_url_requests_are_rejected_without_forwarding():
    client = Client()
    params = ElicitRequestURLParams(
        message="No URL relay", url="https://example.invalid", elicitationId="fixture"
    )
    assert isinstance(await ElicitationTarget(client, 1).forward(params), ErrorData)
    assert not client.calls


async def test_task_augmented_forms_are_not_forwarded():
    client = Client()
    assert isinstance(
        await ElicitationTarget(client, 1).forward(form(task={"ttl": 1000})), ErrorData
    )
    assert not client.calls


@pytest.mark.parametrize(
    "items,valid",
    [
        ({"type": "string", "enum": ["alpha", "beta"]}, True),
        ({"anyOf": [{"const": "alpha", "title": "Alpha"}, {"const": "beta"}]}, True),
        ({"anyOf": [{"const": {"nested": "object"}}]}, False),
    ],
)
async def test_enum_arrays_preserve_string_choices_and_reject_nested_choices(items, valid):
    client = Client()
    client.reply = ElicitResult(action="accept", content={"choices": ["alpha"]})
    params = form().model_copy(
        update={
            "requested_schema": {
                "type": "object",
                "properties": {"choices": {"type": "array", "items": items}},
                "required": ["choices"],
            }
        }
    )
    result = await ElicitationTarget(client, 1).forward(params)
    if valid:
        assert result.content == {"choices": ["alpha"]}
    else:
        assert isinstance(result, ErrorData) and not client.calls


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"type": "object", "properties": {"nested": {"type": "object"}}},
        {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
    ],
)
async def test_unsupported_schemas_cannot_fetch_references_or_open_a_form(schema):
    client = Client()
    params = form().model_copy(update={"requested_schema": schema})
    assert isinstance(await ElicitationTarget(client, 1).forward(params), ErrorData)
    assert not client.calls


async def test_request_and_response_budgets_never_truncate_or_invent_consent():
    client = Client()
    params = form().model_copy(update={"message": "x" * 33_000})
    assert isinstance(await ElicitationTarget(client, 1).forward(params), ErrorData)
    assert not client.calls
    client.reply = ElicitResult(
        action="accept", content={"approved": True, "padding": "x" * 17_000}
    )
    assert isinstance(await ElicitationTarget(client, 1).forward(form()), ErrorData)


async def test_expiry_invalidates_pending_answer_and_releases_its_waiter():
    client = Client()
    client.gate = asyncio.Event()
    route = ElicitationTarget(client, 1, timeout=0.02)
    result = await route.forward(form())
    assert isinstance(result, ErrorData) and not route.pending
    client.gate.set()  # A late answer cannot resurrect the completed waiter.
    route.close()
    assert isinstance(await route.forward(form()), ErrorData)
    assert len(client.calls) == 1


async def test_no_ambient_or_late_route_can_answer_a_different_operation():
    client = Client()
    client.gate = asyncio.Event()
    route = ElicitationTarget(client, 1)
    router = ElicitationRouter()
    assert isinstance(await router.callback(None, form()), ErrorData)
    async with router.bind(route):
        pending = asyncio.create_task(router.callback(None, form()))
        await client.started.wait()
        assert isinstance(await router.callback(None, form()), ErrorData)  # One outstanding form.
    assert isinstance(await asyncio.wait_for(pending, 1), ErrorData)
    client.gate.set()
    replacement = ElicitationTarget(client, 2)
    async with router.bind(replacement):
        result = await router.callback(None, form())
        assert result.action == "accept" and client.calls[-1][2] == 2
    assert replacement.closed.is_set()


@pytest.mark.parametrize("active_at_receipt", [False, True])
async def test_queued_sdk_form_cannot_adopt_a_route_bound_after_its_receipt(
    monkeypatch, active_at_receipt
):
    client = Client()
    router = ElicitationRouter()
    session = RoutingDispatcher(None, None, router)
    gate, tasks = asyncio.Event(), []

    async def dispatch(self, *args, **kwargs):
        async def delayed():
            await gate.wait()
            return await self.router.callback(None, form())

        tasks.append(asyncio.create_task(delayed()))

    monkeypatch.setattr(JSONRPCDispatcher, "_dispatch_request", dispatch)
    first = ElicitationTarget(client, 1)
    async with router.bind(first if active_at_receipt else None):
        await session._dispatch_request(None, None, None, None)
    replacement = ElicitationTarget(client, 2)
    async with router.bind(replacement):
        gate.set()
        assert isinstance(await asyncio.wait_for(tasks[0], 1), ErrorData)
        assert not client.calls
        # A fresh form actually received during this call remains usable.
        assert (await router.callback(None, form())).action == "accept"
        assert client.calls[-1][2] == 2


async def test_each_confirmation_is_fresh_and_prompt_count_is_bounded():
    client = Client()
    route = ElicitationTarget(client, 1)
    for _ in range(8):
        assert (await route.forward(form())).action == "accept"
    assert isinstance(await route.forward(form()), ErrorData) and len(client.calls) == 8


async def test_sdk_cancellation_cleans_up_confirmation_waiters():
    client = Client()
    client.gate = asyncio.Event()
    target = ElicitationTarget(client, 1)
    scopes = []

    async def caller():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await target.forward(form())

    task = asyncio.create_task(caller())
    await client.started.wait()
    scopes[0].cancel()
    await asyncio.wait_for(task, 1)
    assert target.closed.is_set() and not target.pending


async def test_worker_cancellation_invalidates_route_before_waiting_for_operation_cleanup(channels):
    create, opened, _ = channels
    pool = create()
    client = Client()
    client.gate = asyncio.Event()
    target = ElicitationTarget(client, 1)
    operation = asyncio.create_task(
        pool.execute(
            lambda session: target.forward(form()),
            on_cancel=target.close,
        )
    )
    await client.started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert target.closed.is_set() and not target.pending and opened[0].closed
    assert len(client.calls) == 1 and not pool._workers
