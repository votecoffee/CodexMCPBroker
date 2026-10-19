# Developing broker-compatible MCP servers

Use this guide when building a new app-owned MCP server or adding broker support to an existing one. The design applies to any application domain and implementation language. No vendor-specific broker adapter or new Universal Tools plugin is required for an ordinary supported MCP endpoint.

CodexMCPBroker is experimental. **MCP protocol conformance, broker compatibility and native application acceptance are separate claims.** This guide defines a development profile for this broker; it is not an MCP certification program or a promise that every MCP feature is relayed.

## Reading path

| Document | Use it to |
| --- | --- |
| [Quickstart](quickstart.md) | Build a harmless read-only server and compare direct and broker calls in an isolated state. |
| [Architecture](architecture.md) | Put shared application services behind the GUI, standalone plugin and broker route. |
| [Tool and workflow design](tools-and-workflows.md) | Design schemas, side-effect annotations, revisions, reviewed writes, jobs and results. |
| [Sessions and recovery](sessions-and-recovery.md) | Choose transient, exclusive or shared mode and define resource ownership and loss behavior. |
| [Registration and discovery](registration-and-discovery.md) | Register transports, manage dependencies and publish optional inner catalogs. |
| [Workflow contributions](../workflow-contributions.md) | Supply app guidance through one Universal Tools plugin, while retaining independent standalone packaging. |
| [Native skill bundles](../native-skill-bundles.md) | Publish broker-compatible native skills/resources for composition into one Universal Tools package and Codex reload. |
| [Acceptance and release](acceptance.md) | Run a staged test matrix and complete a reusable integration record. |

The [app contract](../../APP_CONTRACT.md) and checked-in [service schema](../../examples/service.schema.json) are the broker's detailed references. The [capability catalog contract](../capability-catalog.md) governs optional inner discovery. Follow the [plugin coexistence policy](../plugin-coexistence.md) for standalone packaging and future installer choices. Avoid copying limits into app code without versioning the integration profile.

## Start with the smallest required profile

| Profile | Required design and evidence |
| --- | --- |
| Read-only tools | Negotiated initialization, complete tool listing, explicit read annotations, bounded schemas/results, deterministic direct/broker comparison. |
| Reviewed writes | Read-only profile plus app authorization, explicit target/revision binding, one-use plans where required, exact write policy, independent effect counts and uncertain-outcome reconciliation. |
| Retained state | An assessed exclusive or shared mode, release/loss semantics, resource identity and two-frontend tests. |
| Inner feature catalogs | An explicitly registered normalized read-only provider and current native discovery references; ordinary outer tools still work without it. |
| Full selected-workflow integration | Applicable profiles plus actual Codex host acceptance, supported standalone packaging/skills, performance criteria and separately authorized native acceptance. |

Do not add a dependency on every optional MCP facility. Select the capabilities the workflow actually requires and check the broker's current support before choosing its interface.

## Current broker boundary

Supported routes are local stdio executables and MCP Streamable HTTP at literal loopback endpoints. The broker handles tools, attributed initialization guidance, retained sessions, request-bound form elicitation and opt-in progress. Registrations supply policy and bounded payload/deadline settings. Windows with Python 3.13 is the validated development environment; other platforms need their own evidence.

Resources, prompts, sampling, roots, URL elicitation, task APIs, general notifications and app UI routing are outside the current relay contract. Native skill bundles use a separate explicit packaging/install/reload operation, not automatic MCP distribution. One exclusive registration has one frontend owner; shared calls are serialized and deliberately share app state/authorization. Independent private parallel contexts require additional app-defined fencing. See [limitations](../../README.md#limitations).

Every app distributes its own MCP server and standalone Codex plugin after adoption. Broker-mode users should need only Universal Tools; app plugins are for the standalone choice, with coexistence optional. The workflow contribution profile is a reference path through existing tools, not native skill hot-loading or established host parity. Conditional app-plugin installation and automatic route selection remain proposed installer work. The broker neither installs app plugins nor switches a failed operation to a direct route.

## Standards and implementation choices

Implement the [MCP initialization lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) and [tools interface](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) for a negotiated version supported by both endpoints. Negotiate capabilities before using them. A newer SDK does not establish wire compatibility by itself.

The broker currently pins MCP Python 1.30.0. Its optional cross-SDK profile tests a separate Python 2.3.0 server on the negotiated `2025-11-25` path; that does not establish support for every newer SDK feature. Apps may use another language/SDK when their actual endpoint passes the selected profile. Pin and record the app's own dependencies instead of forcing them into the broker's environment.
