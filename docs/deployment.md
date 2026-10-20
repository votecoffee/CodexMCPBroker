# OAuth and shared deployments

These opt-in framework profiles supplement the local reference host. They do not change installed app plugins, start external infrastructure or migrate an app. The runtime uses MCP Python SDK 2.3.0. See the [extension guide](extension-development.md) for Tasks, Skills and native protocol federation.

## Choose a process model

| Model | State contract |
| --- | --- |
| Local `ApplicationHost` / optional broker | One process owns retained adapters, write journals and resource reservations. Multiple chats can use that process under the configured ownership rules. |
| Remote OAuth `ApplicationHost` | The same single owning process, with verified OAuth principals and HTTPS provided by the deployment. In-memory approval operations require that process to stay available. |
| Replicated native `ExtensionServer` | Share request-state encryption keys, a subscription bus and a durable Tasks repository. Application resources, jobs, receipts and fencing must also be shared. Native SDK continuations can move between replicas; live Python coroutine stacks and native engineering sessions cannot. |

`create_surface` and `ApplicationHost.create_server` reject a replicated deployment. Running several independent broker journals against one native target would not provide shared write deduplication. Use a single owning app service behind replicated stateless frontends, or implement the app's shared resource/receipt contract explicitly. A persisted task handle does not establish survival of its native job.

## Remote OAuth server

The app supplies a normal SDK `TokenVerifier`. It must validate signatures or introspection, expiry and the authorization server's token rules. Return `AccessToken` with the resource, issuer claim, subject, client ID and scopes. The profile additionally enforces its configured issuer, resource, required scopes and, by default, a nonempty subject. Machine-only deployments can explicitly select `require_subject=False`; that makes the verified OAuth client the principal.

```python
from universal_tools.deployment import OAuthDeployment

deployment = OAuthDeployment(
    issuer_url="https://identity.example",
    resource_url="https://tools.example/mcp",
    token_verifier=app_token_verifier,
    scopes=("app.read",),
    request_state_keys=app_secret_store.request_state_keys(),
)
server = host.create_server(deployment=deployment)
http_app = deployment.http_app(server)
```

Run the app's `ApplicationHost` and SDK session manager lifespans exactly once around the ASGI application's lifespan, as in the local reference host. For a native SDK app, use `deployment.create_server("Example", extensions=[...])` instead of `host.create_server`. The SDK publishes protected-resource metadata and the `WWW-Authenticate` challenge. Credentials never come from tool arguments or another app's bearer token. Ownership derives from the verified client/issuer/subject tuple, not a chat-supplied header.

Serve the public URL over HTTPS with a trusted reverse proxy or ASGI TLS configuration. The profile admits only the resource URL's exact Host authority and rejects browser origins by default. Do not disable those checks to fix proxy routing; preserve the public Host and configure the infrastructure correctly. The public resource URL and issuer are stable deployment identifiers.

Request-state keys are app secrets; never place them in source or public examples. The first key seals, all configured keys unseal. Rotate using the SDK key-ring procedure and keep the same audience across replicas. Separate services must use separate audiences. SDK state protection binds request arguments, principal, audience and expiry. App effect receipts remain necessary; a signed continuation is not a single-use business transaction.

## Outbound service OAuth and DPoP

`http_session(..., auth=provider)` accepts an app-selected `httpx2.Auth`, including the SDK's interactive OAuth/PKCE provider. The app owns its token storage and user-interaction callbacks. The framework also provides `OAuthServiceAuth` for the client-credentials and RFC 7523 workload-identity JWT bearer grants. These represent a service principal rather than an end user's delegated identity.

```python
from universal_tools.authorization import DPoPKey, OAuthServiceAuth

auth = OAuthServiceAuth(
    resource_url="https://tools.example/mcp",
    token_endpoint="https://identity.example/token",
    client_id="example-service",
    scopes=("app.read",),
    assertion_provider=app_identity_provider.fresh_assertion,
    dpop_key=DPoPKey(app_secret_store.dpop_p256_key()),
)
```

The asynchronous assertion callback returns the authorization server's accepted signed workload JWT. Optional client authentication uses a secret supplied by the app. Token responses require the selected token type, bounded token/expiry fields and all requested scopes if a scope field is returned. Tokens are cached only within that auth instance and bound to the exact MCP endpoint. Redirects do not receive credentials.

DPoP uses fresh ES256 proofs with a public JWK, method/URL binding and the access-token hash. The private key never enters a proof. Authorization-server nonce negotiation can repeat the token grant once. A resource-server nonce is retained for a subsequent explicit request; the framework does **not** retry the protected MCP operation. This is a client profile, not a DPoP authorization server or a replacement for TLS. See [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449.html) and [RFC 7523](https://www.rfc-editor.org/rfc/rfc7523.html).

Registrations can select an explicitly remote endpoint:

```json
{
  "type": "streamable-http",
  "url": "https://tools.example/mcp",
  "allow_remote": true,
  "oauth": {
    "token_endpoint": "https://identity.example/token",
    "client_id": "example-service",
    "scopes": ["app.read"],
    "assertion_env": "EXAMPLE_WORKLOAD_ASSERTION",
    "dpop_key_env": "EXAMPLE_DPOP_PRIVATE_KEY"
  }
}
```

The optional key environment variable contains a PEM P-256 private key; assertion and client-secret references are `assertion_env` and `client_secret_env`. Prefer an embedding with a secret-store callback for rotating workload assertions. Environment references are resolved locally; values are not included in metadata or diagnostics. A remote registration requires HTTPS, explicit opt-in and either its own bearer environment reference or an OAuth profile. Local HTTP remains restricted to literal loopback.

## Shared subscriptions and durable Tasks

Install `universal-codex-tools[redis]` to obtain the optional Redis client. Apps may supply a compatible asynchronous client instead. Keep one app-specific namespace and channel per trust boundary; configure Redis TLS, authentication and ACLs outside the framework.

```python
import redis.asyncio as redis
from universal_tools.redis_tasks import RedisTaskStore
from universal_tools.subscription_bus import RedisSubscriptionBus

client = redis.from_url(app_secret_store.redis_url())
bus = RedisSubscriptionBus(client, "example-app:notifications")
store = RedisTaskStore(client, "example-app", replicas=1)
# Pass store to TaskExtension and bus to OAuthDeployment(subscriptions=bus,
# replicated=True). Use deployment.create_server for this native host profile.
```

`ExtensionServer` starts and closes the bus subscription lifespan. The app owns and closes the Redis client after the server stops. Each server instance uses its own bus object; they share the configured channel. Messages contain bounded invalidation hints only. Subscriber Tasks re-read and reauthorize full state before delivery. No credentials, approval answers or task results are published to Redis pub/sub. Delivery has no replay; reconnect and refetch after a dropped stream. Monitor `bus.healthy` / `bus.failure` and replace an unhealthy worker.

`RedisTaskStore` provides atomic operation-key claims, optimistic record versions, owner checks and capacity/byte quotas. A bounded namespace lease serializes Task operations across replicas so observation cannot prematurely reconcile an intent still being dispatched. Busy callers receive an error and may poll later; native effects are never retried. Lease loss fences further effects and cannot release another worker's lease. Configure independent namespaces for genuinely independent app contexts; this profile does not promise parallel mutations within one namespace. JSON payloads remain opaque strings in Lua so 64-bit integers retain their exact values. Records and deduplication claims have no Redis TTL: expiry denies access without erasing the operation-key ledger. Archival must preserve that ledger.

Use Redis 7.2 or later with AOF enabled and a no-eviction policy. The default durability barrier uses `WAITAOF` on the **same connection** as the mutation and requires local fsync plus the configured replica count before app effects. Failed or lost durability acknowledgments preserve the intent and prevent dispatch. A deployment may supply an equivalent asynchronous `durable` barrier; it assumes responsibility for that guarantee. Redis persistence, failover, replica read consistency, native job recovery and multi-machine resource fencing require deployment acceptance. The tests use an isolated Redis-compatible command model; they do not certify an external Redis installation.

## Verification boundaries

The test profiles exercise real SDK HTTP authentication, principal separation, a native continuation resumed on another server instance, signed DPoP proofs, nonce handling without operation replay, shared repository claims and notification fan-out. Interactive interfaces have protocol and fallback tests using [the runnable Apps example](../examples/native_features.py). Actual host iframe rendering, identity-provider configuration, proxy/TLS deployment, Redis failover and app-native behavior remain separate acceptance work.
