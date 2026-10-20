"""SDK Apps composition preserves UI metadata, CSP and meaningful text fallback."""

import runpy
from pathlib import Path

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID


@pytest.mark.parametrize("ui", [True, False])
async def test_app_tool_resource_and_fallback_through_framework_host(ui):
    factory = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples/native_features.py")
    )["create_server"]
    server = factory()
    extensions = [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})] if ui else []
    async with Client(server, mode="2026-07-28", extensions=extensions) as client:
        tools = await client.list_tools()
        uri = tools.tools[0].meta["ui"]["resourceUri"]
        assert uri == "ui://example/status.html"
        result = await client.call_tool("status")
        assert "Example service is ready" in result.content[0].text
        assert ("text view" in result.content[0].text) is not ui
        html = (await client.read_resource(uri)).contents[0]
        assert html.mime_type == APP_MIME_TYPE
        assert "event.source !== parent" in html.text and ".textContent" in html.text
        assert html.meta["ui"]["csp"]["connectDomains"] == []
