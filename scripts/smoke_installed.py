"""Exercise two installed-plugin frontends and live discovery against the example stack."""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from universal_tools.cli import register
from universal_tools.models import atomic_json, load_settings


def data(result):
    if result.isError:
        raise RuntimeError("smoke operation returned an MCP error")
    return result.structuredContent or json.loads(result.content[0].text)


async def main(args):
    settings = load_settings(args.state)
    runtime = json.loads((args.plugin / "runtime.json").read_text(encoding="utf-8"))
    headers = {"Authorization": f"Bearer {settings.token}"}
    with httpx.Client(trust_env=False) as client:
        instance_before = client.get(settings.url + "/health", headers=headers).json()[
            "instance_id"
        ]
    async with AsyncExitStack() as stack:
        sessions = []
        for _ in range(2):
            params = StdioServerParameters(
                command=runtime["python"], args=[str(args.plugin / "server/launch.py")]
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions.append(session)
        definitions_before = (await sessions[0].list_tools()).model_dump(mode="json")

        async def invoke(session, tool_id, arguments):
            description = data(await session.call_tool("describe_tool", {"tool_id": tool_id}))
            return data(
                await session.call_tool(
                    "call_read_tool",
                    {
                        "tool_id": tool_id,
                        "catalog_revision": description["catalog_revision"],
                        "arguments": arguments,
                    },
                )
            )

        results = await asyncio.gather(
            *(
                invoke(session, "example-math/echo", {"value": f"chat-{number}", "delay": 0.1})
                for number, session in enumerate(sessions)
            )
        )
        assert [entry["value"] for entry in results] == ["chat-0", "chat-1"]
        original = json.loads(
            (args.state / "services/example-math.json").read_text(encoding="utf-8")
        )
        registration = {**original, "id": "smoke-added-app", "display_name": "Temporary smoke app"}
        source = args.state / "smoke-descriptor.json"
        destination = args.state / "services/smoke-added-app.json"
        if destination.exists() or source.exists():
            raise RuntimeError("temporary smoke registration already exists; inspect it first")
        atomic_json(source, registration)
        registered = False
        try:
            register(args.state, source)
            registered = True
            data(await sessions[0].call_tool("refresh_services"))
            total = await invoke(sessions[1], "smoke-added-app/add", {"a": 12, "b": 30})
            assert total["sum"] == 42
            definitions_after = (await sessions[1].list_tools()).model_dump(mode="json")
            assert definitions_before == definitions_after
        finally:
            if registered:
                destination.unlink()
            source.unlink()
            data(await sessions[0].call_tool("refresh_services"))
    with httpx.Client(trust_env=False) as client:
        instance_after = client.get(settings.url + "/health", headers=headers).json()["instance_id"]
    assert instance_before == instance_after
    report = {
        "ok": True,
        "installed_plugin": str(args.plugin),
        "clients": 2,
        "same_broker_instance": True,
        "separate_client_results": [entry["value"] for entry in results],
        "hot_registration_visible_to_existing_client": True,
        "native_tool_definitions_unchanged": True,
        "routed_result": 42,
        "broker_tool_count": len(definitions_before["tools"]),
        "temporary_registration_removed": not destination.exists(),
        "scope": "MCP SDK and installed launcher; no model-driven Codex chat acceptance",
    }
    if args.report:
        atomic_json(args.report, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    asyncio.run(main(parser.parse_args()))
