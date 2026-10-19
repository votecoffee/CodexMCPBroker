# Verification

This guide describes repeatable checks for the public source. Keep machine-specific results, installed-plugin records, app compatibility captures and deployment history under ignored `.local/private-docs/` or outside the checkout.

## Development environment

From the repository root on Windows:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

The core suite checks registration, schema restrictions, conservative classification, revocation, catalog revisions, serialization/concurrency, durable write claims and uncertain outcomes. Integration tests use real MCP SDK clients and isolated example HTTP/stdio processes to exercise authentication, routing, changing discovery, multiple frontends and result/error forwarding. Packaging tests check that runtime state and Git metadata remain outside source inventories.

Invocation-guidance checks cover explicit true/false and missing/null annotations, legacy overrides, master write policy and exact case-sensitive allowlists. Descriptions must preserve the app's definition and report the channel/request-ID requirement without implying that registration policy grants app authorization or ownership. Actual-adapter fixtures should include any read/export operation the app deliberately marks write-capable.

Bulk-request checks use isolated synthetic receivers over both stdio and HTTP. Exercise 16,777,216-unit UTF-16 XML strings (about 48 MiB UTF-8), ASCII-escaped HTTP bodies larger than canonical arguments, exact service and HTTP boundaries, declared and chunked oversize rejection, ownership, completed receipts and duplicate suppression. Verify both opt-ins independently, unchanged defaults for older settings, credential/port/registration preservation, and refusal to configure under either the startup or broker lock. No native app or normal registration participates. A transport hash match does not establish native acceptance or host capacity.

Session tests use synthetic process-local state and AnyIO task groups to verify same-task SDK ownership, workflow continuity across refresh, competing frontends, idle expiry, alias conflicts, cancellation, bounded initialization and context loss. Protocol tests run a disposable stdio app through HTTP and multiple stdio broker frontends, including prepare/apply continuity, process exit and in-flight timeout. They leave installed plugins and normal app registrations untouched.

For an existing app, follow the [adapter session assessment](docs/adapter-assessment.md). Freeze its actual factory/handlers and inject fake providers below them; keep source captures and completed worksheets private. Compare complete deterministic results with a direct reference, verify dynamic token bindings through the workflow, and count fake writes independently. Record excluded workers/dependencies and test unsuitable lifecycle modes when needed to explain a mode choice. These adapter checks supplement the generic suite; they do not establish host display, native behavior or migration approval.

Durable ownership checks cover atomic resource reservations, hashed frontend identities, interrupted initialization/retirement, blocked discovery after restart, indefinite lost-context barriers, changed/renamed/transient routes, and exact-handle recovery under the stopped-broker OS lock. The process-crash test terminates only its own isolated broker and reconciles disposable fixture state through the administrative recovery path. These checks do not prove a real native application's work stopped or resumed.

Renewal checks verify that frequent expiry-snapshot checks avoid database changes, refresh at the bounded interval, and reject missing/lost/retiring contexts on every attempt. They cover short leases, changed durations, clock rollback, exact live deadline renewal and crashes after coalesced snapshots. Initial activation and state transitions remain durable, database handles close normally, and journal failure after dispatch retains uncertain write handling. Performance samples belong in local reports with input hashes, sample sizes and direct-versus-broker boundaries; no machine-specific latency threshold is part of the regression suite.

Initialization metadata checks cover optional and Unicode instructions, capability preservation, bounded snapshots, registration revocation, recovery barriers, refresh races and metadata isolation between owners. Real HTTP and two stdio frontends verify transport capture and delivery, stable owner guidance through another frontend's refresh, unchanged native instance identity, and the separation of app capability advertisements from broker relay support. Description must not connect to an app or extend its lease.

Frontend pool tests verify bounded concurrent channels, sequential reuse, cancellation before/after dispatch, initialization and operation deadlines, cleanup under SDK cancellation, queue wakeup on shutdown, and isolation between pools. Failed writes must be redacted and returned once without replay or identity changes. Protocol checks count broker handshakes across repeated requests, exercise ownership over four channels and across fresh HTTP connections, and confirm that pool shutdown preserves the app's owned native session. The broker crash/recovery checks also run with reused frontend connections.

Elicitation checks use synthetic forms and clients to verify accept, negative form values, decline, cancel, unsupported clients, invalid replies, URL/task rejection, bounded schemas/answers, fresh responses, inactive routes, cancellation and cleanup. Protocol checks traverse two stdio frontends, the HTTP broker and a disposable app, plus transient/exclusive HTTP apps. They verify origin-only delivery, no answer swapping between concurrent apps, zero fixture commits on rejected paths, and no late acceptance after an app timeout. HTTP session IDs must reject a different or omitted frontend identity, and restarted sessions must fail without replay. The native host's real confirmation UI remains a separate acceptance check.

Queued callback tests hold SDK dispatch until another call owns the route. Forms received while idle or during the preceding call must be rejected without opening the replacement client's form. A fresh form during the replacement call must still work. These checks preserve the route at receipt; they do not authenticate app causality for an unsolicited legacy form that first arrives during a later operation.

These tests do not operate native engineering applications or establish their migration readiness. Test counts and measurements belong in local run records rather than a permanent public validation log.

Discovery-failure checks distinguish connection/initialization, tool listing, catalog reads and normal cleanup, including wrapped SDK/HTTP timeouts. They verify redacted diagnostics, stale-route eviction, healthy-app isolation, owned-worker recovery barriers, cancellation propagation and absence of automatic retries. A deliberately stalled HTTP initializer must actually receive `initialize` before the deadline expires; its reported failure must reach another frontend without another connection or tool dispatch.

Each fixture process has an independent bounded readiness window, including the broker after the example apps and after a fixture restart. Setup must fail promptly if its child exits, retain isolated process logs and include the recorded service status in failed discovery assertions. Keep app operation deadlines unchanged. Record host resource conditions alongside clean full-suite and cross-SDK runs; a passing rerun alone does not explain an earlier startup failure, and setup-budget fixes do not establish a production root cause.

Exclusive background job checks use a real stdio adapter and an independent, in-memory HTTP backend. They verify handle polling, revision rejection, conservative start/cancel routes, write receipts, foreign-context denial, cancellation pending an app outcome, and job state retained after clean adapter release. A lost start reply leaves one backend job, an uncertain receipt and a recovery barrier; only the stopped fixture broker is acknowledged after inspecting its fake state, and a fresh context reconciles the existing handle without another start. These checks do not establish durable native jobs, per-job authorization, actual cancellation or rollback. HTTP sessions opened before the fixture broker restart remain invalid.

Separate transient-mode checks use two frontends and new adapter processes while the same fake backend retains job state. They verify shared visibility according to the fake app policy, cancellation requests from another frontend, and an uncertain start receipt without an exclusive context reservation. An existing job can be read after the lost reply, and the original write ID cannot dispatch a second start. These are distinct from the exclusive recovery-barrier cases and do not authorize a new native start or infer per-job privacy.

Shared-mode checks verify lazy initialization, a broker-owned worker reused across frontend identities and ownerless clients, serialization, immediate policy revocation, duplicate claims, normal app errors, idle retirement, initialization failure, interrupted calls, journal/teardown failure and restart barriers. Internal recovery handles and worker initialization stay outside frontend metadata. Alias conflicts include transient and retained routes. Shared capability probes reuse the worker and publish only the opted-in public index; invalid or failed probes evict stale hints. Real stdio frontends reuse the same adapter process while polling/cancelling fake service jobs, and a lost start reply blocks both chats with one fake job and an uncertain receipt. Origin-only forms and equal progress tokens are checked while multiple frontends use one retained worker; HTTP app paths also exercise shared mode. These fixtures do not establish that an actual app adapter is safe to share or supplies per-job authorization.

Progress checks cover exact original tokens (including zero), finite increasing values, whole-message limits, per-call event budgets, burst coalescing, slow/disconnected receivers and cleanup under SDK cancellation. Real stdio and HTTP fixtures exercise equal tokens in separate frontends, concurrent calls on pooled channels, ignored previous/unknown native tokens, no opt-in or metadata routes, forms, duplicate write claims, app errors and unchanged app deadlines with uncertain writes and lost-context barriers. Progress delivery does not establish host display, native completion or cancellation. The fixtures never operate a native app.

Policy and payload checks verify exact write allowlists, the service-wide master flag, conservative classification, immediate revocation and no claims on rejected writes. Synthetic stdio/HTTP apps exercise the complete frontend/broker/app path with one MiB of raw binary data carried as base64, large Unicode results and exact 64-bit JSON integers. Boundary checks retain the earlier 8 MiB argument-budget and 10 MiB HTTP-default cases alongside the new bulk profile, checking one-byte overflow and unchanged defaults. No batch splitting or file/device staging occurs. These checks use the pinned broker SDK; other app SDK versions and the real host still need their own assessment.

Use [MIGRATION.md](MIGRATION.md) to select a non-migrating compatibility review scope. Keep app-specific captures and decisions local; successful broker fixtures do not certify a native integration.

For an app-specific production-worker assessment, follow the [worker checklist](docs/adapter-assessment.md#exercise-app-owned-feature-workers). Keep the original MCP gateway, worker manager and pure worker executable unchanged while injecting fake native providers below them. Compare direct and broker results, record actual worker identities and hashes, and check refresh continuity, selected reload, rejected replacement, worker exit and release cleanup. Preserve failure logs and distinguish normal app errors from a lost adapter or ambiguous write. Verify path constraints before interpreting a startup failure, and keep any shorter fixture runtime outside normal installations. These checks are a private adapter profile, not part of the synthetic default suite or evidence of native acceptance.

Inner catalog checks use opt-in synthetic providers with versioned, bounded metadata. They cover explicit read-only declarations, schema/reference checks, malformed/duplicate catalogs, detached snapshots, stale revisions, registration revocation, changed discovery definitions and private owner visibility through expiry and transport loss. Real stdio frontends and stdio/HTTP apps verify install/refresh without frontend definition changes, inner feature additions without changing outer tool revisions, native rejection of stale app revisions, global refresh without owner probing, invalid metadata without native context loss, and zero host prompts or fixture commits from metadata confirmation attempts. They do not establish that an existing app's native catalog implements the new normalized format.

Provider deadline cases cover both stdio and a prestarted HTTP fixture. They record entry into the delayed provider before checking that its timeout evicts owned search hints and retains the recovery barrier. The stdio case allows cold process initialization; the HTTP case keeps a short operation budget after server startup. Thus an initialization failure cannot substitute for the intended in-flight provider timeout.

## Cross-SDK profile

The optional `interop/` profile uses the broker's locked MCP 1.30 client and a separate MCP 2.3 server environment. It is outside the default `tests/` collection so ordinary development does not install a second SDK or silently skip matrix checks. Invoke it explicitly from the repository root:

```powershell
py -3.13 -m venv .local/interop-sdk2
.\.local\interop-sdk2\Scripts\python.exe -m pip install -r interop/requirements-mcp2-lock.txt
.\.venv\Scripts\python.exe -m pytest -q interop --sdk2-python .local/interop-sdk2/Scripts/python.exe
```

Use the broker development interpreter for pytest; `--sdk2-python` must identify a separate interpreter with `mcp==2.3.0`. The profile rejects missing or incorrect SDK versions. Its dependency snapshot targets Windows/Python 3.13. Do not install it over the broker's runtime lock. No normal registrations, installed plugins, native apps, projects or devices participate.

The synthetic SDK 2.3 client supplies a same-SDK reference using its legacy handshake. The broker/frontends must preserve initialization, tool definitions and complete received result fields, including text, structured content, image bytes/MIME/metadata, Unicode and 64-bit integers with a one-MiB binary/base64 payload. Snapshots exclude unset SDK model fields: newer client defaults are not assumed to have arrived on the wire. Application errors, revision guards, competing owners, refresh continuity, clean release, validation before write claims, and synchronous timeout with an uncertain receipt and durable recovery barrier are also checked. Only the disposable fixture's stopped broker can acknowledge its own lost reservation during cleanup.

This profile checks the negotiated `2025-11-25` path described by the [MCP lifecycle contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle). It does not enable the newer SDK's default protocol, its discovery/cache APIs, result envelopes, extensions or other unsupported features. An app can use a newer SDK while retaining an older wire contract; its actual adapter still needs separate metadata, workflow, host and native acceptance. Capture that adapter's exact source/dependencies and compare both direct and broker paths with a fake native backend, keeping app-specific captures local.

## Source package

Refresh the source manifest after changing distributed files:

```powershell
.\.venv\Scripts\python.exe scripts/package_release.py
.\.venv\Scripts\python.exe scripts/verify_manifest.py
git diff --check
```

The packager writes a versioned source ZIP and SHA-256 sidecar under ignored `dist/`. Its exact-membership inventory excludes `.git`, `.local`, virtual environments, build outputs, caches and the generated plugin runtime pointer. Git ignore rules alone are not release filters: keep private material in `.local/`, which the packager explicitly excludes.

For release verification, extract the ZIP to a fresh directory, run its manifest verifier, and run the appropriate checks from that extracted source. Check the committed Git tree against the manifest as well, including line-ending normalization. Keep archive hashes and run logs in an ignored local report or a separately published release artifact.

## Installed plugin checks

For future conditional app installers, use the [coexistence acceptance checklist](docs/plugin-coexistence.md#acceptance-checklist). Exercise absent/disabled/incompatible broker states, an explicit standalone override, missing registration or skills, upgrade/reinstall choice preservation and broker removal with active or uncertain work. Use fake CLI/profile inputs and disposable host acceptance targets. Verify that no app plugin is removed or disabled merely because Universal Tools exists, and that no write crosses routes automatically. The current broker has no conditional app-installer implementation to exercise.

Branding checks should compare the bundled PNG bytes and exact dimensions with the supplied icon set, inspect every ICO directory entry and embedded PNG, and resolve the plugin's `composerIcon` and `logo` paths within its root. Create a shortcut only in an isolated copy, then read it through Windows Script Host to verify its target, working directory and icon without invoking it. Check PowerShell 7 and Windows PowerShell when available. Generated `.local` shortcuts contain installation paths and must stay outside Git and source packages. Shortcut creation alone must not install a plugin or start a broker.

The root `InstallAndRun.cmd` delegates to `scripts/InstallAndRun.ps1`, which sequences setup, plugin installation, broker start and status. Check its PowerShell syntax without running the normal installation. For launcher smoke checks, use a temporary copy with stub scripts and fake CLI commands: verify a path with spaces, invocation from another current directory, option forwarding and existing-port preservation, failure propagation, and rejection of a running broker before setup. Check both PowerShell 7 and Windows PowerShell when available. These checks must not install plugins or start an ordinary registered app stack. Use `-NoPause` in scripted checks.

`scripts/smoke_installed.py` is an optional example-stack integration check. Run its `--help` output for required arguments. It opens two installed-plugin frontends, routes example reads and temporarily registers an example service before removing it. Use a disposable example state or an explicitly selected development stack. Do not run it against an unrelated user's configuration.

Model-driven selection in a fresh Codex chat is a separate manual acceptance check. App integration also needs app-owned tests for context lifetime, revisions, permissions, human confirmations, large arguments/results and failure recovery. Preserve app-specific evidence locally and report the boundary of any native validation explicitly.
