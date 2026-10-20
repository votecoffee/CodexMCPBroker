"""OAuth HTTP isolation and a stateless continuation resumed on another replica."""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from test_tasks_extension import FORM

from universal_tools.deployment import OAuthDeployment
from universal_tools.identity import client_identity


class Verifier:
    async def verify_token(self, token):
        if token not in {"alice", "bob", "wrong-resource", "wrong-issuer", "wrong-scope"}:
            return None
        return AccessToken(
            token=token,
            client_id="shared-client",
            subject=token,
            scopes=[] if token == "wrong-scope" else ["read"],
            resource="https://other.invalid/mcp"
            if token == "wrong-resource"
            else "https://app.invalid/mcp",
            claims={
                "iss": "https://bad.invalid" if token == "wrong-issuer" else "https://auth.invalid"
            },
        )


@asynccontextmanager
async def connect(app, token):
    @asynccontextmanager
    async def transport():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer " + token}
        ) as http:
            async with streamable_http_client(
                "https://app.invalid/mcp", http_client=http
            ) as streams:
                yield streams

    async with Client(transport(), mode="2026-07-28", cache=None) as client:
        yield client


def replica(profile):
    server = profile.create_server("Native shared-state fixture")

    @server.tool(structured_output=False)
    async def review(ctx: Context) -> CallToolResult | InputRequiredResult:
        assert client_identity.get()
        if ctx.input_responses:
            assert ctx.request_state == "native-prepared"
            return CallToolResult(content=[])
        return InputRequiredResult(
            input_requests={
                "review": ElicitRequest(
                    params=ElicitRequestFormParams.model_validate(FORM["params"])
                )
            },
            request_state="native-prepared",
        )

    return server, profile.http_app(server, json_response=True)


async def test_shared_keys_resume_native_state_across_replicas_and_reject_another_user():
    profile = OAuthDeployment(
        "https://auth.invalid", "https://app.invalid/mcp", Verifier(), ("read",), (b"s" * 32,)
    )
    first, app1 = replica(profile)
    second, app2 = replica(profile)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(first.session_manager.run())
        await stack.enter_async_context(second.session_manager.run())
        a = await stack.enter_async_context(connect(app1, "alice"))
        b = await stack.enter_async_context(connect(app2, "alice"))
        foreign = await stack.enter_async_context(connect(app2, "bob"))
        pending = await a.session.call_tool("review", {}, allow_input_required=True)
        assert pending.request_state != "native-prepared"
        answer = {"review": ElicitResult(action="accept", content={"approved": True})}
        with pytest.raises(MCPError):
            await foreign.session.call_tool(
                "review",
                {},
                input_responses=answer,
                request_state=pending.request_state,
                allow_input_required=True,
            )
        final = await b.session.call_tool(
            "review",
            {},
            input_responses=answer,
            request_state=pending.request_state,
            allow_input_required=True,
        )
        assert final.content == []


async def test_oauth_discovery_denies_missing_wrong_issuer_resource_and_scope():
    profile = OAuthDeployment(
        "https://auth.invalid", "https://app.invalid/mcp", Verifier(), ("read",), (b"s" * 32,)
    )
    server, app = replica(profile)
    async with server.session_manager.run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            for token in (None, "unknown", "wrong-resource", "wrong-issuer"):
                headers = {"Authorization": "Bearer " + token} if token else {}
                response = await http.post("https://app.invalid/mcp", headers=headers, json={})
                assert response.status_code == 401
            response = await http.post(
                "https://app.invalid/mcp", headers={"Authorization": "Bearer wrong-scope"}, json={}
            )
            assert response.status_code == 403
