"""An app-owned interactive MCP tool using the shared framework's extension host."""

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from universal_tools.extensions import ExtensionServer

HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Example status</title><p id="status">Waiting for the app result.</p>
<script>
let initialized = false;
window.addEventListener('message', (event) => {
  if (event.source !== parent || event.data?.jsonrpc !== '2.0') return;
  const message = event.data;
  if (message.id === 1 && message.result?.protocolVersion) {
    initialized = true;
    parent.postMessage({jsonrpc:'2.0', method:'ui/notifications/initialized'}, '*');
  }
  if (initialized && message.method === 'ui/notifications/tool-result') {
    document.getElementById('status').textContent =
      message.params?.content?.find(item => item.type === 'text')?.text ?? 'No status.';
  }
});
parent.postMessage({jsonrpc:'2.0', id:1, method:'ui/initialize', params:{
  appInfo:{name:'example-status', version:'1.0.0'}, appCapabilities:{},
  protocolVersion:'2026-01-26'}}, '*');
</script></html>"""


def create_server():
    apps = Apps()

    @apps.tool(
        resource_uri="ui://example/status.html", annotations=ToolAnnotations(read_only_hint=True)
    )
    async def status(ctx: Context) -> str:
        # Both UI-capable hosts and text-only clients receive meaningful content.
        return (
            "Example service is ready."
            if client_supports_apps(ctx)
            else "Example service is ready (text view)."
        )

    apps.add_html_resource(
        "ui://example/status.html",
        HTML,
        title="Example status",
        csp=ResourceCsp(connect_domains=[], resource_domains=[]),
    )
    return ExtensionServer("Native features example", extensions=[apps])


if __name__ == "__main__":
    create_server().run()
