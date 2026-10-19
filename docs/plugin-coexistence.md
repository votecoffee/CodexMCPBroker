# Standalone plugins and broker integration

Every participating app continues to ship and support its own MCP server and standalone Codex plugin after broker adoption. Its application and direct integration remain usable without CodexMCPBroker. **Broker users install Universal Tools in place of the participating app plugins; standalone users install the chosen app's plugin without needing the broker.** Shipping a standalone plugin does not require installing it in broker mode. Both routes expose the same app-owned services, validation and authorization.

Broker adoption selects an additional integration route. It does not retire the app's plugin, transfer app ownership to the broker or guarantee independent parallel native contexts. Each app retains an explicit way to install or restore its standalone integration.

## Proposed installer policy

The following policy is a design requirement for future app installers. Automatic route selection, plugin detection and per-app skill installation are **not implemented by this broker**. Existing app installations and plugin settings remain unchanged during compatibility assessment.

| Requested route | Intended installer behavior |
| --- | --- |
| Automatic, fresh installation | Prefer the broker and skip the app plugin only when it is enabled, compatible and accepted for this app's selected workflow, the app registration is usable and required workflow contributions are available through Universal Tools alone. Otherwise offer the standalone plugin. A known unresolved operation or recovery barrier blocks automatic switching. |
| Standalone | Install or retain the app's plugin and direct MCP connection, including when Universal Tools is installed. Preserve app preflight and resource checks. |
| Broker | Use an explicitly selected broker registration and assessed session mode. If prerequisites are missing, report them and leave the prior route intact; do not silently select a direct connection. |
| Upgrade or reinstall | Preserve the recorded route and explicit user choices. Recheck compatibility before changing routing. Do not uninstall or disable an existing app plugin just because Universal Tools was discovered. |

The presence of a plugin folder, package name or running daemon is insufficient. Readiness needs the intended Codex profile's enabled integration, compatible broker/app versions, an authorized registration and its applicable policy, supported workflow features, required skills and completed acceptance gates. An installed but disabled or inaccessible Universal Tools plugin does not provide a usable route. A compatible broker need not already be running if its validated frontend can start it normally.

Installer readiness checks must stay within installation authorization and the app's metadata contract. Use passive configuration checks and authorized initialization/tool listing where appropriate; do not attach a native project, invoke a write, reload a worker or generate a confirmation prompt to discover readiness. An installer must not expand write policy to make a probe succeed. Record the selected route and prerequisite failures in local installation state, outside the public source.

Keep application installation separate from optional Codex integration. A missing broker must not prevent the app from installing or running independently. Broker removal must leave the standalone package and its documented installation procedure available; it must not trigger an in-flight route change.

## Aggregate app workflow guidance

Skipping the standalone plugin must not lose required workflow instructions, references or templates. The [workflow contribution profile](workflow-contributions.md) uses a single packaged Universal Tools skill to discover and read app-owned guides through the existing capability and read-tool interface. The app's plugin can remain uninstalled. Actual standalone-to-broker guidance parity still needs per-app and Codex host acceptance.

The two installation goals are:

- **Universal Tools mode:** one plugin supplies discovery/routing and loads the selected app's versioned workflow guidance on demand. Native app skill packages are not required to be installed alongside it.
- **Standalone mode:** the app's own plugin supplies its direct MCP connection and native skills. Universal Tools is not required.

Do not treat a tool refresh as native skill installation. OpenAI documents [MCP skill import](https://developers.openai.com/plugins/build/skills#import-a-skill-from-mcp) as a package snapshot. Runtime workflow guides are task-scoped app data, not hot-loaded native skills. A future reviewed native-skill packaging synchronizer would be separate installation work. The current reference contribution profile does not implement that synchronizer, arbitrary scripts or binary asset installation.

Required app instructions, explicit targets, preview/review/apply steps and confirmation policy must work through the chosen route. A missing required contribution blocks migration; it is not a reason to require both plugin sets permanently or silently change routes. Existing direct installations remain intact during assessment until an accepted migration is explicitly selected.

## Route changes and recovery

Select a route for a workflow before obtaining a target attachment, preview, approval or job handle. Use that route for the workflow's stateful sequence. Matching tool names or native targets do not make context IDs, review tokens or request journals interchangeable between adapters.

Finish or reconcile active work before changing routes. Broker failure, timeout, disabled-plugin detection or uninstall must not automatically send a pending write through the standalone plugin. An uncertain receipt remains uncertain until app-owned state is inspected; a direct connection cannot clear the broker's recovery barrier or supply a replacement approval.

Both plugin packages may coexist when explicitly chosen; this is not the default integration target. If direct and broker connections are enabled deliberately, app-owned resource locks, target revisions and authorization must protect cross-route access. Broker ownership does not fence direct plugins, the GUI or another broker. Shared business-service state does not make private adapter sessions safe to share. Assess concurrent access or keep the selected native resource to one route at a time.

Switching back to standalone is a local installation/configuration action after reconciliation, not an automatic runtime failover. Retain the prior configuration and app package so the change is reviewable and reversible.

## Acceptance checklist

Before implementing or enabling conditional plugin installation, verify:

1. Fresh standalone installation with no broker, including app startup, plugin discovery and required skills.
2. Universal Tools absent, disabled, inaccessible, incompatible or missing the app registration; automatic selection must preserve a usable standalone option without changing an active workflow.
3. An accepted Universal Tools-only route with the app plugin absent, correct registration, session mode, policy and contributed guidance; prove actual fresh-chat workflow/tool selection, reference availability and confirmation behavior against the standalone workflow.
4. Explicit standalone selection when the broker is available, plus upgrade/reinstall preserving that choice.
5. Broker removal or failure during a prepared or uncertain operation; verify zero automatic cross-route writes and preserved recovery evidence.
6. Any supported coexistence policy, including app resource fencing and explicit route restoration after work is finished or reconciled.

Store completed app/profile decisions locally. The [migration assessment](../MIGRATION.md) still requires selected adapter, host and separately authorized native acceptance. Until those gates pass, keep existing standalone integrations as they are.
