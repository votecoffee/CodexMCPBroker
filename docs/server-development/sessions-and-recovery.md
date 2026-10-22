# Sessions, resource ownership and recovery

[Developer guide](README.md) · [Next: registration and discovery](registration-and-discovery.md)

## Choose a mode from the app contract

| Mode | Appropriate app state | What to prove |
| --- | --- | --- |
| `transient` | Calls are independent or a separate app service retains all required state. | Reopening the gateway/connection between calls preserves the selected workflow. |
| `exclusive` | A private adapter attachment, document, credential scope or review store must survive calls. | The owner's sequence survives refresh; another frontend is refused before dispatch even with known context/plan IDs. |
| `shared` | App state and authorization are intentionally shared; requests identify their own targets. | No frontend borrows private selections, credentials, review tokens or confirmations; shared visibility is deliberate app policy. |

All modes serialize calls to one registration. An exclusive registration has one frontend owner, and shared mode is not a substitute for independent private contexts. If the workflow needs concurrent private same-app contexts, define app-owned resource fencing and assess a suitable architecture before migrating it.

Apps do not receive an authenticated Codex chat principal from the broker. Frontend affinity and broker context IDs are not native resource authorization. App-owned jobs and credentials still need their own access contract.

## Define resource identity across routes

Use a stable registration `resource_id` for broker routes that address the same native resource. Document aliases and their scope. The broker rejects known route/resource conflicts involving retained sessions; it cannot infer arbitrary equivalent endpoints or hold a lock inside the GUI or a direct client.

Native resource locks, revisions and authorization must work across supported interfaces. If direct and broker connections can reach one target concurrently, test their app-owned conflict handling. Otherwise keep that native resource to one route at a time. A new registration ID must not be used to bypass a lost context.

## Define normal retirement

An exclusive owner acquires a context and passes its ID on each app call. Descriptions/status queries do not renew its lease. Shared workers are opened and retained by the broker without an exclusive acquire/release handle. Use the [detailed lifecycle contract](../../APP_CONTRACT.md) for renewal, idle expiry and policy changes.

Clean exclusive release, idle retirement, reconfiguration or shutdown closes the downstream session. That can discard adapter-local documents, plans and Undo history or affect child workers. Define whether the app saves anything, cancels jobs or closes a native attachment; do not silently save user work on transport cleanup. An independent app service may continue running, but that behavior needs its own evidence.

Test clean reacquisition and require a fresh native selection/read where appropriate. A new gateway is not a restored old context, even when durable files or job handles remain.

## Track owned software and subprocesses

An MCP server must account for every resource it creates: native application instances, documents, worker trees, Node processes, shell wrappers, console hosts, temporary files and IPC endpoints. Record an owner/generation, acquisition method and exact resource identity. For processes, retain a handle or creation identity as well as the PID. A PID alone can be reused; an absent launcher parent alone does not establish ownership or permission to terminate a process.

| Acquisition | Shutdown responsibility |
| --- | --- |
| App launches a private instance | Close its owned documents/handles and stop its software and descendant workers when the owning instance closes. Bound cleanup and retain failures. |
| App attaches to an existing user instance | Release only its attachment and resources. Leave the user's application and documents open. |
| Several clients deliberately share a supervised instance | Track leases or references in the surviving supervisor. A departing client releases its own resources; the declared last-owner policy controls shutdown. |
| Ownership or write outcome is unknown | Fence replacement/replay, retain evidence and surface a recovery requirement. Do not guess ownership from an executable name. |

Implement cleanup for normal close, client EOF, cancelled/failed startup, partial initialization and unexpected owner death. Closing a transport, management endpoint or MCP adapter is not evidence that its native software and child processes stopped. Use OS lifetime containment for owned process trees where appropriate, with an explicit native-software cleanup path when an application launches outside that containment. Do not rely solely on an interpreter's exit callback. Keep instance limits and startup admission under the same supervisor so reconnects cannot create unbounded replacements.

Shutdown first fences new work and records outstanding operations, then closes owned resources through a bounded app-specific policy. Define how unsaved documents and interrupted writes are preserved or refused before a controlled shutdown; never silently save, discard, retry or transfer them to another route. After forced owner loss, retain the uncertain operation and recovery evidence outside ephemeral process state. A manager status callback must report cleanup failures and remaining owned resources from a cached inventory without opening software to discover them.

The tray manager observes app-owned endpoints and requests an app-owned restart. It does not adopt arbitrary processes or enforce native application ownership. Framework dependency upgrades alone cannot fix an app's missing lifecycle tracking.

## Treat loss separately from a normal app error

For a retained session, an operation timeout, transport loss or crash can leave a durable recovery barrier. A lost write reply can mean the native change happened even though the caller received no result. Preserve the original broker request ID and app evidence; do not retry with a new ID, switch routes or open another alias to continue it.

An app-owned pure worker can fail while the MCP adapter remains healthy and returns a normal app error. Define explicit worker recovery and plan invalidation in the app. That error is distinct from losing the gateway, but still does not prove the absence of partial app effects. Assess both failure classes.

Recovery acknowledgment is a local administrative action after actual app state has been inspected/reconciled and the broker is stopped. It is not an MCP tool. It does not cancel, roll back, reattach or verify a native operation. Release, refresh and reacquire cannot clear a durable loss barrier. Never delete the journal to make a workflow continue.

## Assess required workers

Retain actual worker manifests, hashes, process identities and generations in private evidence. Check refresh continuity, successful/failed replacement, invalidation of old plans, unrelated-worker stability and release cleanup according to the app's contract. A local-engine substitute cannot establish production worker IPC behavior.

For lifecycle acceptance, capture a baseline and prove that clean close, EOF, startup failure and forced owner loss leave no owned child/native instances. Separately prove that attached user instances and other clients' shared resources survive. Repeat connect/disconnect cycles and check bounded process/resource counts, PID-reuse rejection and duplicate-start races. Run these checks only with isolated fixture software and explicit runtime authorization; package/static checks do not establish cleanup behavior.

Use exact fixture process identity and path checks before deliberately terminating a worker in a test. Keep native providers replaced and never terminate by a broad executable name. Record deployment path limits and compare identical hashes if a shorter isolated runtime is needed. See the [worker assessment checklist](../adapter-assessment.md#exercise-app-owned-feature-workers).
