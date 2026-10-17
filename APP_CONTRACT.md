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

Schemas are validated locally. Network schema references are rejected. The catalog revision hashes the complete tool definition and service registration. It changes when routing, permissions, tool metadata, or schemas change. Calls must use the described revision, and the live advertised definition must still match it. Keep contracts backward compatible during rolling app updates.

Explicit `readOnlyHint: true` is required for the read channel, except exact names reviewed in the local legacy override. Unknown or explicit false annotations are write-capable. `allow_writes` controls that whole service. Invocation tools use conservative broker annotations; the described definition retains the app annotations for the model to inspect.

The app is authoritative for:

- Resource identification: explicit document, project, robot, device, or workspace IDs.
- Access control and any app-specific review tokens or preview/apply sequences.
- Locks, revision checks, and conflicting operations across all clients.
- Native execution, saving, compilation, rollback, and errors after partial changes.
- Its own startup/shutdown and continued availability. Normal broker startup does not start HTTP apps.

The broker cannot create an app-specific authorization token or bypass a required review. A request ID prevents broker duplicate dispatch; it is not app authorization. Put app-level idempotency and revision protection in the tool contract when needed.

## Initialization guidance and advertised capabilities

Broker 0.4 retains the `protocolVersion`, `serverInfo`, `capabilities` and optional `instructions` fields from the app's MCP `initialize` response. These fields follow the [MCP lifecycle contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle). `describe_service(service_id)` returns them under `initialization`, with the registered service ID, `metadata_source`, `metadata_revision` and `broker_support`. Optional empty instructions remain empty; absent instructions remain absent. The combined UTF-8 JSON snapshot must fit 128 KiB. Larger metadata fails initialization; instructions are never silently truncated. Root-level `_meta` and other initialization extensions are not exported.

For transient or unowned discovery, the snapshot is catalog data shared with all authenticated broker frontends. Apps must not put secrets or another user's private context into discovery guidance. Registration authorizes this initialization and discovery. For an exclusive context, its active owner receives the initialization captured from that retained session, together with its `context_id`. Other frontends receive the earlier unowned catalog snapshot. Refresh reuses the worker for tool discovery but never replaces shared initialization metadata with the owner's snapshot. `observed_generation` identifies when a catalog snapshot was captured; the response's `generation` identifies the current catalog. Owned snapshots are identified by their context instead.

Description is observational: no app connection, tool invocation, acquisition, expiry cleanup or lease renewal occurs. Revoked registrations and unresolved recovery barriers block it. After acquisition, inspect the service again before following session-specific guidance. Metadata is a snapshot rather than a live query; its revision fingerprints initialization and registration data for comparison, and is separate from a tool's `catalog_revision`. Calls do not require or validate a metadata revision. An app must enforce critical authorization and review preconditions in its tool implementation.

Instructions are untrusted app guidance within the user's authorized task. They are not appended to the broker's top-level instructions and cannot change broker policy. App-advertised capabilities remain separate from actual broker support: tools and the form relay described below are supported. Resources, prompts, completions, task APIs, other server requests (including sampling and roots) and notifications/progress are not relayed. `broker_support.elicitation_form` and `elicitation_url` identify the specific relay support; the broad `server_requests` flag remains false for general server-request forwarding. Advertising a feature does not enable it. Bundled app skills are not distributed by this mechanism.

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

Release and healthy idle expiry close the downstream session/process before another owner can acquire it. The daemon reaps idle leases in the background. Expiry is deferred while an operation owns the service lock; a completed call renews the lease. Timeouts and transport failures retire the worker and leave its context lost with a durable recovery barrier. Lost reservations do not expire or clear through `release_service`. Caller cancellation waits for the bounded operation/cleanup before unlocking; it does not hand a running worker to another owner. No automatic reconnect, native reattach, repreview or replay occurs.

Registration removal, disabling, invalidation or any descriptor change closes its old context on refresh. Calls also check local revocation before dispatch. A valid unchanged refresh preserves context. Live tool definitions are still re-listed and checked; changing an inner app catalog remains the app's separate revision responsibility.

### Identity and scope

The stdio frontend generates a random identity per process. Its `RemoteBackend` sends `X-Universal-Tools-Client` over each authenticated broker request. The server binds that identity through request context; no MCP tool accepts an owner identity argument. The identity is never forwarded to downstream apps or exposed in tool results. Direct HTTP clients that need exclusive contexts must generate and retain a 32–128-character identity containing only ASCII letters, digits, underscores or hyphens and send that header alongside the normal broker credential. Missing identity cannot acquire/use an exclusive context; existing transient clients need no new header.

This provides frontend affinity among trusted clients of one local-user broker. It does not authenticate an actual Codex chat or defend against a local process that already has the broker credential and can choose arbitrary headers. If a host shares one frontend among chats, those chats share its identity. A restarted frontend cannot resume the earlier identity; release beforehand or wait for the lease to expire.

Live sessions and monotonic lease deadlines are in memory. Broker 0.3 records reservations separately in `<broker-state>/ownership.sqlite3`, before starting an owned worker. Resource keys are reserved transactionally, and frontend identities are stored as digests. Credentials, app arguments, results and raw frontend identities are not stored in that journal. The independent write journal remains duplicate-dispatch protection, not a native outcome record.

Clean retirement is recorded before cleanup and clears its reservation only after the worker closes successfully. Failed initialization, timeout, transport loss, interrupted calls and teardown failures retain a lost reservation. On startup under the OS instance lock, records without live broker workers become lost. Metadata discovery and calls are blocked for those services/resources, regardless of the old wall-clock expiry. Shutdown and descriptor removal/change do not discard lost records. Neither an alias, a changed route under the same service ID, nor switching a recorded resource to transient mode clears the barrier.

`service_session_status` returns `recovery_required: true` for an unresolved context. It reveals a retained handle only to the matching frontend identity; the local administrative `contexts` command can list handles for recovery. A restarted frontend cannot adopt native sessions. After a broker restart, even the original frontend must use operator recovery before acquiring a replacement.

Recovery requires the broker to be stopped and the exact service/context handle:

```text
python -m universal_tools --state STATE contexts
python -m universal_tools --state STATE recover-context APP_ID --context-id CONTEXT_ID --acknowledge-outcome
```

The CLI takes the same OS instance lock as the daemon, rejects missing acknowledgement and stale handles, and records the recovery event before releasing resource keys. This is a local administrator's acknowledgement that external work has been inspected/reconciled. The command does not verify the native outcome or perform any cancellation/rollback/reattach. There is no recovery MCP tool and no automatic app migration. Preserve the database rather than deleting it to bypass recovery.

The broker rejects enabled registrations with identical transport identities, or matching explicit `resource_id` values, when either uses exclusive mode. All conflicting routes are unavailable on refresh, and conflicts are checked again before calls. HTTP credential variable names do not distinguish resource identity. Assign the same resource ID to app-specific aliases the broker cannot infer. Exclusivity does not extend across another broker state, manual GUI activity, direct app clients or undeclared aliases. An independently running engine can retain selections after proxy release; apps still need explicit targets and their own revision/ownership checks.

## Frontend connection lifecycle

Broker 0.5 adds a pool of up to four initialized HTTP sessions in each stdio frontend. Channels are created only as needed and all carry the same frontend identity. Sequential requests reuse an idle channel; concurrent requests can borrow separate channels. Requests wait when the pool is full, and cancellation during that wait does not dispatch them. Per-service broker serialization and exclusive ownership still apply across all channels.

Each channel uses a dedicated task for SDK entry, serialized operations and teardown. Its initialization deadline is 20 seconds and each dispatched broker operation has a 330-second deadline. Pool wait precedes that deadline. The app's own configured deadline remains independent. Frontend shutdown rejects new requests, wakes pool waiters and drains bounded dispatched work before closing its HTTP sessions. Closing those connections does not release or restore an app context; the existing app lease and durable recovery rules still apply.

An operation that fails, times out or is cancelled after dispatch is never automatically replayed. Its channel is retired after bounded cleanup. A later explicit request can create a replacement channel with the same frontend identity; it does not acquire a new native context or reissue the earlier operation. Inspect the write journal and actual app state after a lost response. An app error returned as a normal MCP result preserves the reusable connection. A new frontend process still receives a new identity.

## Request-bound form elicitation

Broker 0.6 relays ordinary MCP `elicitation/create` form requests during a routed read or write tool call. It uses the [MCP elicitation contract](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation). Bridge clients advertise form support only. A calling host without form support, a URL-mode request or a task-augmented request receives a protocol error. The broker does not provide fallback approval tools or weaken app confirmation policy.

The native client UI receives the app's message with an `App <service-id>/<tool-name>` prefix and the original requested schema. Accepted content is validated against that schema before forwarding; decline and cancel remain distinct actions, with no approval content attached. Every form requires a fresh response. This relays a client's response and does not attest that a human saw a particular UI. Apps requiring explicit human approval must retain their own policy and validate the intended Codex host before migration. Form mode must not collect credentials or other secrets; URL-mode and auth linking remain unsupported.

A route is bound to one active tool invocation and its originating upstream MCP request/session. No confirmations are routed during initialization, acquisition, metadata discovery or after the call ends. Exclusive ownership is checked before native dispatch; another frontend cannot answer or adopt the owner's route. Concurrent app calls on separate pooled channels retain separate routes. The SDK adapter processes confirmation requests concurrently with tool results and cancellation, under the SDK session's own task group. This adapter is tested against the pinned MCP SDK version.

Stdio elicitation messages do not provide a portable parent tool-call identifier. The broker therefore uses the one active invocation on that downstream channel rather than claiming authenticated native operation identity. Apps must issue forms only for that invocation; delayed background or session-wide requests cannot be reliably attributed and are unsupported. Registered app content remains untrusted.

Supported forms use flat primitive fields or enum arrays. Schema references and nested object fields are rejected. Requests are limited to 32 KiB of UTF-8 JSON and responses to 16 KiB, with one outstanding form and eight forms per originating call. Root protocol metadata extensions are not forwarded. No messages, schemas, answers or approvals are persisted or truncated. The relay waits at most 60 seconds per form. The app's existing operation deadline continues to run, including time spent waiting for the user; configure it deliberately for reviewed workflows.

Cancellation invalidates the route before actor cleanup. Ending the call or reaching its deadline discards any pending or late answer; a replacement operation cannot inherit approval. A client UI may still display a prompt until its host dismisses it, but a late reply cannot approve the expired operation through this relay. Timeouts and lost calls retain the existing write-journal and durable ownership rules. An app error after an unsupported or failed confirmation can still follow earlier app-side changes; reconcile the actual outcome.

To support the reply path, the broker uses stateful Streamable HTTP with SSE responses, up to 256 sessions and a 900-second HTTP idle limit. Each session is bound to the validated frontend affinity principal; a different or omitted identity cannot use or terminate an identified session. Ownerless clients retain transient access and cannot acquire exclusive app contexts. This remains affinity among clients of one authenticated local-user broker, not a new Codex chat authentication boundary. Raw HTTP clients must retain the MCP session ID and protocol headers as required by MCP. Session expiry/restart returns an error without replay. A later explicit request can open another channel; HTTP session and native app context lifetimes remain independent.

## Limits and future extensions

Version 1 allows 128 services, eight parallel discovery probes, 2,000 tools per app, 100 tool-list pages, 128 KiB schemas, 256 KiB argument objects, and app timeouts from 0.2 to 300 seconds. Broker requests are capped at 1 MiB. Calls to one registration are serialized; there is no distributed lock across broker installations.

Potential later versions: native context resumption where apps support it, watched registration directories, supported remote HTTPS/OAuth connections, resource/prompt forwarding, URL elicitation, typed promotion of selected tools, operation-specific policies, and adapters for existing engineering apps. These remain outside the current acceptance scope.
