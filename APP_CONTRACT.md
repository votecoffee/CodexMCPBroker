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

Implement normal MCP initialization, paginated `tools/list`, and `tools/call`. The default `session_mode: "transient"` opens a fresh MCP connection/session for each discovery or invocation. Broker 0.2 adds opt-in `session_mode: "exclusive"` to retain an owned downstream session across calls. Both modes recheck tool definitions before dispatch. HTTP app engines must retain business state beyond individual HTTP request lifetimes, even when the broker retains its SDK client.

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

In transient mode the broker launches a process for each connection group and the SDK tears it down afterward. Exclusive mode keeps a single stdio host alive for its owner. Registering an executable authorizes its initialization on refresh; merely listing a local descriptor does not launch it.

## Persistent contexts and frontend ownership

These optional version-1 descriptor fields require broker 0.2 or newer:

```json
{
  "session_mode": "exclusive",
  "lease_seconds": 900.0,
  "resource_id": "my-app-engine"
}
```

`session_mode` defaults to `transient`. `lease_seconds` defaults to 900, with a 5–86,400-second range. `resource_id` is an optional lowercase ID using the same format as the service ID. These are administrative registration settings, never tool-supplied process commands or policy overrides. See [app-exclusive.service.json](examples/app-exclusive.service.json) for a complete descriptor.

The exclusive workflow is:

1. Call `acquire_service(service_id)`. It connects the downstream session and returns an opaque `context_id`, state and remaining lease time. Only one frontend can own a service. An active owner's repeated acquire renews the existing lease and returns the same context.
2. Pass `context_id` with every `call_read_tool` or `call_write_tool`. Context and frontend identity are checked after queue wait, before app dispatch or write claims. Tool schemas, revisions and app authorization still apply. Successful app calls extend the idle deadline.
3. Inspect `service_session_status(service_id)` when needed; it does not renew or release the lease and does not reveal another owner's handle. Complete app save/review/reconciliation before `release_service(service_id, context_id)`.

The dedicated worker task enters and exits the SDK context and runs its operations in that same task. Catalog refresh reuses an active worker; it does not replace the owning engine. Before acquisition, metadata discovery still initializes a temporary host, so app initialization must tolerate metadata-only discovery and shutdown.

Release and idle expiry close the downstream session/process before another owner can acquire it. The daemon reaps idle leases in the background. Expiry is deferred while an operation owns the service lock; a completed call renews the lease. Timeouts and transport failures retire the worker and leave its context lost until explicit release/expiry. Caller cancellation waits for the bounded operation/cleanup before unlocking; it does not hand a running worker to another owner. No automatic reconnect, native reattach, repreview or replay occurs.

Registration removal, disabling, invalidation or any descriptor change closes its old context on refresh. Calls also check local revocation before dispatch. A valid unchanged refresh preserves context. Live tool definitions are still re-listed and checked; changing an inner app catalog remains the app's separate revision responsibility.

### Identity and scope

The stdio frontend generates a random identity per process. Its `RemoteBackend` sends `X-Universal-Tools-Client` over each authenticated broker request. The server binds that identity through request context; no MCP tool accepts an owner identity argument. The identity is never forwarded to downstream apps or exposed in tool results. Direct HTTP clients that need exclusive contexts must generate and retain a 32–128-character identity containing only ASCII letters, digits, underscores or hyphens and send that header alongside the normal broker credential. Missing identity cannot acquire/use an exclusive context; existing transient clients need no new header.

This provides frontend affinity among trusted clients of one local-user broker. It does not authenticate an actual Codex chat or defend against a local process that already has the broker credential and can choose arbitrary headers. If a host shares one frontend among chats, those chats share its identity. A restarted frontend cannot resume the earlier identity; release beforehand or wait for the lease to expire.

Context leases are in memory. Broker shutdown closes workers; a crash/restart invalidates all handles and does not restore ownership or app state. Before explicit reacquisition after a crash, reconcile any external work that may still be running. The persisted write journal remains duplicate-dispatch protection, not a durable context or native outcome record.

The broker rejects enabled registrations with identical transport identities, or matching explicit `resource_id` values, when either uses exclusive mode. All conflicting routes are unavailable on refresh, and conflicts are checked again before calls. HTTP credential variable names do not distinguish resource identity. Assign the same resource ID to app-specific aliases the broker cannot infer. Exclusivity does not extend across another broker state, manual GUI activity, direct app clients or undeclared aliases. An independently running engine can retain selections after proxy release; apps still need explicit targets and their own revision/ownership checks.

## Limits and future extensions

Version 1 allows 128 services, eight parallel discovery probes, 2,000 tools per app, 100 tool-list pages, 128 KiB schemas, 256 KiB argument objects, and app timeouts from 0.2 to 300 seconds. Broker requests are capped at 1 MiB. Calls to one registration are serialized; there is no distributed lock across broker installations.

Potential later versions: durable context recovery, watched registration directories, supported remote HTTPS/OAuth connections, resource/prompt forwarding, elicitation relay, typed promotion of selected tools, operation-specific policies, and adapters for existing engineering apps. Persistent frontend-to-broker HTTP reuse is also separate from this version's downstream worker reuse. These are not part of the v0.2 acceptance claims.
