"""Failed metadata discovery stays redacted, isolated and free of retries."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ListToolsResult, Tool, ToolAnnotations
from test_sessions import OWNER_A, acquire
from test_sessions import stateful as stateful

import universal_tools.broker as broker_module
from universal_tools.broker import Broker
from universal_tools.capabilities import CapabilityState
from universal_tools.models import BrokerError, ServiceManifest, atomic_json


@pytest.fixture
async def discovery(tmp_path, monkeypatch):
    control = {
        "block": None,
        "error": None,
        "connects": [],
        "closed": [],
        "entered": asyncio.Event(),
    }

    async def stage(service_id, phase):
        if service_id != "bad":
            return
        if control["block"] == phase:
            control["entered"].set()
            await asyncio.Event().wait()
        if control["error"] is not None and phase == "list_tools":
            raise control["error"]

    class Session:
        initialization = {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "Synthetic discovery", "version": "1"},
            "capabilities": {"tools": {}},
        }

        def __init__(self, service_id):
            self.service_id = service_id

        async def list_tools(self, cursor=None):
            await stage(self.service_id, "list_tools")
            return ListToolsResult(
                tools=[
                    Tool(
                        name="inspect",
                        inputSchema={"type": "object"},
                        annotations=ToolAnnotations(readOnlyHint=True),
                    )
                ]
            )

    @asynccontextmanager
    async def connect(config):
        control["connects"].append(config.id)
        try:
            await stage(config.id, "connect_initialize")
            yield Session(config.id)
        finally:
            try:
                await stage(config.id, "close_session")
            finally:
                control["closed"].append(config.id)

    async def catalog(session, config, tools):
        await stage(config.id, "capability_catalog")
        return CapabilityState()

    monkeypatch.setattr(broker_module, "service_session", connect)
    monkeypatch.setattr(broker_module, "read_catalog", catalog)
    broker = Broker(tmp_path)
    for service_id, port in [("bad", 19981), ("healthy", 19982)]:
        manifest = ServiceManifest.model_validate(
            {
                "id": service_id,
                "display_name": "Synthetic discovery",
                "transport": {"type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp"},
                "timeout_seconds": 0.2,
                "capability_catalog": {"tool": "inspect"},
            }
        )
        atomic_json(broker.registry / f"{service_id}.json", manifest.model_dump(mode="json"))
    await broker.refresh()
    assert set(broker.catalog) == {"bad/inspect", "healthy/inspect"}
    try:
        yield broker, control
    finally:
        await broker.aclose()


@pytest.mark.parametrize(
    "phase", ["connect_initialize", "list_tools", "capability_catalog", "close_session"]
)
async def test_timeout_identifies_last_stage_and_evicts_only_failed_app(discovery, phase):
    broker, control = discovery
    control["block"] = phase
    await broker.refresh()
    assert broker.services["bad"]["failure"] == {
        "phase": phase,
        "kind": "timeout",
        "timeout_seconds": 0.2,
    }
    assert broker.services["bad"]["status"] == "unavailable"
    assert broker.services["healthy"]["status"] == "available"
    assert set(broker.catalog) == {"healthy/inspect"}
    assert "bad" not in broker._metadata
    assert control["connects"].count("bad") == control["closed"].count("bad") == 2
    assert broker.ownership.get("bad") is None
    # Only a later explicit refresh may try again; a timeout never retries.
    control["block"] = None
    await broker.refresh()
    assert broker.services["bad"]["status"] == "available"
    assert "failure" not in broker.services["bad"]
    assert control["connects"].count("bad") == 3


@pytest.mark.parametrize(
    "error,kind",
    [
        (RuntimeError("PRIVATE-DISCOVERY-DATA"), "error"),
        (httpx.ReadTimeout("PRIVATE-DISCOVERY-DATA"), "timeout"),
        (
            McpError(
                ErrorData(
                    code=408,
                    message="PRIVATE-DISCOVERY-DATA",
                    data={"token": "PRIVATE-DISCOVERY-DATA"},
                )
            ),
            "timeout",
        ),
        (
            ExceptionGroup(
                "PRIVATE-DISCOVERY-DATA",
                [
                    ValueError("PRIVATE-DISCOVERY-DATA"),
                    ExceptionGroup("private", [TimeoutError("PRIVATE-DISCOVERY-DATA")]),
                ],
            ),
            "timeout",
        ),
        (BrokerError("Reviewed registration rejected"), "broker_error"),
    ],
)
async def test_failure_kind_never_exports_app_exception_details(discovery, error, kind):
    broker, control = discovery
    control["error"] = error
    result = await broker.refresh()
    failure = broker.services["bad"]["failure"]
    assert failure["phase"] == "list_tools" and failure["kind"] == kind
    assert ("timeout_seconds" in failure) == (kind == "timeout")
    assert "PRIVATE-DISCOVERY-DATA" not in json.dumps(result)
    assert control["connects"].count("bad") == 2


async def test_invalid_registration_is_identified_without_exporting_its_contents(discovery):
    broker, control = discovery
    (broker.registry / "bad.json").write_text('{"PRIVATE-DISCOVERY-DATA":', encoding="utf-8")
    result = await broker.refresh()
    assert broker.services["bad"]["failure"] == {"phase": "registration", "kind": "error"}
    assert "PRIVATE-DISCOVERY-DATA" not in json.dumps(result)
    assert control["connects"].count("bad") == 1


async def test_refresh_cancellation_propagates_and_closes_its_connection(discovery):
    broker, control = discovery
    control["block"] = "list_tools"
    task = asyncio.create_task(broker.refresh())
    await asyncio.wait_for(control["entered"].wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert control["connects"].count("bad") == control["closed"].count("bad") == 2


async def test_owned_discovery_failure_keeps_recovery_barrier_and_redacts_details(stateful):
    broker, sessions = stateful
    context = await acquire(broker)

    async def broken(cursor=None):
        raise ConnectionError("PRIVATE-DISCOVERY-DATA")

    sessions[-1].list_tools = broken
    result = await broker.refresh()
    assert broker.services["stateful"]["status"] == "recovery_required"
    assert broker.services["stateful"]["failure"] == {"phase": "list_tools", "kind": "error"}
    assert "PRIVATE-DISCOVERY-DATA" not in json.dumps(result)
    with pytest.raises(BrokerError, match="operator recovery"):
        await broker.acquire_service("stateful", owner_id=OWNER_A)
    assert broker.ownership.get("stateful")["context_id"] == context
