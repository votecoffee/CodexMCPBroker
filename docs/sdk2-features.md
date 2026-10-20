# SDK 2 features

Framework 0.17.0 keeps MCP Python SDK 2.3.0 pinned and exercises SDK 2.2 app backends through explicit integration profiles. Other SDK versions need their own assessment. SDK versions and wire revisions are independent. No existing registration, installation, plugin or native project is migrated by upgrading the package. The [SDK support matrix](sdk-feature-support.md) distinguishes framework support, inherited SDK APIs, relay gaps and deployment acceptance.

## Modern connections and ownership

Service publications and registrations accept these additional fields:

```json
{
  "protocol_mode": "auto",
  "session_mode": "exclusive",
  "discovery_cache_ttl_seconds": 10,
  "watch_catalog": true
}
```

`protocol_mode` defaults to `legacy`, preserving existing handshake behavior. `auto` probes discovery and negotiates the modern `2026-07-28` profile or falls back to a handshake. `2026-07-28` requires modern discovery. Negotiation never retries a business operation. Both supported SDK versions are exercised with modern resolver-based forms; the separate legacy profile remains supported.

Modern requests have no retained MCP session identity. HTTP app hosts use the existing authenticated loopback boundary plus validated frontend affinity. Caller-supplied `clientInfo` and model arguments never establish an owner. This is affinity within one authenticated local-user deployment, not independent cryptographic chat authentication. Cross-user identity must be supplied by an app-owned authenticated adapter.

An embedding app can use `host.create_server(frontend_identity=opaque_id)` for one trusted frontend. The ID must be an opaque 32-128 character identifier selected by the deployment. Never expose that constructor option as a model tool or assign one shared ID to independent private clients. The packaged stdio proxy supplies its own internal frontend ID. A modern standalone app client without a trusted identity cannot acquire implicit ownership.

## Approval continuations

The fixed invocation tools accept modern protocol input rounds without adding model-facing approval arguments. The framework retains the original operation while a form is outstanding, returns an `InputRequiredResult`, and resumes that operation after a valid response. It does not restart the outer write or mint another write request ID.

Continuation tokens are random, single-use and bound to the original frontend, invocation name and exact arguments, including the target, catalog revision, context and write request ID. The SDK additionally seals the wire `requestState`. Changing arguments, using another frontend, reusing a consumed token or answering after expiry is rejected. A process restart cannot recover a pending continuation.

There are at most 256 retained operations and eight per frontend. A pending form expires after at most 60 seconds; the app's shorter operation deadline continues to apply. Shutdown and abandoned active requests cancel their operation and run normal actor cleanup. Lost or cancelled writes retain uncertain receipts and recovery barriers. A lost response is never permission to retry a write.

Ordinary form and URL requests are supported. The existing flat-schema, size, response validation and eight-form budget remain enforced. Multiple embedded forms are answered sequentially. URL requests require explicit client URL capability and a bounded HTTPS or literal loopback URL. URL acceptance carries no credential/form content and does not attest to authentication completion. Sampling, roots and nested tasks are not routed. Decline and cancel contain no approval content. These are protocol tests, not proof that a human saw a particular host UI. The app must validate its target and review revision after approval and before native effects.

Modern progress follows the active request leg. While the client is answering a returned form there is no active response stream, so progress is dropped. Progress remains optional telemetry and never proves native cancellation or completion.

## Notifications and discovery caching

`watch_catalog: true` requires a retained service and `auto` or modern protocol mode. The actor listens for backend tool-list changes, invalidates its hints and asks the broker engine to refresh discovery. Changes are coalesced by the existing one-second maintenance loop. Broken streams invalidate hints and are re-established with bounded backoff. A fallback legacy backend still requires explicit refresh. No native operation is replayed after notification loss.

The frontend exposes the read-only `broker://catalog` resource containing only the public catalog generation. Modern clients may subscribe to that URI and refetch discovery when it changes. Local hosts publish committed refreshes immediately; the stdio remote frontend observes broker generations every two seconds. This does not change the fourteen tool schemas or make a host reload native plugin definitions. A dropped subscription requires refetching; events are not a durable log.

Discovery caching is opt-in with a maximum TTL of 60 seconds. It honors a backend's shorter `ttlMs` hint; missing hints disable caching. Cache entries are detached copies confined to one retained connection, so private owner results cannot enter another connection's cache. Tool-list events invalidate the cache, including events received during a list request. There is no shared cross-principal cache and no caching of app results or approvals.

Every actual invocation bypasses discovery caching for its live schema/effect check. Registration, write policy, ownership, target/review contracts and durable duplicate protection remain authoritative. A cached read-only annotation can never authorize a tool that changed into a write.

## Response limits and diagnostics

An HTTP transport can set `max_sse_event_bytes` from 1 KiB through 128 MiB. `null` preserves the existing unbounded SDK event behavior for larger app results. This is a limit on one encoded SSE event, not on native document size. Existing argument and HTTP request-body limits remain independent. Changing the event budget does not create a different native resource identity. A response rejected after dispatch may still follow native effects; inspect receipts and reconcile the app rather than replaying it.

Framework spans use the `universal_tools` OpenTelemetry tracer for discovery and routed backend calls. No exporter is installed or enabled by the framework. Apps can configure their own exporter and retention. The framework's own spans suppress automatic exception recording and contain only fixed stage/outcome labels. Arguments, identities, paths, URLs, tokens, answers and result bodies are excluded. Exporters also receive SDK/app instrumentation, which the embedding app must assess separately.

`broker_status.runtime` reports process-local aggregate call counts and elapsed seconds. It is diagnostic, not an operation receipt or a completion claim. Restarting the process resets it. Timings include any application or approval wait within that stage and are not isolated CPU measurements.

## Skills and Tasks extensions

The framework provides opt-in `SkillCatalog`, `SkillRegistry`, `TaskExtension` and `TaskClient` components through the SDK's extension hooks. Skill catalogs support explicit live replacement, downstream verification/aggregation, pagination and optional directory reads. Tasks adapt app-owned durable jobs with ownership, polling, authorized push notifications, ordinary form/URL inputs and cooperative cancellation. These features do not automatically convert generic broker calls into tasks or grant skill execution permissions.

See the [extension developer guide](extension-development.md) for APIs, lifecycle hooks, host publication profiles, structural limits and app authorization/recovery requirements. CLI skill options remain explicit local-admin choices. Native skill bundle composition and host reload remain available independently.

## Validation boundaries

Synthetic tests cover both protocol generations, real HTTP envelopes and seals, cross-frontend denial, consumed/expired token rejection, uncertain-write cleanup, automatic discovery refresh, scoped cache copies, bounded responses, skill snapshots and tracing confidentiality. The optional cross-SDK profile adds a real SDK 2.2 modern backend. Use the [verification guide](../VERIFICATION.md) for commands and package checks.

Optional [extension adapters](extension-development.md) separately support native resource/prompt/completion federation, Tasks and Skills. [OAuth deployment profiles](deployment.md) provide verified cross-user identity. Automatic downstream task relay, native application acceptance and automatic migration remain separate. App-owned jobs remain app-owned. The framework remains experimental.

Upstream references: [protocol generations](https://py.sdk.modelcontextprotocol.io/whats-new/), [subscriptions](https://py.sdk.modelcontextprotocol.io/handlers/subscriptions/), [caching](https://py.sdk.modelcontextprotocol.io/client/caching/), [input rounds](https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/), [Skills convention](https://py.sdk.modelcontextprotocol.io/advanced/skills/).
