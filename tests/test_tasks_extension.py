"""The released Tasks wire protocol against synthetic app-owned durable jobs."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
)

from universal_tools.extensions import ExtensionServer
from universal_tools.models import BrokerError
from universal_tools.tasks import (
    TASKS,
    TaskClient,
    TaskExtension,
    TaskObservation,
    TaskParams,
    TaskRoute,
    TaskStore,
)

FORM = {
    "method": "elicitation/create",
    "params": {
        "mode": "form",
        "message": "Approve synthetic export?",
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    },
}


async def host_form_ui(*args):
    raise AssertionError(
        "Tasks must expose forms through polling, not invoke callbacks automatically"
    )


class Jobs:
    def __init__(self):
        self.jobs = {}
        self.starts = 0
        self.updates = []
        self.cancels = 0
        self.lost_start = False
        self.lost_update = False
        self.revoked = False

    async def authorize(self, context, handle):
        if self.revoked:
            raise BrokerError("synthetic authorization revoked")

    async def start(self, context, arguments):
        self.starts += 1
        assert context.task_id not in self.jobs
        self.jobs[context.task_id] = TaskObservation(
            "input_required", input_requests={"review": FORM}
        )
        if self.lost_start:
            raise ConnectionError("private transport detail")
        return "job:" + context.task_id

    async def inspect(self, context, handle, pending):
        assert context.task_id in self.jobs
        observation = self.jobs[context.task_id]
        return TaskObservation(
            observation.status,
            result=observation.result,
            error=observation.error,
            input_requests=observation.input_requests,
            handle="job:" + context.task_id,
            reconciled=True,
        )

    async def update(self, context, handle, responses):
        self.updates.append(responses)
        accepted = responses["review"]["action"] == "accept"
        self.jobs[context.task_id] = TaskObservation(
            "completed",
            result={
                "content": [{"type": "text", "text": "export" if accepted else "declined"}],
                "isError": not accepted,
            },
        )
        if self.lost_update:
            raise ConnectionError("lost after app recorded response")

    async def cancel(self, context, handle):
        self.cancels += 1  # Cooperative request: job does not stop yet.


def assert_wire(value, definition):
    schema = json.loads(
        (Path(__file__).parent / "protocol/tasks-2026-07-28.schema.json").read_bytes()
    )
    Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}).validate(
        value
    )


def extension(tmp_path, jobs, **kwargs):
    def validate(args):
        if set(args) != {"request_id"}:
            raise BrokerError("invalid synthetic job arguments")

    return TaskExtension(
        TaskStore(tmp_path / "tasks.sqlite3"), {"export": TaskRoute(jobs, "v1", validate)}, **kwargs
    )


def server(ext, owner="a" * 32):
    result = ExtensionServer("Synthetic Tasks", extensions=[ext], frontend_identity=owner)

    @result.tool()
    async def export(request_id: str) -> dict:
        raise AssertionError("task route must not dispatch the synchronous handler")

    return result


async def test_real_wire_create_input_updates_cancel_and_immutable_terminal(tmp_path):
    jobs = Jobs()
    ext = extension(tmp_path, jobs)
    tasks = TaskClient()
    async with Client(
        server(ext),
        mode="2026-07-28",
        extensions=[tasks],
        elicitation_callback=host_form_ui,
        cache=None,
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "export-001"})
        assert created["resultType"] == "task"
        assert_wire(created, "CreateTaskResult")
        task_id = created["taskId"]
        assert task_id and created["status"] == "working"
        duplicate = await tasks.create(client.session, "export", {"request_id": "export-001"})
        assert duplicate["taskId"] == task_id and jobs.starts == 1
        first = await tasks.get(client.session, task_id)
        assert first["status"] == "input_required" and first["inputRequests"] == {"review": FORM}
        assert_wire(first, "GetTaskResult")
        with pytest.raises(MCPError):
            await tasks.update(
                client.session, task_id, {"review": {"action": "accept", "content": {}}}
            )
        assert not jobs.updates
        assert (await tasks.update(client.session, task_id, {"unknown": {}}))[
            "resultType"
        ] == "complete"
        assert (await tasks.cancel(client.session, task_id))["resultType"] == "complete"
        assert (await tasks.get(client.session, task_id))["status"] == "input_required"
        assert jobs.cancels == 1
        await tasks.update(
            client.session,
            task_id,
            {"review": {"action": "decline", "content": {"approved": True}}},
        )
        assert "content" not in jobs.updates[0]["review"]
        final = await tasks.get(client.session, task_id)
        assert_wire(final, "GetTaskResult")
        assert final["status"] == "completed" and final["result"]["isError"] is True
        jobs.jobs[task_id] = TaskObservation("cancelled")
        assert await tasks.get(client.session, task_id) == final
        await tasks.update(
            client.session, task_id, {"review": {"action": "accept", "content": {"approved": True}}}
        )
        assert len(jobs.updates) == 1


async def test_lost_start_and_update_reconcile_after_extension_restart_without_dispatch(tmp_path):
    jobs = Jobs()
    jobs.lost_start = True
    tasks = TaskClient()
    async with Client(
        server(extension(tmp_path, jobs)),
        mode="2026-07-28",
        extensions=[tasks],
        elicitation_callback=host_form_ui,
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "lost-job-001"})
    task_id = created["taskId"]
    restored = extension(tmp_path, jobs)
    async with Client(
        server(restored), mode="2026-07-28", extensions=[tasks], elicitation_callback=host_form_ui
    ) as client:
        duplicate = await tasks.create(client.session, "export", {"request_id": "lost-job-001"})
        assert duplicate["taskId"] == task_id
        assert (await tasks.get(client.session, task_id))["status"] == "input_required"
        jobs.lost_update = True
        with pytest.raises(MCPError, match="outcome unknown"):
            await tasks.update(
                client.session,
                task_id,
                {"review": {"action": "accept", "content": {"approved": True}}},
            )
    async with Client(
        server(extension(tmp_path, jobs)),
        mode="2026-07-28",
        extensions=[tasks],
        elicitation_callback=host_form_ui,
    ) as client:
        assert (await tasks.get(client.session, task_id))["status"] == "completed"
        await tasks.update(
            client.session, task_id, {"review": {"action": "accept", "content": {"approved": True}}}
        )
    assert jobs.starts == 1 and len(jobs.updates) == 1


async def test_per_request_capabilities_owner_identity_and_no_implicit_fallback(tmp_path):
    jobs = Jobs()
    ext = extension(tmp_path, jobs)
    tasks = TaskClient()
    async with Client(server(ext), mode="2026-07-28", cache=None) as client:
        with pytest.raises(MCPError) as error:
            await client.call_tool("export", {"request_id": "no-caps-001"})
        assert error.value.error.code == -32021
    async with Client(server(ext), mode="legacy") as client:
        with pytest.raises(MCPError):
            await client.call_tool("export", {"request_id": "legacy-001"})
    assert jobs.starts == 0
    async with Client(
        server(ext), mode="2026-07-28", extensions=[tasks], elicitation_callback=host_form_ui
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "owned-job-001"})
    task_id = created["taskId"]
    async with Client(
        server(ext, "b" * 32),
        mode="2026-07-28",
        extensions=[tasks],
        elicitation_callback=host_form_ui,
    ) as client:
        for operation in (
            tasks.get(client.session, task_id),
            tasks.cancel(client.session, task_id),
            tasks.update(client.session, task_id, {}),
        ):
            with pytest.raises(MCPError, match="unknown or expired"):
                await operation
    # Principal hashes, not raw identities, are persisted.
    raw = (tmp_path / "tasks.sqlite3").read_bytes()
    assert ("a" * 32).encode() not in raw and hashlib.sha256(
        ("a" * 32).encode()
    ).hexdigest().encode() in raw
    assert jobs.starts == 1 and jobs.cancels == 0


async def test_task_input_does_not_bypass_underlying_form_capability(tmp_path):
    jobs = Jobs()
    tasks = TaskClient()
    async with Client(
        server(extension(tmp_path, jobs)), mode="2026-07-28", extensions=[tasks]
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "no-form-ui-001"})
        with pytest.raises(MCPError) as error:
            await tasks.get(client.session, created["taskId"])
        assert error.value.error.code == -32021
    assert jobs.starts == 1 and not jobs.updates


def context(owner="owner", capabilities=True):
    return SimpleNamespace(
        protocol_version="2026-07-28",
        session=SimpleNamespace(
            client_capabilities=ClientCapabilities(
                extensions={TASKS: {}} if capabilities else {},
                elicitation=ElicitationCapability(form=FormElicitationCapability()),
            )
        ),
        owner=owner,
    )


async def test_store_expiry_quota_revision_and_request_key_conflicts(tmp_path, monkeypatch):
    jobs = Jobs()
    ext = extension(tmp_path, jobs, identity=lambda ctx: ctx.owner)
    params = CallToolRequestParams(name="export", arguments={"request_id": "request-001"})
    result = await ext.intercept_tool_call(params, context(), None)
    task_id = result["taskId"]
    # A different definition cannot silently poll an old job.
    ext.routes["export"] = TaskRoute(jobs, "v2", lambda args: None)
    with pytest.raises(MCPError, match="revision unavailable"):
        await ext.get_task(context(), TaskParams(taskId=task_id))
    ext.routes["export"] = TaskRoute(jobs, "v1", lambda args: None)
    with pytest.raises(MCPError, match="different arguments"):
        await ext.intercept_tool_call(
            params.model_copy(update={"arguments": {"request_id": "request-001", "changed": True}}),
            context(),
            None,
        )
    ext.store.capacity = 1
    with pytest.raises(MCPError, match="capacity"):
        await ext.intercept_tool_call(
            params.model_copy(update={"arguments": {"request_id": "request-002"}}), context(), None
        )
    monkeypatch.setattr("universal_tools.tasks.time.time", lambda: 1e20)
    with pytest.raises(MCPError, match="expired"):
        await ext.get_task(context(), TaskParams(taskId=task_id))
    assert jobs.starts == 1


async def test_json_rpc_errors_and_reused_form_keys_are_distinct(tmp_path):
    jobs = Jobs()
    ext = extension(tmp_path, jobs, identity=lambda ctx: ctx.owner)
    result = await ext.intercept_tool_call(
        CallToolRequestParams(name="export", arguments={"request_id": "failure-001"}),
        context(),
        None,
    )
    task_id = result["taskId"]
    jobs.jobs[task_id] = TaskObservation(
        "failed", error={"code": -32603, "message": "synthetic failure"}
    )
    assert (await ext.get_task(context(), TaskParams(taskId=task_id)))["error"]["code"] == -32603
    jobs.revoked = True
    with pytest.raises(MCPError, match="revoked"):
        await ext.get_task(context(), TaskParams(taskId=task_id))


async def test_superseded_form_key_cannot_reappear_and_duplicate_starts_race_once(tmp_path):
    import asyncio

    jobs = Jobs()
    ext = extension(tmp_path, jobs, identity=lambda ctx: ctx.owner)
    params = CallToolRequestParams(name="export", arguments={"request_id": "racing-job-001"})
    results = await asyncio.gather(
        *(ext.intercept_tool_call(params, context(), None) for _ in range(8))
    )
    assert jobs.starts == 1 and len({result["taskId"] for result in results}) == 1
    task_id = results[0]["taskId"]
    await ext.get_task(context(), TaskParams(taskId=task_id))
    jobs.jobs[task_id] = TaskObservation("working")
    await ext.get_task(context(), TaskParams(taskId=task_id))
    jobs.jobs[task_id] = TaskObservation("input_required", input_requests={"review": FORM})
    with pytest.raises(MCPError, match="reused"):
        await ext.get_task(context(), TaskParams(taskId=task_id))


async def test_start_deadline_preserves_intent_for_reconciliation_and_byte_quota(tmp_path):
    import asyncio

    jobs = Jobs()
    original = jobs.start

    async def delayed(context, args):
        await original(context, args)
        await asyncio.sleep(10)

    jobs.start = delayed
    ext = extension(tmp_path, jobs, identity=lambda ctx: ctx.owner, operation_timeout=0.02)
    params = CallToolRequestParams(name="export", arguments={"request_id": "timeout-job-001"})
    result = await ext.intercept_tool_call(params, context(), None)
    task_id = result["taskId"]
    assert (await ext.get_task(context(), TaskParams(taskId=task_id)))["status"] == "input_required"
    await ext.intercept_tool_call(params, context(), None)
    assert jobs.starts == 1
    ext.store.max_storage_bytes = 1
    with pytest.raises(MCPError, match="quota"):
        await ext.intercept_tool_call(
            params.model_copy(update={"arguments": {"request_id": "quota-job-001"}}),
            context(),
            None,
        )
    assert jobs.starts == 1


async def test_extension_lifecycle_cleanup_and_failure(tmp_path):
    events = []
    from contextlib import asynccontextmanager

    from mcp.server.extension import Extension

    class Managed(Extension):
        identifier = "org.example/managed"

        @asynccontextmanager
        async def lifespan(self, server):
            events.append("start")
            try:
                yield
            finally:
                events.append("stop")

    async with Client(ExtensionServer("Managed", extensions=[Managed()]), mode="2026-07-28"):
        assert events == ["start"]
    assert events == ["start", "stop"]
    with pytest.raises(ValueError):
        ExtensionServer("Duplicate", extensions=[Managed(), Managed()])
