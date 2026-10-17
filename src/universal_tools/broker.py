"""Atomic catalog refresh, conservative classification, and per-app call serialization."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, Tool

from .journal import WriteJournal
from .models import ID_PATTERN, BrokerError, ServiceManifest, canonical_digest, load_manifest
from .ownership import OwnershipJournal, owner_digest
from .sessions import SessionWorker
from .transport import list_all_tools, service_session


def broker_reason(error: BaseException) -> BrokerError | None:
    if isinstance(error, BrokerError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            reason = broker_reason(child)
            if reason is not None:
                return reason
    return None


def check_schema(schema: dict) -> None:
    if len(json.dumps(schema).encode("utf-8")) > 128 * 1024:
        raise BrokerError("tool schema exceeds 128 KiB")

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise BrokerError("tool schemas cannot fetch external references")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    validator = validator_for(schema, default=None)
    if validator is None:
        # MCP schemas without a dialect declaration use the current default.
        if "$schema" in schema:
            raise BrokerError("unsupported JSON Schema dialect")
        validator = validator_for(schema)
    validator.check_schema(schema)


@dataclass(frozen=True)
class CatalogTool:
    manifest: ServiceManifest
    tool: Tool

    @property
    def id(self) -> str:
        return f"{self.manifest.id}/{self.tool.name}"

    @property
    def read_only(self) -> bool:
        # An explicit false annotation wins over any local legacy override.
        hint = self.tool.annotations.readOnlyHint if self.tool.annotations else None
        return hint is True or (hint is None and self.tool.name in self.manifest.read_only_tools)

    @property
    def revision(self) -> str:
        return canonical_digest(
            {"service": self.manifest.digest, "tool": self.tool.model_dump(mode="json")}
        )

    def describe(self) -> dict:
        return {
            "tool_id": self.id,
            "catalog_revision": self.revision,
            "service_id": self.manifest.id,
            "classification": "read" if self.read_only else "write_or_unknown",
            "callable": self.read_only or self.manifest.allow_writes,
            "session_mode": self.manifest.session_mode,
            "context_required": self.manifest.session_mode == "exclusive",
            "definition": self.tool.model_dump(mode="json", exclude_none=True),
        }


@dataclass
class Lease:
    manifest: ServiceManifest
    owner: str
    context_id: str
    expires: float
    worker: SessionWorker
    lost: bool = False


class Broker:
    def __init__(self, state: Path):
        self.state = state
        self.registry = state / "services"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.journal = WriteJournal(state / "writes.sqlite3")
        self.ownership = OwnershipJournal(state / "ownership.sqlite3")
        self.catalog: dict[str, CatalogTool] = {}
        self.services: dict[str, dict] = {}
        self.generation = 0
        self._refresh_lock = asyncio.Lock()
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._manifests: dict[str, ServiceManifest] = {}
        self._leases: dict[str, Lease] = {}
        self._reaper: asyncio.Task | None = None
        self._closed = False
        self._clock = time.monotonic

    async def start(self):
        if self._reaper is None:
            # Called under the daemon's OS instance lock. Never reconstruct native sessions.
            self.ownership.orphaned({lease.context_id for lease in self._leases.values()})
            self._reaper = asyncio.create_task(self._reap(), name="service-lease-expiry")

    async def _reap(self):
        while True:
            await asyncio.sleep(1)
            for service_id in list(self._leases):
                async with self.lock(service_id):
                    await self._expire(service_id)

    async def _retire(self, service_id: str) -> bool:
        lease = self._leases.get(service_id)
        if lease:
            lost = lease.lost or lease.worker.failed
            if not lost:
                self.ownership.retiring(lease.context_id)
            await lease.worker.close()
            lost = lost or lease.worker.failed
            if lost:
                self.ownership.lose(lease.context_id, "session_lost")
            else:
                self.ownership.finish(service_id, lease.context_id, "closed")
            self._leases.pop(service_id, None)
            return not lost
        return False

    async def _expire(self, service_id: str):
        lease = self._leases.get(service_id)
        if lease and (lease.lost or lease.worker.failed or lease.expires <= self._clock()):
            await self._retire(service_id)

    def _recovery_blocker(self, manifest: ServiceManifest) -> dict | None:
        records = self.ownership.blockers(manifest.resource_keys)
        same_id = self.ownership.get(manifest.id)
        if same_id and not any(row["context_id"] == same_id["context_id"] for row in records):
            records.append(same_id)
        for record in records:
            lease = self._leases.get(record["service_id"])
            if (
                lease
                and lease.context_id == record["context_id"]
                and lease.manifest.digest == manifest.digest
                and lease.manifest.id == manifest.id
                and not lease.lost
                and lease.worker.alive
                and record["state"] in {"opening", "active"}
            ):
                continue
            return record
        return None

    def _check_recovery(self, manifest: ServiceManifest):
        if self._recovery_blocker(manifest):
            raise BrokerError(
                "service context is lost or reserved; operator recovery is required. "
                "Stop the broker, reconcile app state, then use recover-context"
            )

    async def _renew(self, lease: Lease):
        try:
            self.ownership.activate(lease.context_id, lease.manifest.lease_seconds)
        except Exception:
            lease.lost = True
            try:
                self.ownership.lose(lease.context_id, "reservation_update_failed")
            finally:
                await lease.worker.close()
            raise BrokerError("ownership update failed; operator recovery is required") from None
        lease.expires = self._clock() + lease.manifest.lease_seconds

    async def aclose(self):
        self._closed = True
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        for service_id in list(self._leases):
            async with self.lock(service_id):
                await self._retire(service_id)

    def _conflicting(self, manifest: ServiceManifest) -> bool:
        for path in self.registry.glob("*.json"):
            if path.stem == manifest.id:
                continue
            try:
                other = load_manifest(path)
            except Exception:
                continue
            if (
                other.enabled
                and "exclusive" in {manifest.session_mode, other.session_mode}
                and manifest.resource_keys & other.resource_keys
            ):
                return True
        return False

    def lock(self, service_id: str) -> asyncio.Lock:
        return self._service_locks.setdefault(service_id, asyncio.Lock())

    async def refresh(self) -> dict:
        async with self._refresh_lock:
            if self._closed:
                raise BrokerError("broker is closing")
            files = sorted(self.registry.glob("*.json"))
            if len(files) > 128:
                raise BrokerError("registry exceeds 128 services")
            catalog = {}
            statuses = {}
            manifests = {}
            # Revocation/configuration changes retire owned workers before discovery.
            for service_id, lease in list(self._leases.items()):
                async with self.lock(service_id):
                    try:
                        current = load_manifest(self.registry / f"{service_id}.json")
                        keep = (
                            current.enabled
                            and current.digest == lease.manifest.digest
                            and not self._conflicting(current)
                        )
                    except Exception:
                        keep = False
                    if keep:
                        await self._expire(service_id)
                    else:
                        await self._retire(service_id)

            async def discover(path: Path):
                service_id = path.stem
                try:
                    manifest = load_manifest(path)
                    if manifest.id != path.stem:
                        raise BrokerError("manifest filename must be <service-id>.json")
                    if not manifest.enabled:
                        statuses[service_id] = {"service_id": service_id, "status": "disabled"}
                        return
                    if self._recovery_blocker(manifest):
                        statuses[service_id] = {
                            "service_id": service_id,
                            "status": "recovery_required",
                            "session_mode": manifest.session_mode,
                        }
                        manifests[service_id] = manifest
                        return
                    if self._conflicting(manifest):
                        raise BrokerError("resource has conflicting registrations; remove aliases")
                    async with self.lock(service_id):
                        lease = self._leases.get(service_id)
                        if lease:
                            # Metadata refresh must preserve the owner's native context.
                            tools = await lease.worker.execute(list_all_tools)
                        else:
                            async with asyncio.timeout(manifest.timeout_seconds):
                                async with service_session(manifest) as session:
                                    tools = await list_all_tools(session)
                    for tool in tools:
                        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool.name):
                            raise BrokerError("app advertised an unsupported tool name")
                        check_schema(tool.inputSchema)
                        if tool.outputSchema is not None:
                            check_schema(tool.outputSchema)
                    entries = [CatalogTool(manifest, tool) for tool in tools]
                    catalog.update({entry.id: entry for entry in entries})
                    manifests[service_id] = manifest
                    statuses[service_id] = {
                        "service_id": service_id,
                        "display_name": manifest.display_name,
                        "description": manifest.description,
                        "status": "available",
                        "transport": manifest.transport.type,
                        "allow_writes": manifest.allow_writes,
                        "tool_count": len(entries),
                        "session_mode": manifest.session_mode,
                    }
                except Exception as error:
                    lease = self._leases.get(service_id)
                    if lease and lease.worker.failed:
                        lease.lost = True
                        self.ownership.lose(lease.context_id, "discovery_failed")
                        await lease.worker.close()
                    # Do not retain stale tools for failed or malformed registrations.
                    statuses[service_id] = {
                        "service_id": service_id,
                        "status": "unavailable",
                        "error": str(error)
                        if isinstance(error, BrokerError)
                        else (
                            f"discovery failed ({type(error).__name__}); check the app and manifest"
                        ),
                    }

            # Bounded fanout, each app may independently fail without blocking other apps.
            semaphore = asyncio.Semaphore(8)

            async def bounded(path):
                async with semaphore:
                    await discover(path)

            await asyncio.gather(*(bounded(path) for path in files))
            self.catalog, self.services = catalog, statuses
            self._manifests = manifests
            self.generation += 1
            return self.list_services()

    def list_services(self) -> dict:
        return {
            "generation": self.generation,
            "services": [self.services[key] for key in sorted(self.services)],
            "tool_count": len(self.catalog),
        }

    def search(self, query: str = "", service_id: str | None = None, limit: int = 30) -> dict:
        if not 1 <= limit <= 100:
            raise BrokerError("limit must be between 1 and 100")
        words = query.casefold().split()
        matches = []
        for entry in sorted(self.catalog.values(), key=lambda item: item.id):
            if service_id and entry.manifest.id != service_id:
                continue
            haystack = f"{entry.id} {entry.tool.description or ''}".casefold()
            if all(word in haystack for word in words):
                matches.append(
                    {
                        "tool_id": entry.id,
                        "description": entry.tool.description or "",
                        "classification": "read" if entry.read_only else "write_or_unknown",
                        "callable": entry.read_only or entry.manifest.allow_writes,
                        "context_required": entry.manifest.session_mode == "exclusive",
                    }
                )
        return {"matches": matches[:limit], "total": len(matches), "generation": self.generation}

    def describe(self, tool_id: str) -> dict:
        return self._entry(tool_id).describe()

    def _entry(self, tool_id: str) -> CatalogTool:
        try:
            return self.catalog[tool_id]
        except KeyError:
            if self.services.get(tool_id.split("/", 1)[0], {}).get("status") == "recovery_required":
                raise BrokerError(
                    "service context is lost; operator recovery is required"
                ) from None
            raise BrokerError("unknown tool_id; refresh_services and search_tools first") from None

    def _registration_unchanged(self, entry: CatalogTool) -> None:
        self._registration_matches(entry.manifest)

    def _registration_matches(self, manifest: ServiceManifest) -> None:
        try:
            current = load_manifest(self.registry / f"{manifest.id}.json")
        except Exception:
            raise BrokerError("registration is removed or invalid; refresh_services") from None
        if not current.enabled or current.digest != manifest.digest:
            raise BrokerError("registration changed; refresh_services and describe_tool again")
        if self._conflicting(current):
            raise BrokerError("resource has conflicting registrations; remove aliases and refresh")
        self._check_recovery(current)

    def _owned_manifest(self, service_id: str) -> ServiceManifest:
        if self._closed:
            raise BrokerError("broker is closing")
        if not re.fullmatch(ID_PATTERN, service_id) or service_id not in self._manifests:
            raise BrokerError("unknown or unavailable service; refresh_services")
        manifest = self._manifests[service_id]
        if manifest.session_mode != "exclusive":
            raise BrokerError("service is transient; exclusive contexts are not enabled")
        return manifest

    @staticmethod
    def _service_id(service_id: str):
        if not re.fullmatch(ID_PATTERN, service_id):
            raise BrokerError("invalid service_id")

    @staticmethod
    def _owner(owner_id: str | None) -> str:
        if owner_id is None:
            raise BrokerError("service ownership requires an identified broker frontend")
        return owner_id

    async def acquire_service(self, service_id: str, *, owner_id: str | None) -> dict:
        owner = self._owner(owner_id)
        self._service_id(service_id)
        if self._closed:
            raise BrokerError("broker is closing")
        async with self.lock(service_id):
            manifest = self._owned_manifest(service_id)
            self._registration_matches(manifest)
            await self._expire(service_id)
            lease = self._leases.get(service_id)
            if lease:
                if lease.owner != owner:
                    raise BrokerError("service is owned by another frontend")
                if lease.lost or not lease.worker.alive:
                    raise BrokerError("service context is lost; release_service before reacquiring")
            else:
                context_id = secrets.token_urlsafe(32)
                # Commit the reservation before starting a potentially stateful process.
                self.ownership.reserve(manifest, owner, context_id)
                worker = SessionWorker(manifest, service_session)
                lease = Lease(manifest, owner, context_id, 0, worker)
                self._leases[service_id] = lease
                try:
                    await worker.execute(list_all_tools)
                    self._registration_matches(manifest)
                except BaseException as error:
                    lease.lost = True
                    self.ownership.lose(context_id, "initialization_failed")
                    await worker.close()
                    self._leases.pop(service_id, None)
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise BrokerError(
                        "app session could not be acquired; operator recovery is required"
                    ) from None
            await self._renew(lease)
            return self._lease_info(service_id, owner)

    def _lease_info(self, service_id: str, owner: str | None) -> dict:
        lease = self._leases.get(service_id)
        if not lease:
            record = self.ownership.get(service_id)
            if record:
                info = {"service_id": service_id, "state": "lost", "recovery_required": True}
                if owner is not None and record["owner_digest"] == owner_digest(owner):
                    info["context_id"] = record["context_id"]
                return info
            return {"service_id": service_id, "state": "unowned"}
        if lease.owner != owner:
            return {"service_id": service_id, "state": "owned_by_another"}
        return {
            "service_id": service_id,
            "context_id": lease.context_id,
            "state": (
                "lost"
                if lease.lost or not lease.worker.alive
                else "expired"
                if lease.expires <= self._clock()
                else "active"
            ),
            "expires_in_seconds": max(0, round(lease.expires - self._clock(), 3)),
            "recovery_required": lease.lost or not lease.worker.alive,
        }

    async def service_session_status(self, service_id: str, *, owner_id: str | None) -> dict:
        self._service_id(service_id)
        async with self.lock(service_id):
            return self._lease_info(service_id, owner_id)

    async def release_service(
        self, service_id: str, context_id: str, *, owner_id: str | None
    ) -> dict:
        owner = self._owner(owner_id)
        self._service_id(service_id)
        async with self.lock(service_id):
            await self._require_lease(service_id, context_id, owner)
            if not await self._retire(service_id):
                raise BrokerError("context is lost; operator recovery is required")
            return {"service_id": service_id, "released": True}

    async def _require_lease(self, service_id: str, context_id: str | None, owner: str) -> Lease:
        await self._expire(service_id)
        record = self.ownership.get(service_id)
        if record and record["state"] == "lost":
            raise BrokerError("context is lost; operator recovery is required")
        lease = self._leases.get(service_id)
        if not lease or lease.owner != owner or lease.context_id != context_id:
            raise BrokerError("context is missing, expired, or owned by another frontend")
        return lease

    async def call(
        self,
        tool_id: str,
        catalog_revision: str,
        arguments: dict[str, Any],
        *,
        write: bool,
        request_id: str | None = None,
        context_id: str | None = None,
        owner_id: str | None = None,
    ) -> CallToolResult:
        if self._closed:
            raise BrokerError("broker is closing")
        entry = self._entry(tool_id)
        if entry.revision != catalog_revision:
            raise BrokerError("catalog revision changed; describe_tool again")
        if write and entry.read_only:
            raise BrokerError("this tool is read-only; use call_read_tool")
        if not write and not entry.read_only:
            raise BrokerError("tool is write-capable or unclassified; use call_write_tool")
        if write and not entry.manifest.allow_writes:
            raise BrokerError("writes are disabled in this app's local registration")
        if write and request_id is None:
            raise BrokerError("write calls require a unique request_id")
        if len(json.dumps(arguments, allow_nan=False).encode("utf-8")) > 256 * 1024:
            raise BrokerError("arguments exceed 256 KiB")
        try:
            validator_for(entry.tool.inputSchema)(entry.tool.inputSchema).validate(arguments)
        except ValidationError:
            raise BrokerError("arguments do not match the app's input schema") from None

        claimed = False
        async with self.lock(entry.manifest.id):
            if self._closed:
                raise BrokerError("broker is closing")
            self._registration_unchanged(entry)
            if self._entry(tool_id).revision != entry.revision:
                raise BrokerError("catalog changed while waiting; describe_tool again")
            lease = None
            if entry.manifest.session_mode == "exclusive":
                lease = await self._require_lease(
                    entry.manifest.id, context_id, self._owner(owner_id)
                )
                if lease.lost or not lease.worker.alive:
                    raise BrokerError("service context is lost; release_service before reacquiring")
            elif context_id is not None:
                raise BrokerError("transient services do not accept a context_id")

            async def invoke(session):
                nonlocal claimed
                live = {tool.name: tool for tool in await list_all_tools(session)}
                if (
                    entry.tool.name not in live
                    or CatalogTool(entry.manifest, live[entry.tool.name]).revision != entry.revision
                ):
                    raise BrokerError("app tool definition changed; refresh and describe again")
                self._registration_unchanged(entry)
                if write:
                    fingerprint = canonical_digest(
                        {"tool_id": tool_id, "revision": catalog_revision, "args": arguments}
                    )
                    self.journal.claim(request_id, fingerprint, tool_id)
                    claimed = True
                result = await session.call_tool(entry.tool.name, arguments)
                if write:
                    self.journal.finish(request_id, "app_error" if result.isError else "completed")
                return result

            try:
                if lease:
                    result = await lease.worker.execute(invoke)
                    await self._renew(lease)
                    return result
                async with asyncio.timeout(entry.manifest.timeout_seconds):
                    async with service_session(entry.manifest) as session:
                        return await invoke(session)
            except BaseException as error:
                if claimed:
                    # Even cancellation or process loss leaves a durable claim. Never retry it.
                    self.journal.finish(request_id, "uncertain")
                if lease and not lease.worker.alive:
                    lease.lost = True
                    self.ownership.lose(lease.context_id, "call_interrupted")
                    await lease.worker.close()
                if isinstance(error, asyncio.CancelledError):
                    raise
                reason = broker_reason(error)
                if reason is not None:
                    raise reason from None
                if not isinstance(error, Exception):
                    raise
                raise BrokerError(
                    f"app call failed ({type(error).__name__}); "
                    + (
                        "write outcome may be uncertain; inspect app state"
                        if claimed
                        else ("check app availability and refresh_services")
                    )
                ) from None

    def request_status(self, request_id: str) -> dict:
        entry = self.journal.status(request_id)
        entry.pop("fingerprint")
        entry["note"] = (
            "in_progress can also mean interrupted before status was saved; "
            "app_error does not prove no partial changes; reconcile with the app"
        )
        return entry
