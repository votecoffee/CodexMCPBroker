"""Service grants and DPoP signatures against isolated HTTP receivers."""

import base64
import hashlib
from urllib.parse import parse_qs

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from universal_tools.authorization import DPoPKey, OAuthServiceAuth
from universal_tools.models import BrokerError, HttpTransport


async def test_workload_grant_dpop_nonce_signature_hash_and_no_request_replay():
    key = DPoPKey(ec.generate_private_key(ec.SECP256R1()))
    grants, effects = [], []

    async def grant(request):
        form = parse_qs(request.content.decode())
        claims = jwt.decode(request.headers["DPoP"], key._key.public_key(), algorithms=["ES256"])
        assert jwt.get_unverified_header(request.headers["DPoP"])["jwk"] == key.public_jwk
        assert claims["htm"] == "POST" and claims["htu"] == "https://auth.invalid/token"
        grants.append((form, claims))
        if len(grants) == 1:
            return httpx2.Response(
                400, json={"error": "use_dpop_nonce"}, headers={"DPoP-Nonce": "as-nonce"}
            )
        assert claims["nonce"] == "as-nonce"
        return httpx2.Response(
            200, json={"access_token": "synthetic-token", "token_type": "DPoP", "expires_in": 60}
        )

    async def resource(request):
        claims = jwt.decode(request.headers["DPoP"], key._key.public_key(), algorithms=["ES256"])
        assert claims["htu"] == "https://app.invalid/mcp"
        assert (
            claims["ath"]
            == base64.urlsafe_b64encode(hashlib.sha256(b"synthetic-token").digest())
            .rstrip(b"=")
            .decode()
        )
        assert "d" not in jwt.get_unverified_header(request.headers["DPoP"])["jwk"]
        effects.append(claims)
        return httpx2.Response(
            401 if len(effects) == 1 else 200, headers={"DPoP-Nonce": "rs-nonce"}
        )

    async def assertion():
        return "synthetic-workload-jwt"

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(grant)) as token_http:
        auth = OAuthServiceAuth(
            resource_url="https://app.invalid/mcp",
            token_endpoint="https://auth.invalid/token",
            client_id="service",
            assertion_provider=assertion,
            dpop_key=key,
            token_http=token_http,
        )
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(resource), auth=auth
        ) as client:
            assert (
                await client.post(auth.resource_url, json={"synthetic": "operation"})
            ).status_code == 401
            assert len(effects) == 1
            assert (await client.post(auth.resource_url, json={})).status_code == 200
            assert effects[1]["nonce"] == "rs-nonce" and effects[0]["jti"] != effects[1]["jti"]
            with pytest.raises(BrokerError, match="different MCP endpoint"):
                await client.post("https://another.invalid/mcp")
    assert len(grants) == 2 and len(effects) == 2
    assert grants[0][0]["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]


@pytest.mark.parametrize(
    "changes",
    [
        {"token_type": "DPoP"},
        {"expires_in": -1},
        {"access_token": "contains\nnewline"},
        {"scope": "other"},
    ],
)
async def test_bad_token_metadata_never_reaches_resource(changes):
    async def grant(request):
        return httpx2.Response(
            200, json={"access_token": "ok", "token_type": "Bearer", "expires_in": 60, **changes}
        )

    async def resource(request):
        pytest.fail("a rejected token reached the protected resource")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(grant)) as token_http:
        auth = OAuthServiceAuth(
            resource_url="https://app.invalid/mcp",
            token_endpoint="https://auth.invalid/token",
            client_id="service",
            client_secret="synthetic-secret",
            scopes=["read"],
            token_http=token_http,
        )
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(resource), auth=auth
        ) as client:
            with pytest.raises(BrokerError):
                await client.get(auth.resource_url)


def test_remote_registration_requires_explicit_https_and_credential_scope():
    with pytest.raises(ValueError):
        HttpTransport(
            type="streamable-http", url="https://app.invalid/mcp", bearer_token_env="TOKEN"
        )
    transport = HttpTransport(
        type="streamable-http",
        url="https://app.invalid/mcp",
        allow_remote=True,
        oauth={
            "token_endpoint": "https://auth.invalid/token",
            "client_id": "service",
            "assertion_env": "WORKLOAD_ASSERTION",
        },
    )
    assert transport.oauth.assertion_env == "WORKLOAD_ASSERTION"
