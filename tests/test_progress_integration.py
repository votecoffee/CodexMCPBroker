"""Disposable progress fixtures through real stdio frontends, HTTP broker and apps."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack, asynccontextmanager

import pytest
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult, ProgressNotification, ServerNotification
from test_elicitation_integration import http_confirmation_app as http_confirmation_app
from test_integration import data, exclusive_app
from test_integration import stack as stack

from universal_tools.models import atomic_json
from universal_tools.surface import RemoteBackend
from universal_tools.transport import InitializedSession


def progress_app(stack, **options):
    # Most cases test routing, not startup speed on a loaded Windows host.
    options.setdefault("timeout", 20)
    return exclusive_app(stack, fixture="progress_mcp.py", **options)


@asynccontextmanager
async def clients(state, count=1, answer=None):
    async with AsyncExitStack() as resources:
        result = []
        for _ in range(count):
            events = []

            async def receive(message, events=events):
                if isinstance(message, ServerNotification) and isinstance(
                    message.root, ProgressNotification
                ):
                    events.append(message.root.params)

            streams = await resources.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "universal_tools", "--state", str(state), "proxy"],
                    )
                )
            )
            session = await resources.enter_async_context(
                InitializedSession(
                    *streams,
                    elicitation_callback=answer,
                    message_handler=receive,
                )
            )
            await session.initialize()
            result.append((session, events))
        yield result


async def context(session, service="stateful"):
    return data(await session.call_tool("acquire_service", {"service_id": service}))["context_id"]


async def call(
    session,
    handle,
    *,
    service="stateful",
    operation="progress_work",
    arguments=None,
    token=None,
    callback=None,
    request_id=None,
):
    tool_id = f"{service}/{operation}"
    description = data(await session.call_tool("describe_tool", {"tool_id": tool_id}))
    payload = dict(
        tool_id=tool_id,
        catalog_revision=description["catalog_revision"],
        arguments=arguments or {"label": "fixture"},
        context_id=handle,
    )
    if request_id is not None:
        payload["request_id"] = request_id
    return await session.call_tool(
        "call_write_tool" if request_id is not None else "call_read_tool",
        payload,
        meta={"progressToken": token} if token is not None else None,
        progress_callback=callback,
    )


@asynccontextmanager
async def second_app(state, session):
    path = state / "services/stateful-two.json"
    config = json.loads((state / "services/stateful.json").read_text())
    config["id"] = "stateful-two"
    config["transport"]["args"] += ["--label", "second"]
    atomic_json(path, config)
    try:
        data(await session.call_tool("refresh_services"))
        yield
    finally:
        path.unlink()
        data(await session.call_tool("refresh_services"))


@pytest.mark.parametrize("token", [0, "same-token-in-each-chat"])
async def test_two_chats_reusing_a_token_cannot_receive_each_others_progress(stack, token):
    async with progress_app(stack), clients(stack[0], 2) as ((first, a), (second, b)):
        async with second_app(stack[0], first):
            handles = await asyncio.gather(context(first), context(second, "stateful-two"))
            denied = await call(second, handles[0], token=token)
            assert denied.isError and not a and not b
            results = await asyncio.gather(
                call(first, handles[0], token=token, arguments={"label": "first"}),
                call(
                    second,
                    handles[1],
                    service="stateful-two",
                    token=token,
                    arguments={"label": "second"},
                ),
            )
            assert [data(result)["label"] for result in results] == ["first", "second"]
            for events, label, service in ((a, "first", "stateful"), (b, "second", "stateful-two")):
                assert events and all(event.progressToken == token for event in events)
                assert all(
                    event.message == f"App {service}/progress_work progress:\n{label}"
                    for event in events
                )
                assert [event.progress for event in events] == sorted(
                    {event.progress for event in events}
                )
            counts = (len(a), len(b))
            data(await first.call_tool("broker_status"))
            assert counts == (len(a), len(b))


async def test_concurrent_calls_in_one_frontend_keep_separate_progress_callbacks(stack):
    async with progress_app(stack), clients(stack[0]) as ((session, events),):
        async with second_app(stack[0], session):
            handles = await asyncio.gather(context(session), context(session, "stateful-two"))
            observed = [[], []]

            async def callback(index, progress, total, message):
                observed[index].append((progress, total, message))

            results = await asyncio.gather(
                *(
                    call(
                        session,
                        handles[index],
                        service=service,
                        arguments={"label": service},
                        callback=lambda p, t, m, i=index: callback(i, p, t, m),
                    )
                    for index, service in enumerate(("stateful", "stateful-two"))
                )
            )
            assert all(not result.isError for result in results)
            assert len({event.progressToken for event in events}) == 2
            for updates, service in zip(observed, ("stateful", "stateful-two"), strict=True):
                assert updates and all(
                    item[2] == f"App {service}/progress_work progress:\n{service}"
                    for item in updates
                )


async def test_opt_in_and_late_tokens_follow_only_the_active_native_call(stack):
    async with progress_app(stack), clients(stack[0]) as ((session, events),):
        handle = await context(session)
        assert not data(await call(session, handle))["progress_requested"]
        assert not events
        assert data(await call(session, handle, token="old"))["progress_requested"]
        old_count = len(events)
        data(await call(session, handle, token="new", arguments={"label": "next"}))
        assert old_count and len(events) > old_count
        assert all(
            event.progressToken == "new" and event.message.endswith("next")
            for event in events[old_count:]
        )
        count = len(events)
        data(await session.call_tool("refresh_services", meta={"progressToken": "metadata"}))
        data(
            await session.call_tool(
                "acquire_service", {"service_id": "stateful"}, meta={"progressToken": "acquire"}
            )
        )
        assert count == len(events)
        info = data(await session.call_tool("describe_service", {"service_id": "stateful"}))
        assert info["broker_support"]["progress"] is True
        assert info["broker_support"]["notifications"] is False


@pytest.mark.parametrize("action,commits", [("accept", 1), ("decline", 0)])
async def test_progress_and_forms_preserve_write_outcomes_and_duplicate_claims(
    stack, action, commits
):
    prompts = []

    async def answer(_, params):
        prompts.append(params)
        return ElicitResult(action=action, content={"approved": True})

    async with progress_app(stack), clients(stack[0], answer=answer) as ((session, events),):
        handle = await context(session)
        request_id = f"progress-form-{action}"
        args = {"candidate": "reviewed", "confirmation": True}
        result = data(
            await call(
                session,
                handle,
                operation="progress_edit",
                arguments=args,
                token=0,
                request_id=request_id,
            )
        )
        assert result["commits"] == commits and len(prompts) == 1 and events
        count = len(events)
        repeated = await call(
            session,
            handle,
            operation="progress_edit",
            arguments=args,
            token=0,
            request_id=request_id,
        )
        assert repeated.isError and len(prompts) == 1 and len(events) == count
        state = data(await session.call_tool("get_request_status", {"request_id": request_id}))
        assert state["status"] == "completed"


async def test_progress_does_not_turn_an_app_error_into_success(stack):
    async with progress_app(stack), clients(stack[0]) as ((session, events),):
        handle = await context(session)
        result = await call(
            session,
            handle,
            operation="progress_edit",
            token=0,
            arguments={"candidate": "changed", "fail": True},
            request_id="progress-app-error",
        )
        assert result.isError and events
        state = data(
            await session.call_tool("get_request_status", {"request_id": "progress-app-error"})
        )
        assert state["status"] == "app_error"


async def test_progress_cannot_extend_app_deadline_or_recover_a_lost_write(stack):
    async with progress_app(stack, timeout=5), clients(stack[0]) as ((session, events),):
        handle = await context(session)
        started = asyncio.get_running_loop().time()
        result = await call(
            session,
            handle,
            operation="progress_edit",
            token=0,
            arguments={"candidate": "changed", "steps": 300, "delay": 0.1},
            request_id="progress-deadline",
        )
        assert result.isError and events and asyncio.get_running_loop().time() - started < 15
        state = data(
            await session.call_tool("get_request_status", {"request_id": "progress-deadline"})
        )
        assert state["status"] == "uncertain"
        count = len(events)
        status = data(await session.call_tool("service_session_status", {"service_id": "stateful"}))
        assert status["state"] == "lost"
        assert (await call(session, handle, token=0)).isError and len(events) == count


@pytest.mark.parametrize("mode", ["transient", "exclusive"])
@pytest.mark.parametrize("http_confirmation_app", ["progress_mcp.py"], indirect=True)
async def test_progress_through_an_http_app(stack, http_confirmation_app, mode):
    path = stack[0] / "services/progress-http.json"
    atomic_json(
        path,
        dict(
            id="progress-http",
            display_name="Synthetic HTTP progress",
            transport=dict(type="streamable-http", url=http_confirmation_app),
            session_mode=mode,
            timeout_seconds=20,
        ),
    )
    async with RemoteBackend(stack[1]) as admin:
        try:
            data(await admin.invoke("refresh_services", {}))
            async with clients(stack[0]) as ((session, events),):
                handle = await context(session, "progress-http") if mode == "exclusive" else None
                result = data(await call(session, handle, service="progress-http", token=0))
                assert result["progress_requested"] and events
                assert all(
                    event.message == "App progress-http/progress_work progress:\nfixture"
                    for event in events
                )
        finally:
            path.unlink()
            data(await admin.invoke("refresh_services", {}))
