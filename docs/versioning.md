# Timestamp release versions

Application, manager and shared-package releases use **`YY.MMDD.HHmmss`**, for example `26.1022.173624`. The release time zone is **America/New_York** (Windows `Eastern Standard Time`, including daylight-saving rules). Choose one aware instant per release and keep it fixed through source verification, packaging, installation and status reporting. Startup never generates a new version. Preserve old release identities and receipts.

Stamp source metadata from PowerShell:

```powershell
$releaseZone = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$releaseInstant = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $releaseZone)
python scripts/stamp_version.py --at $releaseInstant.ToString('o')
python scripts/stamp_version.py --check
```

The stamp command updates `pyproject.toml` and the fixed `universal_tools.__version__` constant together. It requires an explicit offset, rejects backwards timestamp releases, and reports package and Windows file identities. Update the current README checkpoint, regenerate the source manifest, and verify the actual wheel, source archive, installer pointer and manager title. After a daylight-saving clock rollback, wait for a later unique local release time rather than reusing an identity.

Python PEP 440 normalizes numeric leading zeroes: display version `26.0101.000102` has canonical package identity `26.101.102`. Preserve the padded display label and compare installed/running/source versions numerically. Strict SemVer package/plugin validators also require canonical numeric components. Padding differences alone must not cause an update, restart or source-drift warning. The manager retains reported component strings for inspection.

.NET assembly and Windows file versions have four bounded numeric components; `HHmmss` can exceed their limit. Use the requested timestamp for Product/Informational/display metadata and **`YY.MMDD.HHmm.ss`** for assembly/file metadata: `26.1022.1736.24`. `universal_tools.versioning.file_version()` supplies this mapping; `package_version()` supplies the canonical package identity. Validate actual package and host loaders as well as formatting.

Timestamp versions identify builds, not maturity or compatibility. SDK/protocol versions and each app's reviewed framework dependency pins stay separate. Changing version format does not authorize a migration, restart, plugin enablement, native launch or controller operation.
