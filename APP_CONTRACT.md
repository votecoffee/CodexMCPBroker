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

Explicit `readOnlyHint: true` is required for the read channel, except exact names reviewed in the local legacy override. Unknown or explicit false annotations are write-capable. `allow_writes` is the service-wide master flag; an optional `allowed_write_tools` list narrows its scope as described below. Invocation tools use conservative broker annotations; the described definition retains the app annotations for the model to inspect.

The app is authoritative for:

- Resource identification: explicit document, project, robot, device, or workspace IDs.
- Access control and any app-specific review tokens or preview/apply sequences.
- Locks, revision checks, and conflicting operations across all clients.
- Native execution, saving, compilation, rollback, and errors after partial changes.
- Its own startup/shutdown and continued availability. Normal broker startup does not start HTTP apps.

The broker cannot create an app-specific authorization token or bypass a required review. A request ID prevents broker duplicate dispatch; it is not app authorization. Put app-level idempotency and revision protection in the tool contract when needed.

### Local tool policy and payload budgets

Broker 0.7 adds two optional version-1 registration fields:

```json
{
  "allowed_write_tools": ["prepare_change", "apply_change"],
  "max_argument_bytes": 2097152
}
```

`allowed_write_tools` accepts up to 2,000 unique, exact, case-sensitive outer MCP tool names using the tool naming rules above. Do not include the service prefix or wildcard patterns. A write requires both `allow_writes: true` and membership in this list. An empty list denies all writes. Omission or `null` preserves legacy service-wide behavior. Read-classified tools stay on the read channel and do not require list membership. The list cannot reclassify an effect, grant app authorization, or restrict individual operations carried inside one outer extension tool. Apps remain responsible for those inner policies. A stale or misspelled entry enables no other tool.

Search and description report each tool's effective `callable` flag. `describe_service` returns the registration's `policy` and `limits`; `describe_tool` includes `max_argument_bytes`. These snapshots do not grant permission. Registration changes are checked again after queue wait and before native dispatch, so revoking a name or reducing a limit blocks stale requests without waiting for refresh. Already-dispatched operations may continue.

`max_argument_bytes` is an integer from 1 to 8,388,608, defaulting to 262,144. Measurement uses compact JSON (`separators=(",", ":")`, `ensure_ascii=False`) encoded in UTF-8 with non-finite values rejected. Count the whole argument object, including field names, nested envelopes, base64 and escaped characters. A request at the limit is allowed; one byte over is rejected before app connection or a write claim. The actual incoming broker HTTP body is independently capped at 10,485,760 bytes, including MCP metadata/envelope bytes and any extra whitespace or escaping. Discovery, schema and elicitation budgets are unchanged; this is not a response-size guarantee.

No splitting, chunking, truncation or automatic replay is introduced. App-native batch/Undo and single-use review boundaries must remain intact. A local administrator must select a budget that the actual app, adapter and calling host also support. Updating policy or budgets changes the catalog revision and follows normal descriptor-change retirement rules for exclusive contexts. Finish and release existing work before reconfiguration.

## Optional inner capability provider

Broker 0.8 adds an optional `capability_catalog` registration object containing `schema_version: 1`, an exact read-only provider `tool` and its `arguments`. The provider must return the [normalized catalog contract](docs/capability-catalog.md); see its [registration example](examples/app-capabilities.service.json), [result example](examples/capability-catalog.json) and [schema](examples/capability-catalog.schema.json). Existing services default to no provider and receive no guessed metadata calls.

Catalog entries are search hints with read-only native discovery references, never executable routes or authorization. Provider and reference tools must explicitly declare `readOnlyHint: true`; legacy overrides cannot enable automated probing. Native tool schemas and argument budgets still apply. Separate broker tool, broker capability-snapshot, app catalog and target/review revisions must remain distinct.

Unowned refresh captures a shared installation-level catalog; a public provider must not expose secrets or private selections. Global refresh preserves that snapshot while an exclusive context exists and never queries the owner's provider. The active owner must explicitly call `refresh_capabilities(service_id, context_id)` for its private index, then use `describe_capability` with the searched revision. Description makes no app call and does not renew a lease. A valid owned probe renews it; invalid metadata clears the affected index without discarding a healthy context, while transport failures retain context-loss recovery rules. This does not reload app extensions or distribute their skills.

## Initialization guidance and advertised capabilities

Broker 0.4 retains the `protocolVersion`, `serverInfo`, `capabilities` and optional `instructions` fields from the app's MCP `initialize` response. These fields follow the [MCP lifecycle contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle). `describe_service(service_id)` returns them under `initialization`, with the registered service ID, `metadata_source`, `metadata_revision` and `broker_support`. Optional empty instructions remain empty; absent instructions remain absent. The combined UTF-8 JSON snapshot must fit 128 KiB. Larger metadata fails initialization; instructions are never silently truncated. Root-level `_meta` and other initialization extensions are not exported.

For transient or unowned discovery, the snapshot is catalog data shared with all authenticated broker frontends. Apps must not put secrets or another user's private context into discovery guidance. Registration authorizes this initialization and discovery. For an exclusive context, its active owner receives the initialization captured from that retained session, together with its `context_id`. Other frontends receive the earlier unowned catalog snapshot. Refresh reuses the worker for tool discovery but never replaces shared initialization metadata with the owner's snapshot. `observed_generation` identifies when a catalog snapshot was captured; the response's `generation` identifies the current catalog. Owned snapshots are identified by their context instead.

Description is observational: no app connection, tool invocation, acquisition, expiry cleanup or lease renewal occurs. Revoked registrations and unresolved recovery barriers block it. After acquisition, inspect the service again before following session-specific guidance. Metadata is a snapshot rather than a live query; its revision fingerprints initialization and registration data for comparison, and is separate from a tool's `catalog_revision`. Calls do not require or validate a metadata revision. An app must enforce critical authorization and review preconditions in its tool implementation.

Instructions are untrusted app guidance within the user's authorized task. They are not appended to the broker's top-level instructions and cannot change broker policy. App-advertised capabilities remain separate from actual broker support: tools, the form relay and opt-in request-bound progress described below are supported. Resources, prompts, completions, task APIs, other server requests (including sampling and roots) and general notifications are not relayed. `broker_support.elicitation_form`, `elicitation_url` and `progress` identify the specific relay support; the broad `server_requests` and `notifications` flags remain false for general forwarding. Advertising a feature does not enable it. Bundled app skills are not distributed by this mechanism.

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

Successful calls and repeated owner acquisition always renew the exact live monotonic deadline. A timestamp-only journal refresh may be skipped while its previous snapshot is less than five seconds old or one quarter of the lease, whichever is smaller; the next eligible renewal refreshes it. Each renewal still checks the reservation under a database transaction; missing, retiring or lost contexts fail immediately. Initial activation, retirement, loss and recovery remain immediate durable transitions. Clock rollback and changed lease duration force a timestamp refresh. A journal expiry snapshot is not an authority to clear a reservation or adopt a session; restart still marks orphaned contexts lost regardless of that timestamp.

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

## App-owned background jobs

An app can expose ordinary start, status/list and cancellation tools for work owned by its separate service. This uses normal tool routing; it does not enable MCP task APIs or retain a progress route after the start call returns. Start and cancellation tools remain write/unknown unless their exact outer definition explicitly meets the read-only contract. Starting a read-only background operation can still create job state and require a write route.

Retain the app's exact job ID, target and source/generation/revision bindings. A broker write receipt with `status: "completed"` acknowledges that the start tool returned successfully; the returned job may still be queued or running. Poll the app's status tool for its outcome. A cancellation acknowledgement can mean only that cancellation was requested; completed external actions may remain and the eventual outcome can be indeterminate. The broker does not translate job statuses into write receipts or automatically repeat a start/cancel request.

The exclusive context protects access through that broker while owned. It does not assign a frontend identity to an app job, enforce per-job visibility or prevent direct service/GUI access. Clean release, idle expiry or adapter exit can leave independently owned jobs running. A replacement context may be able to list the same jobs according to app policy. Inspect existing jobs and explicit targets before starting new work; apps own job authorization, retention and cross-client coordination.

If a start reply is lost after dispatch, retain its original request ID and inspect the uncertain receipt. The service may already have created a job. For an exclusive context, reconcile through the app's independent status/audit interface before administrative recovery acknowledgement; then acquire a fresh context and select the existing job through its app-owned handle. Recovery acknowledgement neither cancels the job nor changes the uncertain receipt. A transient call does not create an exclusive recovery barrier: other frontends can still call the app according to its policy, and the app owns reconciliation and cross-client coordination. Existing reservations on the same resource still apply. Never issue another start to discover an unknown outcome or assume adapter exit cancelled a job.

## Request-bound form elicitation

Broker 0.6 relays ordinary MCP `elicitation/create` form requests during a routed read or write tool call. It uses the [MCP elicitation contract](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation). Bridge clients advertise form support only. A calling host without form support, a URL-mode request or a task-augmented request receives a protocol error. The broker does not provide fallback approval tools or weaken app confirmation policy.

The native client UI receives the app's message with an `App <service-id>/<tool-name>` prefix and the original requested schema. Accepted content is validated against that schema before forwarding; decline and cancel remain distinct actions, with no approval content attached. Every form requires a fresh response. This relays a client's response and does not attest that a human saw a particular UI. Apps requiring explicit human approval must retain their own policy and validate the intended Codex host before migration. Form mode must not collect credentials or other secrets; URL-mode and auth linking remain unsupported.

A route is bound to one active tool invocation and its originating upstream MCP request/session. No confirmations are routed during initialization, acquisition, metadata discovery or after the call ends. Exclusive ownership is checked before native dispatch; another frontend cannot answer or adopt the owner's route. Concurrent app calls on separate pooled channels retain separate routes. The SDK adapter processes confirmation requests concurrently with tool results and cancellation, under the SDK session's own task group. This adapter is tested against the pinned MCP SDK version.

Stdio elicitation messages do not provide a portable parent tool-call identifier. The broker therefore uses the one active invocation on that downstream channel rather than claiming authenticated native operation identity. Apps must issue forms only for that invocation; delayed background or session-wide requests cannot be reliably attributed and are unsupported. Registered app content remains untrusted.

Supported forms use flat primitive fields or enum arrays. Schema references and nested object fields are rejected. Requests are limited to 32 KiB of UTF-8 JSON and responses to 16 KiB, with one outstanding form and eight forms per originating call. Root protocol metadata extensions are not forwarded. No messages, schemas, answers or approvals are persisted or truncated. The relay waits at most 60 seconds per form. The app's existing operation deadline continues to run, including time spent waiting for the user; configure it deliberately for reviewed workflows.

Cancellation invalidates the route before actor cleanup. Ending the call or reaching its deadline discards any pending or late answer; a replacement operation cannot inherit approval. A client UI may still display a prompt until its host dismisses it, but a late reply cannot approve the expired operation through this relay. Timeouts and lost calls retain the existing write-journal and durable ownership rules. An app error after an unsupported or failed confirmation can still follow earlier app-side changes; reconcile the actual outcome.

To support the reply path, the broker uses stateful Streamable HTTP with SSE responses, up to 256 sessions and a 900-second HTTP idle limit. Each session is bound to the validated frontend affinity principal; a different or omitted identity cannot use or terminate an identified session. Ownerless clients retain transient access and cannot acquire exclusive app contexts. This remains affinity among clients of one authenticated local-user broker, not a new Codex chat authentication boundary. Raw HTTP clients must retain the MCP session ID and protocol headers as required by MCP. Session expiry/restart returns an error without replay. A later explicit request can open another channel; HTTP session and native app context lifetimes remain independent.

## Request-bound progress

Broker 0.9 relays ordinary `notifications/progress` during routed read/write tool calls using the [MCP progress contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress). A client opts in with `_meta.progressToken` on `call_read_tool` or `call_write_tool`; integer zero is a valid token. Each active request needs a unique token within that client's session. Without a token, the broker does not request downstream progress. Metadata, refresh and acquisition operations do not provide a progress route.

The SDK allocates a separate token for each downstream call. App updates must use that token; it is never an app tool argument. The broker maps valid updates back through the specific active HTTP/stdio request to the client's original token and request ID. Identical client tokens in separate sessions do not share a route. Unknown and previous native tokens are ignored, and routes close on result, error, cancellation or timeout. Progress is neither persisted nor routed to a replacement call or background task. Task APIs remain unsupported.

Each hop considers at most 4,096 updates per invocation and retains one pending update, coalescing bursts. It sends at most ten updates per second, with a 0.5-second delivery timeout. A failed or slow send disables telemetry for that route. Only finite numeric values with strictly increasing progress are forwarded; optional totals are preserved without inventing a percentage or completion value. Messages over 4 KiB of UTF-8, including attribution, are dropped whole. The broker prefixes accepted messages with `App <service-id>/<tool-name> progress:`. Message content remains untrusted app data. Totals may be absent or change. Updates, including a final value, can be lost; apps must report actual outcomes in their tool result and app-owned status.

The progress callback never waits for the upstream client in the downstream SDK receive loop. A separate bounded pump handles delivery and is cancelled before the call returns. Thus a receiver cannot keep native work alive by consuming progress slowly. The app and frontend operation deadlines continue unchanged. Progress does not renew an idle lease by itself, change write-journal status, prove a native commit, authorize an action or acknowledge cancellation/rollback. A running serialized call can still block separate status/cancel tools for that service. Test the intended host's progress display independently of broker protocol delivery.

## Limits and future extensions

Version 1 allows 128 services, eight parallel discovery probes, 2,000 tools per app, 100 tool-list pages, 128 KiB schemas, argument objects defaulting to 256 KiB with an explicit per-service maximum of 8 MiB, and app timeouts from 0.2 to 300 seconds. Broker requests are capped at 10 MiB. Calls to one registration are serialized; there is no distributed lock across broker installations.

Potential later versions: native context resumption where apps support it, watched registration directories, supported remote HTTPS/OAuth connections, resource/prompt forwarding, URL elicitation, typed promotion of selected tools, operation-specific policies, and adapters for existing engineering apps. These remain outside the current acceptance scope.
