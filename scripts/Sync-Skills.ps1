param(
    [string[]]$BundleManifest = @(),
    [string[]]$RemoveBundle = @(),
    [string]$RollbackRelease = '',
    [switch]$Install
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
if ($RollbackRelease -and ($BundleManifest.Count -gt 0 -or $RemoveBundle.Count -gt 0)) {
    throw 'Choose rollback or bundle selection changes in one invocation.'
}
$Runtime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
foreach ($Manifest in $BundleManifest) {
    $InspectionJson = & $Runtime.python -m universal_tools --state $Runtime.state skills inspect $Manifest
    if ($LASTEXITCODE -ne 0) { throw 'Native skill bundle inspection failed.' }
    $Inspection = $InspectionJson | ConvertFrom-Json
    & $Runtime.python -m universal_tools --state $Runtime.state skills add $Manifest --expected-sha256 $Inspection.manifest_sha256
    if ($LASTEXITCODE -ne 0) { throw 'Native skill bundle registration failed.' }
}
foreach ($Bundle in $RemoveBundle) {
    & $Runtime.python -m universal_tools --state $Runtime.state skills remove $Bundle
    if ($LASTEXITCODE -ne 0) { throw 'Native skill bundle removal failed.' }
}
if ($RollbackRelease) {
    & $Runtime.python -m universal_tools --state $Runtime.state skills rollback $RollbackRelease
} else {
    & $Runtime.python -m universal_tools --state $Runtime.state skills sync --base-plugin (Join-Path $ProjectRoot 'plugins\universal-tools')
}
if ($LASTEXITCODE -ne 0) { throw 'Native skill package selection failed; inspect skills status before installing.' }
if ($Install) {
    & (Join-Path $PSScriptRoot 'Install-Plugin.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Native skill package installation failed.' }
} else {
    Write-Host 'Package selected. Run scripts\Install-Plugin.ps1 to refresh the installed copy, then restart Codex and test a fresh chat.'
}
