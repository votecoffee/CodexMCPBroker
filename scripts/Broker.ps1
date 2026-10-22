param(
    [Parameter(Position=0)]
    [ValidateSet('start','start-demo','status','stop','refresh','list','contexts','configure-request-limit')]
    [string]$Action = 'status',
    [ValidateRange(1,134217728)]
    [long]$Bytes
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeFile = Join-Path $ProjectRoot '.local\development-runtime.json'
if (-not (Test-Path -LiteralPath $RuntimeFile)) { throw 'Run Setup.ps1 first.' }
$RuntimeSettings = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
$CommandArguments = @('-m', 'universal_tools', '--state', $RuntimeSettings.state, $Action)
if ($Action -eq 'configure-request-limit') {
    if (-not $PSBoundParameters.ContainsKey('Bytes')) { throw 'Specify -Bytes for the request limit.' }
    $CommandArguments += @('--bytes', $Bytes)
} elseif ($PSBoundParameters.ContainsKey('Bytes')) {
    throw '-Bytes applies only to configure-request-limit.'
}
& $RuntimeSettings.python @CommandArguments
if ($LASTEXITCODE -ne 0) { throw "Broker command failed: $Action" }
