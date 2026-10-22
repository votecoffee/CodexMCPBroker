param(
    [string]$PythonVersion = '3.13',
    [string]$RuntimeDirectory
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeDirectory) { $RuntimeDirectory = Join-Path $ProjectRoot '.local\development' }
$RuntimePython = Join-Path $RuntimeDirectory 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $RuntimePython)) {
    & py "-$PythonVersion" -m venv $RuntimeDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
& $RuntimePython -m pip install -r (Join-Path $ProjectRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked runtime dependency installation failed.' }
& $RuntimePython -m pip install --no-deps $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
$StateDirectory = Join-Path $ProjectRoot '.local\development-state'
@{ python = $RuntimePython; state = $StateDirectory } | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $ProjectRoot '.local\development-runtime.json') -Encoding utf8
Write-Host 'Shared framework development runtime installed. No plugin or MCP server was installed or started.'
Write-Host 'Use InstallAndRun.cmd to install and open the independent service manager.'
