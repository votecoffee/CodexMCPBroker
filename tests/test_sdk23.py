"""SDK 2 migration boundaries use synthetic streams and app results only."""

from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_REQUEST,
    REQUEST_TIMEOUT,
    ErrorData,
    ListToolsResult,
    Tool,
)

from universal_tools.elicitation import ElicitationRouter, routed_call
from universal_tools.transport import InitializedSession, list_all_tools


def test_bridge_advertises_forms_without_url_or_task_support():
    session = InitializedSession(None, None, relay=True)
    capabilities = session._build_capabilities("2025-11-25")
    assert capabilities.elicitation.form is not None
    assert capabilities.elicitation.url is None
    assert capabilities.tasks is None


async def test_sdk2_pagination_uses_typed_params_and_preserves_wire_definitions():
    seen = []

    async def listing(*, params):
        seen.append(params.cursor)
        return ListToolsResult(
            tools=[
                Tool(name="second" if params.cursor else "first", input_schema={"type": "object"})
            ],
            next_cursor=None if params.cursor else "next-page",
        )

    tools = await list_all_tools(SimpleNamespace(list_tools=listing))
    assert seen == [None, "next-page"]
    assert [tool.name for tool in tools] == ["first", "second"]
    assert tools[0].model_dump(by_alias=True)["inputSchema"] == {"type": "object"}


@pytest.mark.parametrize(
    "code,message,normalized",
    [
        (INVALID_REQUEST, "local refusal", True),
        (INVALID_REQUEST, "unrelated app failure", False),
        (REQUEST_TIMEOUT, "local refusal", False),
    ],
)
async def test_only_this_calls_exact_form_refusal_becomes_an_app_error(code, message, normalized):
    router = ElicitationRouter()

    async def call_tool(*args, **kwargs):
        router.rejection = ErrorData(code=INVALID_REQUEST, message="local refusal")
        raise MCPError(code=code, message=message)

    session = SimpleNamespace(elicitation_router=router, call_tool=call_tool)
    if normalized:
        result = await routed_call(session, "confirm", {}, None)
        assert result.is_error and result.content[0].text == "local refusal"
        assert result.meta["universal_tools/elicitation_error"]["code"] == INVALID_REQUEST
    else:
        with pytest.raises(MCPError):
            await routed_call(session, "confirm", {}, None)


async def test_a_prior_refusal_cannot_classify_a_new_call_as_an_app_error():
    router = ElicitationRouter()
    router.rejection = ErrorData(code=INVALID_REQUEST, message="prior refusal")

    async def call_tool(*args, **kwargs):
        raise MCPError(code=INVALID_REQUEST, message="prior refusal")

    with pytest.raises(MCPError):
        await routed_call(
            SimpleNamespace(elicitation_router=router, call_tool=call_tool), "edit", {}, None
        )


async def test_modern_stateless_requests_cannot_invent_implicit_app_ownership(tmp_path):
    from mcp import Client

    from universal_tools.framework import ApplicationHost
    from universal_tools.models import atomic_json

    publication = tmp_path / "app.json"
    atomic_json(
        publication,
        {"schema_version": 1, "id": "synthetic", "display_name": "Synthetic", "backends": []},
    )
    async with ApplicationHost(tmp_path / "state", publication) as host:
        async with Client(host.create_server(), mode="2026-07-28", cache=None) as client:
            result = await client.call_tool("acquire_service", {"service_id": "synthetic"})
            assert result.is_error
            assert "retained MCP connection" in result.content[0].text
            assert host._engine.ownership.entries() == []
