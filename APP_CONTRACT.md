# App integration contract, version 1

An app owns its tools and business state. Universal Tools owns discovery and routing. Apps do not need to know about Codex conversations.

## HTTP registration

```json
{
  "schema_version": 1,
  "id": "my-app",
  "display_name": "My App",
  "description": "Describe the app's user-facing capabilities.",
  "transport": {
    "type": "streamable-http",
    "url": "http://127.0.0.1:8790/mcp",
    "bearer_token_env": "MY_APP_MCP_TOKEN"
  },
  "enabled": true,
  "allow_writes": false,
  "read_only_tools": [],
  "timeout_seconds": 20.0
}
```

`bearer_token_env` is optional. The daemon inherits environment variables at launch, so restart it after changing credentials. Authentication headers belong to that app and are never inherited from the incoming Codex request. Redirects and environment HTTP proxies are disabled. Version 1 accepts literal loopback HTTP URLs only: no embedded credentials, query strings, fragments, DNS hostnames, or remote endpoints. Each app should independently authenticate requests if its capabilities need protection from other local clients.

The app installer calls the CLI to validate and atomically copy its manifest to `<broker-state>/services/<id>.json`. Stable IDs use lowercase letters, digits, underscores, and hyphens, start with a letter, and contain at most 64 characters. The broker scans only this directory on `refresh_services`. Uninstalling an app removes its registration using the CLI, then refreshes. It does not delete the app's data.

The broker must be initialized before registration. CLI examples, using the configured Python runtime:

```text
python -m universal_tools --state STATE register app.service.json
python -m universal_tools --state STATE register app.service.json --replace
python -m universal_tools --state STATE unregister my-app
python -m universal_tools --state STATE refresh
```

Registrations reject unknown fields. Inline secrets and shell command strings are not supported. Existing registrations require `--replace` to update them. Local app installers are responsible for obtaining their normal installation authorization; untrusted tool results cannot register services.

## App-owned MCP endpoint

Implement normal MCP initialization, paginated `tools/list`, and `tools/call`. The broker currently opens a fresh MCP connection/session for each discovery or invocation and rechecks tool definitions before dispatch. Keep business state in the long-running app server or durable storage; do not put it exclusively in a connection session.

Each tool supplies a specific name, useful description, input schema, and accurate annotations. A catalog ID is `<service-id>/<tool-name>`. Version 1 supports tool names containing ASCII letters, digits, underscores, periods, and hyphens, up to 128 characters. Every tool in a registration must be valid or the entire registration is unavailable.

Schemas are validated locally. Network schema references are rejected. The catalog revision hashes the complete tool definition and service registration. It changes when routing, permissions, metadata, or schemas change. Calls must use the described revision, and the live advertised definition must still match it. Keep contracts backward compatible during rolling app updates.

Explicit `readOnlyHint: true` is required for the read channel, except exact names reviewed in the local legacy override. Unknown or explicit false annotations are write-capable. `allow_writes` controls that whole service. Invocation tools use conservative broker annotations; the described definition retains the app annotations for the model to inspect.

The app is authoritative for:

- Resource identification: explicit document, project, robot, device, or workspace IDs.
- Access control and any app-specific review tokens or preview/apply sequences.
- Locks, revision checks, and conflicting operations across all clients.
- Native execution, saving, compilation, rollback, and errors after partial changes.
- Its own startup/shutdown and continued availability. Normal broker startup does not start HTTP apps.

The broker cannot create an app-specific authorization token or bypass a required review. A request ID prevents broker duplicate dispatch; it is not app authorization. Put app-level idempotency and revision protection in the tool contract when needed.

## Existing stdio apps

Use transport `type: "stdio"`, an absolute executable `command`, an argument array, optional absolute `cwd`, and an explicit `env_vars` list. No shell is invoked. App stderr is discarded to keep credentials and native logs out of broker logs; inspect the app's own diagnostic log when needed.

The broker launches a process for each connection group and the SDK tears it down afterward. This supports stateless wrappers and clients that connect to an independently shared native app service. Persistent app state should use a long-running HTTP MCP server. Registering an executable authorizes its initialization on refresh; merely listing a local descriptor does not launch it.

## Limits and future extensions

Version 1 allows 128 services, eight parallel discovery probes, 2,000 tools per app, 100 tool-list pages, 128 KiB schemas, 256 KiB argument objects, and app timeouts from 0.2 to 300 seconds. Broker requests are capped at 1 MiB. Calls to one registration are serialized; there is no distributed lock across broker installations.

Potential later versions: watched registration directories, persistent downstream sessions, supported remote HTTPS/OAuth connections, resource/prompt forwarding, typed promotion of selected tools, operation-specific policies, and adapters for existing engineering apps. These are not part of the v0.1 acceptance claims.
