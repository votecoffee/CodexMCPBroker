# Beta completion plan

The finish line is a shared app framework and independent Windows manager that standardize backend code and reduce Codex reloads. Apps keep their own plugins. The universal plugin, marketplace, installer and native skill composer are retired in 0.19.0. Retained routing components support app-host internals and isolated protocol fixtures. A reviewable beta is separate from production maturity and app-owned adoption.

## Resource lifetime priority

- [x] Durable per-app/per-owner process/software ownership ledger, independent of live management endpoints.
- [x] Exact process creation identity and bounded Windows job-contained startup; attached/shared resources remain report-only.
- [x] Reviewed manager orphan cleanup, app startup recovery APIs and retained intent/receipt evidence, including uncertain outcomes.
- [x] Host-local session/stdio transport factory selection across discovery, probes, transient calls and retained workers, without default fallback on provider failure.
- [ ] Common atomic contained SDK stdio provider with restricted inherited pipes, byte/message parity and bounded EOF/cancellation teardown.
- [ ] App-owned acceptance of EOF, clean close, startup failure, crash, native document handling and repeated reconnects without process growth.

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
- [ ] Validate multi-chat use of each app's stable MCP host with bounded supervisor-owned native instances.

The [framework guide](docs/app-framework.md) records current APIs, packaging, lifecycle boundaries and prototype limits. Backend refresh avoids native plugin changes only while the front-door schema and packaged skill entrypoint remain stable. Arbitrary in-process code/DLL replacement is outside the framework contract.

## Desktop management

- [x] Windows tray/dashboard, installed plugin inventory, version and pending-update views, and a separate root manager installer.
- [x] Opt-in app coordination with authenticated loopback status, per-instance identity, revision-bound restart requests, durable duplicate/uncertain barriers and app-only reconciliation.
- [ ] App-owned adoption and native restart acceptance for each selected server; unregistered plugins remain unmonitored.

See [service management](docs/service-management.md) for the package interface and operational boundaries.

## Broker foundation

- [x] Reusable fixed discovery/invocation engine and independently versioned app backends. The former universal plugin is retired.
- [x] Multiple frontends, conservative read/write classification, exact write scopes and request-ID journals.
- [x] Retained exclusive and opt-in shared sessions, ownership, durable recovery barriers and no automatic replay.
- [x] App initialization guidance, scoped inner catalog hints, request-bound forms and progress.
- [x] Bounded larger arguments and an independently configurable HTTP body limit, with existing defaults preserved.
- [x] Source packaging, manifests, reusable assessment instructions and local-only app evidence.
- [x] Retire the universal plugin, marketplace and skill composer; keep native skills with each app's plugin and dynamic Skills over MCP with its host.

These features need ongoing regression checks. Passing their synthetic tests does not establish native application behavior.

## Remaining beta acceptance

1. **Refresh the selected adapter evidence against stable current revisions.** Review source drift, preserve direct/broker comparisons, and finish workflow-specific coverage such as app-owned jobs and production extension workers where those are required. Record untested routes explicitly; the objective is a defined workflow, not every hypothetical MCP feature.
2. **Complete each app's own plugin/connection acceptance in fresh chats with disposable data.** Check native skills, dynamic MCP guidance, tool selection, media, human forms, progress and failures. No universal plugin or conditional app-plugin suppression is part of this profile. Validate multi-chat sharing without native instance growth and preserve uncertain-write recovery. Protocol clients cannot establish the host's visible behavior.
3. **Record each pilot decision and its limits.** Use the [assessment worksheet](docs/adapter-assessment.md), state supported session modes and target/resource contracts, and measure acceptable workflow latency. Keep completed app-specific decisions and installation evidence private. Publish a verified source checkpoint and generic limitations.

Exclusive mode deliberately gives one frontend ownership of one registration at a time. Shared mode serves concurrent frontends only when the app's state and authorization are intentionally shared; calls to one registration remain serialized. Parallel independent private contexts are a separate design problem requiring app-defined resource fencing. A workflow that requires them remains a migration blocker; sharing private state is not a substitute.

Large-message ceilings are opt-in limits, not full app/host parity. Check encoded argument bytes, HTTP escaping/envelope overhead, native limits and memory costs together. Prefer existing app-owned file routes when appropriate without splitting an atomic reviewed operation. Retained adapters reduce lifecycle cost; routing and live-definition checks still add overhead.

## Migration acceptance

After beta acceptance, a proposed migration still needs a separately authorized native pilot: explicit disposable targets, fresh app preflight, exact review/approval, native readback and failure reconciliation. Keep a usable direct integration and record the operator's migration decision. Engineering compilation, runtime, commissioning and safety acceptance remain app-specific.

Sampling, roots, automatic downstream task relay and arbitrary notifications remain outside the generic broker contract. Explicit native resource/prompt/completion federation, Tasks push notifications and Skills have the contracts in the [extension guide](docs/extension-development.md). Form and URL input relay is supported. Remote and replicated profiles have the boundaries in the [deployment guide](docs/deployment.md). Native host and app acceptance remain the beta finish line.
