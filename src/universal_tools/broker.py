"""Atomic catalog refresh, conservative classification, and per-app call serialization."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, Tool

from .journal import WriteJournal
from .models import BrokerError, ServiceManifest, canonical_digest, load_manifest
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
            "definition": self.tool.model_dump(mode="json", exclude_none=True),
        }


class Broker:
    def __init__(self, state: Path):
        self.state = state
        self.registry = state / "services"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.journal = WriteJournal(state / "writes.sqlite3")
        self.catalog: dict[str, CatalogTool] = {}
        self.services: dict[str, dict] = {}
        self.generation = 0
        self._refresh_lock = asyncio.Lock()
        self._service_locks: dict[str, asyncio.Lock] = {}

    def lock(self, service_id: str) -> asyncio.Lock:
        return self._service_locks.setdefault(service_id, asyncio.Lock())

    async def refresh(self) -> dict:
        async with self._refresh_lock:
            files = sorted(self.registry.glob("*.json"))
            if len(files) > 128:
                raise BrokerError("registry exceeds 128 services")
            catalog = {}
            statuses = {}

            async def discover(path: Path):
                service_id = path.stem
                try:
                    manifest = load_manifest(path)
                    if manifest.id != path.stem:
                        raise BrokerError("manifest filename must be <service-id>.json")
                    if not manifest.enabled:
                        statuses[service_id] = {"service_id": service_id, "status": "disabled"}
                        return
                    async with self.lock(service_id):
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
                    statuses[service_id] = {
                        "service_id": service_id,
                        "display_name": manifest.display_name,
                        "description": manifest.description,
                        "status": "available",
                        "transport": manifest.transport.type,
                        "allow_writes": manifest.allow_writes,
                        "tool_count": len(entries),
                    }
                except Exception as error:
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
                    }
                )
        return {"matches": matches[:limit], "total": len(matches), "generation": self.generation}

    def describe(self, tool_id: str) -> dict:
        return self._entry(tool_id).describe()

    def _entry(self, tool_id: str) -> CatalogTool:
        try:
            return self.catalog[tool_id]
        except KeyError:
            raise BrokerError("unknown tool_id; refresh_services and search_tools first") from None

    def _registration_unchanged(self, entry: CatalogTool) -> None:
        try:
            current = load_manifest(self.registry / f"{entry.manifest.id}.json")
        except Exception:
            raise BrokerError("registration is removed or invalid; refresh_services") from None
        if not current.enabled or current.digest != entry.manifest.digest:
            raise BrokerError("registration changed; refresh_services and describe_tool again")

    async def call(
        self,
        tool_id: str,
        catalog_revision: str,
        arguments: dict[str, Any],
        *,
        write: bool,
        request_id: str | None = None,
    ) -> CallToolResult:
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
            self._registration_unchanged(entry)
            if self._entry(tool_id).revision != entry.revision:
                raise BrokerError("catalog changed while waiting; describe_tool again")
            try:
                async with asyncio.timeout(entry.manifest.timeout_seconds):
                    async with service_session(entry.manifest) as session:
                        live = {tool.name: tool for tool in await list_all_tools(session)}
                        if (
                            entry.tool.name not in live
                            or CatalogTool(entry.manifest, live[entry.tool.name]).revision
                            != entry.revision
                        ):
                            raise BrokerError(
                                "app tool definition changed; refresh and describe again"
                            )
                        # A local revocation during connection setup also takes effect.
                        self._registration_unchanged(entry)
                        if write:
                            fingerprint = canonical_digest(
                                {
                                    "tool_id": tool_id,
                                    "revision": catalog_revision,
                                    "args": arguments,
                                }
                            )
                            self.journal.claim(request_id, fingerprint, tool_id)
                            claimed = True
                        result = await session.call_tool(entry.tool.name, arguments)
                        if write:
                            self.journal.finish(
                                request_id, "app_error" if result.isError else "completed"
                            )
                        return result
            except BaseException as error:
                if claimed:
                    # Even cancellation or process loss leaves a durable claim. Never retry it.
                    self.journal.finish(request_id, "uncertain")
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
