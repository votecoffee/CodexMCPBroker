"""Atomic catalog refresh, conservative classification, and per-app call serialization."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, Tool

from .capabilities import CapabilityState, read_catalog, tool_revision
from .elicitation import ElicitationTarget, routed_call
from .journal import WriteJournal
from .metadata import BROKER_SUPPORT
from .models import (
    ID_PATTERN,
    MAX_REQUEST_BYTES,
    TOOL_NAME_PATTERN,
    BrokerError,
    ServiceManifest,
    canonical_digest,
    load_manifest,
)
from .ownership import OwnershipJournal, owner_digest
from .schemas import check_schema
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
    def callable(self) -> bool:
        return self.read_only or self.manifest.permits_write(self.tool.name)

    @property
    def revision(self) -> str:
        return tool_revision(self.manifest, self.tool)

    def describe(self) -> dict:
        return {
            "tool_id": self.id,
            "catalog_revision": self.revision,
            "service_id": self.manifest.id,
            "classification": "read" if self.read_only else "write_or_unknown",
            "callable": self.callable,
            "max_argument_bytes": self.manifest.max_argument_bytes,
            "session_mode": self.manifest.session_mode,
            "context_required": self.manifest.session_mode == "exclusive",
            "definition": self.tool.model_dump(mode="json", exclude_none=True),
        }


@dataclass
class Lease:
    manifest: ServiceManifest
    owner: str | None
    context_id: str
    expires: float
    worker: SessionWorker
    lost: bool = False
    capability_state: CapabilityState | None = None


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
        self._metadata: dict[str, dict] = {}
        self._capabilities: dict[str, CapabilityState] = {}
        self._leases: dict[str, Lease] = {}
        # Shared workers belong to this broker instance, never a frontend.
        self._shared_owner = secrets.token_urlsafe(48)
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
                and (manifest.session_mode != "transient" or other.session_mode != "transient")
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
            metadata = {}
            capabilities = {}
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
                manifest = None
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
                        self._registration_matches(manifest)
                        lease = self._leases.get(service_id)
                        if lease:
                            # Metadata refresh must preserve the owner's native context.
                            tools = await lease.worker.execute(list_all_tools)
                            # Owned initialization may contain session-specific app data.
                            # Never publish it into a catalog shared with other frontends.
                            if self._manifests.get(service_id) == manifest:
                                if service_id in self._metadata:
                                    metadata[service_id] = self._metadata[service_id]
                                if service_id in self._capabilities:
                                    capabilities[service_id] = self._capabilities[service_id]
                        else:
                            async with asyncio.timeout(manifest.timeout_seconds):
                                async with service_session(manifest) as session:
                                    tools = await list_all_tools(session)
                                    metadata[service_id] = {
                                        "initialization": deepcopy(session.initialization),
                                        "observed_generation": self.generation + 1,
                                    }
                                    if manifest.capability_catalog is not None:
                                        capabilities[service_id] = await read_catalog(
                                            session, manifest, tools
                                        )
                        self._registration_matches(manifest)
                    for tool in tools:
                        if not re.fullmatch(TOOL_NAME_PATTERN, tool.name):
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
                        "allowed_write_tools": deepcopy(manifest.allowed_write_tools),
                        "max_argument_bytes": manifest.max_argument_bytes,
                        "tool_count": len(entries),
                        "session_mode": manifest.session_mode,
                        "capability_catalog": (
                            capabilities.get(service_id, CapabilityState()).status("catalog")
                            if manifest.capability_catalog is not None
                            else {"status": "not_configured"}
                        ),
                    }
                except Exception as error:
                    metadata.pop(service_id, None)
                    capabilities.pop(service_id, None)
                    lease = self._leases.get(service_id)
                    if lease and lease.worker.failed:
                        lease.lost = True
                        self.ownership.lose(lease.context_id, "discovery_failed")
                        await lease.worker.close()
                    if (
                        manifest is not None
                        and manifest.id == service_id
                        and self._recovery_blocker(manifest)
                    ):
                        manifests[service_id] = manifest
                        statuses[service_id] = {
                            "service_id": service_id,
                            "status": "recovery_required",
                            "session_mode": manifest.session_mode,
                        }
                        return
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
            self._metadata = metadata
            self._capabilities = capabilities
            for service_id, state in list(capabilities.items()):
                state = self._bound_capability_state(manifests[service_id], state)
                capabilities[service_id] = state
                statuses[service_id]["capability_catalog"] = state.status("catalog")
            self.generation += 1
            return self.list_services()

    def list_services(self) -> dict:
        return {
            "generation": self.generation,
            "services": [self.services[key] for key in sorted(self.services)],
            "tool_count": len(self.catalog),
            "capability_count": sum(
                len(state.snapshot.document.capabilities)
                for state in self._capabilities.values()
                if state.snapshot is not None
            ),
        }

    def search(
        self,
        query: str = "",
        service_id: str | None = None,
        limit: int = 30,
        kind: str = "all",
        *,
        owner_id: str | None = None,
    ) -> dict:
        if not 1 <= limit <= 100:
            raise BrokerError("limit must be between 1 and 100")
        if kind not in {"all", "tool", "capability"}:
            raise BrokerError("kind must be all, tool or capability")
        words = query.casefold().split()
        matches = []
        for entry in sorted(self.catalog.values(), key=lambda item: item.id):
            if kind == "capability":
                continue
            if service_id and entry.manifest.id != service_id:
                continue
            haystack = f"{entry.id} {entry.tool.description or ''}".casefold()
            if all(word in haystack for word in words):
                matches.append(
                    {
                        "kind": "tool",
                        "tool_id": entry.id,
                        "description": entry.tool.description or "",
                        "classification": "read" if entry.read_only else "write_or_unknown",
                        "callable": entry.callable,
                        "context_required": entry.manifest.session_mode == "exclusive",
                    }
                )
        if kind != "tool":
            for manifest in sorted(self._manifests.values(), key=lambda item: item.id):
                if manifest.capability_catalog is None:
                    continue
                if service_id and manifest.id != service_id:
                    continue
                try:
                    state, source = self._capability_state(manifest, owner_id)
                except BrokerError:
                    continue
                if state.snapshot is not None:
                    for match in state.snapshot.matches(source):
                        haystack = " ".join(
                            [
                                manifest.id,
                                match["capability_id"],
                                match["description"],
                                *match["tags"],
                            ]
                        ).casefold()
                        if all(word in haystack for word in words):
                            matches.append(match)
        return {"matches": matches[:limit], "total": len(matches), "generation": self.generation}

    def describe(self, tool_id: str) -> dict:
        return self._entry(tool_id).describe()

    async def describe_service(self, service_id: str, *, owner_id: str | None) -> dict:
        self._service_id(service_id)
        async with self.lock(service_id):
            if self._closed:
                raise BrokerError("broker is closing")
            manifest = self._manifests.get(service_id)
            if manifest is None:
                raise BrokerError("unknown or unavailable service; refresh_services")
            self._registration_matches(manifest)
            snapshot = self._metadata.get(service_id)
            if snapshot is None:
                raise BrokerError("service metadata unavailable; refresh_services")
            capability_state, capability_source = (
                self._capability_state(manifest, owner_id)
                if manifest.capability_catalog is not None
                else (None, "catalog")
            )
            result = {
                "service_id": service_id,
                "display_name": manifest.display_name,
                "description": manifest.description,
                "session_mode": manifest.session_mode,
                "context_required": manifest.session_mode == "exclusive",
                "policy": {
                    "allow_writes": manifest.allow_writes,
                    "allowed_write_tools": deepcopy(manifest.allowed_write_tools),
                },
                "limits": {
                    "max_argument_bytes": manifest.max_argument_bytes,
                    "max_request_body_bytes": MAX_REQUEST_BYTES,
                    "timeout_seconds": manifest.timeout_seconds,
                },
                "generation": self.generation,
                "metadata_source": "catalog",
                **deepcopy(snapshot),
                "trust": "untrusted_app_data",
                "broker_support": dict(BROKER_SUPPORT),
                "capability_catalog": (
                    capability_state.status(capability_source)
                    if capability_state is not None
                    else {"status": "not_configured"}
                ),
            }
            lease = self._leases.get(service_id)
            if (
                lease
                and lease.manifest.session_mode == "exclusive"
                and lease.owner == owner_id
                and lease.worker.alive
                and not lease.lost
                and lease.expires > self._clock()
            ):
                result.update(
                    metadata_source="owned_session",
                    initialization=deepcopy(lease.worker.initialization),
                    context_id=lease.context_id,
                )
                result.pop("observed_generation")
            result["metadata_revision"] = canonical_digest(
                {"service": manifest.digest, "initialization": result["initialization"]}
            )
            return result

    def _capability_state(self, manifest, owner_id):
        self._registration_matches(manifest)
        lease = self._leases.get(manifest.id)
        source = "catalog"
        state = self._capabilities.get(manifest.id, CapabilityState())
        if (
            lease
            and lease.manifest.session_mode == "exclusive"
            and lease.owner == owner_id
            and lease.worker.alive
            and not lease.lost
            and lease.expires > self._clock()
        ):
            source = "owned_session"
            state = lease.capability_state or CapabilityState()
        return self._bound_capability_state(manifest, state), source

    def _bound_capability_state(self, manifest, state):
        if state.snapshot is not None:
            if any(
                self.catalog.get(manifest.id + "/" + name) is None
                or self.catalog[manifest.id + "/" + name].revision != revision
                for name, revision in state.snapshot.bindings.items()
            ):
                state = CapabilityState(
                    error="catalog discovery definitions changed; refresh_capabilities"
                )
        return state

    async def describe_capability(
        self,
        service_id: str,
        capability_id: str,
        capability_revision: str,
        *,
        owner_id: str | None,
    ) -> dict:
        self._service_id(service_id)
        async with self.lock(service_id):
            if self._closed:
                raise BrokerError("broker is closing")
            manifest = self._manifests.get(service_id)
            if manifest is None or manifest.capability_catalog is None:
                raise BrokerError("service has no available capability_catalog provider")
            state, source = self._capability_state(manifest, owner_id)
            if state.snapshot is None:
                raise BrokerError(state.error)
            if state.snapshot.revision != capability_revision:
                raise BrokerError("capability revision changed; search_tools again")
            return state.snapshot.describe(capability_id, source)

    async def refresh_capabilities(
        self,
        service_id: str,
        context_id: str | None = None,
        *,
        owner_id: str | None,
    ) -> dict:
        self._service_id(service_id)
        async with self._refresh_lock, self.lock(service_id):
            if self._closed:
                raise BrokerError("broker is closing")
            manifest = self._manifests.get(service_id)
            if manifest is None or manifest.capability_catalog is None:
                raise BrokerError("service has no available capability_catalog provider")
            self._registration_matches(manifest)
            lease = None
            if manifest.session_mode == "exclusive":
                lease = await self._require_lease(service_id, context_id, self._owner(owner_id))
                lease.capability_state = CapabilityState()
            elif context_id is not None:
                raise BrokerError("non-exclusive services do not accept a context_id")
            else:
                self._capabilities.pop(service_id, None)
                self.services[service_id]["capability_catalog"] = CapabilityState().status(
                    "catalog"
                )
            try:
                # A lazy shared start and its probe share one invocation budget.
                async with asyncio.timeout(
                    manifest.timeout_seconds if manifest.session_mode == "shared" else None
                ):
                    if manifest.session_mode == "shared":
                        lease = await self._shared_lease(manifest)
                    if lease:
                        state = await lease.worker.execute(
                            lambda session: read_catalog(session, manifest)
                        )
                    else:
                        async with asyncio.timeout(manifest.timeout_seconds):
                            async with service_session(manifest) as session:
                                state = await read_catalog(session, manifest)
                self._registration_matches(manifest)
                state = self._bound_capability_state(manifest, state)
                if lease and manifest.session_mode == "exclusive":
                    lease.capability_state = state
                else:
                    self._capabilities[service_id] = state
                    self.services[service_id]["capability_catalog"] = state.status("catalog")
                if lease and state.snapshot is not None:
                    await self._renew(lease)
                # Do not publish owner-specific status through list_services.
                state, source = self._capability_state(manifest, owner_id)
                return {"service_id": service_id, **state.status(source)}
            except BaseException as error:
                if lease and not lease.worker.alive:
                    lease.lost = True
                    self.ownership.lose(lease.context_id, "capability_probe_interrupted")
                    await lease.worker.close()
                if isinstance(error, asyncio.CancelledError):
                    raise
                if not isinstance(error, Exception):
                    raise
                reason = broker_reason(error)
                if reason is not None:
                    raise reason from None
                raise BrokerError(
                    "capability catalog probe failed; inspect service_session_status"
                ) from None

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
            raise BrokerError(
                f"service is {manifest.session_mode}; exclusive contexts are not enabled"
            )
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

    async def _open_lease(self, manifest: ServiceManifest, owner: str | None) -> Lease:
        context_id = secrets.token_urlsafe(32)
        # Commit the reservation before starting a potentially stateful process.
        self.ownership.reserve(
            manifest, owner if owner is not None else self._shared_owner, context_id
        )
        worker = SessionWorker(manifest, service_session)
        lease = Lease(manifest, owner, context_id, 0, worker)
        self._leases[manifest.id] = lease
        try:
            await worker.execute(list_all_tools)
            self._registration_matches(manifest)
        except BaseException as error:
            lease.lost = True
            self.ownership.lose(context_id, "initialization_failed")
            await worker.close()
            self._leases.pop(manifest.id, None)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BrokerError(
                "app session could not be acquired; operator recovery is required"
            ) from None
        await self._renew(lease)
        return lease

    async def _shared_lease(self, manifest: ServiceManifest) -> Lease:
        # Caller holds the service lock. Only clean idle retirement may reopen;
        # failure, cancellation and restart keep the durable recovery barrier.
        await self._expire(manifest.id)
        self._registration_matches(manifest)
        lease = self._leases.get(manifest.id)
        if lease is None:
            lease = await self._open_lease(manifest, None)
        return lease

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
                await self._open_lease(manifest, owner)
                return self._lease_info(service_id, owner)
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
        if lease.manifest.session_mode == "shared":
            return self._shared_info(lease)
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

    def _shared_info(self, lease: Lease) -> dict:
        lost = lease.lost or not lease.worker.alive
        return {
            "service_id": lease.manifest.id,
            "session_mode": "shared",
            "state": "lost"
            if lost
            else ("shared_expired" if lease.expires <= self._clock() else "shared_active"),
            "expires_in_seconds": max(0, round(lease.expires - self._clock(), 3)),
            "recovery_required": lost,
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
        if (
            not lease
            or lease.manifest.session_mode != "exclusive"
            or lease.owner != owner
            or lease.context_id != context_id
        ):
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
        relay: ElicitationTarget | None = None,
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
        if write and not entry.manifest.permits_write(entry.tool.name):
            raise BrokerError("tool is not enabled by this app's allowed_write_tools policy")
        if write and request_id is None:
            raise BrokerError("write calls require a unique request_id")
        try:
            size = len(
                json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise BrokerError("arguments must contain valid finite JSON values") from None
        if size > entry.manifest.max_argument_bytes:
            raise BrokerError(
                "arguments exceed this app's max_argument_bytes "
                f"({entry.manifest.max_argument_bytes})"
            )
        try:
            validator_for(entry.tool.inputSchema)(entry.tool.inputSchema).validate(arguments)
        except ValidationError:
            raise BrokerError("arguments do not match the app's input schema") from None

        claimed = False
        dispatch_closed = False

        def close_dispatch():
            nonlocal dispatch_closed
            dispatch_closed = True
            if relay is not None:
                relay.close()

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
                raise BrokerError("non-exclusive services do not accept a context_id")

            async def invoke(session):
                nonlocal claimed
                live = {tool.name: tool for tool in await list_all_tools(session)}
                if (
                    entry.tool.name not in live
                    or CatalogTool(entry.manifest, live[entry.tool.name]).revision != entry.revision
                ):
                    raise BrokerError("app tool definition changed; refresh and describe again")
                self._registration_unchanged(entry)
                if dispatch_closed:
                    raise BrokerError("app call ended before dispatch")
                if write:
                    fingerprint = canonical_digest(
                        {"tool_id": tool_id, "revision": catalog_revision, "args": arguments}
                    )
                    self.journal.claim(request_id, fingerprint, tool_id)
                    claimed = True
                if relay is not None:
                    relay.label = f"App {entry.id} requests input:\n"
                    if relay.progress is not None:
                        relay.progress.label = f"App {entry.id} progress:\n"
                result = await routed_call(session, entry.tool.name, arguments, relay)
                if write:
                    self.journal.finish(request_id, "app_error" if result.isError else "completed")
                return result

            try:
                async with asyncio.timeout(
                    entry.manifest.timeout_seconds
                    if entry.manifest.session_mode == "shared"
                    else None
                ):
                    if entry.manifest.session_mode == "shared":
                        lease = await self._shared_lease(entry.manifest)
                    if lease:
                        result = await lease.worker.execute(invoke, on_cancel=close_dispatch)
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
                    + (
                        "; service context is lost; operator recovery is required"
                        if self._recovery_blocker(entry.manifest)
                        else ""
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
