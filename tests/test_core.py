from __future__ import annotations

import asyncio
import sqlite3
import sys
from contextlib import asynccontextmanager

import pytest
from jsonschema.exceptions import SchemaError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

import universal_tools.broker as broker_module
from universal_tools.broker import Broker, CatalogTool, check_schema
from universal_tools.cli import initialize, register
from universal_tools.daemon import InstanceLock
from universal_tools.journal import WriteJournal
from universal_tools.models import BrokerError, ServiceManifest, atomic_json, load_manifest


def manifest(service_id="app", **overrides):
    return ServiceManifest.model_validate(
        {
            "id": service_id,
            "display_name": "App",
            "transport": {"type": "streamable-http", "url": "http://127.0.0.1:19999/mcp"},
            **overrides,
        }
    )


def tool(read=True):
    return Tool(
        name="echo",
        description="Echo a value",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        annotations=None if read is None else ToolAnnotations(readOnlyHint=read),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/mcp",
        "http://localhost:1234/mcp",
        "http://127.0.0.1.evil:123/mcp",
        "http://user:password@127.0.0.1:1234/mcp",
        "http://127.0.0.1:1234/mcp?token=secret",
        "http://127.0.0.1/mcp",
        "file:///tmp/mcp",
        "http://127.0.0.1:1234/mcp#fragment",
    ],
)
def test_endpoint_boundary(url):
    with pytest.raises(ValidationError):
        manifest(transport={"type": "streamable-http", "url": url})


@pytest.mark.parametrize("service_id", ["../escape", "APP", "a/b", "", "-a", "a" * 65])
def test_service_id_boundary(service_id):
    with pytest.raises(ValidationError):
        manifest(service_id)


def test_manifest_rejects_extra_fields_and_relative_commands():
    with pytest.raises(ValidationError):
        manifest(secret="no embedded secrets")
    with pytest.raises(ValidationError):
        manifest(transport={"type": "stdio", "command": "python"})
    value = manifest(transport={"type": "stdio", "command": sys.executable})
    assert value.transport.command == sys.executable


def test_registration_does_not_overwrite_implicitly(tmp_path):
    source = tmp_path / "source.json"
    atomic_json(source, manifest().model_dump(mode="json"))
    assert register(tmp_path, source) == "app"
    with pytest.raises(BrokerError, match="already registered"):
        register(tmp_path, source)
    assert register(tmp_path, source, replace=True) == "app"
    assert load_manifest(tmp_path / "services/app.json") == manifest()


def test_init_preserves_credentials_and_port(tmp_path):
    first = initialize(tmp_path, 19998)
    assert initialize(tmp_path, 19998) == first
    with pytest.raises(BrokerError):
        initialize(tmp_path, 19997)


def test_singleton_uses_os_lock(tmp_path):
    with InstanceLock(tmp_path / "lock"), pytest.raises(BrokerError):
        with InstanceLock(tmp_path / "lock"):
            pass
    with InstanceLock(tmp_path / "lock"):
        pass


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.com/schema"},
        {"properties": {"x": {"$dynamicRef": "../other.json"}}},
        {"type": "nonsense"},
        {"$schema": "https://example.com/custom-dialect"},
    ],
)
def test_untrusted_schema_cannot_fetch_network(schema):
    with pytest.raises((BrokerError, SchemaError)):
        check_schema(schema)


def test_local_schema_references_work():
    check_schema(
        {
            "type": "object",
            "$defs": {"x": {"type": "string"}},
            "properties": {"x": {"$ref": "#/$defs/x"}},
        }
    )


@pytest.mark.parametrize(
    "hint,overrides,expected",
    [
        (True, [], True),
        (False, [], False),
        (None, [], False),
        (None, ["echo"], True),
        (False, ["echo"], False),
    ],
)
def test_conservative_classification(hint, overrides, expected):
    entry = CatalogTool(manifest(read_only_tools=overrides), tool(hint))
    assert entry.read_only is expected


def test_journal_survives_restart_and_never_reissues_claim(tmp_path):
    path = tmp_path / "journal.sqlite3"
    WriteJournal(path).claim("request-0001", "fingerprint", "app/echo")
    journal = WriteJournal(path)
    assert journal.status("request-0001")["status"] == "in_progress"
    with pytest.raises(BrokerError, match="already claimed"):
        journal.claim("request-0001", "fingerprint", "app/echo")
    with pytest.raises(BrokerError, match="different write"):
        journal.claim("request-0001", "different", "app/echo")
    journal.finish("request-0001", "uncertain")
    assert journal.status("request-0001")["status"] == "uncertain"


def test_journal_closes_database_handles_after_success_and_conflict(tmp_path, monkeypatch):
    connect = sqlite3.connect
    opened = []

    def record(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", record)
    journal = WriteJournal(tmp_path / "journal.sqlite3")
    journal.claim("closed-handles-001", "fingerprint", "app/edit")
    journal.finish("closed-handles-001", "uncertain")
    assert journal.status("closed-handles-001")["status"] == "uncertain"
    with pytest.raises(BrokerError, match="already claimed"):
        journal.claim("closed-handles-001", "fingerprint", "app/edit")
    assert opened
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


class FakeSession:
    def __init__(self):
        self.initialization = {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "Synthetic app", "version": "1"},
            "capabilities": {"tools": {}},
        }
        self.tools = [tool()]
        self.calls = 0
        self.active = 0
        self.maximum = 0
        self.fail = False

    async def list_tools(self, cursor=None):
        return ListToolsResult(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls += 1
        self.active += 1
        self.maximum = max(self.active, self.maximum)
        try:
            await asyncio.sleep(0.02)
            if self.fail:
                raise ConnectionError("SECRET-DO-NOT-EXPOSE")
            return CallToolResult(content=[TextContent(type="text", text=arguments["value"])])
        finally:
            self.active -= 1


@pytest.fixture
async def fake(tmp_path, monkeypatch):
    session = FakeSession()

    @asynccontextmanager
    async def connect(manifest):
        yield session

    monkeypatch.setattr(broker_module, "service_session", connect)
    broker = Broker(tmp_path)
    atomic_json(broker.registry / "app.json", manifest(allow_writes=True).model_dump(mode="json"))
    await broker.refresh()
    return broker, session


async def call(broker, **kwargs):
    return await broker.call(
        "app/echo",
        broker.describe("app/echo")["catalog_revision"],
        {"value": "test"},
        write=False,
        **kwargs,
    )


async def test_same_app_calls_serialize(fake):
    broker, session = fake
    await asyncio.gather(*(call(broker) for _ in range(6)))
    assert session.calls == 6
    assert session.maximum == 1


async def test_independent_apps_can_run_together(fake):
    broker, session = fake
    atomic_json(broker.registry / "other.json", manifest("other").model_dump(mode="json"))
    await broker.refresh()
    await asyncio.gather(
        call(broker),
        broker.call(
            "other/echo",
            broker.describe("other/echo")["catalog_revision"],
            {"value": "second"},
            write=False,
        ),
    )
    assert session.maximum == 2


async def test_schema_and_catalog_revision_are_checked(fake):
    broker, session = fake
    with pytest.raises(BrokerError, match="revision changed"):
        await broker.call("app/echo", "stale", {"value": "test"}, write=False)
    revision = broker.describe("app/echo")["catalog_revision"]
    with pytest.raises(BrokerError, match="input schema"):
        await broker.call("app/echo", revision, {"value": 3}, write=False)
    assert session.calls == 0


async def test_live_tool_changes_are_not_silently_called(fake):
    broker, session = fake
    session.tools = [tool(False)]
    with pytest.raises(BrokerError, match="definition changed"):
        await call(broker)
    assert session.calls == 0


async def test_removed_registration_is_blocked_without_refresh(fake):
    broker, session = fake
    (broker.registry / "app.json").unlink()
    with pytest.raises(BrokerError, match="removed"):
        await call(broker)
    assert session.calls == 0


async def test_refresh_removes_invalid_and_disabled_services(fake):
    broker, session = fake
    (broker.registry / "app.json").write_text("{broken", encoding="utf-8")
    result = await broker.refresh()
    assert result["tool_count"] == 0
    assert result["services"][0]["status"] == "unavailable"
    atomic_json(broker.registry / "app.json", manifest(enabled=False).model_dump(mode="json"))
    result = await broker.refresh()
    assert result["services"][0]["status"] == "disabled"


async def test_unknown_tools_cannot_use_read_channel(fake):
    broker, session = fake
    session.tools = [tool(None)]
    await broker.refresh()
    with pytest.raises(BrokerError, match="write-capable or unclassified"):
        await call(broker)
    assert session.calls == 0


async def test_write_retries_do_not_dispatch_twice(fake):
    broker, session = fake
    session.tools = [tool(False)]
    await broker.refresh()
    revision = broker.describe("app/echo")["catalog_revision"]
    args = ("app/echo", revision, {"value": "write"})
    await broker.call(*args, write=True, request_id="request-0002")
    with pytest.raises(BrokerError, match="already claimed"):
        await broker.call(*args, write=True, request_id="request-0002")
    assert session.calls == 1
    assert broker.request_status("request-0002")["status"] == "completed"


async def test_uncertain_write_is_durable_and_error_redacted(fake):
    broker, session = fake
    session.tools = [tool(False)]
    session.fail = True
    await broker.refresh()
    revision = broker.describe("app/echo")["catalog_revision"]
    with pytest.raises(BrokerError, match="outcome may be uncertain") as error:
        await broker.call(
            "app/echo", revision, {"value": "write"}, write=True, request_id="request-0003"
        )
    assert "SECRET" not in str(error.value)
    assert broker.request_status("request-0003")["status"] == "uncertain"


async def test_write_policy_revocation_is_immediate(fake):
    broker, session = fake
    session.tools = [tool(False)]
    await broker.refresh()
    revision = broker.describe("app/echo")["catalog_revision"]
    atomic_json(broker.registry / "app.json", manifest(allow_writes=False).model_dump(mode="json"))
    with pytest.raises(BrokerError, match="registration changed"):
        await broker.call(
            "app/echo", revision, {"value": "write"}, write=True, request_id="request-0004"
        )
    assert session.calls == 0


async def test_stale_schema_revision_changes_on_policy_and_metadata(fake):
    broker, session = fake
    before = broker.describe("app/echo")["catalog_revision"]
    session.tools[0].description = "Updated app operation"
    await broker.refresh()
    assert before != broker.describe("app/echo")["catalog_revision"]


async def test_search_and_namespace_collisions(fake):
    broker, session = fake
    atomic_json(broker.registry / "second.json", manifest("second").model_dump(mode="json"))
    await broker.refresh()
    assert [item["tool_id"] for item in broker.search("echo")["matches"]] == [
        "app/echo",
        "second/echo",
    ]
    assert broker.search("value", service_id="app")["total"] == 1
    with pytest.raises(BrokerError):
        broker.search(limit=0)
