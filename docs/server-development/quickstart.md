# Read-only server quickstart

[Developer guide](README.md) · [Next: architecture](architecture.md)

This example compares a small stdio server directly and through an isolated broker. It uses only text and a SHA-256 digest: no native application, device, user document or installed plugin participates. It is a starting point for metadata/routing, not a reviewed-write or native acceptance example.

Run commands from a broker source checkout on Windows. The example uses the broker's runtime lock for convenience; future apps can use another assessed SDK/language and their own dependency environment.

## Prepare a disposable development environment

```powershell
py -3.13 -m venv .local/server-guide-venv
$GuidePython = (Resolve-Path .local/server-guide-venv/Scripts/python.exe).Path
& $GuidePython -m pip install -r requirements-lock.txt
& $GuidePython -m pip install --no-deps -e .
$GuideRoot = Join-Path (Get-Location).Path '.local/server-guide'
New-Item -ItemType Directory -Path $GuideRoot -Force | Out-Null
```

Save the following two Python files as UTF-8 inside `$GuideRoot`. Keep all example state local and ignored. Do not copy a real registration directory into this example.

## Create server.py

```python
import hashlib

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer(
    "Example text service",
    instructions="Summarize supplied text. This example never opens files or native applications.",
    log_level="WARNING",
)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def summarize_text(text: str) -> dict:
    """Return the exact supplied text, encoded length and digest, up to 4096 UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) > 4096:
        raise ValueError("Text exceeds the example's 4096-byte limit")
    return {
        "text": text,
        "utf8_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


if __name__ == "__main__":
    server.run(transport="stdio")
```

The read-only annotation is explicit, inputs are bounded in the app, stdout belongs to MCP, and the result preserves Unicode. There is no broker-specific domain code in the server.

## Create probe.py

```python
import asyncio
import json
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def connect(arguments):
    parameters = StdioServerParameters(command=sys.executable, args=arguments)
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        yield session


def data(result):
    if result.is_error:
        raise RuntimeError(result.model_dump_json())
    return result.structured_content or json.loads(result.content[0].text)


def wire(value):
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


async def main():
    state, server_path = sys.argv[1:]
    cases = [{"text": "café 深 μ"}, {"text": ""}, {"text": "x" * 4096}]
    async with connect([server_path]) as direct:
        definitions = (await direct.list_tools()).tools
        references = [wire(await direct.call_tool("summarize_text", case)) for case in cases]
        rejected = wire(await direct.call_tool("summarize_text", {"text": "x" * 4097}))
        assert rejected["isError"]
    async with connect(["-m", "universal_tools", "--state", state, "proxy"]) as broker:
        services = data(await broker.call_tool("refresh_services", {}))["services"]
        row = next(item for item in services if item["service_id"] == "example-text")
        assert row["status"] == "available", row
        matches = data(await broker.call_tool("search_tools", {"query": "summarize_text"}))
        assert any(
            item.get("tool_id") == "example-text/summarize_text" for item in matches["matches"]
        )
        description = data(
            await broker.call_tool("describe_tool", {"tool_id": "example-text/summarize_text"})
        )
        assert description["definition"] == wire(definitions[0])
        assert description["invocation"]["channel"] == "call_read_tool"
        for case, expected in zip(cases, references, strict=True):
            result = await broker.call_tool(
                "call_read_tool",
                {
                    "tool_id": "example-text/summarize_text",
                    "catalog_revision": description["catalog_revision"],
                    "arguments": case,
                },
            )
            assert wire(result) == expected
        result = await broker.call_tool(
            "call_read_tool",
            {
                "tool_id": "example-text/summarize_text",
                "catalog_revision": description["catalog_revision"],
                "arguments": {"text": "x" * 4097},
            },
        )
        assert wire(result) == rejected
    print(json.dumps({"definitions_equal": True, "complete_results_equal": 4}))


if __name__ == "__main__":
    asyncio.run(main())
```

This client uses the broker's fixed tools and current catalog revision. It compares complete app results, including the expected oversized-input app error, instead of only comparing a success flag. It does not establish host UI behavior.

## Register and run in an isolated state

Continue in the same PowerShell session. A unique state directory avoids normal app registrations. Port 18768 must be unused; if it is occupied, choose another unused port. Do not stop the process that owns an unrelated port.

```powershell
$GuideState = Join-Path $GuideRoot ('broker-state-' + [Guid]::NewGuid().ToString('N'))
$GuideServer = Join-Path $GuideRoot 'server.py'
$GuideProbe = Join-Path $GuideRoot 'probe.py'
$GuideManifest = Join-Path $GuideRoot 'example-text.service.json'
$GuideDescriptor = @{
    schema_version = 1
    id = 'example-text'
    display_name = 'Example Text'
    description = 'Pure text summary for isolated development'
    transport = @{ type = 'stdio'; command = $GuidePython; args = @($GuideServer) }
    session_mode = 'transient'
    allow_writes = $false
    allowed_write_tools = @()
}
[IO.File]::WriteAllText($GuideManifest, ($GuideDescriptor | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
function Invoke-GuideBroker {
    & $GuidePython -m universal_tools --state $GuideState @args
    if ($LASTEXITCODE -ne 0) { throw 'Isolated guide command failed; inspect its output.' }
}
Invoke-GuideBroker init --port 18768
Invoke-GuideBroker register $GuideManifest
try {
    Invoke-GuideBroker start
    Invoke-GuideBroker refresh
    & $GuidePython $GuideProbe $GuideState $GuideServer
    if ($LASTEXITCODE -ne 0) { throw 'Direct/broker comparison failed.' }
} finally {
    Invoke-GuideBroker stop
}
```

The probe prints `definitions_equal: true` and `complete_results_equal: 4`. State, logs, descriptors and environment stay under ignored `.local/`. Stopping this explicitly selected state does not stop an unrelated broker. No plugin installation or normal runtime pointer change is needed for the comparison.

Next, move real domain operations into shared app services and choose an assessed [state owner](sessions-and-recovery.md). Add reviewed writes, workers, optional catalogs and host/native acceptance only when the app's selected workflow requires them.
