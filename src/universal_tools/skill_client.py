"""Origin-bound, lazy Skills-over-MCP consumer. No installation or execution."""

from __future__ import annotations

import asyncio
import base64
import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass

from mcp.client.extension import ClientExtension
from mcp.types import DiscoverResult, ReadResourceRequestParams, ReadResourceResult, Request

from .models import BrokerError, canonical_digest
from .skill_catalog import SkillGet, SkillList, SkillParams
from .skill_manifest import (
    MCP_PROFILE,
    approval_digest,
    validate_catalog,
    validate_entry,
    verify_file,
)


class SkillClient(ClientExtension):
    """Advertised only when constructed by a host that uses the verifier below."""

    identifier = "io.modelcontextprotocol/skills"


@dataclass(frozen=True)
class SkillSource:
    # A host-assigned stable identity, not serverInfo or a remote-provided name.
    origin: str
    connect: object


class SkillRegistry:
    """One trusted host principal's registry; never share approvals across users.

    Approval here covers loading verified data only. The embedding host must
    independently gate tool grants, nested activation and ALL code execution.
    This registry deliberately has no execution or filesystem-install API.
    """

    def __init__(self, sources, *, profile=MCP_PROFILE, cache_bytes=32 * 1024 * 1024):
        if not 0 <= cache_bytes <= 128 * 1024 * 1024:
            raise ValueError("invalid skill cache budget")
        self.sources = {}
        for source in sources:
            if (
                not isinstance(source.origin, str)
                or not 1 <= len(source.origin) <= 128
                or source.origin in self.sources
            ):
                raise BrokerError("skill sources need unique host-assigned identities")
            self.sources[source.origin] = source
        if len(self.sources) > 64:
            raise BrokerError("too many skill sources")
        self.profile = profile
        self.cache_bytes = cache_bytes
        self._entries = {}
        self._approved = {}
        self._cache = OrderedDict()
        self._cache_size = 0

    @asynccontextmanager
    async def _connection(self, origin):
        try:
            async with asyncio.timeout(30):
                async with self.sources[origin].connect() as session:
                    yield session
        except ExceptionGroup:
            # SDK/AnyIO in-memory lifespans can wrap a body error on exit.
            # Keep a stable, payload-free library error after the source closes.
            raise BrokerError("downstream skill request failed; refresh before loading") from None

    async def _capabilities(self, session):
        # Explicit modern mode can adopt an empty synthetic discovery record.
        # Observe the actual server declaration before sending extension calls.
        if session.protocol_version == "2026-07-28":
            result = DiscoverResult.model_validate(await session.send_discover("2026-07-28"))
            session.adopt(result)
        caps = session.server_capabilities
        if (
            caps is None
            or caps.resources is None
            or not caps.extensions
            or SkillClient.identifier not in caps.extensions
        ):
            raise BrokerError("downstream server has not advertised Skills and Resources")

    def _retain(self, origin, entries, *, replace=False):
        validated = {}
        names = set()
        for entry in entries:
            validate_entry(entry, self.profile)
            uri = entry["uri"]
            name = entry["frontmatter"]["name"]
            if uri in validated or (self.profile.unique_names and name in names):
                raise BrokerError("duplicate skill URI or host-incompatible skill name")
            names.add(name)
            validated[uri] = deepcopy(entry)
        if len(validated) > self.profile.max_skills:
            raise BrokerError("skill catalog exceeds host count budget")
        validate_catalog(list(validated.values()), self.profile)
        prospective = {
            key: entry for key, entry in self._entries.items() if not replace or key[0] != origin
        }
        prospective.update({(origin, uri): entry for uri, entry in validated.items()})
        if (
            len(prospective) > 512
            or sum(
                len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
                for entry in prospective.values()
            )
            > 32 * 1024 * 1024
        ):
            raise BrokerError("skill registry metadata quota exceeded")
        if replace:
            for key in list(self._entries):
                if key[0] == origin and key[1] not in validated:
                    self._entries.pop(key)
                    self._approved.pop(key, None)
        for uri, entry in validated.items():
            key = (origin, uri)
            digest = approval_digest(entry)
            if self._approved.get(key) != digest:
                self._approved.pop(key, None)
            self._entries[key] = entry
        return deepcopy(list(validated.values()))

    async def discover(self, origin):
        entries = []
        cursors = set()
        cursor = None
        try:
            async with self._connection(origin) as session:
                await self._capabilities(session)
                for _ in range(self.profile.max_pages):
                    result = await session.send_request(
                        Request[dict, str](
                            method="skills/list", params={"cursor": cursor} if cursor else {}
                        ),
                        SkillList,
                    )
                    entries.extend(result.skills)
                    if len(entries) > self.profile.max_skills:
                        raise BrokerError("downstream skill catalog exceeds host budget")
                    cursor = result.next_cursor
                    if cursor is None:
                        return self._retain(origin, entries, replace=True)
                    if not cursor or len(cursor) > 4096 or cursor in cursors:
                        raise BrokerError("invalid or repeated downstream skill cursor")
                    cursors.add(cursor)
        except BaseException:
            # A failed refresh cannot leave stale authorizations active.
            self.revoke_origin(origin)
            raise
        self.revoke_origin(origin)
        raise BrokerError("downstream skill catalog exceeds page budget")

    async def refresh(self, origin, uri):
        # Direct get intentionally supports valid, unlisted skills.
        try:
            async with self._connection(origin) as session:
                await self._capabilities(session)
                result = await session.send_request(
                    Request(method="skills/get", params=SkillParams(uri=uri)), SkillGet
                )
            if result.skill.get("uri") != uri:
                raise BrokerError("downstream returned a different skill")
            self._retain(origin, [result.skill])
            return self.describe(origin, uri)
        except BaseException:
            self._approved.pop((origin, uri), None)
            raise

    def describe(self, origin, uri):
        try:
            entry = self._entries[origin, uri]
        except KeyError:
            raise BrokerError("unknown origin-bound skill; discover or get it first") from None
        return {
            "origin": origin,
            "skill": deepcopy(entry),
            "approval_digest": approval_digest(entry),
        }

    def approve(self, origin, uri, expected_digest):
        description = self.describe(origin, uri)
        if description["approval_digest"] != expected_digest:
            raise BrokerError("skill changed before approval")
        self._approved[origin, uri] = expected_digest

    def revoke_origin(self, origin):
        for key in list(self._approved):
            if key[0] == origin:
                self._approved.pop(key)

    def approved_entry(self, origin, uri):
        description = self.describe(origin, uri)
        if self._approved.get((origin, uri)) != description["approval_digest"]:
            raise BrokerError("skill needs approval for its current complete manifest")
        return description["skill"]

    async def read(self, origin, skill_uri, resource_uri):
        try:
            return await self._read(origin, skill_uri, resource_uri)
        except BaseException:
            self._approved.pop((origin, skill_uri), None)
            raise

    async def _read(self, origin, skill_uri, resource_uri):
        entry = self.approved_entry(origin, skill_uri)
        manifest = next((r for r in entry["resources"] if r["uri"] == resource_uri), None)
        if manifest is None:
            raise BrokerError("unlisted skill resource; refresh and reapprove")
        stamp = approval_digest(entry)
        key = (origin, resource_uri, manifest["digest"])
        if key in self._cache:
            raw = self._cache[key]
            self._cache.move_to_end(key)
        else:
            async with self._connection(origin) as session:
                await self._capabilities(session)
                result = await session.send_request(
                    Request(
                        method="resources/read", params=ReadResourceRequestParams(uri=resource_uri)
                    ),
                    ReadResourceResult,
                )
            if len(result.contents) != 1 or str(result.contents[0].uri) != resource_uri:
                raise BrokerError("skill read returned an unexpected resource")
            content = result.contents[0]
            if hasattr(content, "text"):
                if len(content.text) > manifest["size"]:
                    raise BrokerError("skill text exceeds its declared size")
                raw = content.text.encode("utf-8")
            else:
                if len(content.blob) > (manifest["size"] + 2) // 3 * 4:
                    raise BrokerError("skill blob exceeds its declared size")
                try:
                    raw = base64.b64decode(content.blob, validate=True)
                except ValueError:
                    raise BrokerError("invalid skill blob encoding") from None
        # Recheck after asynchronous I/O in case refresh/revocation raced it.
        if approval_digest(self.approved_entry(origin, skill_uri)) != stamp:
            raise BrokerError("skill changed while reading; refresh and reapprove")
        try:
            verify_file(entry, resource_uri, raw)
        except BrokerError:
            self._approved.pop((origin, skill_uri), None)
            self._cache.pop(key, None)
            self._cache_size = sum(len(raw) for raw in self._cache.values())
            raise
        if key not in self._cache and len(raw) <= self.cache_bytes:
            while self._cache and self._cache_size + len(raw) > self.cache_bytes:
                _, previous = self._cache.popitem(last=False)
                self._cache_size -= len(previous)
            self._cache[key] = raw
            self._cache_size += len(raw)
        return raw

    @property
    def revision(self):
        return canonical_digest(
            [
                [origin, uri, approval_digest(entry)]
                for (origin, uri), entry in sorted(self._entries.items())
            ]
        )
