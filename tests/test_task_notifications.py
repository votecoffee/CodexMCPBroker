"""Real subscription streams and URL inputs; every job remains synthetic."""

import asyncio

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ElicitationCapability,
    ElicitRequestURLParams,
    ElicitResult,
    UrlElicitationCapability,
)
from test_elicitation import Client as ApprovalClient
from test_tasks_extension import Jobs, assert_wire, context, extension, host_form_ui, server

from universal_tools.elicitation import ElicitationTarget, input_validator
from universal_tools.tasks import TaskClient, TaskObservation, TaskParams, TaskUpdateParams


async def test_task_push_delivers_full_state_without_client_poll_and_reauthorizes(tmp_path):
    jobs = Jobs()
    ext = extension(tmp_path, jobs)
    tasks = TaskClient()
    async with Client(
        server(ext), mode="2026-07-28", extensions=[tasks], elicitation_callback=host_form_ui
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "pushed-001"})
        tid = created["taskId"]
        async with tasks.listen(client.session, [tid]) as events:
            async with asyncio.timeout(3):
                initial = await anext(events)
            assert initial["taskId"] == tid and initial["status"] == "input_required"
            jobs.jobs[tid] = TaskObservation("completed", result={"content": []})
            await ext.changed(tid)
            async with asyncio.timeout(3):
                final = await anext(events)
            assert final["status"] == "completed" and final["result"]["content"] == []
            assert_wire(
                {"jsonrpc": "2.0", "method": "notifications/tasks", "params": final},
                "TaskStatusNotification",
            )
            jobs.revoked = True
            await ext.changed(tid)
            with pytest.raises(MCPError):
                async with asyncio.timeout(3):
                    await anext(events)
    assert jobs.starts == 1 and ext.subscription_middleware.streams == 0


async def test_foreign_subscription_and_missing_extension_are_rejected(tmp_path):
    jobs = Jobs()
    tasks = TaskClient()
    async with Client(
        server(extension(tmp_path, jobs)), mode="2026-07-28", extensions=[tasks]
    ) as c:
        tid = (await tasks.create(c.session, "export", {"request_id": "owner-push-001"}))["taskId"]
    async with Client(
        server(extension(tmp_path, jobs), owner="b" * 32), mode="2026-07-28", extensions=[tasks]
    ) as c:
        with pytest.raises(MCPError, match="unknown or expired"):
            async with tasks.listen(c.session, [tid]):
                pytest.fail("foreign task acknowledged")
    async with Client(server(extension(tmp_path, jobs)), mode="2026-07-28") as c:
        with pytest.raises(MCPError) as exc:
            async with tasks.listen(c.session, [tid]):
                pytest.fail("undeclared Tasks extension accepted")
        assert exc.value.code == -32021


async def test_url_relay_requires_capability_preserves_origin_and_discards_credentials():
    client = ApprovalClient(ElicitationCapability(url=UrlElicitationCapability()))

    async def navigate(message, url, elicitation_id, related_request_id):
        client.calls.append((url, elicitation_id, related_request_id))
        return ElicitResult(action="accept")

    client.elicit_url = navigate
    params = ElicitRequestURLParams(
        message="Sign in", url="https://example.invalid/login", elicitation_id="login-001"
    )
    result = await ElicitationTarget(client, 42).forward(params)
    assert result.action == "accept" and result.content is None
    assert client.calls == [(params.url, "login-001", 42)]


@pytest.mark.parametrize(
    "url",
    [
        "file:///secrets",
        "http://public.invalid",
        "javascript:run()",
        "https://user:password@example.invalid",
        "https://example.invalid/\n",
    ],
)
def test_url_validator_rejects_unsafe_navigation(url):
    with pytest.raises(ValueError):
        input_validator(ElicitRequestURLParams(message="Login", url=url, elicitation_id="x"))


async def test_task_url_input_requires_url_capability_and_never_accepts_credentials(tmp_path):
    from mcp.types import CallToolRequestParams

    jobs = Jobs()
    ext = extension(tmp_path, jobs, identity=lambda ctx: ctx.owner)
    ctx = context()
    created = await ext.intercept_tool_call(
        CallToolRequestParams(name="export", arguments={"request_id": "url-task-001"}), ctx, None
    )
    tid = created["taskId"]
    jobs.jobs[tid] = TaskObservation(
        "input_required",
        input_requests={
            "review": {
                "method": "elicitation/create",
                "params": {
                    "mode": "url",
                    "message": "Sign in",
                    "url": "https://identity.invalid/login",
                    "elicitationId": "login-001",
                },
            }
        },
    )
    with pytest.raises(MCPError) as denied:
        await ext.get_task(ctx, TaskParams(taskId=tid))
    assert denied.value.code == -32021
    ctx.session.client_capabilities.elicitation = ElicitationCapability(
        url=UrlElicitationCapability()
    )
    assert (await ext.get_task(ctx, TaskParams(taskId=tid)))["status"] == "input_required"
    with pytest.raises(MCPError):
        await ext.update_task(
            ctx,
            TaskUpdateParams(
                taskId=tid,
                inputResponses={
                    "review": {"action": "accept", "content": {"password": "never-forward"}}
                },
            ),
        )
    assert not jobs.updates
    await ext.update_task(
        ctx, TaskUpdateParams(taskId=tid, inputResponses={"review": {"action": "accept"}})
    )
    assert jobs.updates == [{"review": {"action": "accept"}}]
