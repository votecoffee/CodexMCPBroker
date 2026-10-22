"""Opt-in, per-user app management. Apps own restart admission and supervision."""

from __future__ import annotations

import hmac
import importlib.metadata
import json
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from . import __version__
from .models import ID_PATTERN, StrictModel, atomic_json, canonical_digest

MAX_RECORDS = 256
MAX_BYTES = 32 * 1024
INSTANCE_PATTERN = r"^[a-f0-9]{32}$"


def default_registry() -> Path:
    """One registry per local user, independent of Codex/plugin installations."""
    if os.name == "nt":
        from win32com.shell import shell, shellcon

        base = Path(
            shell.SHGetKnownFolderPath(
                shell.FOLDERID_LocalAppData, shellcon.KF_FLAG_NO_PACKAGE_REDIRECTION
            )
        )
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "CodexMCPManager" / "Registry"


def private_directory(path: Path):
    """Create a dedicated registry directory accessible to its user and SYSTEM."""
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("management directories must not be links or junctions")
    if os.name != "nt":
        path.chmod(0o700)
        return
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for sid in (owner, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS, flags, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


def read_json(path: Path):
    if path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("management files must be ordinary files")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("management record exceeds its byte budget")
    return json.loads(raw)


class ComponentVersion(StrictModel):
    installed: str = Field(max_length=128)
    running: str | None = Field(default=None, max_length=128)
    available: str | None = Field(default=None, max_length=128)
    update_pending: bool = False


class ManagedStatus(StrictModel):
    state: Literal["starting", "running", "busy", "degraded", "stopped", "error"] = "running"
    message: str = Field(default="", max_length=1024)
    versions: dict[str, ComponentVersion] = Field(default_factory=dict, max_length=16)
    active_operations: int = Field(default=0, ge=0, le=1_000_000)
    restart_allowed: bool = False
    restart_reason: str = Field(default="Restart is not configured", max_length=512)

    @field_validator("versions")
    @classmethod
    def component_names(cls, value):
        import re

        if any(not re.fullmatch(ID_PATTERN, name) for name in value):
            raise ValueError("invalid component name")
        return value


class RestartRequest(StrictModel):
    instance_id: str = Field(pattern=INSTANCE_PATTERN)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,128}$")


class RestartReceipt(StrictModel):
    outcome: Literal["accepted", "blocked", "uncertain"]
    message: str = Field(max_length=1024)


class RestartResult(RestartReceipt):
    outcome: Literal["accepted", "blocked"]


class Registration(StrictModel):
    schema_version: Literal[1] = 1
    app_id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(pattern=INSTANCE_PATTERN)
    plugin_id: str | None = Field(default=None, max_length=256)
    pid: int = Field(ge=1)
    started_at: float
    endpoint: str
    token: str = Field(pattern=r"^[a-zA-Z0-9_-]{32,128}$")
    stopped: bool = False
    last_status: ManagedStatus

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.path != "/v1/management"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("management endpoints must be literal loopback HTTP")
        return value


class ManagementRegistry:
    def __init__(self, directory=None):
        self.directory = Path(directory or default_registry()).resolve()

    def prepare(self):
        private_directory(self.directory)
        private_directory(self.directory / "instances")

    def path(self, app_id, instance_id):
        import re

        if not re.fullmatch(ID_PATTERN, app_id) or not re.fullmatch(INSTANCE_PATTERN, instance_id):
            raise ValueError("invalid management identity")
        return self.directory / "instances" / f"{app_id}.{instance_id}.json"

    def publish(self, record: Registration):
        self.prepare()
        atomic_json(self.path(record.app_id, record.instance_id), record.model_dump(mode="json"))

    def records(self):
        directory = self.directory / "instances"
        if not directory.exists():
            return [], []
        paths = []
        # Bound enumeration as well as work; do not traverse installations or native projects.
        for path in directory.iterdir():
            if path.suffix == ".json":
                paths.append(path)
                if len(paths) > MAX_RECORDS:
                    return [], ["Management registry has too many instances"]
        records, errors = [], []
        for path in sorted(paths):
            try:
                record = Registration.model_validate(read_json(path))
                if path != self.path(record.app_id, record.instance_id):
                    raise ValueError("registration identity differs from its filename")
                records.append(record)
            except (ValueError, OSError):
                errors.append("An invalid management registration was ignored")
        return records, errors


class RestartJournal:
    """Durable at-most-once callback admission, including lost/ambiguous replies."""

    def __init__(self, path):
        self.path = path
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS restarts (request_id TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reconciliations (request_id TEXT PRIMARY KEY, "
                "result TEXT NOT NULL, recorded_at REAL NOT NULL)"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def pending(connection, owner, exclude=None):
        rows = connection.execute(
            "SELECT r.request_id,COALESCE(c.result,r.result) FROM restarts r "
            "LEFT JOIN reconciliations c ON c.request_id=r.request_id WHERE r.owner=?",
            (owner,),
        )
        return any(
            key != exclude and json.loads(raw)["outcome"] == "uncertain" for key, raw in rows
        )

    def has_pending(self, owner, exclude=None):
        with self.connect() as connection:
            return self.pending(connection, owner, exclude)

    def pending_requests(self, owner):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT r.request_id,COALESCE(c.result,r.result) FROM restarts r "
                "LEFT JOIN reconciliations c ON c.request_id=r.request_id WHERE r.owner=?",
                (owner,),
            )
            return [
                {"request_id": key, **json.loads(raw)}
                for key, raw in rows
                if json.loads(raw)["outcome"] == "uncertain"
            ]

    def claim(self, owner, fingerprint, request_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT r.fingerprint,COALESCE(c.result,r.result) FROM restarts r "
                "LEFT JOIN reconciliations c ON c.request_id=r.request_id WHERE r.request_id=?",
                (request_id,),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("restart request ID belongs to a different request")
                return json.loads(row[1])
            result = {"outcome": "uncertain", "message": "Inspect app state before another restart"}
            if self.pending(connection, owner):
                return {
                    "outcome": "blocked",
                    "message": "A prior restart needs app-owned reconciliation",
                }
            if connection.execute("SELECT count(*) FROM restarts").fetchone()[0] >= 10_000:
                raise ValueError("management restart journal is full")
            connection.execute(
                "INSERT INTO restarts VALUES (?,?,?,?)",
                (request_id, owner, fingerprint, json.dumps(result)),
            )
        return None

    def finish(self, request_id, result):
        with self.connect() as connection:
            connection.execute(
                "UPDATE restarts SET result=? WHERE request_id=?", (json.dumps(result), request_id)
            )

    def reconcile(self, owner, request_id, result):
        result = RestartResult.model_validate(result).model_dump(mode="json")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner,result FROM restarts WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row or row[0] != owner or json.loads(row[1])["outcome"] != "uncertain":
                raise ValueError("only this app's uncertain restart can be reconciled")
            connection.execute(
                "INSERT INTO reconciliations VALUES (?,?,?)",
                (request_id, json.dumps(result), time.time()),
            )


class _ManagementHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, address, handler):
        self.capacity = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()


class ManagedService:
    """One app-owned process/instance; no MCP schema or plugin changes required.

    status() returns a fast, thread-safe snapshot. restart(request) must atomically
    fence new app work and ask the app's supervisor to drain/restart safely. Return
    an acceptance result before that supervisor closes this management endpoint.
    """

    def __init__(
        self, app_id, display_name, status, *, restart=None, plugin_id=None, registry=None
    ):
        self.registry = ManagementRegistry(registry)
        self.status_callback, self.restart_callback = status, restart
        self.app_id, self.display_name, self.plugin_id = app_id, display_name, plugin_id
        self.instance_id = secrets.token_hex(16)
        self.token = secrets.token_urlsafe(32)
        self.control_lock = threading.RLock()
        self.server = self.thread = self.record = self.journal = None

    def snapshot(self, *, excluding_request=None):
        status = ManagedStatus.model_validate(self.status_callback())
        versions = dict(status.versions)
        try:
            installed = importlib.metadata.version("universal-codex-tools")
        except importlib.metadata.PackageNotFoundError:
            installed = __version__
        versions.setdefault("framework", ComponentVersion(installed=installed, running=__version__))
        status = status.model_copy(update={"versions": versions})
        if (
            self.restart_callback is None
            or status.active_operations
            or status.state in {"starting", "busy"}
        ):
            reason = (
                "Restart is not configured"
                if self.restart_callback is None
                else "Finish active work before restarting"
            )
            status = status.model_copy(update={"restart_allowed": False, "restart_reason": reason})
        if self.journal and self.journal.has_pending(self.app_id, excluding_request):
            status = status.model_copy(
                update={
                    "restart_allowed": False,
                    "restart_reason": "A prior restart needs app-owned reconciliation",
                }
            )
        value = status.model_dump(mode="json")
        return {
            "schema_version": 1,
            "app_id": self.app_id,
            "instance_id": self.instance_id,
            "revision": canonical_digest({"instance_id": self.instance_id, "status": value}),
            "status": value,
        }

    def request_restart(self, raw):
        request = RestartRequest.model_validate(raw)
        if request.instance_id != self.instance_id:
            raise ValueError("service instance changed; refresh status")
        fingerprint = canonical_digest({"app_id": self.app_id, **request.model_dump(mode="json")})
        with self.control_lock:
            previous = self.journal.claim(self.app_id, fingerprint, request.request_id)
            if previous is not None:
                return previous
            current = self.snapshot(excluding_request=request.request_id)
            if current["revision"] != request.expected_revision:
                result = {"outcome": "blocked", "message": "Service status changed; refresh first"}
            elif not current["status"]["restart_allowed"]:
                result = {"outcome": "blocked", "message": current["status"]["restart_reason"]}
            else:
                try:
                    result = RestartResult.model_validate(
                        self.restart_callback(request)
                    ).model_dump(mode="json")
                except Exception:
                    result = {
                        "outcome": "uncertain",
                        "message": "Restart outcome unknown; app reconciliation required",
                    }
            self.journal.finish(request.request_id, result)
            return result

    def reconcile_restart(self, request_id, result):
        """App/operator records a verified prior outcome; never called by the tray/MCP."""
        with self.control_lock:
            self.journal.reconcile(self.app_id, request_id, result)

    def pending_restarts(self):
        """App/operator inspection only; no callback dispatch or barrier clearing."""
        return self.journal.pending_requests(self.app_id)

    def start(self):
        if self.server is not None:
            raise ValueError("managed service is already started")
        self.registry.prepare()
        self.journal = RestartJournal(self.registry.directory / "restart-journal.sqlite3")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Never log credentials, request bodies or app exception details.

            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def respond(self, code, value):
                raw = json.dumps(value, allow_nan=False).encode()
                if len(raw) > MAX_BYTES:
                    code, raw = 500, b'{"error":"management response exceeds its limit"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                return (
                    self.headers.get("Host") == f"127.0.0.1:{owner.server.server_port}"
                    and self.headers.get("Origin") is None
                    and hmac.compare_digest(
                        self.headers.get("Authorization", "").encode("utf-8"),
                        ("Bearer " + owner.token).encode("ascii"),
                    )
                )

            def do_GET(self):
                if not self.authorized():
                    return self.respond(401, {"error": "unauthorized"})
                if self.path != "/v1/management/status":
                    return self.respond(404, {"error": "not found"})
                try:
                    return self.respond(200, owner.snapshot())
                except Exception:
                    return self.respond(503, {"error": "App status is unavailable"})

            def do_POST(self):
                if not self.authorized():
                    return self.respond(401, {"error": "unauthorized"})
                if self.path != "/v1/management/restart":
                    return self.respond(404, {"error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if self.headers.get("Transfer-Encoding") or not 1 <= length <= 8192:
                        return self.respond(413, {"error": "invalid request size"})
                    request = json.loads(self.rfile.read(length))
                    return self.respond(200, owner.request_restart(request))
                except (ValueError, OSError):
                    return self.respond(400, {"error": "Invalid or unavailable restart request"})
                except Exception:
                    return self.respond(
                        503, {"error": "Restart outcome unavailable; inspect app state"}
                    )

        server = _ManagementHTTPServer(("127.0.0.1", 0), Handler)
        self.server = server
        try:
            snapshot = self.snapshot()
            self.record = Registration(
                app_id=self.app_id,
                display_name=self.display_name,
                plugin_id=self.plugin_id,
                instance_id=self.instance_id,
                pid=os.getpid(),
                started_at=time.time(),
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/management",
                token=self.token,
                last_status=ManagedStatus.model_validate(snapshot["status"]),
            )
            self.registry.publish(self.record)
            self.thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="app-management",
                daemon=True,
            )
            self.thread.start()
        except BaseException:
            server.server_close()
            self.server = None
            raise
        return self

    def close(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        stopped = self.record.last_status.model_copy(
            update={
                "state": "stopped",
                "restart_allowed": False,
                "restart_reason": "App is stopped",
            }
        )
        self.registry.publish(
            self.record.model_copy(update={"stopped": True, "last_status": stopped})
        )
        self.server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


class ManagementClient:
    def __init__(self, registry=None, *, timeout=1.5):
        self.registry = ManagementRegistry(registry)
        self.timeout = timeout

    def _get(self, record, method, suffix, body=None):
        with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                method,
                record.endpoint + suffix,
                headers={"Authorization": "Bearer " + record.token},
                json=body,
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("management response exceeds its budget")
                return json.loads(raw)

    def inspect(self, record):
        row = {
            "app_id": record.app_id,
            "display_name": record.display_name,
            "instance_id": record.instance_id,
            "plugin_id": record.plugin_id,
            "pid": record.pid,
            "monitored": True,
            "checked_at": time.time(),
            "revision": None,
            "status": record.last_status.model_dump(mode="json"),
        }
        if record.stopped:
            return row
        try:
            result = self._get(record, "GET", "/status")
            status = ManagedStatus.model_validate(result["status"])
            expected = canonical_digest(
                {"instance_id": record.instance_id, "status": status.model_dump(mode="json")}
            )
            if (
                result["app_id"] != record.app_id
                or result["instance_id"] != record.instance_id
                or result["schema_version"] != 1
                or not hmac.compare_digest(result["revision"], expected)
            ):
                raise ValueError("management identity/revision mismatch")
            row.update(status=status.model_dump(mode="json"), revision=expected)
        except Exception:
            row["status"].update(
                state="offline",
                message="App is not responding",
                restart_allowed=False,
                restart_reason="Reconnect to the app before requesting a restart",
            )
        return row

    def snapshot(self):
        records, errors = self.registry.records()
        with ThreadPoolExecutor(max_workers=16) as executor:
            rows = list(executor.map(self.inspect, records))
        return {
            "schema_version": 1,
            "updated_at": time.time(),
            "services": rows,
            "warnings": errors,
        }

    def restart(self, app_id, instance_id, expected_revision, request_id):
        request = RestartRequest(
            instance_id=instance_id, expected_revision=expected_revision, request_id=request_id
        )
        record = Registration.model_validate(read_json(self.registry.path(app_id, instance_id)))
        if record.app_id != app_id or record.instance_id != instance_id or record.stopped:
            return {"outcome": "blocked", "message": "Service instance changed; refresh status"}
        try:
            return RestartReceipt.model_validate(
                self._get(record, "POST", "/restart", request.model_dump(mode="json"))
            ).model_dump(mode="json")
        except Exception:
            return {
                "outcome": "uncertain",
                "message": "No restart receipt received. Inspect app state; do not retry.",
            }
