param(
    [string]$PythonVersion = '3.13',
    [ValidateRange(0, 65535)]
    [int]$Port = 0,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StateDirectory = Join-Path $ProjectRoot '.local'
$RuntimeFile = Join-Path $ProjectRoot 'plugins\universal-tools\runtime.json'
$SettingsFile = Join-Path $StateDirectory 'settings.json'
$LauncherExitCode = 0

try {
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        throw 'Codex CLI was not found on PATH. Install Codex with its plugin CLI, then run this launcher again.'
    }
    if (Test-Path -LiteralPath $RuntimeFile) {
        $Runtime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
        if (Test-Path -LiteralPath $Runtime.python) {
            $StatusJson = & $Runtime.python -m universal_tools --state $Runtime.state status
            if ($LASTEXITCODE -ne 0) { throw 'Cannot check the existing broker. Run scripts\Broker.ps1 status to inspect it.' }
            $Status = $StatusJson | ConvertFrom-Json
            if ($Status.running -ne $false) {
                throw 'The broker is already running. Finish active app work, run scripts\Broker.ps1 stop, then rerun this launcher to install updates.'
            }
        }
    }
    if ($Port -eq 0) {
        $Port = if (Test-Path -LiteralPath $SettingsFile) {
            (Get-Content -LiteralPath $SettingsFile -Raw | ConvertFrom-Json).port
        } else { 8768 }
    }

    Write-Host 'Installing the broker runtime...'
    & (Join-Path $PSScriptRoot 'Setup.ps1') -PythonVersion $PythonVersion -Port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Runtime setup failed.' }
    Write-Host 'Installing the Universal Tools Codex plugin...'
    & (Join-Path $PSScriptRoot 'Install-Plugin.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Codex plugin installation failed.' }
    Write-Host 'Starting the broker...'
    & (Join-Path $PSScriptRoot 'Broker.ps1') start
    if ($LASTEXITCODE -ne 0) { throw 'Broker startup failed.' }
    & (Join-Path $PSScriptRoot 'Broker.ps1') status
    if ($LASTEXITCODE -ne 0) { throw 'Broker status check failed.' }
    Write-Host 'Installed and running. Open a new Codex chat to load Universal Tools.'
} catch {
    [Console]::Error.WriteLine("Install and run failed: $($_.Exception.Message)")
    $LauncherExitCode = 1
} finally {
    if (-not $NoPause) { [void](Read-Host 'Press Enter to close') }
}
exit $LauncherExitCode
