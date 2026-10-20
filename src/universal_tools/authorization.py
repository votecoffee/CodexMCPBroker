"""Opt-in service OAuth grants and DPoP; credentials remain app-owned."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import secrets
import time
from urllib.parse import urlsplit

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from .models import BrokerError


def https_url(value):
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError("remote OAuth endpoints require credential-free HTTPS URLs")
    return value


class DPoPKey:
    """ES256 sender constraint; private keys are supplied by the app's secret store."""

    def __init__(self, key):
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("DPoP requires a P-256 private key")
        self._key = key
        self.public_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))

    @property
    def thumbprint(self):
        raw = json.dumps(
            {k: self.public_jwk[k] for k in ("crv", "kty", "x", "y")},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()

    def proof(self, method, url, *, token=None, nonce=None):
        https_url(url)
        claims = {
            "jti": secrets.token_urlsafe(24),
            "htm": method.upper(),
            "htu": str(httpx2.URL(url).copy_with(query=None, fragment=None)),
            "iat": int(time.time()),
        }
        if token is not None:
            claims["ath"] = (
                base64.urlsafe_b64encode(hashlib.sha256(token.encode("ascii")).digest())
                .rstrip(b"=")
                .decode()
            )
        if nonce:
            claims["nonce"] = nonce
        return jwt.encode(
            claims,
            self._key,
            algorithm="ES256",
            headers={"typ": "dpop+jwt", "jwk": self.public_jwk},
        )


class OAuthServiceAuth(httpx2.Auth):
    """Explicit machine-to-machine profile: client credentials or JWT bearer grant.

    This represents one service principal, not an end user's delegated identity.
    It never retries a protected MCP request. A nonce challenge is remembered
    for a subsequent explicit request; only token-endpoint nonce negotiation
    may repeat once. Browser/PKCE flows can use the SDK OAuth provider directly.
    """

    def __init__(
        self,
        *,
        resource_url,
        token_endpoint,
        client_id,
        scopes=(),
        client_secret=None,
        assertion_provider=None,
        dpop_key=None,
        token_http=None,
    ):
        self.resource_url = https_url(resource_url)
        self.token_endpoint = https_url(token_endpoint)
        self.client_id, self.scopes = client_id, tuple(scopes)
        if not client_id or len(client_id) > 1024 or len(self.scopes) > 64:
            raise ValueError("invalid OAuth client profile")
        if not assertion_provider and not client_secret:
            raise ValueError("a service grant requires credentials or an assertion provider")
        self._secret = client_secret
        self._assertion = assertion_provider
        self._dpop = dpop_key
        self._http = token_http
        self._token = None
        self._expires = 0
        self._lock = asyncio.Lock()
        self._token_nonce = self._resource_nonce = None

    @staticmethod
    def _nonce(response):
        nonce = response.headers.get("DPoP-Nonce")
        if nonce is not None and (
            not 1 <= len(nonce) <= 1024 or any(ord(c) < 33 or ord(c) > 126 for c in nonce)
        ):
            raise BrokerError("invalid OAuth nonce")
        return nonce

    async def _grant(self, http):
        form = {"client_id": self.client_id, "resource": self.resource_url}
        if self.scopes:
            form["scope"] = " ".join(self.scopes)
        if self._secret is not None:
            form["client_secret"] = self._secret
        if self._assertion:
            assertion = await self._assertion()
            if not isinstance(assertion, str) or not 1 <= len(assertion) <= 32 * 1024:
                raise BrokerError("invalid workload identity assertion")
            form.update(
                grant_type="urn:ietf:params:oauth:grant-type:jwt-bearer", assertion=assertion
            )
        else:
            form["grant_type"] = "client_credentials"
        for attempt in range(2):
            headers = {}
            if self._dpop:
                headers["DPoP"] = self._dpop.proof(
                    "POST", self.token_endpoint, nonce=self._token_nonce
                )
            async with http.stream(
                "POST", self.token_endpoint, data=form, headers=headers, follow_redirects=False
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64 * 1024:
                        raise BrokerError("OAuth token response exceeds its budget")
                try:
                    value = json.loads(raw)
                    nonce = self._nonce(response)
                except Exception:
                    raise BrokerError("invalid OAuth token response") from None
                if self._dpop and nonce:
                    self._token_nonce = nonce
                if (
                    attempt == 0
                    and self._dpop
                    and nonce
                    and response.status_code == 400
                    and value.get("error") == "use_dpop_nonce"
                ):
                    continue
                if response.status_code != 200:
                    raise BrokerError("OAuth token grant denied")
            token = value.get("access_token")
            expires = value.get("expires_in")
            expected = "DPoP" if self._dpop else "Bearer"
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 32 * 1024
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or value.get("token_type", "").lower() != expected.lower()
                or type(expires) not in (int, float)
                or not math.isfinite(expires)
                or not 1 <= expires <= 86400
            ):
                raise BrokerError("invalid or incompatible OAuth token metadata")
            if "scope" in value and not set(self.scopes) <= set(value["scope"].split()):
                raise BrokerError("OAuth token omits required scopes")
            self._token = token
            self._expires = time.monotonic() + expires - min(30, expires / 10)
            return
        raise BrokerError("OAuth nonce negotiation did not complete")

    async def async_auth_flow(self, request):
        # Exact endpoint binding prevents forwarding these credentials to a
        # redirected route, sibling resource, query target or another server.
        if str(request.url) != self.resource_url:
            raise BrokerError("OAuth credentials are scoped to a different MCP endpoint")
        async with self._lock:
            if self._token is None or time.monotonic() >= self._expires:
                try:
                    if self._http is not None:
                        await self._grant(self._http)
                    else:
                        async with httpx2.AsyncClient(trust_env=False, timeout=20) as http:
                            await self._grant(http)
                except BrokerError:
                    raise
                except Exception:
                    raise BrokerError("OAuth token endpoint unavailable") from None
            token = self._token
        request.headers["Authorization"] = ("DPoP " if self._dpop else "Bearer ") + token
        if self._dpop:
            request.headers["DPoP"] = self._dpop.proof(
                request.method, str(request.url), token=token, nonce=self._resource_nonce
            )
        response = yield request
        if self._dpop:
            nonce = self._nonce(response)
            if nonce:
                self._resource_nonce = nonce
        if response.status_code == 401 and not response.headers.get("DPoP-Nonce"):
            # Invalidate only the token actually rejected, not a newer grant.
            if self._token == token:
                self._token = None
