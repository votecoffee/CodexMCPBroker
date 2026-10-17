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

Session tests use synthetic process-local state and AnyIO task groups to verify same-task SDK ownership, workflow continuity across refresh, competing frontends, idle expiry, alias conflicts, cancellation, bounded initialization and context loss. Protocol tests run a disposable stdio app through HTTP and multiple stdio broker frontends, including prepare/apply continuity, process exit and in-flight timeout. They leave installed plugins and normal app registrations untouched.

Durable ownership checks cover atomic resource reservations, hashed frontend identities, interrupted initialization/retirement, blocked discovery after restart, indefinite lost-context barriers, changed/renamed/transient routes, and exact-handle recovery under the stopped-broker OS lock. The process-crash test terminates only its own isolated broker and reconciles disposable fixture state through the administrative recovery path. These checks do not prove a real native application's work stopped or resumed.

Initialization metadata checks cover optional and Unicode instructions, capability preservation, bounded snapshots, registration revocation, recovery barriers, refresh races and metadata isolation between owners. Real HTTP and two stdio frontends verify transport capture and delivery, stable owner guidance through another frontend's refresh, unchanged native instance identity, and the separation of app capability advertisements from broker relay support. Description must not connect to an app or extend its lease.

Frontend pool tests verify bounded concurrent channels, sequential reuse, cancellation before/after dispatch, initialization and operation deadlines, cleanup under SDK cancellation, queue wakeup on shutdown, and isolation between pools. Failed writes must be redacted and returned once without replay or identity changes. Protocol checks count broker handshakes across repeated requests, exercise ownership over four channels and across fresh HTTP connections, and confirm that pool shutdown preserves the app's owned native session. The broker crash/recovery checks also run with reused frontend connections.

Elicitation checks use synthetic forms and clients to verify accept, negative form values, decline, cancel, unsupported clients, invalid replies, URL/task rejection, bounded schemas/answers, fresh responses, inactive routes, cancellation and cleanup. Protocol checks traverse two stdio frontends, the HTTP broker and a disposable app, plus transient/exclusive HTTP apps. They verify origin-only delivery, no answer swapping between concurrent apps, zero fixture commits on rejected paths, and no late acceptance after an app timeout. HTTP session IDs must reject a different or omitted frontend identity, and restarted sessions must fail without replay. The native host's real confirmation UI remains a separate acceptance check.

These tests do not operate native engineering applications or establish their migration readiness. Test counts and measurements belong in local run records rather than a permanent public validation log.

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

`scripts/smoke_installed.py` is an optional example-stack integration check. Run its `--help` output for required arguments. It opens two installed-plugin frontends, routes example reads and temporarily registers an example service before removing it. Use a disposable example state or an explicitly selected development stack. Do not run it against an unrelated user's configuration.

Model-driven selection in a fresh Codex chat is a separate manual acceptance check. App integration also needs app-owned tests for context lifetime, revisions, permissions, human confirmations, large arguments/results and failure recovery. Preserve app-specific evidence locally and report the boundary of any native validation explicitly.
