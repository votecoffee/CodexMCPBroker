"""Host-local lifecycle selection with fake sessions; no MCP or native process starts."""

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from test_capabilities import CatalogSession, Controller
from test_core import manifest

import universal_tools.broker as broker_module
import universal_tools.framework as framework_module
import universal_tools.transport as transport_module
from universal_tools.broker import Broker
from universal_tools.framework import ApplicationHost
from universal_tools.models import atomic_json


def provider(sessions, *, scope="public", refuse=False):
    @asynccontextmanager
    async def connect(config):
        if refuse:
            raise RuntimeError("selected lifecycle refused")
        async with anyio.create_task_group():
            session = CatalogSession(scope, Controller())
            sessions.append(session)
            try:
                yield session
            finally:
                assert asyncio.current_task() is session.task
                session.closed = True

    return connect


def forbid_default(*args, **kwargs):
    raise AssertionError("selected lifecycle must not use the default transport")


@pytest.mark.parametrize("mode", ["transient", "exclusive", "shared"])
async def test_selected_factory_covers_discovery_probes_calls_and_retained_actors(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(broker_module, "service_session", forbid_default)
    sessions = []
    engine = Broker(tmp_path, session_factory=provider(sessions))
    selected = manifest(session_mode=mode, capability_catalog={"tool": "catalog"})
    atomic_json(engine.registry / "app.json", selected.model_dump(mode="json"))
    try:
        await engine.refresh()
        assert len(sessions) == 1 and sessions[0].closed
        context = None
        if mode == "exclusive":
            context = (await engine.acquire_service("app", owner_id="a" * 48))["context_id"]
        await engine.refresh_capabilities("app", owner_id="a" * 48, context_id=context)
        assert len(sessions) == 2
        assert sessions[1].closed is (mode == "transient")
        revision = engine.describe("app/describe_feature")["catalog_revision"]
        result = await engine.call(
            "app/describe_feature",
            revision,
            {"capability": "holes.drill"},
            write=False,
            context_id=context,
            owner_id="a" * 48,
        )
        assert not result.is_error and len(sessions) == (3 if mode == "transient" else 2)
        if mode == "exclusive":
            await engine.release_service("app", context, owner_id="a" * 48)
        if mode == "transient":
            assert sessions[-1].closed
        else:
            assert sessions[-1].task is not asyncio.current_task()
    finally:
        await engine.aclose()
    assert all(session.closed for session in sessions)


async def test_selected_factory_refusal_has_no_default_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_module, "service_session", forbid_default)
    engine = Broker(tmp_path, session_factory=provider([], refuse=True))
    selected = manifest()
    atomic_json(engine.registry / "app.json", selected.model_dump(mode="json"))
    try:
        await engine.refresh()
        assert not engine.catalog
        assert engine.services["app"]["status"] != "ready"
        with pytest.raises(RuntimeError, match="selected lifecycle refused"):
            async with engine._connect_session(selected):
                pytest.fail("a refused lifecycle cannot yield a session")
    finally:
        await engine.aclose()


def test_invalid_factory_is_rejected_before_engine_state_is_created(tmp_path):
    state = tmp_path / "absent"
    with pytest.raises(TypeError, match="session_factory"):
        Broker(state, session_factory=object())
    assert not state.exists()


async def test_application_host_keeps_factory_across_publication_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_module, "service_session", forbid_default)
    publication = tmp_path / "publication.json"
    selected = manifest(capability_catalog={"tool": "catalog"})
    value = {
        "schema_version": 1,
        "id": "example-app",
        "display_name": "Example app",
        "backends": [{"revision": "one", "service": selected.model_dump(mode="json")}],
    }
    atomic_json(publication, value)
    sessions = []
    async with ApplicationHost(
        tmp_path / "state", publication, session_factory=provider(sessions)
    ) as host:
        assert len(sessions) == 1
        value["backends"][0]["revision"] = "two"
        atomic_json(publication, value)
        await host.refresh()
        assert len(sessions) == 2
        assert host._selected["app"].revision == "two"
    assert all(session.closed for session in sessions)


def test_reference_host_forwards_factory_before_starting_server(tmp_path, monkeypatch):
    selected = provider([])
    observed = {}

    class StopBeforeServer(Exception):
        pass

    def host(*args, **kwargs):
        observed.update(kwargs)
        raise StopBeforeServer

    monkeypatch.setattr(framework_module, "ApplicationHost", host)
    monkeypatch.setattr(
        framework_module,
        "load_settings",
        lambda _: SimpleNamespace(max_request_body_bytes=1024),
    )
    with pytest.raises(StopBeforeServer):
        framework_module.serve_application(
            tmp_path, tmp_path / "publication.json", session_factory=selected
        )
    assert observed["session_factory"] is selected


async def test_selected_stdio_context_receives_allowlisted_environment_and_sdk_parameters(
    tmp_path, monkeypatch
):
    captured = {}
    read, write = object(), object()

    @asynccontextmanager
    async def selected(parameters, *, errlog):
        captured.update(parameters=parameters, errlog=errlog, task=asyncio.current_task())
        try:
            yield read, write
        finally:
            assert asyncio.current_task() is captured["task"]
            captured["closed"] = True

    class Session:
        def __init__(self, actual_read, actual_write, *args, **kwargs):
            assert actual_read is read and actual_write is write

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def connect(self, mode, **kwargs):
            captured["connected"] = mode

    monkeypatch.setenv("EXAMPLE_APP_CREDENTIAL", "private-test-value")
    monkeypatch.setenv("EXAMPLE_UNSELECTED", "must-not-forward")
    monkeypatch.setattr(transport_module, "stdio_client", forbid_default)
    monkeypatch.setattr(transport_module, "InitializedSession", Session)
    selected_manifest = manifest(
        transport={
            "type": "stdio",
            "command": sys.executable,
            "args": ["--example", "深 μ", ""],
            "cwd": str(tmp_path),
            "env_vars": ["EXAMPLE_APP_CREDENTIAL"],
        }
    )
    async with transport_module.service_session(
        selected_manifest, stdio_client_factory=selected
    ) as session:
        assert isinstance(session, Session)
    parameters = captured["parameters"]
    assert parameters.env == {"EXAMPLE_APP_CREDENTIAL": "private-test-value"}
    assert parameters.args == ["--example", "深 μ", ""]
    assert parameters.cwd == str(tmp_path)
    assert captured["closed"] and captured["errlog"].closed


async def test_stdio_refusal_is_not_retried_with_sdk_launcher(monkeypatch):
    monkeypatch.setattr(transport_module, "stdio_client", forbid_default)

    @asynccontextmanager
    async def refused(parameters, *, errlog):
        raise RuntimeError("owned launch refused")
        yield  # pragma: no cover

    selected = manifest(transport={"type": "stdio", "command": sys.executable})
    with pytest.raises(RuntimeError, match="owned launch refused"):
        async with transport_module.service_session(selected, stdio_client_factory=refused):
            pytest.fail("a refused launch cannot create an SDK session")


async def test_http_does_not_call_stdio_provider(monkeypatch):
    sentinel = object()

    @asynccontextmanager
    async def http(*args, **kwargs):
        yield sentinel

    monkeypatch.setattr(transport_module, "http_session", http)
    async with transport_module.service_session(
        manifest(), stdio_client_factory=forbid_default
    ) as session:
        assert session is sentinel
