# Adapter session assessment

Use this worksheet before proposing a broker registration for an existing app. Assess a selected workflow and an exact adapter version. Store completed worksheets, source exports, timing samples and installation details under ignored `.local/private-docs/` or outside the checkout. Keep the app's direct integration available throughout assessment.

## Identify the state owner

Read the adapter's initialization, tool handlers, service constructors and shutdown path. A tool returning a plan or job ID does not establish where that state lives or how long it survives.

| State to trace | Evidence to record | Consequence to test |
| --- | --- | --- |
| Selected target, project or engineering version | Object that retains the selection and every way it can change | A second call must use the intended target; another frontend must not borrow a private selection. |
| Captured reads and fingerprints | Receipt store, endpoint binding and stale-read checks | A digest alone may be insufficient after a new adapter starts. |
| Prepared plans and review tokens | Plan store, expiry, generation and one-use rules | Read/prepare/review/apply must remain in the required session. |
| Credentials and approval policy | Scope of configuration and per-client authorization | Sharing a worker must not share a private authorization scope. |
| Jobs and cancellation | Worker owner, durable history, cancellation mechanism and shutdown behavior | Durable history does not mean a job continues after adapter release. |
| Extension generations and catalogs | Public metadata versus private/session metadata; reload effects | A broker refresh must not reload features or invalidate an owned plan. |
| Native resource locks | Physical targets, aliases and direct-client behavior | Broker ownership only covers registered aliases; it does not fence the GUI or direct clients. |

Record which state belongs to the adapter process, a separately running app service, durable storage, or the native application. Include shutdown side effects such as cancelling jobs, closing an engineering attachment or discarding plans. State files can preserve evidence while the corresponding live operation is already gone.

## Select a candidate mode

| Adapter contract | Candidate | Required negative check |
| --- | --- | --- |
| Calls are independent; an app service retains all required workflow state | `transient` | Restart between calls must preserve the selected workflow's semantics. |
| Workflow state or authorization belongs to one retained adapter/client | `exclusive` | A different frontend must be refused before dispatch, even when supplied with the context and plan IDs. |
| An app-owned service deliberately shares targets, jobs and authorization; the adapter has no private client state | `shared` | A different frontend must not adopt private state, scope or confirmation. Any intentional shared access must be documented by the app owner. |

Test the unsuitable modes in a disposable fake-backed environment when that helps establish the boundary. A plan disappearing under transient mode or becoming accessible under shared mode can be expected behavior of those modes. It is evidence against selecting that mode for a private workflow, rather than a reason to bypass app review or weaken broker ownership.

Exclusive mode protects calls to one retained session. It does not transfer the session to another chat. Clean release or idle expiry can discard adapter-local state; loss requires reconciliation and does not restore it. Shared mode has no per-chat plan or job privacy. See the [app contract](../APP_CONTRACT.md) for lifecycle, resource aliases and recovery requirements.

## Compare an actual adapter with fake providers

1. Freeze exact source and dependencies, including a working-tree diff if assessing uncommitted code. Hash the inputs and keep them private. Recheck drift before using the results for a pilot.
2. Identify an injection seam below the actual MCP factory/handlers. Keep those files unchanged. Use synthetic targets and isolated state; replace native/file/network providers with explicit in-memory fakes. Disable unrelated feature workers or record exactly which workers were exercised.
3. Make accidental native dispatch fail before connecting or constructing a real provider. Check fixture logs for guard violations. Label results as fake-backed even when the real app policy and preview/commit code ran.
4. Capture a direct MCP reference with initialization metadata, every tool definition and complete results for selected deterministic cases. Compare broker results field-for-field, including application errors and structured content. For dynamic IDs, paths and timestamps, preserve raw results and verify their bindings through subsequent calls; do not normalize away a target, hash, revision, generation or review token.
5. Run read/prepare/review/apply through two real broker frontends. Check owner denial, exact hashes, stale revisions, refresh continuity, duplicate request IDs, consumed plans and clean release/reacquisition. The app's own preflight/readback checks must still run against the fake provider.
6. Exercise approval accept, negative values, decline, cancel, malformed responses, unsupported clients and late answers. Count fake writes independently of the returned result. Rejected paths must make zero writes; a successful synthetic confirmation does not prove a human saw a host prompt.
7. Lose a reply after the fake provider records a write. Check the uncertain receipt, recovery barrier and absence of replay. Inspect the fake operation count independently. Only a stopped fixture broker may acknowledge its own reservation during cleanup.
8. Include the app's actual payload boundary, filename/type restrictions and encoding overhead. Check a valid large request and an app-rejected request separately. A broker argument budget cannot override the app's validation.

Include local side effects in the policy assessment. An export/read tool may explicitly declare `readOnlyHint: false`; it then requires the write channel and a unique request ID. Inspect `describe_tool.invocation` rather than inferring a channel from the tool's name. Validate the advertised channel and blockers through the fake-backed workflow without changing the app's annotation or broadening normal registration policy.

When comparing efficiency, separate cold startup, warm calls and native work. Use the same frozen adapter and payloads, state sample counts, and preserve raw measurements. Measure competing frontends separately from serialized service work. A faster shared worker is only useful if its state and authorization contract also passes review.

## Record the decision

Use `passed`, `blocked` or `not assessed` for each gate; do not calculate a readiness percentage.

| Gate | Evidence needed |
| --- | --- |
| Metadata and wire contract | Frozen source/dependencies, initialization, definitions, required MCP features and result comparisons. |
| Selected fake-backed workflow | Candidate mode, state ownership, target/revision bindings, approval matrix, payload limits and independent fake-operation counts. |
| Failure and lifecycle | Release/expiry behavior, job shutdown, timeout/crash/restart, uncertain outcomes and no replay. |
| Efficiency | Stated direct/cold/warm samples and remaining native/host costs. |
| Codex host acceptance | Fresh-chat discovery, app instructions/skills, required media, approval and progress display. |
| Native pilot | Separately authorized targets and operations, fresh preflight, app-owned validation/readback and reconciliation. |

A selected workflow may pass the fake-backed gates while host and native acceptance remain unassessed. State the next proposed gate and its scope. Passing an assessment neither changes a registration nor authorizes migration. See the [migration guide](../MIGRATION.md) for the staged process.
