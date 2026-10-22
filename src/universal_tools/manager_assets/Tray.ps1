param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$RegistryDirectory,
    [switch]$ShowWindow,
    [switch]$NoCodexInventory,
    [int]$ExitAfterSeconds = 0,
    [string]$SmokeTestReport
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$RegistryDirectory = [IO.Path]::GetFullPath($RegistryDirectory)
if ($RegistryDirectory -match '["\r\n]' -or $Python -match '["\r\n]') { throw 'Invalid manager path.' }
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'The manager runtime is missing. Run InstallManager.cmd again.' }
$hashProvider = [Security.Cryptography.SHA256]::Create()
$identity = [BitConverter]::ToString($hashProvider.ComputeHash([Text.Encoding]::UTF8.GetBytes($RegistryDirectory.ToLowerInvariant()))).Replace('-', '')
$hashProvider.Dispose()
$created = $false
$mutex = [Threading.Mutex]::new($true, "Local\CodexMCPManager-$identity", [ref]$created)
if (!$created) { $mutex.Dispose(); exit 0 }
$uiDirectory = Join-Path $RegistryDirectory 'ui'
[void][IO.Directory]::CreateDirectory($uiDirectory)
$snapshotFile = Join-Path $uiDirectory "snapshot-$PID.json"
$stopFile = Join-Path $uiDirectory "stop-$PID"
$refreshFile = Join-Path $uiDirectory "refresh-$PID"
$script:exiting = $false
$script:lastSnapshot = $null
$script:pending = $null
$script:lastError = ''
$started = [DateTime]::UtcNow
$watch = $null
$icon = $null
$timer = $null

function Quote-Argument([string]$Value) {
    if ($Value -match '["\r\n]') { throw 'Invalid process argument.' }
    return '"' + $Value.TrimEnd('\') + '"'
}

function Show-Dashboard {
    $form.Show()
    $form.WindowState = 'Normal'
    $form.Activate()
}

function Request-Refresh {
    [IO.File]::WriteAllText($refreshFile, [Guid]::NewGuid().ToString('N'))
    $footer.Text = 'Checking service status...'
}

function Get-SelectedService {
    if ($grid.CurrentRow) { return $grid.CurrentRow.Tag }
    return $null
}

function Show-Selection {
    $selected = Get-SelectedService
    $restartButton.Enabled = $selected -and $selected.restart_allowed -and !$script:pending
    $restartMenu.Enabled = $restartButton.Enabled
    if (!$selected) { $details.Text = 'Select a service to see its versions and restart availability.'; return }
    $lines = @($selected.details.message, $selected.details.restart_reason) | Where-Object { $_ }
    if ($selected.details.versions) {
        foreach ($component in $selected.details.versions.PSObject.Properties) {
            $version = $component.Value
            $lines += "$($component.Name): installed $($version.installed); running $($version.running); available $($version.available)"
        }
    }
    $details.Text = $lines -join [Environment]::NewLine
}

function Request-Restart {
    $selected = Get-SelectedService
    if (!$selected -or !$selected.restart_allowed -or $script:pending) { return }
    $requestId = [Guid]::NewGuid().ToString('N')
    $resultFile = Join-Path $uiDirectory "restart-$requestId.json"
    $arguments = @('-B', '-m', 'universal_tools.manager_cli', '--registry', (Quote-Argument $RegistryDirectory),
        'restart', '--app-id', $selected.app_id, '--instance-id', $selected.instance_id,
        '--revision', $selected.revision, '--request-id', $requestId, '--result-file', (Quote-Argument $resultFile))
    $process = Start-Process -FilePath $Python -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $uiDirectory "restart-$requestId.log") `
        -RedirectStandardError (Join-Path $uiDirectory "restart-$requestId.err")
    $script:pending = @{ Process = $process; Result = $resultFile; Name = $selected.name }
    $footer.Text = "Requesting a safe restart of $($selected.name)..."
    Show-Selection
}

try {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'MCP Service Manager'
    $form.Size = [Drawing.Size]::new(1120, 650)
    $form.MinimumSize = [Drawing.Size]::new(850, 480)
    $form.StartPosition = 'CenterScreen'
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.Icon = [Drawing.Icon]::new((Join-Path $PSScriptRoot 'icon.ico'))

    $header = New-Object System.Windows.Forms.Label
    $header.Text = 'Plugins and MCP services'
    $header.Font = [Drawing.Font]::new('Segoe UI', 17, [Drawing.FontStyle]::Bold)
    $header.Dock = 'Top'
    $header.Height = 48
    $header.Padding = [Windows.Forms.Padding]::new(12, 8, 0, 0)

    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.Dock = 'Bottom'
    $buttons.Height = 46
    $buttons.Padding = [Windows.Forms.Padding]::new(10, 5, 0, 0)
    $refreshButton = New-Object System.Windows.Forms.Button
    $refreshButton.Text = 'Refresh'
    $refreshButton.AutoSize = $true
    $refreshButton.Add_Click({ Request-Refresh })
    $restartButton = New-Object System.Windows.Forms.Button
    $restartButton.Text = 'Restart selected service'
    $restartButton.AutoSize = $true
    $restartButton.Enabled = $false
    $restartButton.Add_Click({ Request-Restart })
    [void]$buttons.Controls.Add($refreshButton)
    [void]$buttons.Controls.Add($restartButton)

    $footer = New-Object System.Windows.Forms.Label
    $footer.Dock = 'Bottom'
    $footer.Height = 36
    $footer.Padding = [Windows.Forms.Padding]::new(12, 5, 0, 0)
    $footer.Text = 'Loading plugin inventory and service status...'
    $details = New-Object System.Windows.Forms.TextBox
    $details.Dock = 'Bottom'
    $details.Height = 105
    $details.Multiline = $true
    $details.ReadOnly = $true
    $details.ScrollBars = 'Vertical'
    $details.BackColor = [Drawing.SystemColors]::Window

    $grid = New-Object System.Windows.Forms.DataGridView
    $grid.Dock = 'Fill'
    $grid.ReadOnly = $true
    $grid.AllowUserToAddRows = $false
    $grid.AllowUserToDeleteRows = $false
    $grid.AllowUserToResizeRows = $false
    $grid.MultiSelect = $false
    $grid.SelectionMode = 'FullRowSelect'
    $grid.RowHeadersVisible = $false
    $grid.AutoSizeColumnsMode = 'Fill'
    $grid.BackgroundColor = [Drawing.SystemColors]::Window
    foreach ($column in @('Service', 'Status', 'Installed', 'Running', 'Available', 'Update')) {
        [void]$grid.Columns.Add($column, $column)
    }
    $grid.Columns[0].FillWeight = 200
    $grid.Add_SelectionChanged({ Show-Selection })
    foreach ($control in @($grid, $details, $buttons, $footer, $header)) { [void]$form.Controls.Add($control) }

    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    [void]$menu.Items.Add('Open dashboard', $null, { Show-Dashboard })
    [void]$menu.Items.Add('Refresh status', $null, { Request-Refresh })
    $restartMenu = $menu.Items.Add('Restart selected service', $null, { Request-Restart })
    $restartMenu.Enabled = $false
    [void]$menu.Items.Add('-')
    [void]$menu.Items.Add('Exit manager', $null, {
        $script:exiting = $true
        [Windows.Forms.Application]::Exit()
    })
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = $form.Icon
    $icon.Text = 'MCP Service Manager'
    $icon.ContextMenuStrip = $menu
    $icon.Visible = $true
    $icon.Add_DoubleClick({ Show-Dashboard })
    $form.Add_FormClosing({
        param($sender, $event)
        if (!$script:exiting) { $event.Cancel = $true; $form.Hide() }
    })

    $watchArgs = @('-B', '-m', 'universal_tools.manager_cli', '--registry', (Quote-Argument $RegistryDirectory),
        'watch', '--snapshot-file', (Quote-Argument $snapshotFile), '--stop-file', (Quote-Argument $stopFile),
        '--refresh-file', (Quote-Argument $refreshFile), '--parent-pid', "$PID")
    if ($NoCodexInventory) { $watchArgs += '--no-codex' }
    $watch = Start-Process -FilePath $Python -ArgumentList $watchArgs -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $uiDirectory "watch-$PID.log") `
        -RedirectStandardError (Join-Path $uiDirectory "watch-$PID.err")

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 1000
    $timer.Add_Tick({
        try {
            if ($script:pending -and $script:pending.Process.HasExited) {
                if (Test-Path -LiteralPath $script:pending.Result) {
                    $result = Get-Content -LiteralPath $script:pending.Result -Raw | ConvertFrom-Json
                    $script:lastError = "$($script:pending.Name): $($result.message)"
                } else { $script:lastError = 'Restart outcome unavailable. Inspect the app before another request.' }
                $script:pending.Process.Dispose()
                $script:pending = $null
                Request-Refresh
            }
            if (Test-Path -LiteralPath $snapshotFile) {
                $snapshot = Get-Content -LiteralPath $snapshotFile -Raw | ConvertFrom-Json
                if (!$script:lastSnapshot -or $script:lastSnapshot.updated_at -ne $snapshot.updated_at) {
                    $selected = Get-SelectedService
                    $selectedKey = if ($selected) { $selected.key } else { '' }
                    $grid.Rows.Clear()
                    foreach ($row in $snapshot.rows) {
                        $state = switch ($row.state) {
                            'unmonitored' { 'Not reporting' }
                            'offline' { 'Not responding' }
                            default { [Globalization.CultureInfo]::InvariantCulture.TextInfo.ToTitleCase($row.state) }
                        }
                        $index = $grid.Rows.Add($row.name, $state, $row.installed, $row.running, $row.available, $row.update)
                        $grid.Rows[$index].Tag = $row
                        if ($row.state -in @('error', 'degraded', 'offline')) { $grid.Rows[$index].DefaultCellStyle.ForeColor = [Drawing.Color]::DarkRed }
                        elseif ($row.update) { $grid.Rows[$index].DefaultCellStyle.ForeColor = [Drawing.Color]::DarkOrange }
                        elseif ($row.state -in @('unmonitored', 'disabled', 'stopped')) { $grid.Rows[$index].DefaultCellStyle.ForeColor = [Drawing.Color]::DimGray }
                        if ($row.key -eq $selectedKey) { $grid.CurrentCell = $grid.Rows[$index].Cells[0] }
                    }
                    $script:lastSnapshot = $snapshot
                    $icon.Text = "MCP Manager: $($snapshot.running) running, $($snapshot.updates) pending"
                    if ($icon.Text.Length -gt 63) { $icon.Text = $icon.Text.Substring(0,63) }
                    Show-Selection
                }
                $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $snapshot.updated_at
                if ($age -gt 20) {
                    $footer.Text = 'Status is stale. The manager is not receiving fresh updates.'
                    $restartButton.Enabled = $false; $restartMenu.Enabled = $false
                } elseif ($script:lastError) { $footer.Text = $script:lastError }
                elseif ($snapshot.warnings.Count) { $footer.Text = $snapshot.warnings -join '; ' }
                elseif (!$script:pending) { $footer.Text = "$($snapshot.rows.Count) entries. Installed plugins do not report live status until their app enables management." }
            }
            if ($watch.HasExited) {
                $footer.Text = 'The status monitor stopped. Exit and reopen the manager.'
                $restartButton.Enabled = $false; $restartMenu.Enabled = $false
            }
            if ($ExitAfterSeconds -gt 0 -and ([DateTime]::UtcNow - $started).TotalSeconds -ge $ExitAfterSeconds) {
                if ($SmokeTestReport) {
                    @{ tray_visible = $icon.Visible; column_count = $grid.Columns.Count; row_count = $grid.Rows.Count;
                       monitor_running = !$watch.HasExited; snapshot_received = [bool]$script:lastSnapshot;
                       restart_enabled = $restartButton.Enabled } |
                        ConvertTo-Json | Set-Content -LiteralPath $SmokeTestReport -Encoding utf8
                }
                $script:exiting = $true
                [Windows.Forms.Application]::Exit()
            }
        } catch { $footer.Text = 'Status is temporarily unavailable. Waiting for the next update.' }
    })
    $timer.Start()
    if ($ShowWindow) { Show-Dashboard }
    [Windows.Forms.Application]::Run()
} finally {
    if ($timer) { $timer.Stop(); $timer.Dispose() }
    if ($icon) { $icon.Visible = $false; $icon.Dispose() }
    [IO.File]::WriteAllText($stopFile, 'stop')
    if ($watch) {
        if (!$watch.WaitForExit(2000)) { $watch.Kill() }
        $watch.Dispose()
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
