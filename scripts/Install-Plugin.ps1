param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
& codex plugin marketplace add $ProjectRoot --json
if ($LASTEXITCODE -ne 0) { throw 'Local marketplace registration failed.' }
& codex plugin add universal-tools@universal-tools-local --json
if ($LASTEXITCODE -ne 0) { throw 'Local plugin installation failed.' }
Write-Host 'Universal Tools is installed. Start a new Codex chat to use its tools.'
