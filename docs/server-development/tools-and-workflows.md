# Tool and workflow design

[Developer guide](README.md) · [Next: sessions and recovery](sessions-and-recovery.md)

## Describe an operation precisely

Use stable, specific tool names and object input schemas. Describe required targets, inputs, local/native side effects, validation and result meaning. Avoid ambiguous operations that act on whichever object the GUI happens to have selected. Require explicit project, document, device, workspace or job IDs where the app needs them.

Publish useful schemas through normal MCP tool listing, including pagination when needed. Keep schemas self-contained; the broker validates a bounded supported subset and does not fetch external schema references. Tool annotations are discovery information, not app authorization. Test actual emitted definitions rather than only the source decorators.

Use the offline [catalog-quality checks](../catalog-quality.md) on a complete captured selected-profile catalog. Deterministic checks cover descriptions, schemas, references and hints. Optional TDQS-inspired reviews include full input/output schemas and sibling context; selection-only fixtures can measure a chosen model's decisions without executing engineering tools. Keep numeric grades advisory and separate from actual host discovery, lifecycle and native acceptance. Existing app packages are not automatically upgraded by this development feature.

The broker uses explicit `readOnlyHint: true` for its read channel. False or unknown annotations use the write channel unless a separately reviewed exact legacy override applies. A read/export operation that creates artifacts may intentionally be write-capable. Never rename or reclassify it to avoid policy. `describe_tool.invocation` states the current channel and request-ID requirement.

## Bind a reviewed change to its target

For a workflow requiring review, keep these conceptual operations in the app service. Names and fields are illustrative, not additional broker methods:

| Step | App-owned contract |
| --- | --- |
| Read | Return explicit resource identity, current revision/fingerprint and the data needed for review. |
| Preview | Validate the requested edit against that identity/revision; produce a candidate, exact digest and change summary without changing the native target. |
| Apply | Require the intended plan, candidate digest, current target guards and any required approval. Enforce expiry and one-use behavior. |
| Reconcile | Inspect actual target/outcome records after a lost or partial result; determine what happened before another intended operation. |

Bind plans to native target identity, relevant revisions, adapter/service scope, authorization and worker generation where needed. Recheck current state immediately before a native change. A candidate hash alone does not bind a new attachment to the original target. Document whether a failed apply consumes the plan and how failed/uncertain attempts are recorded.

An outer extension tool can contain many inner operations. Its broker write allowlist does not authorize every inner action. The app must enforce the selected operation's schema, effect, target, scope and approval. Keep broker tool revisions, broker capability snapshots, app catalog revisions and native review revisions distinct.

## Return evidence the caller can use

Return structured results with the applicable identity, revision, candidate hash, operation/job ID and outcome. Preserve relevant text/media and MCP error fields. Do not expose credentials or unnecessary native object handles. Bound diagnostic content and keep protocol stdout clean for stdio servers.

Separate facts such as accepted request, created job, imported candidate, compiled project, saved artifact and verified native readback. Do not report a later acceptance stage merely because an earlier one succeeded. An application error can follow partial changes; errors do not imply rollback. Provide reconciliation data where the app can do so safely.

Compare complete direct and broker `CallToolResult` objects. For dynamic IDs and paths, preserve raw results and verify later bindings rather than normalizing away the important fields. Byte preservation does not prove that the Codex host displayed media or approval UI correctly.

## Keep retries and jobs explicit

The broker requires a unique `request_id` for each intended write and prevents another dispatch of a claimed ID. It does not provide native exactly-once execution, cache the result for replay or replace app authorization. Keep app-level idempotency where native operations require it.

For long work, consider an app-owned job contract: start returns a handle, status returns actual state, and cancel requests cancellation with its own acknowledged outcome. Bind the handle to target/revision/generation and app access policy. A completed broker start receipt establishes only the returned start result; it does not establish terminal job completion. A cancel response is not necessarily a completed cancellation.

Calls to one registration are serialized. A long synchronous call can block status and cancellation traffic on that registration. App-owned jobs can release the request while work continues in an independent service. Assess shutdown behavior; a persisted history entry does not prove the work survives gateway exit.

## Forms, progress and payloads

Use negotiated request-bound form/URL elicitation only when the selected host supports the required policy. Validate accepted fields in the app; negative values, decline, cancel, malformed content, unsupported clients and late replies must not authorize the change. Never replace required human approval with a fabricated argument. URL acceptance acknowledges navigation without carrying credentials or proving external completion. Explicit app-owned Tasks support polling, authorized push and form/URL input; generic routing does not adopt downstream Tasks automatically. See the [extension guide](../extension-development.md).

Progress is optional telemetry for an active request. It can be coalesced or dropped and cannot prove completion, cancellation or approval. It does not extend the broker operation deadline. Required outcomes belong in app results and status methods.

Budget encoded arguments, base64/JSON escaping, HTTP envelopes and native string limits separately. Define app-specific maximum inputs/results and valid large cases. A larger broker limit cannot override native validation. Prefer an assessed app-owned file route for large immutable inputs when suitable; retain exact hashes, path authorization and atomic review. Do not split an atomic operation to bypass limits. See the [current limits](../../APP_CONTRACT.md#limits-and-future-extensions) and [payload guidance](../../MIGRATION.md).
