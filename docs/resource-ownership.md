# Durable resource ownership and cleanup

The shared `universal_tools.resources` API records process/software ownership independently of MCP connections and live management endpoints. It needs no plugin. Apps keep their own plugins, supervisors, domain authorization and native recovery journals. Adoption is explicit; installing this framework does not discover or adopt existing software automatically.

## Record ownership before starting work

`ResourceLedger` stores bounded ordinary JSON records under the private management registry's `resources/` directory. Each record retains app ID, supervisor generation, the owner's PID/creation time/image, resource identity and label, ownership, cleanup policy, process identity, containment, current state, recovery barriers and cleanup receipt. Command lines, environment values, credentials and project contents are not recorded. Windows identities use PID, decimal creation FILETIME and the normalized executable path captured from the same process handle.

Records use a byte-range OS lock, flushed temporary files and atomic replacement. A cleanup intent is durable before termination. History remains after exit; uncertain receipts are never replaced by an automatic retry. Invalid records and capacity violations fail closed. The default admission limit is 32 non-retired resources per app across supervisor generations, with an explicit 1–256 override; history has a separate 4,096-record limit and each JSON record is bounded to 32 KiB. Preserve evidence when performing administrative history archival; the package does not erase old claims automatically.

Each fingerprint also has a `host_id`: SHA-256 of the canonical lowercase hyphenated Windows MachineGuid read from the 64-bit `HKLM\SOFTWARE\Microsoft\Cryptography` view. A foreign or unverifiable host is blocked before local PID lookup for termination. Creation times are canonical decimal strings and image paths are normalized; a live image mismatch is unknown, not evidence of owner death. This is a per-host/user ledger, not a remote fleet control protocol.

The runtime model `ResourceRecord.model_json_schema()` and [checked-in schema](../examples/resource-record.schema.json) define the language-neutral record, including required integer `schema_version: 1`. The current native process driver is Windows-only. Other platforms report unknown rather than enabling PID-based termination. Native .NET bindings remain future work; another runtime must implement the same ownership, locking, identity and receipt rules before writing interoperable records. The owner must be the actual surviving supervisor, not a temporary Python/CLI registration helper that exits immediately.

Release 26.1022.173624 serializes competing ledger writers with a two-second admission budget across its thread and OS file locks. It waits only before entering the critical section. If the ledger remains busy, the operation fails before reservation, record mutation or cleanup dispatch; no side effect is retried. Historical package 0.20.1 does not include this bounded wait. Long-lived broker/manager instance ownership continues to reject a second owner immediately. Applications must handle an admission refusal without launching an unrecorded worker or bypassing the lock.

| Ownership | App responsibility | Manager cleanup |
| --- | --- | --- |
| `private` | Resource was created exclusively for this supervisor; define whether forced teardown is safe. | Only with explicit `cleanup_mode="terminate"`, a departed owner and verified resource identity. |
| `attached` | Release this app's attachment to pre-existing user software. | Report only; never terminate the user application. |
| `shared` | The app supervisor maintains its own references/leases and last-owner policy. | Report only; never guess that other clients have finished. |

An absent parent PID is not proof of ownership. The manager never discovers a process by executable name and adopts it for cleanup. An app must not declare a private process that predates its supervisor. Another retained claim referencing the process or a recorded tree member blocks termination.

## Launch a contained Windows worker

Use a stable app supervisor as the owner, not a short-lived chat connection when multiple chats intentionally share the application. Instantiate its ledger against the same explicit registry used for management:

```python
from universal_tools.resources import ResourceLedger

ledger = ResourceLedger(registry_directory)
with ledger.owner("example-app") as resources:
    with resources.launch(
        [absolute_worker_executable, "--app-owned-ipc", endpoint],
        "Example background worker",
        allow_termination=True,
        max_processes=32,
    ) as worker:
        run_existing_application(worker.pid)
        worker.refresh()  # Persist current verifiable tree members for diagnosis.
```

The identifiers in this sketch belong to the embedding application. `allow_termination=True` is an explicit app declaration that forced teardown is appropriate. Preserve dirty-document and interrupted-write evidence in the app before opting in. Native applications needing a close/save/refusal sequence must perform that app-owned sequence before teardown; the library does not save or discard native documents for them.

The launcher writes a launch reservation, creates the worker suspended with a private Windows job assigned **during process creation**, durably records its identity, then resumes it. The job is not inherited as an open handle, disallows breakaway and bounds its active processes. Closing the owning handle/process terminates the contained tree, including descendants not yet sampled into JSON. Normal `close()` confirms the job has no active processes and stores a receipt. Startup failure closes the job before returning an error. There is no uncontained fallback.

The helper requires Windows 10/Server 2016 or later and the locked Windows dependencies. Its default background-worker profile inherits no standard streams. The development [owned stdio provider](app-framework.md#contained-stdio-development-profile) supplies narrowly inherited pipe handles through a separate opt-in profile. Apps integrating existing launchers must retain correct client EOF/parent cleanup and either add equivalent atomic containment or register their processes separately.

The [session/transport factory API](app-framework.md#app-owned-session-and-transport-factories) lets an app select the same reviewed provider for every engine connection. Historical package 0.20.1 provides selection only; timestamp releases include the opt-in experimental provider, with real SDK 2.3 fixture coverage. Application/native acceptance is still required before enabling an app route. Direct standalone lifecycle testing is a separate deployment scope.

Membership is a snapshot of verifiable live processes, not a complete OS process-audit history. The job owns all contained descendants even between snapshots. Processes launched indirectly by another service, COM broker, WMI or another mechanism outside the job require separate explicit resource records and app-specific cleanup. Never assume an SDK connection or Windows parent relationship contains native software.

The underlying Windows contracts are documented by Microsoft: [job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) and [process-creation job attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

## Observe a process without termination permission

Use `ledger.driver.observe(identity)` for an existing full identity recorded by the app. It opens only synchronization and limited-query rights, checks creation identity and image once, and retains that kernel handle for the observation scope:

```python
with ledger.driver.observe(record.process.model_dump()) as observed:
    recorded_identity = observed.identity  # Detached original-generation snapshot.
    exited = observed.wait(25)  # A checked kernel signal, not a fresh PID lookup.
```

Known observations do not reopen the PID or repeat image queries. Those queries can fail during shutdown before the process handle signals. A retained handle cannot switch to a reused PID's new generation while it remains open. `alive()` and bounded `wait()` raise on invalid/closed handles; an unknown query or failed wait is never an exit proof. Initial capture denial or identity mismatch refuses the observation. `terminate()` is explicitly refused on observation handles; approved manager cleanup continues to use its separate identity-checked termination path.

The recorded identity is historical evidence, not a claim that the process is currently alive. Keep the original checked handle until observations finish. Do not close/reassign it and then substitute another handle by PID. Whole-job process/memory bounds remain independent requirements; a root exit does not prove its descendants exited. Optional repeated image/module inventory may be unavailable without rewriting the original identity or inventing a successful essential check.

## Own native applications before binding automation

Where a native application supports explicit process creation, prefer the contained launcher before binding its automation object. A COM class activation or an apparently fresh PID alone does not close the crash window between activation and ownership registration. Bind only to the exact privately launched instance, and keep unrelated user/shared applications untouched.

Tie each native lease to its requesting helper's generation. Helper loss, timeout or owner loss must close the appropriate private process tree. Keep workbook/project write intent and uncertain-effect evidence separate from the ability to clean up the owned job; terminating a private process does not establish rollback or authorize replay. Review fixed utility entry points and indirect COM/service/WMI launches explicitly. The process job covers inherited descendants; it does not imply ownership of an external service or an existing application.

## Track existing app-owned launchers or attachments

For a native API or existing launcher, write a reservation before creation and bind the returned process identity immediately:

```python
intent = resources.begin_launch("Native application", kind="native-app", cleanup_mode="report")
pid = app_owned_native_launcher()
record = resources.bind_process(intent.resource_id, pid)
```

This records the app's launch without claiming the framework atomically contained it. A crash between an external launch and identity binding leaves an unresolved reservation. The manager must not guess which process was created. Use the contained launcher where appropriate or implement equivalent app-owned containment/recovery for the native API. Explicitly choosing `cleanup_mode="terminate"` permits exact-process termination only; it does not infer an unrecorded descendant tree.

To record an attachment instead:

```python
attachment = resources.remember_process(
    existing_user_process_id, "User application attachment", ownership="attached"
)
app_owned_detach()
resources.release_attachment(attachment.resource_id)
```

`release_attachment()` releases only an attached/shared claim; it cannot forget a live private resource. The application remains responsible for releasing the real native handle. Applications exposing a deliberately shared instance must keep their own lease/refcount policy and resource fencing; the ledger is not an authenticated Codex chat identity or a native software authorization mechanism.

## Recover after a server restart or owner loss

The manager's **Owned resources** view remains available when an app's management endpoint has disappeared. Select an eligible orphan, inspect its application, process and owner, then confirm **Clean up selected orphan**. Live owners, attached/shared/report-only records, unknown identities, other retained claims, recovery barriers and already claimed cleanup intents are blocked.

Equivalent manager commands are:

```powershell
universal-tools-manager --registry TEST_REGISTRY resources
universal-tools-manager --registry TEST_REGISTRY resource-plan --resource-id RESOURCE_ID
universal-tools-manager --registry TEST_REGISTRY cleanup-resource --resource-id RESOURCE_ID --revision REVIEWED_REVISION --request-id UNIQUE_32_HEX_ID --apply
```

Use a reviewed current revision and a unique request ID. The manager rechecks policy, owner death and exact process identity under the ledger lock; it holds the checked OS process handle through termination. PID reuse retires the old claim without touching the replacement process. A lost reply is inspected through the existing record/receipt, not replayed with a new request ID. The CLI and tray do not execute a stored arbitrary command or auto-relaunch software.

An app may use `ledger.plan()` and `ledger.cleanup()` for a declared startup-recovery policy, with the same gates. Recovery is not implicit in construction or status reads. A duplicate identical cleanup ID returns its existing receipt; an uncertain/dispatching receipt blocks replacement requests.

## Preserve uncertain native work

Use `resources.set_recovery_barrier(resource_id, reason)` when the app needs to prohibit cleanup pending native inspection. `resolve_recovery_barrier(resource_id, expected_revision, evidence)` is an app-only attestation after that inspection; it retains the actor, evidence and prior revision and cannot clear a cleanup receipt.

After independent inspection establishes that a private process is gone, `resources.reconcile(resource_id, expected_revision, evidence)` can retire its old claim while preserving the original uncertain receipt/barrier. For attached/shared resources it releases only the current or departed owner's claim. Another live generation cannot be adopted. These app-only APIs are not exposed through the tray, CLI or MCP surface and never clear engineering write journals, roll back a native operation or authorize replay.

Normal cleanup also needs a native-state policy: fence new work, refuse unsafe restart, close owned documents/handles, stop the owned resources, then verify closure. A hard owner crash can forcibly terminate a contained application. Backups, dirty documents, unresolved writes and user recovery remain app-owned responsibilities.

## Acceptance before app adoption

Use isolated harmless executables, an explicit private test registry and retained process identities. Verify normal close, client EOF, partial/failed startup, owner death before binding/resume, child/grandchild cleanup, duplicate-start admission, PID reuse, stale plans, foreign claims, report-only/attached/shared preservation and uncertain receipt recovery without replay. Separately validate app-owned native close/save/refusal behavior with explicitly authorized disposable targets. Framework fixtures do not establish Studio 5000, CAD, controller or Codex host acceptance.
