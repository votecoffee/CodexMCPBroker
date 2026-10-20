"""Optional extension composition and live resources for app embeddings."""

from __future__ import annotations

import base64
import json
import mimetypes
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager

from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    BlobResourceContents,
    ListResourcesResult,
    ReadResourceResult,
    Resource,
    TextResourceContents,
)

from .identity import client_identity


class BoundExtension(Extension):
    """Give both additive methods and tool interception the same trusted identity."""

    def __init__(self, extension, identity):
        self.identifier = extension.identifier
        self.extension = extension
        self.identity = identity

    @contextmanager
    def scope(self):
        token = client_identity.set(client_identity.get() or self.identity)
        try:
            yield
        finally:
            client_identity.reset(token)

    def settings(self):
        return self.extension.settings()

    def tools(self):
        return self.extension.tools()

    def resources(self):
        return self.extension.resources()

    def methods(self):
        def wrap(handler):
            async def bound(ctx, params):
                with self.scope():
                    return await handler(ctx, params)

            return bound

        return tuple(
            MethodBinding(m.method, m.params_type, wrap(m.handler), m.protocol_versions)
            for m in self.extension.methods()
        )

    async def intercept_tool_call(self, params, ctx, call_next):
        with self.scope():
            return await self.extension.intercept_tool_call(params, ctx, call_next)


class ExtensionServer(MCPServer):
    """Reusable opt-in lifecycle and live resource seams above the pinned SDK.

    Extension identifiers/contribution kinds stay fixed for a server lifecycle.
    Only a selected provider's *content* can change. Providers claim explicit,
    disjoint URI namespaces; no remote extension or permission is auto-loaded.
    """

    def __init__(
        self,
        *args,
        extensions=(),
        resource_providers=(),
        frontend_identity=None,
        lifespan=None,
        **kwargs,
    ):
        selected = tuple(extensions or ())
        self.resource_providers = tuple(resource_providers)
        middleware = list(kwargs.pop("middleware", ()) or ())
        for extension in selected:
            if handler := (
                getattr(extension, "request_middleware", None)
                or getattr(extension, "subscription_middleware", None)
            ):
                bound = BoundExtension(extension, frontend_identity)

                async def subscription(ctx, call_next, handler=handler, bound=bound):
                    with bound.scope():
                        return await handler(ctx, call_next)

                middleware.append(subscription)

        @asynccontextmanager
        async def combined_lifespan(server):
            async with AsyncExitStack() as stack:
                for extension in selected:
                    if factory := getattr(extension, "lifespan", None):
                        await stack.enter_async_context(factory(server))
                context = Context(mcp_server=server, subscriptions=server._subscriptions)

                async def changed():
                    await context.notify_resources_changed()
                    await context.notify_resource_updated("broker://skills")

                for provider in self.resource_providers:
                    provider.listeners.add(changed)
                    stack.callback(provider.listeners.discard, changed)
                value = await stack.enter_async_context(lifespan(server)) if lifespan else None
                yield value

        super().__init__(
            *args,
            extensions=[BoundExtension(e, frontend_identity) for e in selected],
            lifespan=combined_lifespan,
            middleware=middleware,
            **kwargs,
        )
        for extension in selected:
            if install := getattr(extension, "install", None):
                install(self)
        if self.resource_providers:

            @self.resource("broker://skills", mime_type="application/json")
            async def skill_generations():
                return json.dumps(
                    {
                        "providers": [
                            {
                                "identifier": provider.identifier,
                                "generation": provider.generation,
                                "revision": provider.revision,
                            }
                            for provider in self.resource_providers
                        ]
                    }
                )

    async def _handle_list_resources(self, ctx, params):
        result = await super()._handle_list_resources(ctx, params)
        resources = [
            r
            for r in result.resources
            if not any(p.claims_uri(str(r.uri)) for p in self.resource_providers)
        ]
        for provider in self.resource_providers:
            resources.extend(Resource.model_validate(r) for r in provider.resource_metadata())
        if len({str(r.uri) for r in resources}) != len(resources):
            raise MCPError(INVALID_PARAMS, "live resource providers have conflicting URIs")
        return ListResourcesResult(resources=resources)

    async def _handle_read_resource(self, ctx, params):
        uri = str(params.uri)
        if sum(provider.claims_uri(uri) for provider in self.resource_providers) > 1:
            raise MCPError(INVALID_PARAMS, "live resource namespace is ambiguous")
        for provider in self.resource_providers:
            if provider.claims_uri(uri):
                raw = await provider.read_bytes(uri)
                mime = mimetypes.guess_type(uri)[0] or "application/octet-stream"
                content = BlobResourceContents(
                    uri=uri,
                    blob=base64.b64encode(raw).decode("ascii"),
                    mime_type=mime,
                )
                if mime.startswith("text/"):
                    try:
                        content = TextResourceContents(
                            uri=uri, text=raw.decode("utf-8"), mime_type=mime
                        )
                    except UnicodeError:
                        pass  # Binary bytes in a text-named file still round-trip.
                return ReadResourceResult(contents=[content])
        return await super()._handle_read_resource(ctx, params)
