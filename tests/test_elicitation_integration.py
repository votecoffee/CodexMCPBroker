"""Protocol-only confirmation fixtures through real stdio and HTTP hops."""

import asyncio
import json
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult
from test_integration import data, exclusive_app, free_port, remote_call
from test_integration import stack as stack

from universal_tools.cli import process_options
from universal_tools.models import atomic_json
from universal_tools.surface import RemoteBackend
from universal_tools.transport import InitializedSession


class Frontend:
    def __init__(self, session):
        self.session = session

    async def invoke(self, name, arguments):
        return await self.session.call_tool(name, arguments)


@asynccontextmanager
async def frontends(state, *callbacks, concurrent=False):
    async with AsyncExitStack() as resources:
        clients = []
        for callback in callbacks:
            streams = await resources.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "universal_tools", "--state", str(state), "proxy"],
                    )
                )
            )
            session = await resources.enter_async_context(
                (InitializedSession if concurrent else ClientSession)(
                    *streams, elicitation_callback=callback
                )
            )
            await session.initialize()
            clients.append(Frontend(session))
        yield clients


@pytest.mark.parametrize(
    "action,approved",
    [
        ("accept", True),
        ("accept", False),
        ("decline", True),
        ("cancel", True),
    ],
)
async def test_confirmation_reaches_only_the_calling_frontend_and_preserves_the_answer(
    stack, action, approved
):
    prompts, unexpected = [], []

    async def owner_answer(_, params):
        prompts.append(params)
        return ElicitResult(action=action, content={"approved": approved})

    async def other_answer(_, params):
        unexpected.append(params)
        return ElicitResult(action="accept", content={"approved": True})

    async with (
        exclusive_app(stack),
        frontends(stack[0], owner_answer, other_answer) as (owner, other),
    ):
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        denied = await remote_call(
            other,
            "confirm",
            {"candidate": "stolen"},
            context,
            request_id=f"confirm-denied-{action}-{approved}",
        )
        assert denied.isError and not unexpected and not prompts
        result = data(
            await remote_call(
                owner,
                "confirm",
                {"candidate": "reviewed"},
                context,
                request_id=f"confirm-owner-{action}-{approved}",
            )
        )
        committed = action == "accept" and approved
        assert result["committed"] is committed and result["commits"] == int(committed)
        assert len(prompts) == 1 and not unexpected
        assert (
            prompts[0].message
            == "App stateful/confirm requests input:\nConfirm synthetic candidate: reviewed"
        )
        state = data(await remote_call(owner, "confirmation_state", {}, context))
        assert state == {
            "commits": int(committed),
            "value": "reviewed" if committed else "",
            "form": True,
            "url": False,
        }


@pytest.mark.parametrize("mode", ["unsupported", "invalid", "url"])
async def test_unavailable_confirmation_paths_make_zero_fixture_commits(stack, mode):
    prompts = []

    async def invalid_answer(_, params):
        prompts.append(params)
        return ElicitResult(action="accept", content={"approved": "true"})

    callback = None if mode == "unsupported" else invalid_answer
    async with exclusive_app(stack), frontends(stack[0], callback) as (owner,):
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        operation = "ask_url" if mode == "url" else "confirm"
        args = {} if mode == "url" else {"candidate": "must not commit"}
        result = await remote_call(
            owner, operation, args, context, request_id=f"confirm-rejected-{mode}"
        )
        assert result.isError
        assert len(prompts) == (1 if mode == "invalid" else 0)
        assert data(await remote_call(owner, "confirmation_state", {}, context))["commits"] == 0
        status = data(
            await owner.invoke("get_request_status", {"request_id": f"confirm-rejected-{mode}"})
        )
        assert status["status"] == "app_error"


async def test_second_form_requires_its_own_answer_instead_of_reusing_an_approval(stack):
    prompts = []

    async def answer(_, params):
        prompts.append(params)
        return ElicitResult(
            action="accept" if len(prompts) == 1 else "decline", content={"approved": True}
        )

    async with exclusive_app(stack), frontends(stack[0], answer) as (owner,):
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        result = data(
            await remote_call(
                owner,
                "confirm",
                {"candidate": "reviewed", "repeats": 2},
                context,
                request_id="confirm-each-form-001",
            )
        )
        assert not result["committed"] and len(prompts) == 2
        assert data(await remote_call(owner, "confirmation_state", {}, context))["commits"] == 0


async def test_app_deadline_cannot_be_overridden_by_a_late_acceptance(stack, tmp_path):
    prompt, gate = asyncio.Event(), asyncio.Event()
    audit = tmp_path / "fixture-commits.jsonl"

    async def late_answer(_, params):
        prompt.set()
        await gate.wait()
        return ElicitResult(action="accept", content={"approved": True})

    async with (
        exclusive_app(stack, timeout=2, audit_path=audit),
        frontends(stack[0], late_answer) as (owner,),
    ):
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        pending = asyncio.create_task(
            remote_call(
                owner, "confirm", {"candidate": "late"}, context, request_id="confirm-timeout-001"
            )
        )
        try:
            await asyncio.wait_for(prompt.wait(), 3)
            # Observe the timeout before allowing this stock SDK test client's
            # UI callback to return its deliberately late acceptance.
            async with RemoteBackend(stack[1]) as observer:
                async with asyncio.timeout(12):
                    while (
                        data(
                            await observer.invoke(
                                "get_request_status",
                                {"request_id": "confirm-timeout-001"},
                            )
                        )["status"]
                        != "uncertain"
                    ):
                        await asyncio.sleep(0.05)
            gate.set()
            assert (await asyncio.wait_for(pending, 12)).isError
            assert not audit.exists()
            status = data(
                await owner.invoke("get_request_status", {"request_id": "confirm-timeout-001"})
            )
            assert status["status"] == "uncertain"
            info = data(await owner.invoke("service_session_status", {"service_id": "stateful"}))
            assert info["recovery_required"]
        finally:
            gate.set()


async def test_http_session_id_cannot_be_adopted_with_another_frontend_identity(stack):
    settings = stack[1]
    headers = {
        "Authorization": f"Bearer {settings.token}",
        "Accept": "application/json, text/event-stream",
        "X-Universal-Tools-Client": "a" * 48,
    }
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        opened = await client.post(
            settings.url + "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "fixture", "version": "1"},
                    "capabilities": {},
                },
            },
        )
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]
        headers.update({"Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-11-25"})
        try:
            for identity in ("b" * 48, ""):
                stolen = {**headers, "X-Universal-Tools-Client": identity}
                assert (
                    await client.post(
                        settings.url + "/mcp",
                        headers=stolen,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    )
                ).status_code == 404
                assert (
                    await client.delete(settings.url + "/mcp", headers=stolen)
                ).status_code == 404
            assert (
                await client.post(
                    settings.url + "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            ).status_code == 202
            allowed = await client.post(
                settings.url + "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            )
            assert allowed.status_code == 200 and "broker_status" in allowed.text
        finally:
            await client.delete(settings.url + "/mcp", headers=headers)


async def test_read_tool_can_elicit_input_without_using_the_write_channel(stack):
    async def answer(_, params):
        assert params.message.startswith("App stateful/choose_label")
        return ElicitResult(action="accept", content={"label": "chosen"})

    async with exclusive_app(stack), frontends(stack[0], answer) as (owner,):
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        assert data(await remote_call(owner, "choose_label", {}, context)) == {
            "action": "accept",
            "label": "chosen",
        }
        assert data(await remote_call(owner, "confirmation_state", {}, context))["commits"] == 0


async def test_concurrent_app_forms_do_not_swap_answers_on_pooled_broker_channels(stack):
    seen, both = set(), asyncio.Event()

    async def answer(_, params):
        second = params.message.startswith("App stateful-two/")
        seen.add("second" if second else "first")
        if len(seen) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return ElicitResult(action="accept", content={"approved": not second})

    async with (
        exclusive_app(stack, timeout=10),
        frontends(stack[0], answer, concurrent=True) as (owner,),
    ):
        path = stack[0] / "services/stateful-two.json"
        config = json.loads((stack[0] / "services/stateful.json").read_text())
        config["id"] = "stateful-two"
        config["transport"]["args"] += ["--label", "second"]
        atomic_json(path, config)
        try:
            data(await owner.invoke("refresh_services", {}))
            contexts = {}
            for service in ("stateful", "stateful-two"):
                contexts[service] = data(
                    await owner.invoke("acquire_service", {"service_id": service})
                )["context_id"]

            async def confirm(service):
                tool_id = service + "/confirm"
                revision = data(await owner.invoke("describe_tool", {"tool_id": tool_id}))[
                    "catalog_revision"
                ]
                return data(
                    await owner.invoke(
                        "call_write_tool",
                        {
                            "tool_id": tool_id,
                            "catalog_revision": revision,
                            "arguments": {"candidate": service},
                            "context_id": contexts[service],
                            "request_id": "concurrent-confirm-" + service,
                        },
                    )
                )

            first, second = await asyncio.gather(confirm("stateful"), confirm("stateful-two"))
            assert first["committed"] and not second["committed"] and len(seen) == 2
        finally:
            path.unlink()
            data(await owner.invoke("refresh_services", {}))


@pytest.fixture
def http_confirmation_app(tmp_path):
    port = free_port()
    with (tmp_path / "synthetic-http.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "fixtures/stateful_mcp.py"),
                "--port",
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **process_options(),
        )
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while True:
                    try:
                        client.get(f"http://127.0.0.1:{port}/mcp")
                        break
                    except httpx.TransportError:
                        if time.monotonic() > deadline or process.poll() is not None:
                            pytest.fail("synthetic HTTP confirmation app failed to start")
                        time.sleep(0.05)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


@pytest.mark.parametrize("mode", ["transient", "exclusive"])
async def test_confirmation_through_an_http_app_preserves_response_and_separate_identity(
    stack, http_confirmation_app, mode
):
    path = stack[0] / "services/confirm-http.json"
    atomic_json(
        path,
        {
            "id": "confirm-http",
            "display_name": "Synthetic HTTP confirmation app",
            "transport": {"type": "streamable-http", "url": http_confirmation_app},
            "session_mode": mode,
            "allow_writes": True,
            "timeout_seconds": 5,
        },
    )

    async def answer(_, params):
        assert params.message.startswith("App confirm-http/confirm")
        return ElicitResult(action="accept", content={"approved": True})

    async with frontends(stack[0], answer) as (owner,):
        try:
            data(await owner.invoke("refresh_services", {}))
            context = None
            if mode == "exclusive":
                context = data(
                    await owner.invoke("acquire_service", {"service_id": "confirm-http"})
                )["context_id"]

            async def invoke(name, arguments, write=False):
                tool_id = "confirm-http/" + name
                description = data(await owner.invoke("describe_tool", {"tool_id": tool_id}))
                payload = {
                    "tool_id": tool_id,
                    "catalog_revision": description["catalog_revision"],
                    "arguments": arguments,
                    "context_id": context,
                }
                if write:
                    payload["request_id"] = "http-confirm-" + mode
                return data(
                    await owner.invoke("call_write_tool" if write else "call_read_tool", payload)
                )

            assert (await invoke("confirm", {"candidate": "http-reviewed"}, write=True))[
                "committed"
            ]
            assert (await invoke("confirmation_state", {}))["commits"] == 1
            assert (await invoke("transport_headers", {}))["frontend_identity"] is None
        finally:
            path.unlink()
            data(await owner.invoke("refresh_services", {}))
