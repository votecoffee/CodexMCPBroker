param(
    [string]$PythonVersion = '3.13',
    [switch]$Broker,
    [ValidateRange(0, 65535)][int]$Port = 0,
    [string]$InstallDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\App'),
    [string]$RegistryDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'CodexMCPManager\Registry'),
    [switch]$StartAtLogon,
    [switch]$InstallOnly,
    [switch]$NoShortcuts,
    [switch]$Reinstall,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
if ($Broker) {
    foreach ($option in @('InstallDirectory', 'RegistryDirectory', 'StartAtLogon', 'InstallOnly', 'NoShortcuts', 'Reinstall')) {
        if ($PSBoundParameters.ContainsKey($option)) { throw "-$option applies to the manager; omit -Broker." }
    }
    & (Join-Path $PSScriptRoot 'Install-Broker.ps1') -PythonVersion $PythonVersion -Port $Port -NoPause:$NoPause
} else {
    if ($PSBoundParameters.ContainsKey('Port')) { throw 'Use -Broker to select a broker port.' }
    & (Join-Path $PSScriptRoot 'Install-Manager.ps1') -PythonVersion $PythonVersion `
        -InstallDirectory $InstallDirectory -RegistryDirectory $RegistryDirectory `
        -StartAtLogon:$StartAtLogon -InstallOnly:$InstallOnly -NoShortcuts:$NoShortcuts `
        -Reinstall:$Reinstall -ShowWindow -NoPause:$NoPause
}
exit $LASTEXITCODE
