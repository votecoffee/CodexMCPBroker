# Installers and host settings

[Developer guide](README.md) · [Registration and discovery](registration-and-discovery.md)

Each MCP Server owns its installer, standalone plugin, native skill package and selected deployment profile. CodexMCPBroker supplies a shared framework dependency and an independent Windows management app. Installing the manager does not install servers, edit Codex settings or make native operations available. There is no universal plugin.

## Keep Windows entry points usable

Ship a root CMD entry point that resolves scripts from `%~dp0`, quotes paths, forwards arguments, disables delayed expansion and returns the installer's exit code. Explain its supported switches and whether it installs, starts a service or opens a dashboard. Noninteractive checks must not pause. The manager's `-InstallOnly -NoShortcuts -NoPause` combination installs without opening the tray or creating shortcuts.

Test the actual CMD entry point from another working directory and a checkout path containing spaces. Exercise success and failure propagation, quoted destination paths, unsupported options and an already-installed runtime. Use stubs for argument-dispatch checks and a disposable installation for package/runtime checks. A stub does not prove that dependencies can install. Keep tests away from normal settings, user projects, running services and native software unless separately authorized.

Pin each app's reviewed framework and SDK dependencies. Updating this repository or installing the manager does not upgrade app environments. Keep SDK/protocol identities separate from the app's fixed New York release timestamp; see [versioning](../versioning.md).

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

Regression fixtures should cover CRLF and LF originals, UTF-8 with and without BOM, non-ASCII paths, comments, literal/basic multiline values, a missing final newline, idempotence and unrelated plugin/server entries. Reproduce Windows text translation explicitly even when tests run on another platform. Assert no lone CR or doubled CR, valid on-disk TOML, expected settings and preservation of unrelated content. Include a concurrent-edit refusal case.

## Publish connections before retiring caches

Treat package installation, plugin enablement, server-route enablement and runtime readiness as separate stages. Preserve the previous runtime, cache and settings selection until the replacement connection has been resolved and verified. A plugin update can prune an old cache while a separately configured MCP route still points to it; a restart cannot repair that missing launcher.

An installer must inventory its own existing route pins before updating the plugin, back up their immutable payloads and retain referenced versions through interrupted or refused publication. It must not delete a cache solely because a newer plugin version was installed. Retire old payloads only after no configuration or active client needs them, according to app-owned lifecycle policy.

Verify that command, argument array, working directory, environment and referenced files resolve to the reviewed package. If a particular host leaves a packaged path placeholder unresolved, report it. A separately authorized host-local absolute binding may select the same immutable cached launcher, while the unresolved duplicate route is disabled and the app-owned plugin/skills remain enabled. Do not edit immutable cache files or silently substitute a different runtime/profile.

After enablement, exercise the selected cached route with synthetic input: real protocol discovery, current guidance, a harmless operation, honest management status where implemented, normal EOF and exact ownership cleanup. CLI inventory alone does not establish discovery in an already-open desktop chat. Missing management support means **Not reporting** and unavailable restart; it is not permission to invent health or launch an unrelated service.

If the user prohibits settings changes, leave the live file untouched. Complete source fixes, disposable tests and package preparation; report any connection publication still pending. Never bypass that restriction through a CLI that writes the same file.
