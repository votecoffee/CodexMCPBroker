param(
    [string]$PythonVersion = '3.13',
    [string]$RuntimeDirectory,
    [int]$Port = 8768
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeDirectory) { $RuntimeDirectory = Join-Path $ProjectRoot '.local\runtime' }
$RuntimePython = Join-Path $RuntimeDirectory 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $RuntimePython)) {
    & py "-$PythonVersion" -m venv $RuntimeDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
& $RuntimePython -m pip install -r (Join-Path $ProjectRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked runtime dependency installation failed.' }
& $RuntimePython -m pip install --no-deps $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
$StateDirectory = Join-Path $ProjectRoot '.local'
& $RuntimePython -m universal_tools --state $StateDirectory init --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Broker initialization failed.' }
& $RuntimePython -m universal_tools --state $StateDirectory configure-plugin (Join-Path $ProjectRoot 'plugins\universal-tools')
if ($LASTEXITCODE -ne 0) { throw 'Plugin configuration failed.' }
& (Join-Path $PSScriptRoot 'Create-LauncherShortcut.ps1') -Broker
Write-Host 'Setup complete. Use Broker.ps1 start-demo for the examples, or Broker.ps1 start for registered apps.'
Write-Host 'Use Install-Plugin.ps1 to install this local plugin into Codex.'
