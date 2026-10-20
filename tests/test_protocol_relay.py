"""Native SDK federation without installing any app or selecting native targets."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp import Client
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import Completion, PromptReference, ResourceTemplateReference

from universal_tools.extensions import ExtensionServer
from universal_tools.protocol_relay import ProtocolRelay, RelaySource


def fixture():
    upstream = MCPServer("Source")

    @upstream.resource("demo://hello")
    async def hello():
        return "exact\r\nUnicode é"

    @upstream.resource("demo://item/{name}")
    async def item(name: str):
        return name

    @upstream.prompt()
    async def greet(name: str):
        return "Hello " + name

    @upstream.completion()
    async def complete(ref, argument, context):
        return Completion(values=[argument.value + "-completed"])

    revoked = set()
    accesses = []

    async def authorize(owner, method, target):
        accesses.append((owner, method, target))
        if owner in revoked:
            raise ValueError("private reason")

    @asynccontextmanager
    async def connect(owner):
        async with Client(upstream, mode="2026-07-28", cache=None) as client:
            yield client.session

    source = RelaySource("example", "v1", connect, authorize)
    relay = ProtocolRelay([source], page_size=1)
    server = ExtensionServer("Federated", extensions=[relay], frontend_identity="a" * 32)
    return upstream, relay, server, revoked, accesses


async def test_lists_reads_templates_prompts_completions_and_revocation():
    _, _, server, revoked, accesses = fixture()
    async with Client(server, mode="2026-07-28", cache=None) as client:
        resources = await client.list_resources()
        uri = str(resources.resources[0].uri)
        assert uri == "relay://example/v1/demo://hello"
        content = await client.read_resource(uri)
        assert content.contents[0].text == "exact\r\nUnicode é"
        templates = await client.list_resource_templates()
        template = templates.resource_templates[0].uri_template
        result = await client.read_resource(template.replace("{name}", "sample"))
        assert result.contents[0].text == "sample"
        prompts = await client.list_prompts()
        name = prompts.prompts[0].name
        assert name == "example:v1:greet"
        prompt = await client.get_prompt(name, arguments={"name": "User"})
        assert prompt.messages[0].content.text == "Hello User"
        for ref in [PromptReference(name=name), ResourceTemplateReference(uri=template)]:
            complete = await client.complete(ref, argument={"name": "name", "value": "abc"})
            assert complete.completion.values == ["abc-completed"]
        revoked.add("a" * 32)
        with pytest.raises(MCPError):
            await client.read_resource(uri)
    assert all(owner == "a" * 32 for owner, _, _ in accesses)


async def test_subscription_preserves_owner_and_resource_namespace():
    upstream, _, server, revoked, accesses = fixture()
    async with Client(server, mode="2026-07-28", cache=None) as client:
        uri = str((await client.list_resources()).resources[0].uri)
        async with client.listen(resource_subscriptions=[uri]) as events:
            await Context(
                mcp_server=upstream, subscriptions=upstream._subscriptions
            ).notify_resource_updated("demo://hello")
            async with asyncio.timeout(3):
                event = await anext(events)
            assert event.uri == uri
    assert any(method == "subscriptions/listen" for _, method, _ in accesses)
