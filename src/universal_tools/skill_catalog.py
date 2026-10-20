"""Opt-in Skills-over-MCP catalogs with explicit, atomic publication."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import Resource
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams, Result
from pydantic import Field

from .models import BrokerError, canonical_digest
from .skill_manifest import (
    MCP_PROFILE,
    approval_digest,
    frontmatter,
    safe_uri,
    validate_catalog,
    validate_entry,
)


class SkillParams(RequestParams):
    uri: str = Field(min_length=1, max_length=4096)


class DirectoryParams(PaginatedRequestParams):
    uri: str = Field(min_length=1, max_length=4096)


class SkillList(Result):
    skills: list[dict]
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    ttl_ms: int | None = Field(default=None, alias="ttlMs")
    cache_scope: str | None = Field(default=None, alias="cacheScope")


class SkillGet(Result):
    skill: dict
    ttl_ms: int | None = Field(default=None, alias="ttlMs")
    cache_scope: str | None = Field(default=None, alias="cacheScope")


class SnapshotResource(Resource):
    data: bytes

    async def read(self):
        return self.data.decode("utf-8") if self.mime_type.startswith("text/") else self.data


class SkillCatalog(Extension):
    """Admin-selected snapshots and approved downstream skills; no code executes.

    Live publication requires ExtensionServer, used by the framework surface.
    A plain MCPServer can serve the initial local snapshot via resources().
    Filesystem changes never silently change published bytes.
    """

    identifier = "io.modelcontextprotocol/skills"

    def __init__(self, packages=(), *, profile=MCP_PROFILE, page_size=32, directory_read=False):
        if not 1 <= page_size <= 64:
            raise ValueError("skill page size must be between one and 64")
        self.profile = profile
        self.page_size = page_size
        self.directory_read = directory_read
        self.listeners = set()
        self.generation = 0
        self._entries = {}
        self._files = {}
        self._remote = {}
        self._refresh_lock = asyncio.Lock()
        total = 0
        for package in packages:
            root = Path(package)
            if (
                root.is_symlink()
                or getattr(root, "is_junction", lambda: False)()
                or not root.is_dir()
            ):
                raise BrokerError("skill package must be an ordinary directory")
            files = {}
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise BrokerError("skill package links are not supported")
                if not path.is_file():
                    continue
                relative = path.relative_to(root)
                if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                    raise BrokerError("hidden files and caches are not skill resources")
                if len(files) >= profile.max_files or path.stat().st_size > profile.max_file_bytes:
                    raise BrokerError("skill package exceeds its file budget")
                data = path.read_bytes()
                if len(data) > profile.max_file_bytes:
                    raise BrokerError("skill file grew beyond its budget")
                total += len(data)
                if total > 64 * 1024 * 1024:
                    raise BrokerError("local skill snapshots exceed 64 MiB")
                files[relative.as_posix()] = data
            if "SKILL.md" not in files:
                raise BrokerError("invalid skill frontmatter: missing SKILL.md")
            header = frontmatter(files["SKILL.md"], limit=profile.max_instruction_bytes)
            name = header["name"]
            uri = f"skill://{name}/SKILL.md"
            if uri in self._entries:
                raise BrokerError("duplicate skill name")
            manifest = []
            for relative, data in files.items():
                resource_uri = f"skill://{name}/{quote(relative, safe='/')}"
                manifest.append(
                    {
                        "uri": resource_uri,
                        "size": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    }
                )
                self._files[resource_uri] = data
            self._entries[uri] = {"uri": uri, "frontmatter": header, "resources": manifest}
        self._validate()

    def _validate(self):
        validate_catalog(list(self._entries.values()), self.profile)
        if len(self._entries) > self.profile.max_skills:
            raise BrokerError("too many skill packages for the host profile")
        names = set()
        paths = set()
        for entry in self._entries.values():
            validate_entry(entry, self.profile)
            name = entry["frontmatter"]["name"]
            if self.profile.unique_names and name in names:
                raise BrokerError("host profile requires unique skill names")
            names.add(name)
            for resource in entry["resources"]:
                uri = resource["uri"].casefold()
                if uri in paths:
                    raise BrokerError("published resource paths conflict")
                paths.add(uri)
        self.revision = canonical_digest(self._entries)

    def settings(self):
        return {"directoryRead": True} if self.directory_read else {}

    def resources(self):
        return tuple(
            ResourceBinding(
                SnapshotResource(
                    uri=uri,
                    name=uri.rsplit("/", 1)[-1],
                    data=raw,
                    mime_type=mimetypes.guess_type(uri)[0] or "application/octet-stream",
                )
            )
            for uri, raw in self._files.items()
        )

    def methods(self):
        methods = [
            MethodBinding("skills/list", PaginatedRequestParams, self.list_skills),
            MethodBinding("skills/get", SkillParams, self.get_skill),
        ]
        if self.directory_read:
            methods.append(
                MethodBinding("resources/directory/read", DirectoryParams, self.read_directory)
            )
        return tuple(methods)

    def _options(self, ctx):
        return {"ttlMs": 0, "cacheScope": "private"} if ctx.protocol_version == "2026-07-28" else {}

    def _page(self, values, cursor, *, scope):
        offset = 0
        if cursor:
            try:
                revision, cursor_scope, position = cursor.split(":")
                offset = int(position)
                if (
                    revision != self.revision
                    or cursor_scope != scope
                    or offset <= 0
                    or offset >= len(values)
                    or offset % self.page_size
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise MCPError(INVALID_PARAMS, "stale or invalid skill cursor") from None
        page = values[offset : offset + self.page_size]
        following = offset + len(page)
        next_cursor = f"{self.revision}:{scope}:{following}" if following < len(values) else None
        return deepcopy(page), next_cursor

    async def list_skills(self, ctx, params):
        page, cursor = self._page(list(self._entries.values()), params.cursor, scope="skills")
        return SkillList(skills=page, nextCursor=cursor, **self._options(ctx))

    async def get_skill(self, ctx, params):
        if params.uri not in self._entries:
            raise MCPError(INVALID_PARAMS, "unknown skill URI")
        return SkillGet(skill=deepcopy(self._entries[params.uri]), **self._options(ctx))

    async def read_directory(self, ctx, params):
        try:
            safe_uri(params.uri)
        except (BrokerError, ValueError):
            raise MCPError(INVALID_PARAMS, "invalid skill directory URI") from None
        prefix = params.uri + "/"
        children = {}
        for entry in self._entries.values():
            for resource in entry["resources"]:
                uri = resource["uri"]
                if not uri.startswith(prefix):
                    continue
                name, separator, _ = uri[len(prefix) :].partition("/")
                child = prefix + name
                children[child] = {
                    "uri": child,
                    "name": name,
                    "mimeType": "inode/directory"
                    if separator
                    else (mimetypes.guess_type(child)[0] or "application/octet-stream"),
                }
        if not children:
            raise MCPError(INVALID_PARAMS, "unknown or non-directory skill URI")
        scope = hashlib.sha256(params.uri.encode()).hexdigest()[:16]
        page, cursor = self._page(
            sorted(children.values(), key=lambda r: r["uri"]), params.cursor, scope=scope
        )
        return {
            "resources": page,
            **({"nextCursor": cursor} if cursor else {}),
            **self._options(ctx),
        }

    def resource_metadata(self):
        return [
            {
                "uri": r["uri"],
                "name": r["uri"].rsplit("/", 1)[-1],
                "mimeType": mimetypes.guess_type(r["uri"])[0] or "application/octet-stream",
            }
            for entry in self._entries.values()
            for r in entry["resources"]
        ]

    def owns_resource(self, uri):
        return uri in self._files or uri in self._remote

    def claims_uri(self, uri):
        return uri.startswith("skill://")

    async def read_bytes(self, uri):
        if uri in self._files:
            return self._files[uri]
        remote = self._remote.get(uri)
        if remote is None:
            raise MCPError(INVALID_PARAMS, "unknown skill resource")
        registry, origin, skill, resource, digest = remote
        if approval_digest(registry.approved_entry(origin, skill)) != digest:
            raise MCPError(INVALID_PARAMS, "downstream skill changed; republish after approval")
        return await registry.read(origin, skill, resource)

    async def publish(self, packages=(), *, registry=None, selections=(), expected_revision=None):
        """Atomically replace the catalog, without fetching downstream files.

        selections are (host-assigned origin, original skill URI). Admin approval
        is required before publication; host loading remains a separate decision.
        """
        async with self._refresh_lock:
            if expected_revision is not None and expected_revision != self.revision:
                raise BrokerError("skill publication revision changed")
            candidate = SkillCatalog(
                packages,
                profile=self.profile,
                page_size=self.page_size,
                directory_read=self.directory_read,
            )
            for origin, skill_uri in selections:
                if registry is None:
                    raise BrokerError("downstream skill publication requires a registry")
                entry = registry.approved_entry(origin, skill_uri)
                origin_id = hashlib.sha256(origin.encode()).hexdigest()[:24]
                skill_id = hashlib.sha256(skill_uri.encode()).hexdigest()[:16]
                prefix = f"skill://broker/{origin_id}/{skill_id}/{entry['frontmatter']['name']}/"
                original_root = skill_uri.removesuffix("SKILL.md")
                rewritten = deepcopy(entry)
                rewritten["uri"] = prefix + "SKILL.md"
                rewritten["_meta"] = {
                    "org.codexmcpbroker/provenance": {
                        "origin": origin,
                        "uri": skill_uri,
                        "manifest": approval_digest(entry),
                    }
                }
                for resource in rewritten["resources"]:
                    original = resource["uri"]
                    uri = prefix + original[len(original_root) :]
                    resource["uri"] = uri
                    candidate._remote[uri] = (
                        registry,
                        origin,
                        skill_uri,
                        original,
                        approval_digest(entry),
                    )
                if rewritten["uri"] in candidate._entries:
                    raise BrokerError("duplicate downstream skill selection")
                candidate._entries[rewritten["uri"]] = rewritten
            candidate._validate()
            self._entries, self._files, self._remote = (
                candidate._entries,
                candidate._files,
                candidate._remote,
            )
            self.revision = candidate.revision
            self.generation += 1
            for listener in tuple(self.listeners):
                try:
                    await asyncio.wait_for(listener(), timeout=0.5)
                except Exception:
                    pass  # Delivery failure cannot undo a committed publication.
            return self.revision
