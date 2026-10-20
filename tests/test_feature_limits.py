"""Response bounds, immutable skills and diagnostics confidentiality."""

import json
from contextlib import contextmanager

import pytest
from mcp.shared.exceptions import MCPError
from test_integration import data
from test_integration import stack as stack
from test_payload_integration import payload_app

from universal_tools import diagnostics
from universal_tools.models import BrokerError, ServiceManifest
from universal_tools.skill_catalog import SkillCatalog
from universal_tools.transport import service_session


async def test_registered_sse_budget_rejects_large_result_and_unbounded_default_preserves_it(
    stack, tmp_path
):
    async with payload_app(stack, tmp_path, "http"):
        manifest = json.loads((stack[0] / "services/payload.json").read_bytes())
        manifest["transport"]["max_sse_event_bytes"] = 1024
        async with service_session(ServiceManifest.model_validate(manifest)) as session:
            with pytest.raises(MCPError):
                await session.call_tool("roundtrip", {"payload": {"text": "x" * 4096}})
        manifest["transport"]["max_sse_event_bytes"] = None
        async with service_session(ServiceManifest.model_validate(manifest)) as session:
            result = await session.call_tool("roundtrip", {"payload": {"text": "x" * 4096}})
            assert data(result) == {"text": "x" * 4096}


def test_framework_trace_does_not_record_exception_messages_or_payloads(monkeypatch):
    observed = []

    class Tracer:
        @contextmanager
        def start_as_current_span(self, name, **kwargs):
            observed.append((name, kwargs))
            yield self

        def set_attribute(self, key, value):
            observed.append((key, value))

    monkeypatch.setattr(diagnostics.trace, "get_tracer", lambda _: Tracer())
    with pytest.raises(ValueError):
        with diagnostics.measured("backend_call"):
            raise ValueError("private synthetic payload")
    encoded = json.dumps(observed)
    assert "private synthetic" not in encoded
    assert observed[0][1] == {"record_exception": False, "set_status_on_exception": False}
    assert diagnostics.snapshot()["calls"]["backend_call.failed"] >= 1


@pytest.mark.parametrize("bad", [".env", "__pycache__/cache.pyc"])
def test_skill_snapshot_rejects_hidden_files_and_caches(tmp_path, bad):
    (tmp_path / "SKILL.md").write_bytes(b"---\nname: synthetic\ndescription: Example\n---\nTest\n")
    extra = tmp_path / bad
    extra.parent.mkdir(exist_ok=True)
    extra.write_bytes(b"private")
    with pytest.raises(BrokerError):
        SkillCatalog([tmp_path])


@pytest.mark.parametrize(
    "changes",
    [{"watch_catalog": True}, {"discovery_cache_ttl_seconds": 61}, {"protocol_mode": "bad"}],
)
def test_invalid_protocol_options_are_not_silently_accepted(changes):
    with pytest.raises(ValueError):
        ServiceManifest.model_validate(
            {
                "id": "app",
                "display_name": "App",
                "transport": {"type": "streamable-http", "url": "http://127.0.0.1:1234/mcp"},
                **changes,
            }
        )
