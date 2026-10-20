# Beta completion plan

The primary finish line is a reusable app framework that standardizes backend code and reduces Codex reloads. The universal broker remains an optional aggregation host. A reviewable beta is separate from production maturity and permission to migrate an existing integration. Keep existing direct integrations available during assessment.

## Shared app framework first

- [x] A versioned Python package and app-owned `ApplicationHost` using the existing policy/session/catalog/receipt engine.
- [x] Stable MCP front-door schemas with live backend publication, context-aware deferral, candidate rejection and configuration rollback.
- [x] Dedicated app state, native MCP session affinity and explicit local app-admin reconciliation/healthy shared-adapter retirement.
- [x] Synthetic protocol checks retaining native MCP sessions while a new backend tool becomes searchable.
- [x] Opt-in modern protocol ownership and approval continuations, catalog notifications, scoped discovery caching, SSE budgets, framework tracing and skill resource snapshots; real host activation remains separate.
- [x] Durable, owner-bound MCP Tasks adapters; lazy verified downstream Skills; explicit namespaced federation, live publication, pagination/directory reads, host profiles, extension lifespans and structural budgets. These are optional app integrations with synthetic HTTP/restart/SDK tests.
- [x] Authorized Task notifications, form/URL inputs, native protocol federation, SDK Apps examples/tests, optional HTTPS/OAuth/service and workload grants, DPoP clients, shared subscription buses and durable shared Tasks. Infrastructure, identity providers and native applications still require deployment acceptance.
- [ ] Publish verified package versions through an app-consumable feed; pin dependencies in each adopting app.
- [ ] Implement/test native .NET bindings if required; share versioned contracts without duplicating domain services.
- [ ] Complete real Codex standalone-plugin selection, resource/dependency behavior, reconnect/reload and native app acceptance.
- [ ] Assess optional universal aggregation of framework-based hosts, including nested discovery/policy and route-aware guidance.

The [framework guide](docs/app-framework.md) records current APIs, packaging, lifecycle boundaries and prototype limits. Backend refresh avoids native plugin changes only while the front-door schema and packaged skill entrypoint remain stable. Arbitrary in-process code/DLL replacement is outside the framework contract.

## Broker foundation

- [x] One reusable plugin, fixed discovery/invocation tools and independent app registrations.
- [x] Multiple frontends, conservative read/write classification, exact write scopes and request-ID journals.
- [x] Retained exclusive and opt-in shared sessions, ownership, durable recovery barriers and no automatic replay.
- [x] App initialization guidance, scoped inner catalog hints, request-bound forms and progress.
- [x] Bounded larger arguments and an independently configurable HTTP body limit, with existing defaults preserved.
- [x] Source packaging, manifests, reusable assessment instructions and local-only app evidence.
- [x] Explicit native skill bundle selection, snapshot composition, marketplace packaging and rollback; host skill activation and real app adoption remain acceptance work.

These features need ongoing regression checks. Passing their synthetic tests does not establish native application behavior.

## Remaining beta acceptance

1. **Refresh the selected adapter evidence against stable current revisions.** Review source drift, preserve direct/broker comparisons, and finish workflow-specific coverage such as app-owned jobs and production extension workers where those are required. Record untested routes explicitly; the objective is a defined workflow, not every hypothetical MCP feature.
2. **Complete Universal Tools-only host acceptance in fresh chats with disposable data.** Keep the app plugins absent in that test profile. Check actual discovery and selection, contributed app instructions/references, media display, human approval forms, progress and failure messages against standalone behavior. The [workflow contribution reference profile](docs/workflow-contributions.md) uses existing catalogs/read tools; real app adoption and host activation remain open. Preserve each app's independently available standalone package and verify explicit route selection without automatic cross-route retries. Conditional app-installer selection remains future work. Protocol clients cannot establish that the host showed the right UI.
3. **Record each pilot decision and its limits.** Use the [assessment worksheet](docs/adapter-assessment.md), state supported session modes and target/resource contracts, and measure acceptable workflow latency. Keep completed app-specific decisions and installation evidence private. Publish a verified source checkpoint and generic limitations.

Exclusive mode deliberately gives one frontend ownership of one registration at a time. Shared mode serves concurrent frontends only when the app's state and authorization are intentionally shared; calls to one registration remain serialized. Parallel independent private contexts are a separate design problem requiring app-defined resource fencing. A workflow that requires them remains a migration blocker; sharing private state is not a substitute.

Large-message ceilings are opt-in limits, not full app/host parity. Check encoded argument bytes, HTTP escaping/envelope overhead, native limits and memory costs together. Prefer existing app-owned file routes when appropriate without splitting an atomic reviewed operation. Retained adapters reduce lifecycle cost; routing and live-definition checks still add overhead.

## Migration acceptance

After beta acceptance, a proposed migration still needs a separately authorized native pilot: explicit disposable targets, fresh app preflight, exact review/approval, native readback and failure reconciliation. Keep a usable direct integration and record the operator's migration decision. Engineering compilation, runtime, commissioning and safety acceptance remain app-specific.

Sampling, roots, automatic downstream task relay and arbitrary notifications remain outside the generic broker contract. Explicit native resource/prompt/completion federation, Tasks push notifications and Skills have the contracts in the [extension guide](docs/extension-development.md). Form and URL input relay is supported. Remote and replicated profiles have the boundaries in the [deployment guide](docs/deployment.md). Native host and app acceptance remain the beta finish line.
