# Compatibility and migration assessment

CodexMCPBroker remains experimental and optional. Its current primitives support another scoped, non-migrating compatibility review: persistent contexts, frontend ownership, durable recovery barriers, app initialization guidance, request-bound forms, reusable frontend connections, selected write tools, bounded larger payloads and opt-in inner capability indexing. Passing the broker's synthetic tests does not establish any particular app's readiness.

Keep an app's direct integration installed while assessing it. Use a separate broker state, disposable adapters or fake native backends, and explicit test targets. Do not replace normal registrations, change native approval settings or connect to a project/device merely to assess metadata. Store installation-specific captures, paths, reports and decisions locally rather than in the public source.

## Select the review scope

| Stage | Evidence needed | What it can establish |
| --- | --- | --- |
| Metadata review | Pinned app/adapter/SDK revisions; real initialize and paginated tool-list captures; input/output schemas and initialization guidance | Wire/schema fit and known feature gaps without native tool dispatch |
| Adapter workflow fixtures | Actual adapter with a fake native backend; two frontends; explicit contexts and targets | Context, review and ownership semantics through the adapter, within the fixture |
| Protocol and failure fixtures | Exact result/media/error comparisons, required form responses, payload boundaries, deadlines, process loss and reconciliation | Whether transport/policy behavior preserves a proposed workflow and reports uncertain outcomes |
| Host acceptance | Fresh chat using the intended frontend; actual tool selection, media display and confirmation UI with disposable data | The selected Codex host's behavior for that workflow |
| Native pilot | Separately authorized disposable saved projects/models, explicit native targets and app-owned acceptance checks | Only the tested app revision, native environment and workflow |

Each stage should state what was actually exercised and retain a direct-versus-broker baseline. Record source hashes and transport/SDK versions; reassess when those change. Protocol compatibility does not follow from matching package version numbers.

## Check the workflow contract

- Confirm whether state belongs to the MCP host, its workers or an independently running app. Process-owned documents, selections, Undo, receipts and review plans need an exclusive context or an app-owned gateway that retains them.
- Prove a review/prepare/apply sequence survives calls and refresh in the same context. Check another frontend cannot adopt the context, and that native project/operation revisions still reject stale work. A broker lease does not fence GUI edits, direct clients, another broker or undeclared aliases.
- Exercise clean release, lease expiry, app crash, broker restart and lost replies. Preserve the recovery barrier and inspect actual native state before operator acknowledgement. Closing a proxy is not rollback, native cancellation or state restoration.
- Keep each app's bundled skills available where its workflows require them. `describe_service` carries attributed initialization guidance; it does not install skills. An app can opt into the [normalized inner catalog contract](docs/capability-catalog.md). Review its adapter's public/private metadata scope and read-only provider/reference declarations. Search hints still require native discovery, and app/target revisions remain independent.
- Use an exact outer-tool write allowlist for a narrow pilot where appropriate. Verify app authorization, target selection, preview/review tokens and human confirmation remain required. An allowed extension wrapper still needs app-owned checks for its inner operations.
- Compare the complete `CallToolResult`, including text, images, structured content, errors and metadata that the workflow uses. Test Unicode and integers at the app's own boundaries. Verify the host renders required media rather than assuming byte preservation proves display.
- Select an explicit argument budget without splitting an atomic batch or review operation. Include base64/JSON overhead and the HTTP envelope; check app and host limits independently. A larger broker budget cannot make an incompatible native request valid.
- Test accept, negative values, decline, cancel, unsupported host, timeout and late responses for required forms. Verify rejected paths perform zero fixture commits. Real human-confirmation policy still requires host acceptance; the relay does not attest a person saw a prompt.
- Test queue wait and in-flight failures separately. Writes must retain their original IDs and never be automatically replayed. Long jobs should expose app-owned handles and reconciliation; a running serialized call can block status/cancel traffic for that registration.
- Measure cold and warm calls, large payloads/results and competing frontends using stated sample sizes. Retained connections remove some initialization work, but calls still re-list native definitions. Do not infer native performance parity from example timings.

## Remaining boundaries

The broker does not relay resources, prompts, sampling, roots, URL elicitation, task APIs, progress, general notifications or app UI. It indexes inner operations only through an explicitly registered normalized provider; it does not infer arbitrary catalog formats, distribute app skills, infer native resource aliases, fence direct clients or recreate lost native sessions. Per-tool policy does not filter individual actions inside an outer tool. There is no native exactly-once execution or automatic outcome reconciliation.

Report a missing feature as a workflow blocker when the app actually needs it. A successful metadata or stub-backed review can justify a proposed pilot; it does not authorize migration or certify full functional/efficiency parity. Keep direct integrations until the selected workflow's later acceptance gates are explicitly met.
