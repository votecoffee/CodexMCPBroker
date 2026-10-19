"""An HTTP initialize that never replies must not look like a tool failure."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_integration import data
from test_integration import stack as stack

from universal_tools.models import atomic_json
from universal_tools.surface import RemoteBackend


async def test_initialize_timeout_reaches_frontends_without_retry_or_tool_dispatch(stack):
    entered, release = threading.Event(), threading.Event()
    methods = []
    # The budget includes HTTP client/certificate setup. Keep the server stalled
    # well past it so scheduling/setup cannot substitute for an in-flight initialize.
    timeout_seconds = 3.0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            methods.append(request["method"])
            entered.set()
            release.wait(30)
            try:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = stack[0] / "services/slow-start.json"
    atomic_json(
        path,
        {
            "id": "slow-start",
            "display_name": "Synthetic initialize timeout",
            "transport": {
                "type": "streamable-http",
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
            },
            "timeout_seconds": timeout_seconds,
        },
    )
    try:
        async with RemoteBackend(stack[1]) as admin, RemoteBackend(stack[1]) as other:
            result = data(await admin.invoke("refresh_services", {}))
            status = next(row for row in result["services"] if row["service_id"] == "slow-start")
            assert entered.is_set(), ("initialize must arrive before the deadline", status)
            assert status["status"] == "unavailable"
            assert status["failure"] == {
                "phase": "connect_initialize",
                "kind": "timeout",
                "timeout_seconds": timeout_seconds,
            }
            observed = data(await other.invoke("list_services", {}))
            assert (
                next(row for row in observed["services"] if row["service_id"] == "slow-start")
                == status
            )
            assert methods == ["initialize"]
            assert all(
                row["status"] == "available"
                for row in result["services"]
                if row["service_id"] != "slow-start"
            )
    finally:
        release.set()
        path.unlink()
        async with RemoteBackend(stack[1]) as admin:
            data(await admin.invoke("refresh_services", {}))
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
