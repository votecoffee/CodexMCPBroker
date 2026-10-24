# Installers and host settings

[Developer guide](README.md) · [Registration and discovery](registration-and-discovery.md)

Each MCP Server owns its installer, standalone plugin, native skill package and selected deployment profile. CodexMCPBroker supplies a shared framework dependency and an independent Windows management app. Installing the manager does not install servers, edit Codex settings or make native operations available. There is no universal plugin.

## Keep Windows entry points usable

Ship a root CMD entry point that resolves scripts from `%~dp0`, quotes paths, forwards arguments, disables delayed expansion and returns the installer's exit code. Explain its supported switches and whether it installs, starts a service or opens a dashboard. Noninteractive checks must not pause. The manager's `-InstallOnly -NoShortcuts -NoPause` combination installs without opening the tray or creating shortcuts.

Test the actual CMD entry point from another working directory and a checkout path containing spaces. Exercise success and failure propagation, quoted destination paths, unsupported options and an already-installed runtime. Use stubs for argument-dispatch checks and a disposable installation for package/runtime checks. A stub does not prove that dependencies can install. Keep tests away from normal settings, user projects, running services and native software unless separately authorized.

Pin each app's reviewed framework and SDK dependencies. Updating this repository or installing the manager does not upgrade app environments. Keep SDK/protocol identities separate from the app's fixed New York release timestamp; see [versioning](../versioning.md).

## Required stable launcher contract

Every published MCP connection must use a **stable app-owned launcher and working directory**. They must remain usable when Codex replaces or removes its versioned plugin cache. Do not publish a command, script argument, interpreter or working directory that depends on a disposable cache version, temporary staging directory, build output or development checkout. A stable tool schema and a stable launch path are separate requirements. This contract applies to direct standalone SDK servers as well as apps using the optional framework host.

An illustrative Windows layout is:

```text
App-owned installation root/
  Start-Mcp.ps1              stable entry point
  launcher/current.json      atomic selection of a reviewed launch snapshot
  launcher/snapshots/<hash>/  immutable launcher assets and inventory
  installation.json          reviewed backend path, profile and dependency identities
  runtimes/<release>/         immutable backend and dependency closure
```

An app may combine selection records or use an equivalent native launcher. The required properties are a durable entry path, explicit reviewed selection, verified immutable bytes and app-owned lifecycle policy. Use a dedicated installed application root for the stable entry. A backend may live in an explicitly managed persistent workspace installation when its versioned artifacts and dependencies are retained; launch must not depend on the workspace's current source branch or regenerated build outputs. Keep personal installation paths and actual selection records out of public Git. Ordinary launch must not install dependencies, fetch an update, choose the newest directory or fall back to another profile after a failure.

The stable launcher verifies the selected snapshot and backend identities before execution, preserves the selected protocol and hardened environment, and forwards stdin, stdout, stderr, EOF and exit status correctly. Keep logs off protocol stdout. Missing, malformed, linked/reparse or tampered selections must refuse startup without executing an alternative. Resolve paths independently of the caller's working directory and test spaces and non-ASCII paths. Preserve app-required windowless launchers and any original-wire handling required by the selected SDK.

The actual surviving supervisor must own its process tree and durable claims. A short-lived wrapper or registration helper cannot impersonate that owner. Stable launch paths do not replace process containment, startup admission, exact process identities, parent/EOF/crash cleanup or uncertain native-work barriers. The common experimental contained stdio provider is not mandatory; equivalent app-owned implementations need their own acceptance.

The selected profile must also be compatible with the launcher's runtime and containment contract. An in-process offline loader cannot simply select an incompatible native runtime, and a one-process offline job cannot admit a native profile's workers. Generate installer/pilot settings through the production strict parser and verify the actual cold saved route under its hardened environment. Follow [connection readiness and recovery](connection-readiness.md) for concurrent chat admission, failed-start reservations, lock contention and original-wire checks.

Publish updates in this order:

1. Prepare and verify a new immutable snapshot/runtime and its exact dependency pins while the old selection remains available.
2. Verify the selected profile, signatures/hashes or equivalent app integrity contract, bounded admission and loss behavior using harmless fixtures.
3. Atomically publish the reviewed selection with concurrent-edit detection and exact prior-state backup. Do not change a live or uncertain session's backend; apply the app's quiescence and replacement policy.
4. Verify a fresh connection through the **same stable entry path**. Existing clients retain their original owned generation until their app-defined close/recovery boundary.
5. Retire old assets only when no configured route or active owner needs them. Retain receipts, uncertainty barriers and required rollback evidence. Rollback selects reviewed bytes; it never replays domain work.

Keep each app's standalone Codex plugin and native skills. Use one effective enabled MCP route for the selected profile. Resolve the app-owned launcher using a host-supported mechanism; if a packaged placeholder remains literal, an authorized installer can generate a host-local absolute binding while disabling the unresolved duplicate route. Do not edit immutable Codex cache files to work around resolution. Record launcher/snapshot, installed backend and running backend versions separately where they differ; the manager must report actual observations rather than infer runtime health from installation.

Before adopting or updating this contract, require actual retained evidence for:

- A disposable plugin-cache removal after publication, followed by a successful connection through the unchanged launcher and cwd. Remove only a positively identified test cache.
- An immutable runtime update with an active peer preserved, followed by a new connection selecting the reviewed new generation without changing the host route.
- Tampered/missing selection or payload refusal before execution; interrupted/concurrent publication with the prior valid selection preserved.
- Real protocol discovery, current guidance, a harmless operation, honest management where implemented and normal EOF with exact cleanup receipts. Also test failed startup, parent/owner loss, admission bounds and shared/attached preservation for the enabled launch paths.
- Actual host-chat tool discovery after any required reload. A direct SDK or CLI check alone does not establish availability inside an already-open chat.

Preserve each app's reviewed framework/SDK pins and selected capabilities during launcher adoption. Reuse retained native/domain acceptance only when the relevant immutable bytes and policies remain unchanged; independently validate the new launch path, ownership and host discovery. The contract is the framework's implementation rule; existing app compliance is a separately recorded deployment result. Do not claim that documenting the rule upgraded installed servers or that a host reboot is implementation evidence.

## Write TOML bytes deliberately

TOML accepts LF and CRLF line endings. A lone CR or doubled `CR CR LF` is malformed. On Windows, Python text writes with the default `newline=None` translate every LF to CRLF. Passing an already-CRLF string to such a writer produces `CR CR LF`. Validating the in-memory string before writing misses that corruption.

Read existing settings as bytes and decode explicitly. Preserve their BOM, comments, unrelated tables, Unicode values and selected newline convention. New Windows files can use CRLF. Construct owned additions with that convention. Do not change all line endings as part of an unrelated settings update, and do not repair a user's file without authorization.

Write the final encoded bytes with `write_bytes()` or a binary stream. An explicit text stream with `newline=""` also disables translation. Do not use a default text writer for an already-rendered TOML document. PowerShell writers should likewise use an explicit encoding and newline policy, rather than joining lines that still contain CR characters.

Before publication, validate the **actual temporary-file bytes**, not just the rendered string:

```python
raw = temporary.read_bytes()
if b"\r" in raw.replace(b"\r\n", b""):
    raise ValueError("Malformed TOML line endings")
parsed = tomllib.loads(raw.decode("utf-8-sig"))
if parsed != expected_settings:
    raise ValueError("Settings differ from the reviewed update")
```

Use a TOML-aware edit or a narrowly validated scalar span. A regular expression that scans table-looking lines can mistake multiline string contents for tables. Back up the exact original bytes; detect concurrent edits before publishing; serialize cooperating writers; publish atomically; then validate readback bytes and parsed settings. A no-op should leave the original bytes untouched. Abort on invalid input or an intervening edit, preserving evidence rather than restoring a stale global file over another app's changes.

All installers writing the same host configuration must use the same publication lock, not separate per-app locks. On Windows, a cooperating profile can use `Local\CodexMcpSettingsPublication-v1` with a bounded ten-second acquisition. Hold it across the fresh read, exact backup, owned edits, temporary-byte validation, concurrent-edit check, atomic replace and readback; release it in `finally`. Abandoned ownership requires fresh parsing and validation, not trust in the previous writer's in-memory proposal. A lock coordinates participating writers; keep the concurrent-edit check for other editors.

Regression fixtures should cover CRLF and LF originals, UTF-8 with and without BOM, non-ASCII paths, comments, literal/basic multiline values, a missing final newline, idempotence and unrelated plugin/server entries. Reproduce Windows text translation explicitly even when tests run on another platform. Assert no lone CR or doubled CR, valid on-disk TOML, expected settings and preservation of unrelated content. Include a concurrent-edit refusal case.

## Publish connections before retiring caches

Treat package installation, plugin enablement, server-route enablement and runtime readiness as separate stages. Preserve the previous runtime, cache and settings selection until the replacement connection has been resolved and verified. A plugin update can prune an old cache while a separately configured MCP route still points to it; a restart cannot repair that missing launcher.

An installer must inventory its own existing route pins before updating the plugin, back up their immutable payloads and retain referenced versions through interrupted or refused publication. It must not delete a cache solely because a newer plugin version was installed. Retire old payloads only after no configuration or active client needs them, according to app-owned lifecycle policy.

Verify that command, argument array, working directory, environment and referenced files resolve to the reviewed **app-owned stable launcher**. If a particular host leaves a packaged path placeholder unresolved, report it. A separately authorized host-local absolute binding may select that stable entry, while the unresolved duplicate route is disabled and the app-owned plugin/skills remain enabled. Legacy cache-pinned bindings require a guarded migration; cache restoration alone is a temporary compatibility repair. Do not edit immutable cache files or silently substitute a different runtime/profile.

After enablement, exercise the actual selected stable route with synthetic input: real protocol discovery, current guidance, a harmless operation, honest management status where implemented, normal EOF and exact ownership cleanup. CLI inventory alone does not establish discovery in an already-open desktop chat. Missing management support means **Not reporting** and unavailable restart; it is not permission to invent health or launch an unrelated service.

If the user prohibits settings changes, leave the live file untouched. Complete source fixes, disposable tests and package preparation; report any connection publication still pending. Never bypass that restriction through a CLI that writes the same file.
