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
from test_tasks_extension import Jobs, extension, host_form_ui, server

from universal_tools.elicitation import ElicitationTarget, input_validator
from universal_tools.tasks import TaskClient, TaskObservation


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
