param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TargetPath = Join-Path $ProjectRoot 'InstallAndRun.cmd'
$IconPath = Join-Path $ProjectRoot 'assets\icon.ico'
if (-not (Test-Path -LiteralPath $TargetPath)) { throw 'InstallAndRun.cmd is missing.' }
if (-not (Test-Path -LiteralPath $IconPath)) { throw 'The launcher icon is missing.' }
$LocalDirectory = Join-Path $ProjectRoot '.local'
[void][IO.Directory]::CreateDirectory($LocalDirectory)
$ShortcutPath = Join-Path $LocalDirectory 'InstallAndRun.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.Arguments = ''
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Description = 'Install and open MCP Service Manager'
$Shortcut.WindowStyle = 1
$Shortcut.Save()
Write-Host "Created branded launcher: $ShortcutPath"
