# Acceptance and release

[Developer guide](README.md) · [Assessment worksheet](../adapter-assessment.md)

## Define a selected workflow

State which operations, targets, SDK/protocol version, session mode and required host features the app intends to support. Full integration means those declared workflows pass their applicable gates. It does not mean that every optional MCP feature or native operation has been implemented.

Record unsupported requirements as blockers. Mark a feature that is outside the chosen workflow explicitly rather than treating a missing test as a pass. Keep app-specific records, captured definitions/results, source exports and installation details under ignored `.local/private-docs/` or outside the public checkout.

## Run the gates in order

| Gate | Required evidence and negative checks |
| --- | --- |
| Reproducible source | Exact committed source or an explicit captured diff, dependency locks, build inputs and output hashes. Observe later drift; do not claim acceptance of newer code. |
| Metadata | Real discovery/version negotiation for the selected profile, complete paginated definitions and required capabilities. Legacy connections use initialization; modern connections carry version/capabilities per request. Check that metadata does not attach/operate native targets, prompt or expose private state. |
| Actual adapter with fake providers | Keep original handlers, policy/review code and required pure workers; replace native providers below them. Compare direct/broker definitions and complete selected results. |
| Session and resource behavior | Two frontends, owner denial, target/revision guards, refresh continuity, release/reacquisition, intentional shared visibility and alias handling. Exercise unsuitable modes when useful. |
| Required writes/forms | Independent effect counts, exact candidate binding, consumed/stale plans, wrong hash, policy denial and approval accept/negative/decline/cancel/invalid/unsupported/late responses. Rejected paths make zero fixture effects. |
| Failure and reconciliation | Queue versus in-flight timeout, worker versus adapter loss, lost reply after a recorded fake effect, original write receipt, durable barriers, explicit reconciliation and zero replay/cross-route retry. |
| Required workers/jobs | Production IPC/generations/manifests, reload success/rejection, cleanup, job state after adapter release and cancellation acknowledgment versus actual outcome. |
| Owned software and process lifetime | Explicit private/shared/attached inventory; bounded startup; clean close, EOF, failed startup and owner-death cleanup; no owned native/Node/shell descendants left behind; attached user apps and other clients preserved. See [resource ownership](sessions-and-recovery.md#track-owned-software-and-subprocesses). |
| Payload and efficiency | Valid large and invalid inputs, encodings and JSON/base64 overhead, exact results/media, direct/cold/warm measurements with stated samples and competing frontends. |
| Codex host | Actual fresh-chat discovery with Universal Tools alone and app plugins absent; contributed instructions/references, tool selection, media, human form UI, progress and failures compared with the standalone workflow. Protocol clients cannot establish UI behavior. |
| Installation/coexistence | Standalone without broker; absent/disabled/incompatible broker; explicit override; upgrade/reinstall choice preservation; no automatic route switch with active/uncertain work. |
| Native pilot | Separately authorized disposable targets, fresh app preflight/review, native acceptance/readback and failure reconciliation. Preserve original data and an available standalone route. |

Use the detailed [adapter assessment](../adapter-assessment.md) and [migration guide](../../MIGRATION.md) for failure boundaries. Frozen fake-backed passes can justify a proposed pilot; they neither authorize migration nor prove native validity, runtime behavior, commissioning or safety acceptance.

## Capture a reusable integration record

Copy this template into a private app/profile record and fill it from evidence. Use `passed`, `blocked` or `not assessed` for each applicable gate. Do not calculate a readiness percentage.

```text
App / adapter version:
Frozen source revision and working-tree diff, if any:
Dependency locks, compiled inputs and output hashes:
Observed post-freeze source drift:
Selected workflow and explicitly excluded routes:
Native/provider substitutions and accidental-dispatch guards:
App state owners and cleanup behavior:
Transport / negotiated protocol / SDK:
Candidate session mode and native resource/alias contract:
Write scope and required app approval:
Required host capabilities and unsupported requirements:
Standalone plugin, route-aware skills and installer choice:

Metadata gate:
Direct versus broker complete-result comparison:
Two-frontend ownership/refresh/release gate:
Revision, review, policy and approval negative gates:
Required worker/job gates:
Lost-reply receipt / independent effect count / reconciliation:
Payload boundaries and media fidelity:
Direct/cold/warm latency samples and acceptance criteria:
Actual Codex host gate:
Installation/coexistence gate:
Separately authorized native gate:

Evidence locations and hashes:
Unresolved blockers and limitations:
Next proposed test and authorization scope:
Migration decision and person responsible:
```

## Publish a generic implementation

Before shipping a server, preserve its standalone plugin and shared service API, pin dependencies, document installation and native requirements, and explain state ownership, supported capabilities and uncertain outcomes. Keep machine paths, user projects, credentials and completed acceptance records out of the public package.

Review the app's own extracted package and standalone install, then its broker registration in an isolated state. Verify required skill behavior on both routes. A working development checkout does not establish a clean install. Document tested protocol profiles without claiming every host/SDK/platform combination.

When updating a server or adding a feature, reuse its stable MCP interface or explicit native discovery layer where appropriate. Recheck affected metadata, worker/revision guards, host requirements and package behavior. Broker changes should address reusable protocol needs with synthetic tests, rather than recognizing this app by name.
