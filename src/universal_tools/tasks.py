"""Durable, owner-bound MCP Tasks for explicitly integrated app-owned jobs.

This is the 2026-07-28 extension, not the retired experimental task protocol.
No operation is replayed by this module, including after a process restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from mcp.client.extension import ClientExtension
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver.server import require_client_extension
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    ElicitRequestFormParams,
    ElicitResult,
    Request,
    RequestParams,
    Result,
)
from pydantic import ConfigDict, Field

from .budgets import DEFAULT_JSON_BUDGET
from .elicitation import form_validator
from .identity import client_identity
from .journal import SqliteJournal
from .models import BrokerError, canonical_digest

TASKS = "io.modelcontextprotocol/tasks"
TERMINAL = {"completed", "failed", "cancelled"}
MAX_TASK_BYTES = 4 * 1024 * 1024


def _json(value):
    DEFAULT_JSON_BUDGET.check(value)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_TASK_BYTES:
        raise BrokerError("task data exceeds 4 MiB")
    return raw


def _timestamp():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    principal: str


@dataclass(frozen=True)
class TaskObservation:
    status: Literal["working", "input_required", "completed", "failed", "cancelled"]
    result: dict | None = None
    error: dict | None = None
    input_requests: dict = field(default_factory=dict)
    # True only if the app has reconciled the last recorded operation intent.
    reconciled: bool = False
    handle: str | None = None


class TaskAdapter(Protocol):
    """App implementations authorize every method and reconcile by task_id.

    start must durably bind task_id to the app job. inspect must work with a
    missing handle after a lost start reply. update/cancel must be idempotent
    at the app boundary. Exceptions never cause automatic redispatch.
    """

    async def authorize(self, context: TaskContext, handle: str | None) -> None: ...

    async def start(self, context: TaskContext, arguments: dict) -> str: ...

    async def inspect(
        self, context: TaskContext, handle: str | None, pending: dict | None
    ) -> TaskObservation: ...

    async def update(self, context: TaskContext, handle: str | None, responses: dict) -> None: ...

    async def cancel(self, context: TaskContext, handle: str | None) -> None: ...


@dataclass(frozen=True)
class TaskRoute:
    adapter: TaskAdapter
    revision: str
    validate: Callable[[dict], None]


class TaskParams(RequestParams):
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)


class TaskUpdateParams(TaskParams):
    input_responses: dict = Field(alias="inputResponses")


class TaskStore(SqliteJournal):
    """SQLite task state and permanent deduplication tombstones in app-private state."""

    def __init__(self, path: Path, *, capacity=10_000, max_storage_bytes=64 * 1024 * 1024):
        super().__init__(path)
        self.capacity = capacity
        self.max_storage_bytes = max_storage_bytes
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mcp_tasks (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, route TEXT NOT NULL, revision TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, version INTEGER NOT NULL,
                data TEXT NOT NULL, UNIQUE(owner, request_key))""")

    def create(self, owner, request_key, route, revision, arguments, ttl_ms):
        fingerprint = canonical_digest([route, revision, arguments])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT id, fingerprint FROM mcp_tasks WHERE owner=? AND request_key=?",
                (owner, request_key),
            ).fetchone()
            if old:
                if old[1] != fingerprint:
                    raise BrokerError("task request key was used with different arguments")
                return old[0], False
            if db.execute("SELECT COUNT(*) FROM mcp_tasks").fetchone()[0] >= self.capacity:
                raise BrokerError("task store capacity reached; operator archival required")
            task_id = secrets.token_urlsafe(32)
            stamp = _timestamp()
            data = {
                "wire": {
                    "taskId": task_id,
                    "status": "working",
                    "createdAt": stamp,
                    "lastUpdatedAt": stamp,
                    "ttlMs": ttl_ms,
                    "pollIntervalMs": 1000,
                },
                "handle": None,
                "pending": {"kind": "start"},
                "seen": {},
                "answered": [],
            }
            created = time.time()
            self._quota(db, _json(data))
            db.execute(
                "INSERT INTO mcp_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    task_id,
                    owner,
                    request_key,
                    fingerprint,
                    route,
                    revision,
                    created,
                    created + ttl_ms / 1000,
                    _json(data),
                ),
            )
        return task_id, True

    def load(self, task_id, owner):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM mcp_tasks WHERE id=? AND owner=?", (task_id, owner)
            ).fetchone()
        if row is None or row["expires"] <= time.time():
            raise BrokerError("unknown or expired task")
        record = dict(row)
        record["data"] = json.loads(record["data"])
        return record

    def save(self, record):
        raw = _json(record["data"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._quota(db, raw, replacing=record["id"])
            cursor = db.execute(
                "UPDATE mcp_tasks SET version=version+1, data=? WHERE id=? AND version=?",
                (raw, record["id"], record["version"]),
            )
            if cursor.rowcount != 1:
                raise BrokerError("task changed concurrently; poll before another action")
        record["version"] += 1

    def _quota(self, db, raw, replacing=""):
        used = db.execute(
            "SELECT COALESCE(SUM(length(CAST(data AS BLOB)) + 2048), 0) FROM mcp_tasks WHERE id!=?",
            (replacing,),
        ).fetchone()[0]
        if used + len(raw.encode("utf-8")) + 2048 > self.max_storage_bytes:
            raise BrokerError("task store byte quota reached; operator archival required")


class TaskExtension(Extension):
    """Intercept nominated, already-registered tools. All other calls pass through.

    Task support is explicit per request. Tools requiring Tasks reject clients
    lacking it before dispatch. The optional request-key callback must use a
    trusted caller's operation key (never JSON-RPC IDs, which can be reused).
    """

    identifier = TASKS

    def __init__(
        self,
        store,
        routes,
        *,
        identity=None,
        request_key=None,
        ttl_ms=86_400_000,
        operation_timeout=30,
    ):
        if not 1000 <= ttl_ms <= 30 * 86_400_000:
            raise ValueError("task TTL must be between one second and thirty days")
        if (
            not routes
            or len(routes) > 64
            or any(not 1 <= len(r.revision) <= 128 for r in routes.values())
        ):
            raise ValueError("tasks need bounded, revision-bound routes")
        if not 0 < operation_timeout <= 300:
            raise ValueError("invalid task adapter operation deadline")
        self.store = store
        self.routes = dict(routes)
        self.identity = identity or (lambda ctx: client_identity.get())
        self.request_key = request_key or (
            lambda ctx, params: (params.arguments or {}).get("request_id")
        )
        self.ttl_ms = ttl_ms
        self.operation_timeout = operation_timeout
        # Bounded stripes avoid a lock leak from guessed task IDs.
        self._locks = [asyncio.Lock() for _ in range(64)]

    def methods(self):
        modern = frozenset({"2026-07-28"})
        return (
            MethodBinding("tasks/get", TaskParams, self.get_task, modern),
            MethodBinding("tasks/update", TaskUpdateParams, self.update_task, modern),
            MethodBinding("tasks/cancel", TaskParams, self.cancel_task, modern),
        )

    def _principal(self, ctx):
        require_client_extension(ctx, TASKS)
        principal = self.identity(ctx)
        if not isinstance(principal, str) or not 1 <= len(principal) <= 4096:
            raise MCPError(INVALID_PARAMS, "authenticated task principal required")
        return principal, hashlib.sha256(principal.encode()).hexdigest()

    def _lock(self, task_id):
        return self._locks[int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % 64]

    async def _call(self, operation, *args):
        async with asyncio.timeout(self.operation_timeout):
            return await operation(*args)

    def _load(self, task_id, owner):
        record = self.store.load(task_id, owner)
        route = self.routes.get(record["route"])
        if route is None or route.revision != record["revision"]:
            raise BrokerError("task adapter revision unavailable; app reconciliation required")
        return record, route

    async def intercept_tool_call(self, params, ctx, call_next):
        route = self.routes.get(params.name)
        if route is None:
            return await call_next(ctx)
        # The extension's task result shape has no legacy representation.
        if ctx.protocol_version != "2026-07-28":
            raise MCPError(INVALID_PARAMS, "this tool requires the modern Tasks protocol")
        principal, owner = self._principal(ctx)
        arguments = params.arguments or {}
        try:
            _json(arguments)
            arguments = deepcopy(arguments)
            route.validate(arguments)
            await self._call(route.adapter.authorize, TaskContext("", principal), None)
            key = self.request_key(ctx, params)
            if not isinstance(key, str) or not 8 <= len(key) <= 128:
                raise BrokerError("task operation key must contain 8 to 128 characters")
            task_id, fresh = self.store.create(
                owner, key, params.name, route.revision, arguments, self.ttl_ms
            )
            if fresh:
                async with self._lock(task_id):
                    record, _ = self._load(task_id, owner)
                    # Persisted before dispatch. A lost response or cancellation
                    # leaves an intent for inspect(), never an automatic retry.
                    handle = await self._call(
                        route.adapter.start, TaskContext(task_id, principal), arguments
                    )
                    if not isinstance(handle, str) or not 1 <= len(handle) <= 4096:
                        raise BrokerError("app returned an invalid task handle")
                    record["data"]["handle"] = handle
                    record["data"]["pending"] = None
                    self.store.save(record)
            record, _ = self._load(task_id, owner)
            return {"resultType": "task", **deepcopy(record["data"]["wire"])}
        except BrokerError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from None
        except Exception:
            # Preserve a discoverable handle after ambiguous dispatch. App poll
            # reconciles by task_id even if start never returned its job handle.
            if "task_id" in locals():
                record, _ = self._load(task_id, owner)
                return {"resultType": "task", **deepcopy(record["data"]["wire"])}
            raise MCPError(INVALID_PARAMS, "task creation failed before dispatch") from None

    def _observe(self, record, observation):
        if not isinstance(observation, TaskObservation):
            raise BrokerError("invalid app task observation")
        data = record["data"]
        if data["wire"]["status"] in TERMINAL:
            return
        wire = data["wire"]
        status = observation.status
        if status not in {"working", "input_required", *TERMINAL}:
            raise BrokerError("invalid app task status")
        extras = {}
        if status == "completed":
            if observation.result is None:
                raise BrokerError("completed task needs its tool result")
            extras["result"] = CallToolResult.model_validate(observation.result).model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        elif status == "failed":
            from mcp.types import ErrorData

            if observation.error is None:
                raise BrokerError("failed task needs a JSON-RPC error")
            extras["error"] = ErrorData.model_validate(observation.error).model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        elif status == "input_required":
            requests = observation.input_requests
            _json(requests)
            if not requests or len(requests) > 8:
                raise BrokerError("task needs one to eight outstanding form requests")
            for key, request in requests.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 128:
                    raise BrokerError("invalid task input key")
                pending_answers = (data["pending"] or {}).get("responses", {})
                if (
                    key in data["answered"]
                    or (observation.reconciled and key in pending_answers)
                    or request.get("method") != "elicitation/create"
                ):
                    raise BrokerError("task input key reused or unsupported input method")
                form = ElicitRequestFormParams.model_validate(request.get("params"))
                if form.task is not None:
                    raise BrokerError("nested task elicitation is unsupported")
                form_validator(form)
                digest = canonical_digest(request)
                if key in data["seen"] and data["seen"][key] != digest:
                    raise BrokerError("task input key changed its request")
                data["seen"][key] = digest
            if len(data["seen"]) > 128:
                raise BrokerError("task lifetime form budget exceeded")
            extras["inputRequests"] = deepcopy(requests)
        # Keys that disappear are retired even if no response was submitted.
        for key in wire.get("inputRequests", {}):
            if key not in extras.get("inputRequests", {}) and key not in data["answered"]:
                data["answered"].append(key)
        for key in ("result", "error", "inputRequests"):
            wire.pop(key, None)
        wire.update(status=status, lastUpdatedAt=_timestamp(), **extras)
        if observation.handle is not None:
            if not isinstance(observation.handle, str) or not 1 <= len(observation.handle) <= 4096:
                raise BrokerError("invalid reconciled job handle")
            data["handle"] = observation.handle
        if observation.reconciled:
            pending = data["pending"] or {}
            for key in pending.get("responses", {}):
                if key not in data["answered"]:
                    data["answered"].append(key)
            data["pending"] = None
        self.store.save(record)

    async def _poll(self, record, route, principal):
        if record["data"]["wire"]["status"] not in TERMINAL:
            observation = await self._call(
                route.adapter.inspect,
                TaskContext(record["id"], principal),
                record["data"]["handle"],
                deepcopy(record["data"]["pending"]),
            )
            self._observe(record, observation)

    async def get_task(self, ctx, params):
        principal, owner = self._principal(ctx)
        try:
            async with self._lock(params.task_id):
                record, route = self._load(params.task_id, owner)
                await self._call(
                    route.adapter.authorize,
                    TaskContext(params.task_id, principal),
                    record["data"]["handle"],
                )
                await self._poll(record, route, principal)
                if record["data"]["wire"]["status"] == "input_required":
                    capability = ctx.session.client_capabilities.elicitation
                    if capability is None or (
                        capability.form is None and capability.url is not None
                    ):
                        raise MCPError(
                            -32021,
                            "task requires a form-capable client",
                            data={
                                "requiredCapabilities": {"elicitation": {"form": {}}},
                            },
                        )
                return {"resultType": "complete", **deepcopy(record["data"]["wire"])}
        except MCPError:
            raise
        except BrokerError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from None
        except Exception:
            raise MCPError(
                INVALID_PARAMS, "task observation unavailable; reconcile app state"
            ) from None

    async def update_task(self, ctx, params):
        return await self._action(ctx, params, "update")

    async def cancel_task(self, ctx, params):
        return await self._action(ctx, params, "cancel")

    async def _action(self, ctx, params, kind):
        principal, owner = self._principal(ctx)
        try:
            async with self._lock(params.task_id):
                record, route = self._load(params.task_id, owner)
                await self._call(
                    route.adapter.authorize,
                    TaskContext(params.task_id, principal),
                    record["data"]["handle"],
                )
                data = record["data"]
                if data["wire"]["status"] in TERMINAL:
                    return {"resultType": "complete"}
                if data["pending"] is not None:
                    raise BrokerError(
                        "previous task action needs app reconciliation before another action"
                    )
                responses = {}
                if kind == "update":
                    _json(params.input_responses)
                    outstanding = data["wire"].get("inputRequests", {})
                    for key, response in params.input_responses.items():
                        if key not in outstanding or key in data["answered"]:
                            continue
                        try:
                            form = ElicitRequestFormParams.model_validate(
                                outstanding[key]["params"]
                            )
                            answer = ElicitResult.model_validate(response)
                            if answer.action == "accept":
                                form_validator(form).validate(answer.content or {})
                        except Exception:
                            raise BrokerError(
                                "task response does not match its outstanding form"
                            ) from None
                        responses[key] = answer.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                        if answer.action != "accept":
                            responses[key].pop("content", None)
                    if not responses:
                        return {"resultType": "complete"}
                data["pending"] = {"kind": kind, **({"responses": responses} if responses else {})}
                self.store.save(record)
                context = TaskContext(params.task_id, principal)
                if kind == "cancel":
                    await self._call(route.adapter.cancel, context, data["handle"])
                else:
                    await self._call(route.adapter.update, context, data["handle"], responses)
                    data["answered"].extend(responses)
                    for key in responses:
                        data["wire"].get("inputRequests", {}).pop(key, None)
                    if not data["wire"].get("inputRequests"):
                        data["wire"].pop("inputRequests", None)
                        data["wire"]["status"] = "working"
                data["pending"] = None
                self.store.save(record)
                return {"resultType": "complete"}
        except MCPError:
            raise
        except BrokerError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from None
        except Exception:
            raise MCPError(
                INVALID_PARAMS, "task action outcome unknown; poll to reconcile, do not replay"
            ) from None


class TaskWireResult(Result):
    """Extension-owned results deliberately retained without core result sieving."""

    model_config = ConfigDict(extra="allow")
    result_type: Literal["task", "complete"] = Field(alias="resultType")


class TaskRequest(Request[dict, str]):
    name_param = "taskId"


class TaskClient(ClientExtension):
    """Explicit handle API. No implicit polling, tool retry or input acceptance.

    Pass this instance in Client(extensions=[...]); use these methods with
    client.session. The application controls persistence of returned task IDs
    and presents forms using its existing approval UI.
    """

    identifier = TASKS

    async def _request(self, session, method, params):
        request_type = TaskRequest if method.startswith("tasks/") else Request[dict, str]
        result = await session.send_request(
            request_type(method=method, params=params), TaskWireResult
        )
        value = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        DEFAULT_JSON_BUDGET.check(value)
        _json(value)
        if method == "tasks/get" or value["resultType"] == "task":
            if (
                not isinstance(value.get("taskId"), str)
                or not 1 <= len(value["taskId"]) <= 128
                or value.get("status") not in {"working", "input_required", *TERMINAL}
                or not isinstance(value.get("createdAt"), str)
                or not isinstance(value.get("lastUpdatedAt"), str)
                or "ttlMs" not in value
                or (value["ttlMs"] is not None and type(value["ttlMs"]) is not int)
            ):
                raise BrokerError("server returned invalid task metadata")
            if method == "tasks/get":
                if value["taskId"] != params["taskId"] or value["resultType"] != "complete":
                    raise BrokerError("server returned a different task or incomplete observation")
                if value["status"] == "completed":
                    CallToolResult.model_validate(value.get("result"))
                elif value["status"] == "failed":
                    from mcp.types import ErrorData

                    ErrorData.model_validate(value.get("error"))
                elif value["status"] == "input_required" and not isinstance(
                    value.get("inputRequests"), dict
                ):
                    raise BrokerError("task omitted its outstanding input requests")
        elif method in {"tasks/update", "tasks/cancel"} and value["resultType"] != "complete":
            raise BrokerError("task mutation was not acknowledged")
        return value

    async def create(self, session, name, arguments):
        return await self._request(session, "tools/call", {"name": name, "arguments": arguments})

    async def get(self, session, task_id):
        return await self._request(session, "tasks/get", {"taskId": task_id})

    async def update(self, session, task_id, responses):
        return await self._request(
            session, "tasks/update", {"taskId": task_id, "inputResponses": responses}
        )

    async def cancel(self, session, task_id):
        return await self._request(session, "tasks/cancel", {"taskId": task_id})
