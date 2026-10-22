[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$PythonVersion = '3.13',
    [string]$InstallDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\App'),
    [string]$RegistryDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\Registry'),
    [switch]$StartAtLogon,
    [switch]$InstallOnly,
    [switch]$NoShortcuts,
    [switch]$Reinstall,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Install-Manager.ps1') -PythonVersion $PythonVersion `
    -InstallDirectory $InstallDirectory -RegistryDirectory $RegistryDirectory `
    -StartAtLogon:$StartAtLogon -InstallOnly:$InstallOnly -NoShortcuts:$NoShortcuts `
    -Reinstall:$Reinstall -ShowWindow -NoPause:$NoPause
exit $LASTEXITCODE
