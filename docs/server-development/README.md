# Developing MCP servers with the shared app framework

Use this guide to build app-owned MCP servers with the [shared framework](../app-framework.md), stable app front doors, controlled backend publication and independent Windows manager. Each app keeps its own Codex plugin and native skill packages. The universal plugin is retired. The contracts apply across application domains and implementation languages; native .NET/NuGet bindings are not implemented yet.

CodexMCPBroker is experimental. **MCP protocol conformance, broker compatibility and native application acceptance are separate claims.** This guide defines a development profile for this broker; it is not an MCP certification program or a promise that every MCP feature is relayed.

## Reading path

| Document | Use it to |
| --- | --- |
| [Quickstart](quickstart.md) | Build a harmless read-only server and compare direct and broker calls in an isolated state. |
| [Architecture](architecture.md) | Put shared application services behind the GUI, standalone plugin and broker route. |
| [Shared app framework](../app-framework.md) | Reuse the packaged host/session/policy engine and update backends while keeping the native MCP front door stable. |
| [SDK feature support](../sdk-feature-support.md) | Distinguish implemented framework features, inherited SDK APIs, relay gaps and unverified deployment behavior. |
| [Optional extensions](../extension-development.md) | Integrate durable app-owned Tasks, verified Skills consumption, live publication and host profiles without automatic app migration. |
| [Tray service management](../service-management.md) | Report health, component versions, updates and app-owned guarded restarts to the Windows tray manager. |
| [Release versioning](../versioning.md) | Stamp fixed timestamp releases, normalize package identities and map Windows/.NET file versions. |
| [Durable resource ownership](../resource-ownership.md) | Track owned processes/software across abnormal exits and expose reviewed orphan cleanup without a plugin. |
| [Deployment profiles](../deployment.md) | Select authenticated remote hosts or shared native SDK deployments with explicit ownership and infrastructure contracts. |
| [Tool and workflow design](tools-and-workflows.md) | Design schemas, side-effect annotations, revisions, reviewed writes, jobs and results. |
| [Sessions and recovery](sessions-and-recovery.md) | Choose transient, exclusive or shared mode and define resource ownership and loss behavior. |
| [Registration and discovery](registration-and-discovery.md) | Register transports, manage dependencies and publish optional inner catalogs. |
| [Acceptance and release](acceptance.md) | Run a staged test matrix and complete a reusable integration record. |

The [app contract](../../APP_CONTRACT.md) and [service schema](../../examples/service.schema.json) describe the framework's retained routing engine. The [capability catalog contract](../capability-catalog.md) governs optional inner discovery. Historical universal plugin/coexistence/skill-composition documents are archived reference material, not current installation policy. Avoid copying limits into app code without versioning the integration profile.

## Start with the smallest required profile

| Profile | Required design and evidence |
| --- | --- |
| Read-only tools | Selected protocol discovery/negotiation, complete tool listing, explicit read annotations, bounded schemas/results, deterministic direct/broker comparison. |
| Reviewed writes | Read-only profile plus app authorization, explicit target/revision binding, one-use plans where required, exact write policy, independent effect counts and uncertain-outcome reconciliation. |
| Retained state | An assessed exclusive or shared mode, release/loss semantics, resource identity and two-frontend tests. |
| Inner feature catalogs | An explicitly registered normalized read-only provider and current native discovery references; ordinary outer tools still work without it. |
| Full selected-workflow integration | Applicable profiles plus actual Codex host acceptance, supported standalone packaging/skills, performance criteria and separately authorized native acceptance. |

Do not add a dependency on every optional MCP facility. Select the capabilities the workflow actually requires and check the broker's current support before choosing its interface.

## Current broker boundary

Supported routes are local stdio executables, MCP Streamable HTTP at literal loopback endpoints, and explicitly authenticated/opted-in remote HTTPS endpoints. The broker handles tools, attributed initialization guidance, retained sessions, request-bound form elicitation and opt-in progress. Registrations supply policy and bounded payload/deadline settings. Windows with Python 3.13 is the validated development environment; other platforms need their own evidence.

Ordinary URL elicitation is relayed alongside forms. Explicit framework adapters support Tasks, Skills, native resource/prompt/completion federation and SDK Apps. Sampling, roots, automatic downstream Tasks and UI tool forwarding remain outside generic routing. See the [extension guide](../extension-development.md) and [deployment profiles](../deployment.md). Native skill bundles use a separate explicit packaging/install/reload operation, not automatic MCP distribution. One exclusive registration has one frontend owner; shared calls are serialized and deliberately share app state/authorization. Independent private parallel contexts require additional app-defined fencing. See [limitations](../../README.md#limitations).

Every app distributes its own MCP server and Codex plugin after adoption. The shared dependency and manager need no universal plugin. App installers must not suppress their plugin because a universal plugin exists. Dynamic MCP skill/resource publication remains separate from native skill package installation and activation. No failed write may automatically switch routes or be replayed.

## Standards and implementation choices

Choose the wire profile explicitly. The legacy profile uses the [2025-11-25 initialization lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle). The modern [2026-07-28 protocol](https://modelcontextprotocol.io/specification/2026-07-28) uses `server/discover`, per-request version/capability metadata and stateless requests, without an initialization handshake. Backend registrations default to `protocol_mode: "legacy"`; select `"auto"` or `"2026-07-28"` deliberately. Check capabilities before using them. A newer SDK does not establish wire compatibility by itself.

Framework 0.18.2 and the broker pin MCP Python SDK 2.3.0. The isolated interoperability profile exercises a separate SDK 2.3 server on the legacy path and an SDK 2.2 overlay on the modern path; native extension checks also exercise the SDK 2.2 floor. These checks do not establish every SDK feature or a real app's readiness. Apps may use another language/SDK when their actual endpoint passes the selected profile. Pin and record the app's own dependencies instead of forcing them into the broker's environment. See [verification](../../VERIFICATION.md#cross-sdk-profile) and the [support matrix](../sdk-feature-support.md).
