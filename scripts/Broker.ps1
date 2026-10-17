param(
    [Parameter(Position=0)]
    [ValidateSet('start','start-demo','status','stop','refresh','list','contexts')]
    [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
$RuntimeSettings = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
& $RuntimeSettings.python -m universal_tools --state $RuntimeSettings.state $Action
if ($LASTEXITCODE -ne 0) { throw "Broker command failed: $Action" }
