"""Job handles belong to a separate fake backend, independently of MCP sessions."""

import asyncio
import copy
import json
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_elicitation_integration import frontends
from test_integration import BROKER_TEST_OPERATIONS, data
from test_integration import stack as stack

from universal_tools.models import atomic_json
from universal_tools.ownership import OwnershipJournal
from universal_tools.surface import RemoteBackend


class JobBackend:
    """No native engine, files or devices; only explicit in-memory transitions."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.calls = []
        self.revision = 0
        self.stop = threading.Event()

    def transition(self, job_id, status):
        with self.lock:
            self.jobs[job_id]["status"] = status

    def dispatch(self, method, path, body):
        with self.lock:
            self.calls.append((method, path, copy.deepcopy(body)))
            if method == "GET" and path == "/jobs":
                return 200, {"jobs": copy.deepcopy(list(self.jobs.values()))}, 0
            if method == "POST" and path == "/jobs":
                if body["target"] != "fixture-target":
                    return 400, {"error": "unknown synthetic target"}, 0
                if body["expected_revision"] != self.revision:
                    return 409, {"error": "stale revision"}, 0
                job_id = f"job-{len(self.jobs) + 1}"
                self.revision += 1
                self.jobs[job_id] = dict(
                    job_id=job_id, target=body["target"], revision=self.revision, status="queued"
                )
                return 200, copy.deepcopy(self.jobs[job_id]), body.get("delay_reply", 0)
            pieces = path.strip("/").split("/")
            if len(pieces) >= 2 and pieces[0] == "jobs" and pieces[1] in self.jobs:
                job = self.jobs[pieces[1]]
                if method == "GET" and len(pieces) == 2:
                    return 200, copy.deepcopy(job), 0
                if method == "POST" and pieces[2:] == ["cancel"]:
                    job["status"] = "cancel_requested"
                    return 200, copy.deepcopy(job), 0
            return 404, {"error": "unknown synthetic route or job"}, 0


@pytest.fixture
def job_backend():
    backend = JobBackend()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond("GET", None)

        def do_POST(self):
            self.respond("POST", json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def respond(self, method, body):
            status, value, delay = backend.dispatch(method, self.path, body)
            # Let fixture shutdown end only this fake HTTP wait; cancellation
            # of an adapter request never changes the independently owned job.
            backend.stop.wait(delay)
            raw = json.dumps(value).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield backend, f"http://127.0.0.1:{server.server_port}"
    finally:
        backend.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@asynccontextmanager
async def job_app(stack, url, *, timeout=20, session_mode="exclusive"):
    path = stack[0] / "services/jobs.json"
    atomic_json(
        path,
        dict(
            id="jobs",
            display_name="Synthetic background jobs",
            transport=dict(
                type="stdio",
                command=sys.executable,
                args=[str(Path(__file__).parent / "fixtures/job_mcp.py"), "--backend", url],
            ),
            session_mode=session_mode,
            allow_writes=True,
            allowed_write_tools=["start_job", "cancel_job"],
            timeout_seconds=timeout,
        ),
    )
    try:
        async with RemoteBackend(stack[1]) as admin:
            services = data(await admin.invoke("refresh_services", {}))["services"]
            status = next(s for s in services if s["service_id"] == "jobs")
            assert status["status"] == "available", status
        yield
    finally:
        path.unlink()
        # Tests may restart only their own broker during reconciliation;
        # its previous HTTP sessions must remain invalid after that restart.
        async with RemoteBackend(stack[1]) as admin:
            data(await admin.invoke("refresh_services", {}))
        reservation = OwnershipJournal(stack[0] / "ownership.sqlite3").get("jobs")
        if reservation:
            await asyncio.to_thread(
                BROKER_TEST_OPERATIONS[stack[0]]["recover"],
                "jobs",
                reservation["context_id"],
            )


async def acquire(client):
    return data(await client.invoke("acquire_service", {"service_id": "jobs"}))["context_id"]


async def call(client, context, name, arguments=None, *, request_id=None):
    description = data(await client.invoke("describe_tool", {"tool_id": "jobs/" + name}))
    args = dict(
        tool_id="jobs/" + name,
        catalog_revision=description["catalog_revision"],
        arguments=arguments or {},
        context_id=context,
    )
    if request_id is not None:
        args["request_id"] = request_id
    return await client.invoke("call_write_tool" if request_id else "call_read_tool", args)


async def test_background_handles_poll_without_replay_or_foreign_context_access(stack, job_backend):
    backend, url = job_backend
    async with job_app(stack, url), frontends(stack[0], None, None) as (owner, other):
        context = await acquire(owner)
        args = {"target": "fixture-target", "expected_revision": 0}
        started = data(await call(owner, context, "start_job", args, request_id="job-start"))
        job_id = started["job_id"]
        assert started["status"] == "queued"
        dispatched = len(backend.calls)
        assert (await call(owner, context, "start_job", args, request_id="job-start")).isError
        assert (await call(other, context, "get_job", {"job_id": job_id})).isError
        assert (
            await call(other, context, "cancel_job", {"job_id": job_id}, request_id="job-steal")
        ).isError
        assert len(backend.calls) == dispatched
        data(await other.invoke("refresh_services", {}))
        backend.transition(job_id, "running")
        assert (
            data(await call(owner, context, "get_job", {"job_id": job_id}))["status"] == "running"
        )
        backend.transition(job_id, "succeeded")
        assert data(await call(owner, context, "list_jobs"))["jobs"][0]["status"] == "succeeded"
        assert len(backend.jobs) == 1


async def test_job_validation_and_app_revisions_are_separate_from_request_receipts(
    stack, job_backend
):
    backend, url = job_backend
    async with job_app(stack, url), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        assert (
            await call(
                owner, context, "start_job", {"target": "fixture-target"}, request_id="job-bad"
            )
        ).isError
        assert not backend.calls
        assert (await owner.invoke("get_request_status", {"request_id": "job-bad"})).isError
        args = {"target": "fixture-target", "expected_revision": 0}
        # Routing a start through the read channel is rejected before HTTP.
        assert (await call(owner, context, "start_job", args)).isError and not backend.calls
        data(await call(owner, context, "start_job", args, request_id="job-valid"))
        assert (await call(owner, context, "start_job", args, request_id="job-stale")).isError
        assert (
            data(await owner.invoke("get_request_status", {"request_id": "job-stale"}))["status"]
            == "app_error"
        )
        assert len(backend.jobs) == 1


async def test_job_cancellation_is_a_request_until_the_backend_reports_an_outcome(
    stack, job_backend
):
    backend, url = job_backend
    async with job_app(stack, url), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        job = data(
            await call(
                owner,
                context,
                "start_job",
                {"target": "fixture-target", "expected_revision": 0},
                request_id="job-to-cancel",
            )
        )
        args = {"job_id": job["job_id"]}
        backend.transition(job["job_id"], "running")
        assert (
            data(await call(owner, context, "cancel_job", args, request_id="job-cancel"))["status"]
            == "cancel_requested"
        )
        assert data(await call(owner, context, "get_job", args))["status"] == "cancel_requested"
        backend.transition(job["job_id"], "indeterminate")
        assert data(await call(owner, context, "get_job", args))["status"] == "indeterminate"
        assert (
            data(await owner.invoke("get_request_status", {"request_id": "job-to-cancel"}))[
                "status"
            ]
            == "completed"
        )  # The start receipt acknowledges a handle, not the job's final outcome.


async def test_releasing_an_adapter_does_not_cancel_or_hide_backend_owned_jobs(stack, job_backend):
    backend, url = job_backend
    async with job_app(stack, url), frontends(stack[0], None, None) as (owner, other):
        context = await acquire(owner)
        pid = data(await call(owner, context, "adapter_info"))["pid"]
        job = data(
            await call(
                owner,
                context,
                "start_job",
                {"target": "fixture-target", "expected_revision": 0},
                request_id="job-outlives-adapter",
            )
        )
        data(await owner.invoke("release_service", {"service_id": "jobs", "context_id": context}))
        replacement = await acquire(other)
        assert replacement != context
        assert data(await call(other, replacement, "adapter_info"))["pid"] != pid
        assert data(await call(other, replacement, "get_job", {"job_id": job["job_id"]})) == job
        assert [method for method, _, _ in backend.calls] == ["POST", "GET"]


async def test_lost_start_reply_requires_reconciliation_of_the_existing_backend_job(
    stack, job_backend
):
    backend, url = job_backend
    async with job_app(stack, url, timeout=5):
        async with frontends(stack[0], None, None) as (owner, other):
            context = await acquire(owner)
            args = {"target": "fixture-target", "expected_revision": 0, "delay_reply": 30}
            began = time.monotonic()
            assert (
                await call(owner, context, "start_job", args, request_id="job-lost-start")
            ).isError
            assert time.monotonic() - began < 20
            assert (
                data(await owner.invoke("get_request_status", {"request_id": "job-lost-start"}))[
                    "status"
                ]
                == "uncertain"
            )

            status = data(await owner.invoke("service_session_status", {"service_id": "jobs"}))
            assert status["state"] == "lost" and status["recovery_required"]
            assert (await other.invoke("acquire_service", {"service_id": "jobs"})).isError
            assert (
                await call(owner, context, "start_job", args, request_id="job-lost-start")
            ).isError
            assert (
                await call(owner, context, "start_job", args, request_id="job-no-retry")
            ).isError
            assert len(backend.jobs) == 1 and len(backend.calls) == 1
        # Reconcile only this fixture's fake backend and stopped broker. A
        # real acknowledgement needs app-owned inspection and authorization.
        await asyncio.to_thread(BROKER_TEST_OPERATIONS[stack[0]]["recover"], "jobs", context)
        async with frontends(stack[0], None) as (owner,):
            replacement = await acquire(owner)
            jobs = data(await call(owner, replacement, "list_jobs"))["jobs"]
            assert jobs == list(backend.jobs.values()) and len(jobs) == 1
            assert (
                data(await owner.invoke("get_request_status", {"request_id": "job-lost-start"}))[
                    "status"
                ]
                == "uncertain"
            )


async def test_transient_frontends_share_backend_jobs_with_separate_adapter_processes(
    stack, job_backend
):
    backend, url = job_backend
    async with (
        job_app(stack, url, session_mode="transient"),
        frontends(stack[0], None, None) as (first, second),
    ):
        pid = data(await call(first, None, "adapter_info"))["pid"]
        assert data(await call(first, None, "adapter_info"))["pid"] != pid
        assert (await first.invoke("acquire_service", {"service_id": "jobs"})).isError
        job = data(
            await call(
                first,
                None,
                "start_job",
                {"target": "fixture-target", "expected_revision": 0},
                request_id="shared-backend-start",
            )
        )
        results = await asyncio.gather(
            call(first, None, "get_job", {"job_id": job["job_id"]}),
            call(second, None, "get_job", {"job_id": job["job_id"]}),
        )
        assert [data(result) for result in results] == [job, job]
        # This fake app intentionally has shared job visibility. The broker
        # does not add per-job ownership or forward a frontend principal.
        cancelled = data(
            await call(
                second,
                None,
                "cancel_job",
                {"job_id": job["job_id"]},
                request_id="shared-backend-cancel",
            )
        )
        assert cancelled["status"] == "cancel_requested"
        assert data(await call(first, None, "list_jobs"))["jobs"] == [cancelled]
        assert len(backend.jobs) == 1
        assert OwnershipJournal(stack[0] / "ownership.sqlite3").get("jobs") is None


async def test_transient_lost_reply_retains_claim_without_creating_an_exclusive_barrier(
    stack, job_backend
):
    backend, url = job_backend
    async with (
        job_app(stack, url, session_mode="transient", timeout=10),
        frontends(stack[0], None, None) as (first, second),
    ):
        args = {"target": "fixture-target", "expected_revision": 0, "delay_reply": 30}
        assert (
            await call(first, None, "start_job", args, request_id="transient-lost-start")
        ).isError
        assert (
            data(await second.invoke("get_request_status", {"request_id": "transient-lost-start"}))[
                "status"
            ]
            == "uncertain"
        )
        assert OwnershipJournal(stack[0] / "ownership.sqlite3").get("jobs") is None
        # Only read the existing fake job after the lost reply. Do not issue
        # another start or infer native cancellation from adapter teardown.
        assert data(await call(second, None, "list_jobs"))["jobs"] == list(backend.jobs.values())
        assert (
            await call(second, None, "start_job", args, request_id="transient-lost-start")
        ).isError
        assert len(backend.jobs) == 1
        assert sum(method == "POST" and path == "/jobs" for method, path, _ in backend.calls) == 1


async def test_shared_frontends_reuse_adapter_and_app_owned_jobs_without_context_handles(
    stack, job_backend
):
    backend, url = job_backend
    async with (
        job_app(stack, url, session_mode="shared"),
        frontends(stack[0], None, None) as (first, second),
    ):
        pid = data(await call(first, None, "adapter_info"))["pid"]
        assert data(await call(second, None, "adapter_info"))["pid"] == pid
        assert (await first.invoke("acquire_service", {"service_id": "jobs"})).isError
        job = data(
            await call(
                first,
                None,
                "start_job",
                {"target": "fixture-target", "expected_revision": 0},
                request_id="retained-shared-job",
            )
        )
        data(await second.invoke("refresh_services", {}))
        results = await asyncio.gather(
            call(first, None, "get_job", {"job_id": job["job_id"]}),
            call(second, None, "get_job", {"job_id": job["job_id"]}),
        )
        assert [data(result) for result in results] == [job, job]
        cancelled = data(
            await call(
                second,
                None,
                "cancel_job",
                {"job_id": job["job_id"]},
                request_id="retained-shared-cancel",
            )
        )
        assert cancelled["status"] == "cancel_requested"
        assert data(await call(first, None, "adapter_info"))["pid"] == pid
        assert data(await call(first, None, "list_jobs"))["jobs"] == [cancelled]
        status = data(await second.invoke("service_session_status", {"service_id": "jobs"}))
        assert status["state"] == "shared_active" and "context_id" not in status
        assert len(backend.jobs) == 1


async def test_shared_lost_start_reply_blocks_both_chats_without_an_automatic_restart(
    stack, job_backend
):
    backend, url = job_backend
    async with (
        job_app(stack, url, session_mode="shared", timeout=10),
        frontends(stack[0], None, None) as (first, second),
    ):
        data(await call(first, None, "adapter_info"))  # Warm initialization separately.
        assert (
            await call(
                first,
                None,
                "start_job",
                {"target": "fixture-target", "expected_revision": 0, "delay_reply": 30},
                request_id="retained-shared-lost",
            )
        ).isError
        for client in (first, second):
            status = data(await client.invoke("service_session_status", {"service_id": "jobs"}))
            assert status["recovery_required"] and "context_id" not in status
            assert (await call(client, None, "list_jobs")).isError
        receipt = data(
            await second.invoke("get_request_status", {"request_id": "retained-shared-lost"})
        )
        assert receipt["status"] == "uncertain"
        assert len(backend.jobs) == 1 and len(backend.calls) == 1
