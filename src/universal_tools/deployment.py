"""Remote OAuth and shared native SDK request-state deployment profiles."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components
from mcp.server.auth.settings import AuthSettings
from mcp.server.request_state import RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST

from .authorization import https_url
from .identity import client_identity
from .models import canonical_digest


@dataclass(frozen=True)
class OAuthDeployment:
    issuer_url: str
    resource_url: str
    token_verifier: object
    scopes: tuple[str, ...]
    request_state_keys: tuple[bytes, ...]
    subscriptions: object = None
    require_subject: bool = True
    replicated: bool = False

    def __post_init__(self):
        https_url(self.issuer_url)
        https_url(self.resource_url)
        if not self.scopes or not self.request_state_keys:
            raise ValueError("remote deployments require explicit scopes and request-state keys")
        if self.replicated and self.subscriptions is None:
            raise ValueError("replicated deployments require a shared subscription bus")

    def server_options(self):
        deployment = self

        class Verifier:
            async def verify_token(self, raw):
                token = await deployment.token_verifier.verify_token(raw)
                if (
                    token is None
                    or (token.claims or {}).get("iss") != deployment.issuer_url
                    or (deployment.require_subject and not token.subject)
                ):
                    return None
                return token

        async def principal(ctx, call_next):
            token = get_access_token()
            if token is None:
                raise MCPError(INVALID_REQUEST, "authenticated OAuth principal required")
            binding = client_identity.set(canonical_digest(principal_components(token)))
            try:
                return await call_next(ctx)
            finally:
                client_identity.reset(binding)

        return dict(
            auth=AuthSettings(
                issuer_url=self.issuer_url,
                resource_server_url=self.resource_url,
                required_scopes=list(self.scopes),
                validate_token_resource=True,
            ),
            token_verifier=Verifier(),
            middleware=[principal],
            request_state_security=RequestStateSecurity(
                keys=self.request_state_keys, audience=self.resource_url
            ),
            subscriptions=self.subscriptions,
        )

    def http_options(self):
        parts = urlsplit(self.resource_url)
        return dict(
            host="0.0.0.0",
            stateless_http=True,
            json_response=False,
            streamable_http_path=parts.path or "/mcp",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[parts.netloc],
                allowed_origins=[],
            ),
        )

    def create_server(self, name, **kwargs):
        """Native SDK hosts can share stateless continuations across replicas.

        App resources/jobs must themselves use shared, fenced persistence.
        ApplicationHost's retained workers/journals still have one owning process.
        """
        from .extensions import ExtensionServer

        return ExtensionServer(name, **kwargs, **self.server_options())

    def http_app(self, server, **kwargs):
        return server.streamable_http_app(**{**self.http_options(), **kwargs})
