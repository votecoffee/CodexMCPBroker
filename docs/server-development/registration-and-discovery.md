# Registration and discovery

[Developer guide](README.md) · [Next: acceptance](acceptance.md)

## Provide one supported MCP interface

Implement negotiated MCP initialization, complete tool listing and tool calls using a suitable SDK. Pin the app's dependency/runtime versions and record the negotiated protocol in tests. The app does not have to use Python or share the broker's SDK environment.

Record app release identity separately from SDK and protocol versions. Inspect actual `serverInfo` output: an SDK default version may describe the library rather than the app release. Do not base installer compatibility on that default alone; use an explicit app version when supported and retain source/package/profile identity in acceptance evidence.

| Transport | Current registration boundary |
| --- | --- |
| `stdio` | An executable plus an argument array; explicit working directory and permitted environment variable names where needed. Keep stdout for MCP and diagnostics on stderr. No shell command string. |
| `streamable-http` | Literal loopback HTTP with an explicit port/path, or explicitly opted-in authenticated remote HTTPS; credentials use local environment references. The app owns its HTTP service lifecycle. |

An existing REST API is not automatically an MCP endpoint. Remote HTTPS requires `allow_remote: true` and explicit scoped authentication; see [deployment profiles](../deployment.md). Plain remote HTTP, embedded URL credentials and inline secrets are rejected. Broker credentials are not forwarded to apps; app authentication is independent. See the [transport contract](../../APP_CONTRACT.md#http-registration).

## Generate an installation-specific descriptor

Use the checked-in [service schema](../../examples/service.schema.json) and examples as the source of field names and limits. Start read-only and choose a deliberate session mode. For example, this is a template requiring real installed paths:

```json
{
  "schema_version": 1,
  "id": "example-app",
  "display_name": "Example App",
  "transport": {
    "type": "stdio",
    "command": "C:\\Path\\To\\App\\server.exe",
    "args": ["--mcp"],
    "cwd": "C:\\Path\\To\\App",
    "env_vars": []
  },
  "session_mode": "exclusive",
  "resource_id": "example-app-engine",
  "allow_writes": false,
  "allowed_write_tools": []
}
```

Only use `exclusive` when the app contract needs retained private state; pure independent tools can use the default transient mode. Shared mode requires intentionally shared state and authorization. Do not assign the same example resource ID to unrelated installations without assessing its intended scope.

Register with an explicitly selected initialized state directory:

```text
python -m universal_tools --state STATE register app.service.json
python -m universal_tools --state STATE refresh
```

Registration is a trusted local administrative action that authorizes connection or process launch. An installer supplies installed executable paths, selected configuration and least-privilege tool policy; MCP tool results cannot register arbitrary endpoints. Keep generated descriptors and secrets out of public source. Unknown registration fields are rejected rather than interpreted as extension hooks.

An existing registration requires `--replace` to update. Finish/reconcile active work before reconfiguration can retire a retained session. Uninstallation should remove only the app's own registration through `unregister APP_ID`, then refresh, preserving app data and any required outcome evidence. Do not clear ownership/write journals as cleanup.

## Expose optional inner features without broker changes

An app can keep a stable set of outer tools while publishing its own extensible operation catalog. Native discovery must describe current argument schemas, effects, target requirements and app-owned revisions. An outer write wrapper must still enforce every inner operation's policy.

To make those inner features searchable through Universal Tools, add the [normalized capability provider](../capability-catalog.md). This is an optional broker discovery format, not a new MCP standard or a mandatory replacement for native discovery.

1. Implement an explicitly read-only metadata provider that returns the exact normalized catalog object.
2. For each capability, reference a real, explicitly read-only outer discovery tool with schema-valid arguments.
3. Register the provider by name through `capability_catalog` and test both public and owner-scoped snapshots where applicable.
4. Keep native target/review revisions separate from app catalog and broker snapshot revisions.

The broker does not guess providers from names such as `list_capabilities`, automatically unwrap an arbitrary app result envelope or turn a capability entry into an executable virtual tool. If the existing native format differs, supply an app-owned normalized adapter without breaking the native interface.

Provider registration permits metadata probes; providers must not attach targets, mutate state, load unreviewed workers or request approval. Unowned discovery metadata must be safe for every authenticated frontend. An exclusive owner explicitly refreshes private hints; global refresh must not probe or publish that owner's state. Invalid metadata must not become a reason to weaken native review.

## Package the standalone Codex integration

A broker service descriptor and a Codex plugin package are different artifacts. Keep the app's standalone package available with a layout such as:

```text
app-plugin/
  plugin.json
  mcp.json
  skills/app-workflows/SKILL.md
  assets/
```

Use the current [official OpenAI packaging documentation](https://developers.openai.com/plugins/build/plugins) for plugin identity, local-executor metadata and MCP launch configuration. Resolve installed dependencies through the app's launcher/configuration; do not embed another developer's interpreter or workspace path in a public package. Do not copy a broker service descriptor into `mcp.json`: their schemas and purposes differ.

Publish broker-facing [workflow guides](../workflow-contributions.md) from the app service so Universal Tools can load them without the standalone plugin. Keep standalone native skills in that app's package; reusable content may share a source, with explicit invocation bindings for each route. A broker path discovers/describes the app's tools, acquires its required context and preserves broker request/revision fields. A standalone path uses direct definitions. Both follow the same app-owned target, preview, review and reconciliation rules. Missing tools or a failed write must not make a skill install a plugin, loosen policy or silently change routes.

Package installation, plugin enablement, direct-server enablement, app registration and required skill availability are separate checks. Validate the selected combination in a fresh Codex chat; the read-only quickstart intentionally does not configure any installed plugin.

For native discovery and packaged resources, publish a [broker skill bundle](../native-skill-bundles.md) with explicit route bindings, namespaced skills, file hashes and required service IDs. Its local registry and compose/install/reload step are separate from service registration and MCP refresh. The app installer may call that path after broker integration is selected and accepted; publishing workflow metadata does not authorize installation.

## Make installation and failures observable

Keep app installation, standalone Codex plugin packaging and optional broker registration separate. Follow the [coexistence policy](../plugin-coexistence.md): preserve explicit route choices, required app skills and an independent standalone option. Automatic installer selection is proposed behavior, not a current broker service.

Use `list_services`, `describe_service` and recorded discovery failures to diagnose readiness. Reported phases and typed timeout kinds identify the last observed stage; they do not prove the root cause or authorize retries. Keep full local process logs separately, redact credentials, and never operate a native target merely to make a health indicator succeed.
