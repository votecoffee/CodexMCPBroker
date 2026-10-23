# MCP SDK feature support

This matrix describes framework **26.1023.072616**, pinned to **MCP Python SDK 2.3.0**, with selected interoperability checks on **SDK 2.2.0**. SDK releases, protocol revisions and extension revisions are separate. It is a source/API coverage assessment, not a certification or claim that every combination passed in a real app/host. Applications keep their own plugins; the former universal plugin is retired.

The app host accepts a scoped [session/stdio factory](app-framework.md#app-owned-session-and-transport-factories) across discovery, probes and retained/transient connections. Historical package 0.20.1 supplies selection only. Timestamp releases also include opt-in experimental [contained Windows stdio](app-framework.md#contained-stdio-development-profile) with harmless byte/message lifecycle checks and real SDK 2.3 initialize/discovery/result/error/EOF/crash/startup-failure fixture coverage. Application/native/Codex acceptance remains separate. A selected callable alone does not establish worker ownership.

The framework has two distinct surfaces: `ApplicationHost` uses the fourteen fixed discovery/invocation tools over app-owned backends; `ExtensionServer` subclasses the SDK's `MCPServer` and accepts explicitly composed native extensions. An SDK feature available on the native server is not automatically a downstream broker feature. See the [developer guide](server-development/README.md) for the reading path.

## Implemented framework profiles

| Feature | Current support and boundary |
| --- | --- |
| Protocol generations | Explicit legacy, `auto` and `2026-07-28` connections. Modern discovery and per-request metadata do not provide an authenticated chat identity or make retained app state stateless. |
| Tools and results | Complete bounded paginated discovery, live definition/revision checks, self-contained JSON Schemas, structured results and text/image/audio/resource content. Default invocation routing preserves app result fields; it does not turn every downstream extension result into a native frontend result. |
| Ownership and mutations | Transient, exclusive and deliberately shared adapters, revisions, durable request claims/receipts and uncertain-outcome barriers. Retained framework hosts require one owning process. |
| Software/process lifetime | Optional durable `ResourceLedger`/`ResourceOwner`, Windows atomic job-contained background launching, identity-bound orphan cleanup and app-only recovery attestations. Apps must adopt it; existing SDK stdio launchers and externally brokered native software are not automatically contained. This is a framework management feature, not an MCP SDK extension. |
| Elicitation | Ordinary forms and URL inputs through tool invocation, with owner-bound modern continuations. Primitive fields and enum selections use bounded schemas. URL acceptance is navigation acknowledgment, not credentials or proof of a completed external interaction. |
| Progress and discovery changes | Opt-in request progress, backend tool-list watching, the public catalog-generation resource and scoped tool-discovery caching. Notifications are not durable event history. |
| Native resources, templates, prompts and completions | Explicit `ProtocolRelay` sources with app authorization, namespaced references and principal-scoped modern resource/prompt notifications. Reads require app-owned authentication to be established before retrieval. |
| Tasks | Explicit `TaskExtension` app adapters and `TaskClient`: durable handles, polling, authorized push, form/URL inputs and cooperative cancellation. SQLite and optional Redis stores do not make native jobs durable by themselves. |
| Skills | `SkillCatalog`, `SkillRegistry`, origin/content verification, explicit federation, lazy file reads and live MCP publication. Applications own native plugin/skill packages; the universal skill composer is retired. MCP publication does not activate a native host skill. |
| MCP Apps | SDK Apps extension composition and a tested native UI-resource example with text fallback. Downstream UI tool execution is not automatically proxied. |
| Remote authentication | Optional HTTPS `OAuthDeployment`, service client credentials, RFC 7523 workload assertions and DPoP client proofs. Credential storage, verified identity and deployment remain app-owned. |
| Shared native SDK deployment | Shared request-state keys, `RedisSubscriptionBus` and fenced/durable `RedisTaskStore`. Application resources, receipts and jobs need their own shared state and fencing. |
| Diagnostics and limits | Bounded arguments/structure/HTTP/SSE profiles, aggregate process metrics and redacted framework OpenTelemetry spans. Exporter selection and app/SDK telemetry assessment remain deployment work. |

The [SDK feature guide](sdk2-features.md), [extension guide](extension-development.md), [deployment guide](deployment.md) and [verification guide](../VERIFICATION.md) define the APIs and checks. Limits and ownership/recovery rules still apply to every profile.

## Available through the SDK, with app integration required

`ExtensionServer` retains the inherited SDK server APIs and constructor options. Apps can register their own native tools, resources and prompts, use `Resolve` dependencies, configure resource-template path checks, add middleware and choose cache hints. This does not add those choices to the fixed `ApplicationHost.create_server()` signature or the broker's JSON registration contract. Existing authorization and extension identity boundaries must still be preserved.

The underlying SDK client implements schema-annotated `x-mcp-header` parameters after tool listing, and native result models accept modern structured content. These are inherited mechanisms, not separately established full framework/host parity. Add a real HTTP direct-versus-framework regression for marked routing parameters before depending on that route.

`http_session(..., auth=provider)` accepts an app-selected `httpx2.Auth`. This can be the SDK's interactive OAuth/PKCE provider, configured with Client ID Metadata Documents, or its private-key JWT or enterprise identity-assertion provider. The app supplies token storage, trusted issuer configuration and any identity-provider callback. The broker's service-registration settings currently expose bearer credentials and the narrower service/workload-grant profile; they do not configure all those SDK providers automatically.

## Remaining integration gaps

| Gap | Current behavior and required work |
| --- | --- |
| Relayed resource/prompt input rounds | The SDK supports `InputRequiredResult` from `resources/read` and `prompts/get`. `ProtocolRelay` currently rejects incoming continuation state/responses and does not drive those rounds. Supporting them requires origin/principal/request-bound state, answer validation and lifecycle/failure checks. An app may use the native SDK path directly today. |
| Automatic downstream Tasks | Generic tool routing does not adopt downstream Task handles or forward Task lifecycle methods. Use explicit app-owned Task adapters. A reusable relay would need durable origin/owner mapping, authorization, cancellation and ambiguous-outcome contracts. |
| Automatic downstream Apps UI tools | Native SDK Apps composition is available. A broker UI relay would additionally need tool/resource URI mapping, host permissions/CSP and owner-bound UI calls. A resource relay alone is insufficient. |
| Packaged advanced OAuth profiles | SDK injection is available, but interactive PKCE/CIMD, private-key JWT and enterprise ID-JAG flows lack first-class broker registration/CLI profiles and dedicated framework acceptance fixtures. Workload JWT support alone does not establish enterprise SSO acceptance. |
| Additional SDK parity evidence | Header-annotated HTTP routing, broader schema/result shapes and app-selected SDK constructor options need dedicated profile evidence before making broader parity claims. A feature inherited from the SDK is not automatically missing code, and an untested combination is not a pass. |

These gaps matter when a selected app workflow requires them. They are not mandatory additions for every app. The [acceptance guide](server-development/acceptance.md) requires unsupported workflow requirements to be recorded explicitly.

## Deliberate exclusions and acceptance work

Sampling, roots and MCP protocol logging are deprecated upstream. The generic broker does not relay them; new work should use explicit resource/argument configuration, app-owned provider integration and stderr/OpenTelemetry as appropriate. Legacy SSE is also deprecated. Do not add removed `ping` or modern `logging/setLevel` methods to fill a checklist.

Nested task inputs, arbitrary downstream extension forwarding and automatic permission/approval synthesis are not supported. Replication does not move live Python stacks or native engineering sessions between processes. Native .NET bindings and a public package feed are project roadmap items, not missing Python SDK protocol features.

Actual host skill selection/reload, form and Apps rendering, app-native jobs and effects, real identity-provider grants, HTTPS proxy behavior and external Redis durability/failover remain separate acceptance work. Synthetic checks and source inspection do not establish those outcomes. Record deployment and app-specific evidence outside public source.

## Desktop management

Framework 0.18 additionally provides an opt-in [tray management interface](service-management.md). It does not enable additional MCP protocol relays or migrate installed apps. The Windows manager lists installed plugins and reports live health/restart availability only for app-registered management endpoints.

## Upstream references

- [Python SDK v2 features](https://py.sdk.modelcontextprotocol.io/whats-new/)
- [Multi-round-trip requests, including resources and prompts](https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/)
- [Header parameters](https://py.sdk.modelcontextprotocol.io/advanced/header-parameters/)
- [SDK OAuth clients and Client ID Metadata Documents](https://py.sdk.modelcontextprotocol.io/client/oauth-clients/)
- [Enterprise identity assertion](https://py.sdk.modelcontextprotocol.io/client/identity-assertion/)
- [MCP extension overview](https://modelcontextprotocol.io/extensions/overview)
- [2026-07-28 changes and deprecations](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
