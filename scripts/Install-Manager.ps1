param(
    [string]$PythonVersion = '3.13',
    [string]$InstallDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\App'),
    [string]$RegistryDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\Registry'),
    [switch]$StartAtLogon,
    [switch]$InstallOnly,
    [switch]$NoShortcuts,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$exitCode = 0
$installLock = $null
try {
    $InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
    $RegistryDirectory = [IO.Path]::GetFullPath($RegistryDirectory)
    if ($InstallDirectory -match '["\r\n]' -or $RegistryDirectory -match '["\r\n]') { throw 'Invalid installation path.' }
    if ($InstallDirectory.TrimEnd('\') -eq [IO.Path]::GetPathRoot($InstallDirectory).TrimEnd('\') -or
        $RegistryDirectory.TrimEnd('\') -eq [IO.Path]::GetPathRoot($RegistryDirectory).TrimEnd('\')) { throw 'Use dedicated application directories.' }
    [void][IO.Directory]::CreateDirectory($InstallDirectory)
    $installLock = [IO.File]::Open((Join-Path $InstallDirectory 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $managerLockPath = Join-Path $RegistryDirectory 'manager.lock'
    if (Test-Path -LiteralPath $managerLockPath) {
        try { $managerCheck = [IO.File]::Open($managerLockPath, 'Open', 'ReadWrite', 'None'); $managerCheck.Dispose() }
        catch { throw 'Exit MCP Service Manager from its tray menu before installing an update.' }
    }
    $runtime = Join-Path $InstallDirectory (Join-Path 'runtimes' ([Guid]::NewGuid().ToString('N')))
    Write-Host 'Installing MCP Service Manager in its own runtime...'
    & py "-$PythonVersion" -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the manager runtime.' }
    $python = Join-Path $runtime 'Scripts\python.exe'
    & $python -m pip install -r (Join-Path $ProjectRoot 'requirements-lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Manager dependency installation failed.' }
    & $python -m pip install --no-deps $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Manager package installation failed.' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Manager dependency check failed.' }
    $doctor = & $python -B -m universal_tools.manager_cli doctor | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$doctor.windows -or !$doctor.tray_assets_present) { throw 'Manager installation verification failed.' }
    $tray = Join-Path $runtime 'Lib\site-packages\universal_tools\manager_assets\Tray.ps1'
    if (!(Test-Path -LiteralPath $tray)) { throw 'The tray application was not packaged.' }
    $pointer = @{ schema_version = 1; python = $python; registry = $RegistryDirectory; version = $doctor.version }
    $pointerFile = Join-Path $InstallDirectory 'current.json'
    if (Test-Path -LiteralPath $pointerFile) {
        Copy-Item -LiteralPath $pointerFile -Destination (Join-Path $InstallDirectory 'previous.json') -Force
    }
    $temporary = Join-Path $InstallDirectory ('current-' + [Guid]::NewGuid().ToString('N') + '.json')
    $pointer | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $pointerFile -Force
    if (!$NoShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'MCP Service Manager.lnk'
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $shortcut.Arguments = '-NoLogo -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '" -Python "' + $python + '" -RegistryDirectory "' + $RegistryDirectory.TrimEnd('\') + '"'
        $shortcut.WorkingDirectory = $InstallDirectory
        $shortcut.IconLocation = (Join-Path (Split-Path -Parent $tray) 'icon.ico') + ',0'
        $shortcut.Description = 'Plugin versions, MCP service status and safe restart controls'
        $shortcut.WindowStyle = 7
        $shortcut.Save()
        $startupPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'MCP Service Manager.lnk'
        if ($StartAtLogon -or (Test-Path -LiteralPath $startupPath)) {
            Copy-Item -LiteralPath $shortcutPath -Destination $startupPath -Force
        }
    }
    if (!$InstallOnly) {
        & $python -B -m universal_tools.manager_cli --registry $RegistryDirectory tray
        if ($LASTEXITCODE -ne 0) { throw 'The manager could not start.' }
    }
    Write-Host "MCP Service Manager $($doctor.version) is installed."
    Write-Host 'Open it from the Start menu. Double-click its tray icon to see the dashboard.'
    Write-Host 'Apps report live status and restart availability through the shared management interface.'
} catch {
    [Console]::Error.WriteLine("Manager installation failed: $($_.Exception.Message)")
    $exitCode = 1
} finally {
    if ($installLock) { $installLock.Dispose() }
    if (!$NoPause) { [void](Read-Host 'Press Enter to close') }
}
exit $exitCode
