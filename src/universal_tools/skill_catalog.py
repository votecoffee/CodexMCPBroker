"""Opt-in, immutable SEP-2640 skill snapshots for SDK 2.2/2.3 extension hosts.

Only an app administrator selects filesystem roots. No contributed code executes.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

import yaml
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import Resource
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams, Result
from pydantic import Field

from .models import BrokerError, canonical_digest


class SkillParams(RequestParams):
    uri: str = Field(min_length=1, max_length=4096)


class SkillList(Result):
    skills: list[dict]
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
    """A supplied package snapshot, never an implicit directory or service scan.

    Use a new server/plugin lifecycle to replace the snapshot. Permission-bearing
    frontmatter is untrusted text; this provider never installs or grants it.
    """

    identifier = "io.modelcontextprotocol/skills"

    def __init__(self, packages):
        self._skills = {}
        self._resources = []
        total = 0
        for package in packages:
            root = Path(package)
            if len(self._skills) >= 64:
                raise BrokerError("too many skill packages")
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
                if any(
                    part.startswith(".") or part == "__pycache__"
                    for part in path.relative_to(root).parts
                ):
                    raise BrokerError("hidden files and caches are not skill resources")
                if len(files) >= 128 or path.stat().st_size > 4 * 1024 * 1024:
                    raise BrokerError("skill package exceeds its file budget")
                data = path.read_bytes()
                if len(data) > 4 * 1024 * 1024:
                    raise BrokerError("skill file grew beyond its budget")
                total += len(data)
                if total > 32 * 1024 * 1024:
                    raise BrokerError("skill snapshots exceed 32 MiB")
                files[path.relative_to(root).as_posix()] = data
            try:
                if len(files["SKILL.md"]) > 64 * 1024:
                    raise ValueError()
                text = files["SKILL.md"].decode("utf-8").replace("\r\n", "\n")
                if not text.startswith("---\n"):
                    raise ValueError()
                frontmatter = text.split("---", 2)[1]
                if any(
                    isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(frontmatter)
                ):
                    raise ValueError()
                header = yaml.safe_load(frontmatter)
                name = header["name"]
                canonical_digest(header)
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or len(name) > 64
                    or not isinstance(header.get("description"), str)
                    or not header["description"].strip()
                    or len(header["description"]) > 1024
                ):
                    raise ValueError()
            except (KeyError, ValueError, TypeError, UnicodeError, yaml.YAMLError):
                raise BrokerError("invalid skill frontmatter") from None
            if name in self._skills:
                raise BrokerError("duplicate skill name")
            manifest = []
            for relative, data in files.items():
                uri = f"skill://{name}/{quote(relative, safe='/')}"
                manifest.append(
                    {
                        "uri": uri,
                        "size": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    }
                )
                self._resources.append(
                    ResourceBinding(
                        SnapshotResource(
                            uri=uri,
                            name=relative,
                            mime_type=mimetypes.guess_type(relative)[0]
                            or "application/octet-stream",
                            data=data,
                        )
                    )
                )
            self._skills[f"skill://{name}/SKILL.md"] = {
                "uri": f"skill://{name}/SKILL.md",
                "frontmatter": header,
                "resources": manifest,
            }
        self.revision = canonical_digest(self._skills)

    def resources(self):
        return tuple(self._resources)

    def methods(self):
        return (
            MethodBinding("skills/list", PaginatedRequestParams, self.list_skills),
            MethodBinding("skills/get", SkillParams, self.get_skill),
        )

    async def list_skills(self, ctx, params):
        if params.cursor:
            raise MCPError(INVALID_PARAMS, "unknown skill cursor")
        options = (
            {"ttlMs": 60000, "cacheScope": "private"}
            if ctx.protocol_version == "2026-07-28"
            else {}
        )
        return SkillList(skills=deepcopy(list(self._skills.values())), **options)

    async def get_skill(self, ctx, params):
        if params.uri not in self._skills:
            raise MCPError(INVALID_PARAMS, "unknown skill URI")
        options = (
            {"ttlMs": 60000, "cacheScope": "private"}
            if ctx.protocol_version == "2026-07-28"
            else {}
        )
        return SkillGet(skill=deepcopy(self._skills[params.uri]), **options)
