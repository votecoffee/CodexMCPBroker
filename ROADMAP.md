# Beta completion plan

The next finish line is a reviewable beta for selected app workflows. It is separate from production maturity and from permission to migrate an existing integration. Keep existing direct integrations available during assessment.

## Broker foundation

- [x] One reusable plugin, fixed discovery/invocation tools and independent app registrations.
- [x] Multiple frontends, conservative read/write classification, exact write scopes and request-ID journals.
- [x] Retained exclusive and opt-in shared sessions, ownership, durable recovery barriers and no automatic replay.
- [x] App initialization guidance, scoped inner catalog hints, request-bound forms and progress.
- [x] Bounded larger arguments and an independently configurable HTTP body limit, with existing defaults preserved.
- [x] Source packaging, manifests, reusable assessment instructions and local-only app evidence.

These features need ongoing regression checks. Passing their synthetic tests does not establish native application behavior.

## Remaining beta acceptance

1. **Refresh the selected adapter evidence against stable current revisions.** Review source drift, preserve direct/broker comparisons, and finish workflow-specific coverage such as app-owned jobs and production extension workers where those are required. Record untested routes explicitly; the objective is a defined workflow, not every hypothetical MCP feature.
2. **Complete Codex host acceptance in fresh chats with disposable data.** Check actual discovery and selection, availability of app instructions/skills, media display, human approval forms, progress and failure messages. Protocol clients cannot establish that the host showed the right UI.
3. **Record each pilot decision and its limits.** Use the [assessment worksheet](docs/adapter-assessment.md), state supported session modes and target/resource contracts, and measure acceptable workflow latency. Keep completed app-specific decisions and installation evidence private. Publish a verified source checkpoint and generic limitations.

Exclusive mode deliberately gives one frontend ownership of one registration at a time. Shared mode serves concurrent frontends only when the app's state and authorization are intentionally shared; calls to one registration remain serialized. Parallel independent private contexts are a separate design problem requiring app-defined resource fencing. A workflow that requires them remains a migration blocker; sharing private state is not a substitute.

Large-message ceilings are opt-in limits, not full app/host parity. Check encoded argument bytes, HTTP escaping/envelope overhead, native limits and memory costs together. Prefer existing app-owned file routes when appropriate without splitting an atomic reviewed operation. Retained adapters reduce lifecycle cost; routing and live-definition checks still add overhead.

## Migration acceptance

After beta acceptance, a proposed migration still needs a separately authorized native pilot: explicit disposable targets, fresh app preflight, exact review/approval, native readback and failure reconciliation. Keep a usable direct integration and record the operator's migration decision. Engineering compilation, runtime, commissioning and safety acceptance remain app-specific.

Resources, prompts, sampling, roots, URL elicitation, task APIs and general notifications are outside the current broker contract. Add one only when a selected workflow demonstrates a need. Do not treat an ever-growing feature list as the beta finish line.
