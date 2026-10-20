"""Reusable app-owned MCP host with a fixed front door and controlled backend publication.

The shared package is an app dependency. No universal daemon or Codex plugin is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import uuid
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .broker import Broker
from .daemon import InstanceLock, ProtectedApp
from .identity import client_identity, frontend_connection
from .models import (
    ID_PATTERN,
    MAX_MANIFEST_BYTES,
    MAX_REQUEST_BYTES,
    BrokerError,
    ServiceManifest,
    StrictModel,
    atomic_json,
    canonical_digest,
    load_settings,
)
from .surface import LocalBackend, create_surface, error_result, json_result


class WorkerArtifact(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def verify(self):
        path = Path(self.path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise BrokerError("backend artifact must be an ordinary absolute file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != self.sha256:
            raise BrokerError("backend artifact changed; publish an explicit new revision")


class BackendPublication(StrictModel):
    revision: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    service: ServiceManifest
    artifact: WorkerArtifact | None = None

    @model_validator(mode="after")
    def entrypoint(self):
        transport = self.service.transport
        if transport.type == "stdio":
            if self.artifact is None or self.artifact.path not in [
                transport.command,
                *transport.args,
            ]:
                raise ValueError("stdio publication must pin its executable or entrypoint argument")
        elif self.artifact is not None:
            raise ValueError("HTTP deployment is app-owned; no local artifact is launched")
        return self

    def effective(self):
        value = self.service.model_dump(mode="json", by_alias=True)
        value["description"] += "\nBackend publication: " + self.revision
        return ServiceManifest.model_validate(value)

    def verify(self):
        if self.service.enabled and self.artifact is not None:
            self.artifact.verify()


class ApplicationDefinition(StrictModel):
    schema_version: Literal[1]
    id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=4096)
    backends: list[BackendPublication] = Field(default_factory=list, max_length=32)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def unique(self):
        names = [backend.service.id for backend in self.backends]
        if len(names) != len(set(names)):
            raise ValueError("backend service IDs must be unique")
        for backend in self.backends:
            backend.effective()
        return self


def load_application(path):
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BrokerError("application publication exceeds 256 KiB")
    return ApplicationDefinition.model_validate_json(path.read_bytes())


class ApplicationClient:
    """One trusted in-process frontend; identity is never accepted in model tool arguments."""

    def __init__(self, host):
        self._host = host
        self._identity = secrets.token_urlsafe(32)

    async def invoke(self, name, arguments, *, relay=None):
        token = client_identity.set(self._identity)
        try:
            return await self._host.backend.invoke(name, arguments, relay=relay)
        finally:
            client_identity.reset(token)


class ApplicationBackend(LocalBackend):
    def __init__(self, host):
        super().__init__(host._engine)
        self.host = host
        self.native_connections = False
        self._identities = weakref.WeakKeyDictionary()

    async def invoke(self, name, arguments, *, relay=None):
        identity = client_identity.get()
        if identity is None and self.native_connections:
            session = frontend_connection.get()
            if session is None:
                return error_result(
                    "use a retained MCP connection or an authenticated app frontend"
                )
            identity = self._identities.setdefault(session, secrets.token_urlsafe(32))
        if identity is None:
            return error_result("use an ApplicationClient or an active MCP session")
        token = client_identity.set(identity)
        try:
            if name == "refresh_services":
                return json_result(await self.host.refresh())
            service_id = arguments.get("service_id")
            if not service_id and isinstance(arguments.get("tool_id"), str):
                service_id = arguments["tool_id"].partition("/")[0]
            if service_id:
                async with self.host.gate(service_id):
                    self.host._require_started()
                    selected = self.host._selected.get(service_id)
                    if selected and name in {
                        "acquire_service",
                        "call_read_tool",
                        "call_write_tool",
                        "refresh_capabilities",
                    }:
                        selected.verify()
                    return await super().invoke(name, arguments, relay=relay)
            self.host._require_started()
            result = await super().invoke(name, arguments, relay=relay)
            if name == "broker_status" and not result.is_error:
                return json_result({**result.structured_content, **self.host.publication_status()})
            return result
        except BrokerError as error:
            return error_result(str(error))
        except (ValueError, OSError):
            return error_result(
                "app backend/publication is unavailable; inspect its local deployment"
            )
        finally:
            client_identity.reset(token)


class ApplicationHost:
    """App-owned host reusing broker policy, sessions, forms, catalogs and durable receipts.

    Use a dedicated state directory and an app-controlled publication file. Only backends
    change on refresh. The app owns native code, authorization and update/reconciliation UI.
    """

    def __init__(self, state, publication, *, max_request_body_bytes=MAX_REQUEST_BYTES):
        self.state = Path(state).resolve()
        self.publication = Path(publication).resolve()
        self.definition = load_application(self.publication)
        self.root = self.state / "framework"
        self._engine = Broker(self.state, max_request_body_bytes=max_request_body_bytes)
        self.backend = ApplicationBackend(self)
        self._selected = {}
        self._gates = {}
        self._refresh_lock = asyncio.Lock()
        self._instance_lock = None
        self._started = False
        self._last = []

    def client(self):
        return ApplicationClient(self)

    def gate(self, service_id):
        return self._gates.setdefault(service_id, asyncio.Lock())

    def _require_started(self):
        if not self._started:
            raise BrokerError("application host is not running")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _snapshot(self, selected, *, definition=None):
        value = (definition or self.definition).model_copy(
            update={"backends": [selected[k] for k in sorted(selected)]}
        )
        raw = value.model_dump(mode="json", by_alias=True)
        revision = canonical_digest(raw)
        destination = self.root / "releases" / revision
        if not destination.exists():
            stage = self.root / "releases" / ("stage-" + uuid.uuid4().hex)
            stage.mkdir(parents=True)
            (stage / "services").mkdir()
            atomic_json(stage / "definition.json", raw)
            for backend in value.backends:
                atomic_json(
                    stage / "services" / (backend.service.id + ".json"),
                    backend.effective().model_dump(mode="json", by_alias=True),
                )
            stage.rename(destination)
        if load_application(destination / "definition.json") != value:
            raise BrokerError("framework release definition was modified")
        expected = {
            backend.service.id + ".json": backend.effective().model_dump(mode="json", by_alias=True)
            for backend in value.backends
        }
        actual = {
            p.name: json.loads(p.read_bytes()) for p in (destination / "services").glob("*.json")
        }
        if expected != actual:
            raise BrokerError("framework release registrations were modified")
        return revision, destination / "services"

    async def start(self):
        if self._started or self._engine._closed:
            raise BrokerError("create a new ApplicationHost for a new lifecycle")
        lock = InstanceLock(self.state / "broker.lock")
        lock.__enter__()
        self._instance_lock = lock
        try:
            if any((self.state / "services").glob("*.json")):
                raise BrokerError(
                    "application host needs dedicated state, not broker registrations"
                )
            active = self.root / "active.json"
            if active.exists():
                pointer = json.loads(active.read_bytes())
                if pointer.get("application_id") != self.definition.id:
                    raise BrokerError("framework state belongs to a different application")
                revision = pointer["release_id"]
                if (
                    not isinstance(revision, str)
                    or len(revision) != 64
                    or any(c not in "0123456789abcdef" for c in revision)
                ):
                    raise BrokerError("invalid active framework release")
                previous = load_application(self.root / "releases" / revision / "definition.json")
                self._selected = {b.service.id: b for b in previous.backends}
                checked, _ = self._snapshot(self._selected, definition=previous)
                if checked != revision or previous.id != self.definition.id:
                    raise BrokerError("framework identity changed; use a separate state")
            _, self._engine.registry = self._snapshot(self._selected)
            await self._engine.start()
            self._started = True
            await self.refresh()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        self._started = False
        try:
            await self._engine.aclose()
        finally:
            if self._instance_lock is not None:
                self._instance_lock.__exit__()
                self._instance_lock = None

    def _reload_blocker(self, service_id, previous):
        if service_id in self._engine._leases:
            return "retained_context_requires_release_or_app_shutdown"
        if previous:
            if self._engine._recovery_blocker(previous.effective()):
                return "operator_recovery_required"
            prefix = service_id + "/"
            acknowledgments = self.root / "reconciled-writes.json"
            acknowledged = (
                json.loads(acknowledgments.read_bytes()) if acknowledgments.exists() else {}
            )
            with self._engine.journal.connect() as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM requests WHERE substr(tool_id,1,?)=? AND status!='completed'",
                    (len(prefix), prefix),
                ).fetchall()
            if any(
                acknowledged.get(row["request_id"]) != canonical_digest(dict(row)) for row in rows
            ):
                return "app_write_reconciliation_required"
        return None

    async def acknowledge_reload_outcome(self, request_id, *, expected_updated_at):
        """Local app-admin API, after app-owned verification; not an MCP tool or write retry.

        This records only reload reconciliation. It cannot clear ownership barriers or
        change the original write receipt, and it never proves that native work stopped.
        """
        self._require_started()
        row = self._engine.journal.status(request_id)
        service_id = row["tool_id"].partition("/")[0]
        async with self._refresh_lock, self.gate(service_id):
            row = self._engine.journal.status(request_id)
            if row["updated_at"] != expected_updated_at:
                raise BrokerError("write receipt changed; inspect/reconcile the current outcome")
            path = self.root / "reconciled-writes.json"
            value = json.loads(path.read_bytes()) if path.exists() else {}
            value[request_id] = canonical_digest(row)
            atomic_json(path, value)

    async def retire_shared_backend(self, service_id, *, expected_revision, app_quiescent=False):
        """App-admin API after saving/quiescing app jobs; not exposed as a model tool.

        Private contexts must be released by their frontend owner. This only closes a
        healthy shared adapter; it neither cancels native jobs nor clears recovery barriers.
        """
        self._require_started()
        if app_quiescent is not True:
            raise BrokerError("app-owned quiescence verification is required before retirement")
        async with self._refresh_lock, self.gate(service_id), self._engine.lock(service_id):
            backend = self._selected.get(service_id)
            if backend is None or backend.revision != expected_revision:
                raise BrokerError("backend publication changed; inspect its current revision")
            if backend.service.session_mode != "shared":
                raise BrokerError("private contexts must be released by their frontend owner")
            self._engine._check_recovery(backend.effective())
            lease = self._engine._leases.get(service_id)
            if lease is None:
                return {"service_id": service_id, "closed": False}
            if lease.lost or not lease.worker.alive or not await self._engine._retire(service_id):
                raise BrokerError("shared adapter was lost; operator recovery is required")
            return {"service_id": service_id, "closed": True}

    def publication_status(self):
        return {"application_id": self.definition.id, "backend_publications": list(self._last)}

    async def refresh(self):
        self._require_started()
        async with self._refresh_lock:
            desired = load_application(self.publication)
            if desired.model_dump(exclude={"backends"}) != self.definition.model_dump(
                exclude={"backends"}
            ):
                raise BrokerError("application identity/instructions require an app host restart")
            proposed = {b.service.id: b for b in desired.backends}
            changed = sorted(
                k
                for k in proposed.keys() | self._selected.keys()
                if proposed.get(k) != self._selected.get(k)
            )
            selected, status = dict(self._selected), []
            async with AsyncExitStack() as stack:
                for key in changed:
                    await stack.enter_async_context(self.gate(key))
                    blocker = self._reload_blocker(key, self._selected.get(key))
                    if blocker:
                        status.append({"service_id": key, "status": "deferred", "reason": blocker})
                        continue
                    candidate = proposed.get(key)
                    try:
                        if candidate:
                            previous = self._selected.get(key)
                            if previous and previous.revision == candidate.revision:
                                raise BrokerError(
                                    "changed backend definitions require a new revision"
                                )
                            candidate.verify()
                            selected[key] = candidate
                        else:
                            selected.pop(key, None)
                        status.append({"service_id": key, "status": "applied"})
                    except (ValueError, OSError):
                        status.append(
                            {"service_id": key, "status": "rejected", "reason": "invalid_candidate"}
                        )
                old_registry = self._engine.registry
                try:
                    for backend in selected.values():
                        backend.verify()
                    revision, self._engine.registry = self._snapshot(selected)
                    result = await self._engine.refresh()
                    unavailable = {
                        row["service_id"]
                        for row in result["services"]
                        if row["status"] not in {"available", "disabled"}
                    }
                    failed = {
                        row["service_id"]
                        for row in status
                        if row["status"] == "applied" and row["service_id"] in unavailable
                    }
                    if failed:
                        for key in failed:
                            if key in self._selected:
                                selected[key] = self._selected[key]
                            else:
                                selected.pop(key, None)
                        for row in status:
                            if row["service_id"] in failed:
                                row.update(status="rejected", reason="candidate_discovery_failed")
                        revision, self._engine.registry = self._snapshot(selected)
                        result = await self._engine.refresh()
                    atomic_json(
                        self.root / "active.json",
                        {"application_id": self.definition.id, "release_id": revision},
                    )
                    self._selected, self._last = selected, status
                    return {**result, **self.publication_status()}
                except BaseException:
                    self._engine.registry = old_registry
                    # No new candidate can dispatch after cancellation/failed publication.
                    self._engine.catalog = {
                        k: v
                        for k, v in self._engine.catalog.items()
                        if v.manifest.id not in changed
                    }
                    raise

    def create_server(
        self, *, port=8768, frontend_identity=None, skills=None, extensions=(), deployment=None
    ):
        server = create_surface(
            self.backend,
            port=port,
            name=self.definition.display_name,
            instructions=self.definition.instructions,
            frontend_identity=frontend_identity,
            skills=skills,
            extensions=extensions,
            deployment=deployment,
        )
        self.backend.native_connections = True
        return server


def serve_application(state, publication, *, skills=None):
    """Authenticated loopback reference host. Apps may embed the same ApplicationHost API."""
    import uvicorn
    from starlette.applications import Starlette

    settings = load_settings(state)
    host = ApplicationHost(
        state, publication, max_request_body_bytes=settings.max_request_body_bytes
    )
    server = host.create_server(port=settings.port, skills=skills)
    base = server.streamable_http_app(**server.http_options)

    @asynccontextmanager
    async def lifespan(_):
        async with host:
            async with server.session_manager.run():
                yield

    app = Starlette(routes=base.routes, lifespan=lifespan)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=15,
    )
    process = uvicorn.Server(config)
    config.app = ProtectedApp(app, settings, process, secrets.token_hex(16))
    process.run()
