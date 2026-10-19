# Standalone plugins and broker integration

Every participating app continues to ship and support its own MCP server and standalone Codex plugin after broker adoption. Its application and direct integration remain usable without CodexMCPBroker. Both routes expose the same app-owned services, validation and authorization; the broker adds discovery, routing and session policy without becoming a required app dependency.

Broker adoption selects an additional integration route. It does not retire the app's plugin, transfer app ownership to the broker or guarantee independent parallel native contexts. Each app retains an explicit way to install or restore its standalone integration.

## Proposed installer policy

The following policy is a design requirement for future app installers. Automatic route selection, plugin detection and per-app skill installation are **not implemented by this broker**. Existing app installations and plugin settings remain unchanged during compatibility assessment.

| Requested route | Intended installer behavior |
| --- | --- |
| Automatic, fresh installation | Prefer the broker only when it is enabled, compatible and accepted for this app's selected workflow, the app registration is usable and required app guidance is available. Otherwise offer the standalone plugin. A known unresolved operation or recovery barrier blocks automatic switching. |
| Standalone | Install or retain the app's plugin and direct MCP connection, including when Universal Tools is installed. Preserve app preflight and resource checks. |
| Broker | Use an explicitly selected broker registration and assessed session mode. If prerequisites are missing, report them and leave the prior route intact; do not silently select a direct connection. |
| Upgrade or reinstall | Preserve the recorded route and explicit user choices. Recheck compatibility before changing routing. Do not uninstall or disable an existing app plugin just because Universal Tools was discovered. |

The presence of a plugin folder, package name or running daemon is insufficient. Readiness needs the intended Codex profile's enabled integration, compatible broker/app versions, an authorized registration and its applicable policy, supported workflow features, required skills and completed acceptance gates. An installed but disabled or inaccessible Universal Tools plugin does not provide a usable route. A compatible broker need not already be running if its validated frontend can start it normally.

Installer readiness checks must stay within installation authorization and the app's metadata contract. Use passive configuration checks and authorized initialization/tool listing where appropriate; do not attach a native project, invoke a write, reload a worker or generate a confirmation prompt to discover readiness. An installer must not expand write policy to make a probe succeed. Record the selected route and prerequisite failures in local installation state, outside the public source.

Keep application installation separate from optional Codex integration. A missing broker must not prevent the app from installing or running independently. Broker removal must leave the standalone package and its documented installation procedure available; it must not trigger an in-flight route change.

## Preserve app skills

Skipping the standalone plugin can also omit the skills bundled with it. Broker initialization guidance and tool catalogs do not install those skills, and a skill that names direct MCP tools may need an explicit broker invocation path.

Two packaging choices can preserve the app's workflows:

- Keep the app plugin installed and enabled for its skills; in broker mode, disable only its direct MCP server. Codex documents plugin-scoped MCP server policy separately from plugin packaging in [Package your plugin](https://developers.openai.com/plugins/build/plugins#configure-installed-server-policy). Validate that behavior in the selected host/profile before relying on it.
- Skip installing the standalone plugin only when an approved, version-compatible skills package or another assessed guidance mechanism is available for the broker route. The app still distributes its standalone plugin for independent use. This alternative requires app packaging/installer work; the current broker does not supply a skill distributor.

For the first broker pilots, retaining the app plugin and selectively disabling its direct server is the preferred candidate because it keeps the app's packaged guidance available. This remains subject to route-aware skill and host acceptance. Required app instructions, explicit targets, preview/review/apply steps and confirmation policy must work through the chosen route. No skill should silently fall back to another route when a broker call fails.

## Route changes and recovery

Select a route for a workflow before obtaining a target attachment, preview, approval or job handle. Use that route for the workflow's stateful sequence. Matching tool names or native targets do not make context IDs, review tokens or request journals interchangeable between adapters.

Finish or reconcile active work before changing routes. Broker failure, timeout, disabled-plugin detection or uninstall must not automatically send a pending write through the standalone plugin. An uncertain receipt remains uncertain until app-owned state is inspected; a direct connection cannot clear the broker's recovery barrier or supply a replacement approval.

Both plugin packages may coexist. If direct and broker connections are enabled deliberately, app-owned resource locks, target revisions and authorization must protect cross-route access. Broker ownership does not fence direct plugins, the GUI or another broker. Shared business-service state does not make private adapter sessions safe to share. Assess concurrent access or keep the selected native resource to one route at a time.

Switching back to standalone is a local installation/configuration action after reconciliation, not an automatic runtime failover. Retain the prior configuration and app package so the change is reviewable and reversible.

## Acceptance checklist

Before implementing or enabling conditional plugin installation, verify:

1. Fresh standalone installation with no broker, including app startup, plugin discovery and required skills.
2. Universal Tools absent, disabled, inaccessible, incompatible or missing the app registration; automatic selection must preserve a usable standalone option without changing an active workflow.
3. An accepted broker route with correct registration, session mode, policy and app guidance; prove actual fresh-chat skill/tool selection and confirmation behavior.
4. Explicit standalone selection when the broker is available, plus upgrade/reinstall preserving that choice.
5. Broker removal or failure during a prepared or uncertain operation; verify zero automatic cross-route writes and preserved recovery evidence.
6. Any supported coexistence policy, including app resource fencing and explicit route restoration after work is finished or reconciled.

Store completed app/profile decisions locally. The [migration assessment](../MIGRATION.md) still requires selected adapter, host and separately authorized native acceptance. Until those gates pass, keep existing standalone integrations as they are.
