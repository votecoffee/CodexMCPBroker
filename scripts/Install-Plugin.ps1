param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
$Runtime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
$SkillStatusJson = & $Runtime.python -m universal_tools --state $Runtime.state skills status
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the selected native-skill package. Run skills status or Sync-Skills.ps1 to inspect it.' }
$SkillStatus = $SkillStatusJson | ConvertFrom-Json
$MarketplaceRoot = $ProjectRoot
if ($SkillStatus.active_release) {
    $MarketplaceRoot = $SkillStatus.marketplace_root
    if ($SkillStatus.selection_pending) {
        Write-Host 'Installing the selected release; bundle selection has unpublished changes. Run Sync-Skills.ps1 to publish them.'
    }
} elseif ($SkillStatus.selection_pending) {
    throw 'Native skill bundles are selected but not composed. Run scripts\Sync-Skills.ps1 before installing.'
}
& codex plugin marketplace add $MarketplaceRoot --json
if ($LASTEXITCODE -ne 0) { throw 'Local marketplace registration failed.' }
& codex plugin add universal-tools@universal-tools-local --json
if ($LASTEXITCODE -ne 0) { throw 'Local plugin installation failed.' }
Write-Host 'Universal Tools is installed. Restart Codex, then test its tools and native skills in a fresh chat.'
