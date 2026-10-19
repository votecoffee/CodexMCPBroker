"""Catalog providers are opt-in search metadata, scoped to native session lifetime."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import anyio
import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError
from test_core import manifest
from test_sessions import OWNER_A, OWNER_B

import universal_tools.broker as broker_module
from universal_tools.broker import Broker
from universal_tools.capabilities import MAX_CATALOG_BYTES, parse_document
from universal_tools.models import BrokerError, atomic_json


def document(scope="public"):
    return {
        "schema_version": 1,
        "catalog_revision": "opaque-app-revision:" + scope,
        "capabilities": [
            {
                "id": "holes.drill",
                "description": "Create a counterbore; fixture scope " + scope,
                "tags": ["machining", "holes"],
                "discovery": {
                    "tool": "describe_feature",
                    "arguments": {"capability": "holes.drill"},
                },
            }
        ],
    }


def result(value, *, text=False):
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value))],
        structuredContent=None if text else value,
    )


@pytest.mark.parametrize("text", [False, True])
def test_normalized_catalog_accepts_structured_or_single_json_text(text):
    assert (
        parse_document(result(document(), text=text)).model_dump(mode="json", by_alias=True)
        == document()
    )


@pytest.mark.parametrize(
    "mode",
    [
        "version",
        "boolean_version",
        "float_version",
        "duplicate",
        "extra",
        "size",
        "bad_id",
        "tags",
        "count",
    ],
)
def test_catalog_rejects_invalid_or_unbounded_metadata(mode):
    value = document()
    if mode == "version":
        value["schema_version"] = 2
    elif mode == "boolean_version":
        value["schema_version"] = True
    elif mode == "float_version":
        value["schema_version"] = 1.0
    elif mode == "duplicate":
        value["capabilities"] *= 2
    elif mode == "extra":
        value["unreviewed_extension"] = "PRIVATE-DO-NOT-ECHO"
    elif mode == "size":
        value["catalog_revision"] = "x" * MAX_CATALOG_BYTES
    elif mode == "bad_id":
        value["capabilities"][0]["id"] = "../route"
    elif mode == "tags":
        value["capabilities"][0]["tags"] = ["x" * 65]
    else:
        item = value["capabilities"][0]
        value["capabilities"] = [{**item, "id": f"feature{i}"} for i in range(1001)]
    with pytest.raises(BrokerError, match="invalid or exceeds") as error:
        parse_document(result(value))
    assert "PRIVATE" not in str(error.value)


def test_app_error_and_ambiguous_text_are_not_silently_indexed():
    with pytest.raises(BrokerError, match="app error"):
        parse_document(CallToolResult(content=[], isError=True))
    value = result(document(), text=True)
    value.content *= 2
    with pytest.raises(BrokerError):
        parse_document(value)


@pytest.mark.parametrize(
    "provider",
    [
        {"tool": "catalog", "schema_version": 2},
        {"tool": "catalog", "schema_version": True},
        {"tool": "catalog", "schema_version": 1.0},
        {"tool": "app/catalog"},
        {"tool": "catalog", "arguments": {"x": "z" * (16 * 1024)}},
        {"tool": "catalog", "arguments": {"x": float("nan")}},
    ],
)
def test_provider_registration_is_versioned_and_bounded(provider):
    with pytest.raises(ValidationError):
        manifest(capability_catalog=provider)


class Controller:
    mode = "valid"
    provider_read = True
    discovery_read = True
    definition = "Inspect one feature"


class CatalogSession:
    def __init__(self, scope, control):
        self.scope, self.control = scope, control
        self.initialization = {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "Synthetic catalog app", "version": "1"},
            "capabilities": {"tools": {}},
        }
        self.task = asyncio.current_task()
        self.calls = []
        self.started = asyncio.Event()
        self.closed = False

    async def list_tools(self, *, params=None):
        assert asyncio.current_task() is self.task
        tools = [
            Tool(
                name="catalog",
                description="List installed features",
                inputSchema={"type": "object", "additionalProperties": False},
                annotations=ToolAnnotations(readOnlyHint=self.control.provider_read),
            ),
            Tool(
                name="describe_feature",
                description=self.control.definition,
                inputSchema={
                    "type": "object",
                    "properties": {"capability": {"type": "string"}},
                    "required": ["capability"],
                    "additionalProperties": False,
                },
                annotations=ToolAnnotations(readOnlyHint=self.control.discovery_read),
            ),
            Tool(
                name="execute",
                description="Apply a reviewed feature",
                inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=False),
            ),
        ]
        if self.control.mode == "external_schema":
            tools[0].output_schema = {"$ref": "https://example.invalid/schema"}
        return ListToolsResult(tools=tools)

    async def call_tool(self, name, arguments):
        assert asyncio.current_task() is self.task
        self.calls.append(name)
        self.started.set()
        if name == "describe_feature":
            return result(
                {"capability": arguments["capability"], "catalog_revision": "native:" + self.scope}
            )
        assert name == "catalog", "a metadata probe must never execute a feature"
        if self.control.mode == "disconnect":
            raise ConnectionError("PRIVATE-TRANSPORT-DETAIL")
        if self.control.mode == "stall":
            await asyncio.Event().wait()
        if self.control.mode == "error":
            return CallToolResult(
                content=[TextContent(type="text", text="PRIVATE-ERROR")], isError=True
            )
        value = document(self.scope)
        if self.control.mode == "invalid":
            value["schema_version"] = 2
        return result(value)


@pytest.fixture
async def catalogs(tmp_path, monkeypatch):
    sessions, brokers = [], []
    control = Controller()

    @asynccontextmanager
    async def connect(config):
        async with anyio.create_task_group():
            session = CatalogSession(str(len(sessions) + 1), control)
            sessions.append(session)
            try:
                yield session
            finally:
                assert asyncio.current_task() is session.task
                session.closed = True

    monkeypatch.setattr(broker_module, "service_session", connect)

    async def create(**overrides):
        broker = Broker(tmp_path / str(len(brokers)))
        brokers.append(broker)
        registration = manifest(**{"capability_catalog": {"tool": "catalog"}, **overrides})
        atomic_json(
            broker.registry / "app.json", registration.model_dump(mode="json", by_alias=True)
        )
        await broker.refresh()
        return broker, sessions, control

    yield create
    for broker in brokers:
        await broker.aclose()
    assert all(session.closed for session in sessions)


async def test_catalog_search_resolves_a_read_only_discovery_reference_without_execution(catalogs):
    broker, sessions, _ = await catalogs()
    assert broker.search("counterbore", kind="tool")["total"] == 0
    match = broker.search("counterbore", kind="capability")["matches"][0]
    assert match["kind"] == "capability" and not match["callable"] and match["discovery_only"]
    before = len(sessions)
    description = await broker.describe_capability(
        "app", match["capability_id"], match["capability_revision"], owner_id=None
    )
    assert len(sessions) == before
    reference = description["discovery"]
    assert reference["tool_id"] == "app/describe_feature"
    native = await broker.call(
        reference["tool_id"], reference["catalog_revision"], reference["arguments"], write=False
    )
    assert native.structured_content["capability"] == "holes.drill"
    assert sessions[-1].calls == ["describe_feature"]
    with pytest.raises(BrokerError, match="unknown tool_id"):
        await broker.call("app/holes.drill", match["capability_revision"], {}, write=False)
    with pytest.raises(BrokerError, match="writes are disabled"):
        await broker.call(
            "app/execute",
            broker.describe("app/execute")["catalog_revision"],
            {},
            write=True,
            request_id="catalog-not-permission",
        )


async def test_inner_revision_changes_without_changing_the_outer_tool_revision(catalogs):
    broker, _, _ = await catalogs()
    first = broker.search("counterbore")["matches"][0]
    outer = broker.describe("app/describe_feature")["catalog_revision"]
    await broker.refresh_capabilities("app", owner_id=None)
    second = broker.search("counterbore")["matches"][0]
    assert second["capability_revision"] != first["capability_revision"]
    assert second["app_catalog_revision"] != first["app_catalog_revision"]
    assert outer == broker.describe("app/describe_feature")["catalog_revision"]
    with pytest.raises(BrokerError, match="capability revision changed"):
        await broker.describe_capability(
            "app", "holes.drill", first["capability_revision"], owner_id=None
        )


async def test_invalid_provider_evicts_old_hints_but_preserves_outer_tool_routes(catalogs):
    broker, _, control = await catalogs()
    control.mode = "invalid"
    status = await broker.refresh_capabilities("app", owner_id=None)
    assert status["status"] == "unavailable"
    assert broker.search(kind="capability")["total"] == 0
    assert broker.search(kind="tool")["total"] == 3
    assert broker.list_services()["capability_count"] == 0
    assert broker.list_services()["services"][0]["status"] == "available"


@pytest.mark.parametrize("which", ["provider", "discovery"])
@pytest.mark.parametrize("hint", [False, None])
async def test_provider_and_reference_must_explicitly_declare_reads(catalogs, which, hint):
    broker, sessions, control = await catalogs(read_only_tools=["catalog", "describe_feature"])
    setattr(control, which + "_read", hint)
    status = await broker.refresh_capabilities("app", owner_id=None)
    assert status["status"] == "unavailable" and "readOnlyHint" in status["error"]
    assert sessions[-1].calls == ([] if which == "provider" else ["catalog"])


async def test_unsafe_provider_output_schema_is_blocked_before_dispatch(catalogs):
    broker, sessions, control = await catalogs()
    control.mode = "external_schema"
    status = await broker.refresh_capabilities("app", owner_id=None)
    assert status["status"] == "unavailable" and not sessions[-1].calls


async def test_owned_catalog_requires_explicit_refresh_and_never_leaks_on_global_refresh(catalogs):
    broker, sessions, _ = await catalogs(session_mode="exclusive")
    public = broker.search("counterbore")["matches"][0]
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    assert sessions[-1].calls == []  # Acquisition never guesses a provider invocation.
    assert broker.search(kind="capability", owner_id=OWNER_A)["total"] == 0
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    owned = broker.search("counterbore", owner_id=OWNER_A)["matches"][0]
    assert owned["metadata_source"] == "owned_session"
    assert owned["app_catalog_revision"] != public["app_catalog_revision"]
    before = list(sessions[-1].calls)
    await broker.refresh()
    assert (
        sessions[-1].calls == before
    )  # Another frontend's refresh cannot probe an owner's catalog.
    assert broker.search("counterbore", owner_id=OWNER_B)["matches"][0] == public
    assert broker.search("counterbore", owner_id=OWNER_A)["matches"][0] == owned
    with pytest.raises(BrokerError, match="capability revision changed"):
        await broker.describe_capability(
            "app", "holes.drill", owned["capability_revision"], owner_id=OWNER_B
        )
    with pytest.raises(BrokerError, match="owned by another"):
        await broker.refresh_capabilities("app", context, owner_id=OWNER_B)


async def test_description_is_detached_and_does_not_contact_app_or_renew_lease(catalogs):
    broker, sessions, _ = await catalogs(session_mode="exclusive")
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    match = broker.search(kind="capability", owner_id=OWNER_A)["matches"][0]
    expiry, calls = broker._leases["app"].expires, list(sessions[-1].calls)
    first = await broker.describe_capability(
        "app", "holes.drill", match["capability_revision"], owner_id=OWNER_A
    )
    original = deepcopy(first)
    first["discovery"]["arguments"]["capability"] = "changed"
    again = await broker.describe_capability(
        "app", "holes.drill", match["capability_revision"], owner_id=OWNER_A
    )
    assert again == original and broker._leases["app"].expires == expiry
    assert sessions[-1].calls == calls


async def test_failed_owned_metadata_does_not_discard_healthy_native_context(catalogs):
    broker, _, control = await catalogs(session_mode="exclusive")
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    control.mode = "error"
    status = await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    assert status["status"] == "unavailable" and "PRIVATE" not in str(status)
    assert broker.search(kind="capability", owner_id=OWNER_A)["total"] == 0
    assert broker.search(kind="capability", owner_id=OWNER_B)["total"] == 1
    assert (await broker.service_session_status("app", owner_id=OWNER_A))["state"] == "active"
    control.mode = "valid"
    assert (await broker.refresh_capabilities("app", context, owner_id=OWNER_A))[
        "status"
    ] == "available"


@pytest.mark.parametrize("mode", ["disconnect", "stall"])
async def test_provider_transport_loss_keeps_the_owned_recovery_barrier(catalogs, mode):
    broker, sessions, control = await catalogs(session_mode="exclusive", timeout_seconds=0.2)
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    control.mode = mode
    with pytest.raises(BrokerError):
        await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    assert broker.search(kind="capability", owner_id=OWNER_A)["total"] == 0
    assert (await broker.service_session_status("app", owner_id=OWNER_A))["recovery_required"]
    probes = sum(session.calls.count("catalog") for session in sessions)
    await broker.refresh()
    assert probes == sum(session.calls.count("catalog") for session in sessions)


async def test_revocation_hides_catalog_without_another_probe(catalogs):
    broker, sessions, _ = await catalogs()
    match = broker.search(kind="capability")["matches"][0]
    count = len(sessions)
    (broker.registry / "app.json").unlink()
    assert broker.search(kind="capability")["total"] == 0
    with pytest.raises(BrokerError, match="removed"):
        await broker.describe_capability(
            "app", "holes.drill", match["capability_revision"], owner_id=None
        )
    assert len(sessions) == count


async def test_existing_services_never_guess_or_invoke_a_catalog_provider(catalogs):
    broker, sessions, _ = await catalogs(capability_catalog=None)
    assert not sessions[-1].calls
    assert broker.search(kind="capability")["total"] == 0
    assert broker.search(kind="tool")["total"] == 3
    assert (await broker.describe_service("app", owner_id=None))["capability_catalog"] == {
        "status": "not_configured"
    }


async def test_changed_outer_discovery_definition_invalidates_owned_index(catalogs):
    broker, _, control = await catalogs(session_mode="exclusive")
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    previous = broker.search(kind="capability", owner_id=OWNER_A)["matches"][0]
    control.definition = "New native discovery contract"
    await broker.refresh()
    assert broker.search(kind="capability", owner_id=OWNER_A)["total"] == 0
    with pytest.raises(BrokerError, match="definitions changed"):
        await broker.describe_capability(
            "app", "holes.drill", previous["capability_revision"], owner_id=OWNER_A
        )
    assert (await broker.refresh_capabilities("app", context, owner_id=OWNER_A))[
        "status"
    ] == "available"


async def test_expired_ownership_cannot_expose_its_private_snapshot(catalogs):
    broker, _, _ = await catalogs(session_mode="exclusive")
    public = broker.search(kind="capability")["matches"][0]
    context = (await broker.acquire_service("app", owner_id=OWNER_A))["context_id"]
    await broker.refresh_capabilities("app", context, owner_id=OWNER_A)
    expires = broker._leases["app"].expires
    broker._clock = lambda: expires + 1
    assert broker.search(kind="capability", owner_id=OWNER_A)["matches"][0] == public
