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

## Treat loss separately from a normal app error

For a retained session, an operation timeout, transport loss or crash can leave a durable recovery barrier. A lost write reply can mean the native change happened even though the caller received no result. Preserve the original broker request ID and app evidence; do not retry with a new ID, switch routes or open another alias to continue it.

An app-owned pure worker can fail while the MCP adapter remains healthy and returns a normal app error. Define explicit worker recovery and plan invalidation in the app. That error is distinct from losing the gateway, but still does not prove the absence of partial app effects. Assess both failure classes.

Recovery acknowledgment is a local administrative action after actual app state has been inspected/reconciled and the broker is stopped. It is not an MCP tool. It does not cancel, roll back, reattach or verify a native operation. Release, refresh and reacquire cannot clear a durable loss barrier. Never delete the journal to make a workflow continue.

## Assess required workers

Retain actual worker manifests, hashes, process identities and generations in private evidence. Check refresh continuity, successful/failed replacement, invalidation of old plans, unrelated-worker stability and release cleanup according to the app's contract. A local-engine substitute cannot establish production worker IPC behavior.

Use exact fixture process identity and path checks before deliberately terminating a worker in a test. Keep native providers replaced and never terminate by a broad executable name. Record deployment path limits and compare identical hashes if a shorter isolated runtime is needed. See the [worker assessment checklist](../adapter-assessment.md#exercise-app-owned-feature-workers).
