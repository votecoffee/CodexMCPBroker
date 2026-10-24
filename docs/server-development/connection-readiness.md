# Connection readiness and recovery

[Developer guide](README.md) · [Acceptance](acceptance.md) · [Stable launchers and settings](installers-and-host-settings.md)

Use this contract for standalone SDK servers and framework-backed applications. A running process, installed plugin or successful package test does not establish that another chat can use the intended tools. Keep application-specific observations, process identities and deployment receipts outside public Git.

## State what is ready

Report these facts separately, with the observed version, selected profile and evidence date:

| Layer | Evidence needed |
| --- | --- |
| Source and package | Committed inputs, dependency pins, payload inventory and verified immutable bytes. |
| Installed selection | The actual command, arguments, working directory, environment and atomic runtime selection resolve correctly. |
| Fresh connection | The saved connection negotiates the selected protocol, lists all tools, reads current guidance and completes a harmless supported operation. |
| Concurrent availability | The expected simultaneous client workload works within explicit process, memory and request budgets; one client's close preserves peers. |
| Host activation | A fresh or reconnected host chat actually discovers and calls the intended tools. A standalone protocol probe cannot establish this. |
| Native workflow | The selected native operation succeeds on a disposable target, with required review, readback, save/reopen and cleanup evidence. |

An export-analysis profile must not be reported as a working native editor. An exposed tool declaration is not evidence of a native effect. Keep unsupported operations, unavailable required features and disabled optional features distinct. A disabled optional shared host is not a reason to withhold an otherwise accepted standalone profile.

Capability and doctor reports should identify their evidence scope. Separate source assessment, installed selection, running generation, configured native adapters and accepted workflows. Do not leave obsolete global `blocked` labels over supported operations, or change a label to `passed` without evidence. Report management as unavailable when no endpoint exists; an empty optional plugin inventory is different from an unhealthy serving owner.

## Budget clients separately from expensive work

Inventory every admission pool: connections, analyses, native sessions, workers, shared services and durable resource records. Explain which scope owns each pool and whether it spans processes, profiles or generations. Count wrappers, interpreter redirectors and expected SDK console helpers in the actual process budget.

Metadata, guidance and health requests should not acquire an exclusive native-worker lease for the lifetime of an idle chat. Admit expensive work lazily, after its target and policy are known. Use an app-owned shared supervisor or lightweight front doors where appropriate; deliberate sharing still needs ownership and authorization isolation. Keep connections bounded and measure their idle cost.

Test the host's expected open-chat and reconnect workload, including simultaneous startup. A ninth-client refusal can prove an eight-client bound while leaving the intended workload unsupported. Document that limitation and repair the admission design or establish a measured, reviewed capacity change. Do not blindly raise limits, terminate live peers or delete reservations to make a probe pass.

Distinguish live capacity from a stale record. Check retained owner/process generations and actual kernel ownership where applicable. Unknown ownership remains unknown. A diagnostic should explain the exhausted pool, configured limit, observed occupancy and safe next action without opening native software or acquiring the scarce slot itself.

## Make admission survive failed startup

No failed, cancelled or interrupted start may silently consume a permanent slot. Exercise every boundary between reservation, process creation, identity publication, containment, resume and protocol readiness.

Use durable reservation/binding with bounded failure cleanup, or an equivalent reviewed design: for example, create a stateless child suspended in a private kill-on-close job, atomically admit its full identity, then resume it. Prove that a crash before publication cannot leave an executing untracked child. This is not permission to launch a native effect before its ownership and uncertainty contract is established.

Keep old live generations and unresolved native work during upgrades. Never reinterpret an unbound reservation as proof that no child or effect existed. App-owned recovery must be identity/revision bound and specific to the resource kind; retiring a proven closed stateless frontend does not authorize retiring an unknown engineering write.

For shared ledgers, wait for lock acquisition with a bounded deadline and explicit contention classification. Retry acquisition only, before the protected operation executes; never replay the operation body or a completed close effect. Test interoperability between supported language implementations and simultaneous startups, registration, close and orphan inspection. Preserve the original failure when a cleanup or receipt-publication attempt also fails.

## Keep the original transport and parent identity

Validate the real SDK and launcher combination. A background process launcher is not automatically a stdio transport, and an offline in-process loader is not automatically compatible with a different runtime or a native worker profile. Check runtime/assembly compatibility, process limits, inherited jobs and the complete dependency closure before selecting that profile.

On Windows, a windowless interpreter can start with missing Python standard streams. Reconstruct only the verified inherited byte pipes, preserve Unicode and keep logs on stderr. Classify expected SDK-owned helpers separately from unexpected app-created children; capture their exact identities rather than relying on executable names.

If an SDK redirects standard descriptors, acquire an owned, non-inheritable duplicate of the original protocol handle before redirection. A borrowed descriptor that later points elsewhere cannot establish client EOF. Watch the retained parent independently of blocking pipe readers, SDK queues and active native calls. Do not let a read lock prevent the parent-death watcher from running.

Test actual parent death with another process holding a valid duplicated pipe writer while a reader remains blocked. Publish the trigger, writer-alive proof, exit code and timestamps before assertions or outer fixture cleanup. Distinguish an app's drain fence from an interpreter launcher's containment exit. Both mechanisms require evidence; neither can be inferred from eventual process absence.

## Exercise the real deployed settings

Generate sample and pilot settings through the production schema and strict parser. Test serialized property names, required fields, unknown/duplicate keys and effective defaults. A handwritten fixture that the real server rejects is not an application startup test. Keep failed artifacts and use a new case after correcting the fixture.

Use the [settings publication contract](installers-and-host-settings.md#write-toml-bytes-deliberately): validate actual temporary-file bytes, preserve valid LF/CRLF and unrelated semantics, serialize cooperating publishers, refuse concurrent edits and read back the atomic result. Re-read the live file rather than restoring an old whole-file backup.

Publish exactly one resolved server route and retain the app's own plugin and skills. A prepared installation or binding proposal is not a deployed connection. A writer restriction in one chat must be honored; report the exact ready proposal to the already authorized publisher instead of leaving the repair behind an unstated coordination gate.

Measure cold initialization and ordinary calls through the saved route with its actual hardened environment and timeout settings. Diagnose slow imports, worker startup and contention before changing a timeout. Configure numerical-library thread limits before imports where needed; observe private commit, working set, threads and aggregate process counts. A small working set alone does not establish a small memory commitment.

After publication, verify the selected version and required capabilities through the unchanged stable path. Preserve old clients until their normal close. Never claim that a reboot repairs a missing launcher, exhausted durable ledger or absent native profile.

## Required regression cases

Use disposable roots, synthetic inputs and exact private process handles. These cases supplement the detailed [acceptance matrix](acceptance.md#run-the-gates-in-order):

| Case | Required result |
| --- | --- |
| Cold host startup and reconnect burst | Supported clients receive current tools/guidance within measured budgets; excess admission is bounded and accurately diagnosed. |
| Two idle clients, then real work | Metadata does not monopolize native capacity; required work succeeds and each client preserves the other's ownership. |
| Failure at each startup boundary | No untracked executing child or leaked new admission; genuine uncertainty remains retained and visible. |
| Concurrent ledger acquisition and close | Bounded acquisition, no operation replay, original errors retained, coherent identity-bound receipts. |
| Idle, active and transport-queued EOF | No pre-cancel that hides the tested condition; outcomes and private descendants are accounted for before safety cleanup. |
| Client/owner crash with a held writer | A pinned-parent watcher or proved launcher containment ends private resources within the declared bound; attached/shared resources survive. |
| SDK descriptor diversion and windowless launch | Original byte-stream/Unicode/EOF behavior survives; unexpected helpers and invalid handles fail visibly. |
| Cache removal and active-peer update | Stable entry survives, new connections select reviewed bytes, old peers keep their own immutable generation. |
| Strict settings and schema boundaries | The production parser accepts generated settings; invalid input and stale guidance pins return meaningful caller errors. |
| Selected native workflow | A disposable native target proves the advertised effect/readback and relevant lifecycle behavior; offline analysis is reported separately. |

Retain checked process handles for the entire fault case. A process-query error is an exit only when the matching original creation-checked handle independently signals. A live, unknown, mismatched or invalid handle remains a failure. Record the exact API, identity and traceback. Do not reacquire a shutdown PID to replace evidence from the original handle.

Once the applicable cases pass, record actual saved-route and host results with their capability scope. Notify readiness only for that verified scope. A later regression is a new result; do not rewrite an earlier failed or incomplete case as passed.
