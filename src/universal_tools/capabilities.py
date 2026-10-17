"""Opt-in search metadata; native discovery and execution retain their own contracts."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Literal

from jsonschema.validators import validator_for
from mcp.types import CallToolResult, Tool
from pydantic import Field, field_validator

from .models import (
    TOOL_NAME_PATTERN,
    BrokerError,
    CatalogRequest,
    ServiceManifest,
    StrictModel,
    canonical_digest,
)
from .schemas import check_schema
from .transport import list_all_tools

MAX_CATALOG_BYTES = 512 * 1024


class Capability(StrictModel):
    id: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=4096)
    tags: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=32)
    discovery: CatalogRequest


class CapabilityDocument(StrictModel):
    schema_version: Literal[1]
    catalog_revision: str = Field(min_length=1, max_length=256)
    capabilities: list[Capability] = Field(max_length=1000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("capability catalog schema_version must be integer 1")
        return value


def tool_revision(manifest: ServiceManifest, tool: Tool) -> str:
    return canonical_digest({"service": manifest.digest, "tool": tool.model_dump(mode="json")})


@dataclass(frozen=True)
class CapabilitySnapshot:
    manifest: ServiceManifest
    document: CapabilityDocument
    bindings: dict[str, str]

    @property
    def revision(self):
        return canonical_digest(
            {
                "service": self.manifest.digest,
                "bindings": self.bindings,
                "catalog": self.document.model_dump(mode="json"),
            }
        )

    def status(self, source):
        return {
            "status": "available",
            "metadata_source": source,
            "count": len(self.document.capabilities),
            "capability_revision": self.revision,
            "app_catalog_revision": self.document.catalog_revision,
        }

    def matches(self, source):
        revision = self.revision
        return [self._match(item, source, revision) for item in self.document.capabilities]

    def _match(self, item, source, revision):
        return {
            "kind": "capability",
            "service_id": self.manifest.id,
            "capability_id": item.id,
            "description": item.description,
            "tags": list(item.tags),
            "capability_revision": revision,
            "app_catalog_revision": self.document.catalog_revision,
            "metadata_source": source,
            "discovery_only": True,
            "callable": False,
            "context_required": self.manifest.session_mode == "exclusive",
        }

    def describe(self, capability_id, source):
        for item in self.document.capabilities:
            if item.id == capability_id:
                match = self._match(item, source, self.revision)
                return {
                    **match,
                    "trust": "untrusted_app_data",
                    "discovery": {
                        "tool_id": self.manifest.id + "/" + item.discovery.tool,
                        "catalog_revision": self.bindings[item.discovery.tool],
                        "arguments": deepcopy(item.discovery.arguments),
                    },
                }
        raise BrokerError("unknown capability_id; search_tools or refresh_capabilities")


@dataclass(frozen=True)
class CapabilityState:
    snapshot: CapabilitySnapshot | None = None
    error: str = "capability catalog needs refresh_capabilities"

    def status(self, source):
        if self.snapshot:
            return self.snapshot.status(source)
        return {"status": "unavailable", "metadata_source": source, "count": 0, "error": self.error}


def parse_document(result: CallToolResult) -> CapabilityDocument:
    if result.isError:
        raise BrokerError("capability catalog provider returned an app error")
    try:
        payload = result.structuredContent
        if payload is None:
            if len(result.content) != 1 or result.content[0].type != "text":
                raise ValueError("one JSON object is required")
            text = result.content[0].text
            if len(text.encode("utf-8")) > MAX_CATALOG_BYTES:
                raise ValueError("oversized catalog")
            payload = json.loads(text)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_CATALOG_BYTES:
            raise ValueError("oversized catalog")
        document = CapabilityDocument.model_validate(payload)
        if len({item.id for item in document.capabilities}) != len(document.capabilities):
            raise ValueError("duplicate capability IDs")
        return document
    except Exception:
        raise BrokerError("capability catalog is invalid or exceeds its metadata budget") from None


def read_binding(manifest, tools, request):
    tool = tools.get(request.tool)
    if tool is None or tool.annotations is None or tool.annotations.readOnlyHint is not True:
        raise BrokerError("catalog provider and discovery tools must declare readOnlyHint=true")
    try:
        check_schema(tool.inputSchema)
        if tool.outputSchema is not None:
            check_schema(tool.outputSchema)
        size = len(
            json.dumps(request.arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if size > manifest.max_argument_bytes:
            raise ValueError("service argument limit")
        validator_for(tool.inputSchema)(tool.inputSchema).validate(request.arguments)
    except Exception:
        raise BrokerError(
            "catalog discovery arguments do not fit the tool schema or budget"
        ) from None
    return tool_revision(manifest, tool)


async def read_catalog(session, manifest, tools=None) -> CapabilityState:
    """Protocol/transport failures propagate so an owned worker retains loss semantics."""
    provider = manifest.capability_catalog
    if provider is None:
        raise BrokerError("service has no capability_catalog provider")
    if tools is None:
        tools = await list_all_tools(session)
    by_name = {tool.name: tool for tool in tools}
    try:
        bindings = {provider.tool: read_binding(manifest, by_name, provider)}
    except BrokerError as error:
        return CapabilityState(error=str(error))
    # No elicitation route is bound for an automated metadata probe.
    result = await session.call_tool(provider.tool, deepcopy(provider.arguments))
    try:
        document = parse_document(result)
        for item in document.capabilities:
            bindings[item.discovery.tool] = read_binding(manifest, by_name, item.discovery)
        return CapabilityState(CapabilitySnapshot(manifest, document, bindings))
    except BrokerError as error:
        return CapabilityState(error=str(error))
