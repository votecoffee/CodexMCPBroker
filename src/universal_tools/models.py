"""Strict local registration contracts. Registration is a local administrative action."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MANIFEST_BYTES = 256 * 1024
DEFAULT_ARGUMENT_BYTES = 256 * 1024
MAX_ARGUMENT_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 128 * 1024 * 1024
ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
TOOL_NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"


class BrokerError(ValueError):
    """An actionable error safe to show without transport details or credentials."""


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CatalogRequest(StrictModel):
    tool: str = Field(pattern=TOOL_NAME_PATTERN)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def bounded_json(cls, value: dict) -> dict:
        from .budgets import DEFAULT_JSON_BUDGET

        DEFAULT_JSON_BUDGET.check(value)
        try:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(raw.encode("utf-8")) > 16 * 1024:
                raise ValueError("catalog arguments exceed 16 KiB")
        except (TypeError, UnicodeError, RecursionError):
            raise ValueError("catalog arguments must contain finite UTF-8 JSON") from None
        return value


class CapabilityProvider(CatalogRequest):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("capability catalog schema_version must be integer 1")
        return value


class OAuthServiceSettings(StrictModel):
    token_endpoint: str
    client_id: str = Field(min_length=1, max_length=1024)
    scopes: list[str] = Field(default_factory=list, max_length=64)
    client_secret_env: str | None = None
    assertion_env: str | None = None
    dpop_key_env: str | None = None

    @model_validator(mode="after")
    def valid(self):
        from .authorization import https_url

        https_url(self.token_endpoint)
        if not (self.client_secret_env or self.assertion_env):
            raise ValueError(
                "OAuth service grants need a secret or workload assertion environment reference"
            )
        for name in (self.client_secret_env, self.assertion_env, self.dpop_key_env):
            if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid OAuth credential environment reference")
        return self


class HttpTransport(StrictModel):
    type: Literal["streamable-http"]
    url: str
    bearer_token_env: str | None = None
    allow_remote: bool = False
    oauth: OAuthServiceSettings | None = None
    max_sse_event_bytes: int | None = Field(default=None, ge=1024, le=MAX_REQUEST_BODY_BYTES)

    @field_validator("url")
    @classmethod
    def local_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or (parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "::1"})
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.path
        ):
            raise ValueError("v0.1 requires a literal loopback HTTP endpoint with an MCP path")
        if parts.scheme == "http" and not parts.port:
            raise ValueError("an explicit port is required")
        return value

    @model_validator(mode="after")
    def remote_authorization(self):
        if urlsplit(self.url).scheme == "https":
            from .authorization import https_url

            https_url(self.url)
            if not self.allow_remote or not (self.oauth or self.bearer_token_env):
                raise ValueError(
                    "HTTPS registrations require explicit allow_remote and scoped authentication"
                )
        elif self.allow_remote or self.oauth:
            raise ValueError("remote OAuth registrations require HTTPS")
        if self.oauth and self.bearer_token_env:
            raise ValueError("select one authentication profile per endpoint")
        return self

    @field_validator("bearer_token_env")
    @classmethod
    def env_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("invalid environment variable name")
        return value


class StdioTransport(StrictModel):
    type: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env_vars: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def absolute_paths(self) -> StdioTransport:
        if not Path(self.command).is_absolute():
            raise ValueError("stdio command must be an absolute executable path")
        if self.cwd is not None and not Path(self.cwd).is_absolute():
            raise ValueError("stdio cwd must be absolute")
        for name in self.env_vars:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable name")
        return self


class ServiceManifest(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    transport: Annotated[HttpTransport | StdioTransport, Field(discriminator="type")]
    enabled: bool = True
    allow_writes: bool = False
    # None preserves service-wide enablement; an empty list enables no writes.
    allowed_write_tools: list[Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]] | None = Field(
        default=None, max_length=2000
    )
    # For legacy servers, only app-owner reviewed names can be classified as reads.
    read_only_tools: list[str] = Field(default_factory=list)
    max_argument_bytes: int = Field(default=DEFAULT_ARGUMENT_BYTES, ge=1, le=MAX_ARGUMENT_BYTES)
    max_argument_depth: int = Field(default=64, ge=1, le=128)
    max_argument_nodes: int = Field(default=100_000, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=20.0, ge=0.2, le=300.0)
    session_mode: Literal["transient", "exclusive", "shared"] = "transient"
    lease_seconds: float = Field(default=900.0, ge=5.0, le=86400.0)
    resource_id: str | None = Field(default=None, pattern=ID_PATTERN)
    capability_catalog: CapabilityProvider | None = None
    protocol_mode: Literal["legacy", "auto", "2026-07-28"] = "legacy"
    discovery_cache_ttl_seconds: float = Field(default=0.0, ge=0, le=60)
    watch_catalog: bool = False

    @model_validator(mode="after")
    def retained_watcher(self):
        if self.watch_catalog and (
            self.session_mode == "transient" or self.protocol_mode == "legacy"
        ):
            raise ValueError("catalog watching requires a retained modern/auto connection")
        return self

    @field_validator("allowed_write_tools")
    @classmethod
    def unique_write_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("allowed_write_tools must contain unique exact tool names")
        return value

    def permits_write(self, tool_name: str) -> bool:
        return self.allow_writes and (
            self.allowed_write_tools is None or tool_name in self.allowed_write_tools
        )

    @property
    def resource_keys(self) -> set[str]:
        # Credential variable names do not distinguish two routes to one HTTP app.
        route = self.transport.model_dump(
            mode="json", exclude={"bearer_token_env", "max_sse_event_bytes"}
        )
        keys = {"transport:" + canonical_digest(route)}
        if self.resource_id:
            keys.add("resource:" + self.resource_id)
        return keys

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", by_alias=True))


def load_manifest(path: Path) -> ServiceManifest:
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BrokerError("manifest exceeds 256 KiB")
    return ServiceManifest.model_validate_json(path.read_text(encoding="utf-8-sig"))


class Settings(StrictModel):
    schema_version: Literal[1] = 1
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8768, ge=1024, le=65535)
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    max_request_body_bytes: int = Field(default=MAX_REQUEST_BYTES, ge=1, le=MAX_REQUEST_BODY_BYTES)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_settings(state: Path) -> Settings:
    path = state / "settings.json"
    if not path.is_file():
        raise BrokerError("broker is not initialized; run universal-tools init first")
    return Settings.model_validate_json(path.read_text(encoding="utf-8"))
