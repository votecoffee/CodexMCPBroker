param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [switch]$Replace
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
$RuntimeSettings = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
$RegistrationArgs = @('-m','universal_tools','--state',$RuntimeSettings.state,'register',$Manifest)
if ($Replace) { $RegistrationArgs += '--replace' }
& $RuntimeSettings.python @RegistrationArgs
if ($LASTEXITCODE -ne 0) { throw 'App registration failed.' }
Write-Host 'Run Broker.ps1 refresh, or ask a connected chat to call refresh_services.'
