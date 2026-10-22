# Tray service management

Framework **0.18.0** adds a Windows tray application and the app-facing `universal_tools.management` interface. The manager is independent of the Universal Tools plugin and broker. It does not change app installations, MCP schemas, routes or native project state.

## Install and open

Double-click **`InstallManager.cmd`** in the project root or run:

```powershell
.\InstallManager.cmd -NoPause
# Optional automatic startup at Windows logon.
.\InstallManager.cmd -StartAtLogon -NoPause
# Install without opening the tray.
.\InstallManager.cmd -InstallOnly -NoPause
```

The installer creates a separate versioned Python runtime under the current user's local application data, verifies dependencies/package assets, adds a Start menu shortcut and opens the tray. It reuses the project icon. Double-click the icon to open the dashboard. Closing the window keeps the tray running; **Exit manager** closes it without stopping app services.

Requirements: Windows, Windows PowerShell/Windows Forms, Python 3.11+ and the Windows `py` launcher. Python 3.13 is the default installer profile. No administrator rights are required. Codex CLI is optional: registered app services still work without automatic plugin inventory.

Use dedicated directories for isolated installation checks:

```powershell
.\InstallManager.cmd -InstallDirectory 'C:\Test\ManagerApp' -RegistryDirectory 'C:\Test\ManagerRegistry' -NoShortcuts -InstallOnly -NoPause
```

The default registry is the Windows user Local AppData known folder plus `CodexMCPManager\Registry`. Python agents disable package redirection so a host-specific `LOCALAPPDATA` override cannot silently create a different registry. Use an explicit registry path when an app requires another trust boundary. Exit the manager before upgrading. Each install uses a new runtime and preserves the prior pointer/runtime. Existing logon startup selection is preserved on upgrades. The installer does not reinstall a Codex plugin or replace an MCP server environment. Native skill/plugin changes can still require a host reload.

## Status and versions

The dashboard shows registered service instances, status, installed/running/available versions, updates and restart availability. Different processes receive separate random instance IDs and rows. Component details can include app, plugin, framework and SDK versions. Installed/running mismatch is **Restart pending**; apps explicitly declare **Update pending**. Available versions stay unknown when no source provides them.

The optional inventory adapter runs the fixed read-only `codex plugin list --json` command. Installed plugins without a management endpoint appear as **Not reporting**, with unknown running versions and restart disabled. Enabled installation does not prove server health or availability in a chat. Remote connectors and skill-only plugins can also appear.

For local publisher sources, inventory reads only the plugin manifest version. A newer numeric version indicates an update; an unequal version that cannot be ordered is **Source differs**. This is local metadata, not a GitHub/package-feed update check. Inventory never executes plugin launchers, installs updates or changes settings. Failures retain an explicit stale-data warning.

Service status refreshes about every three seconds; inventory refreshes every minute or on **Refresh**. Unavailable endpoints and stale snapshots disable restart controls. Apps that have not adopted the interface remain unmonitored.

## Integrate an app

Use the normal shared package dependency. Management works with direct SDK servers and `ApplicationHost` applications:

```python
from universal_tools.management import (
    ComponentVersion, ManagedService, ManagedStatus, RestartResult,
)

def status():
    return ManagedStatus(
        state="busy" if supervisor.active_operations else "running",
        active_operations=supervisor.active_operations,
        message=supervisor.status_message,
        versions={
            "app": ComponentVersion(
                installed=installer.installed_version,
                running=supervisor.running_version,
                available=installer.available_version,
                update_pending=installer.update_pending,
            ),
            "plugin": ComponentVersion(installed=installer.plugin_version),
        },
        restart_allowed=supervisor.can_restart,
        restart_reason=supervisor.restart_reason,
    )

def restart(request):
    # Atomically fence new work and enforce app/native safety before scheduling.
    accepted = supervisor.request_safe_restart(request.request_id)
    return RestartResult(
        outcome="accepted" if accepted else "blocked",
        message="Restart scheduled" if accepted else supervisor.restart_reason,
    )

with ManagedService(
    "example-app", "Example MCP Server", status,
    restart=restart, plugin_id="example-app@example-marketplace",
):
    run_existing_app_service()
```

`supervisor`, `installer` and `run_existing_app_service` are app-owned interfaces in this sketch. The [runnable example](../examples/managed_service.py) restarts only its synthetic in-memory worker, with an explicit test registry:

```powershell
python examples/managed_service.py --registry 'C:\Test\ManagerRegistry'
universal-tools-manager --registry 'C:\Test\ManagerRegistry' status
universal-tools-manager --registry 'C:\Test\ManagerRegistry' tray --show
```

Register the owning service/supervisor for its entire lifetime. A stopped registration remains observable; a crashed/unreachable endpoint is offline. Restart of an offline process requires an app-owned surviving supervisor. A live supervisor may explicitly allow restarting its stopped worker; a closed/unreachable management endpoint never gains that permission. The manager never guesses a command or kills/relaunches a PID.

The status callback must be fast and thread-safe, using a cached snapshot without attaching projects or contacting controllers. The framework adds its installed/running version unless the app supplies that component deliberately. Other bounded component versions can be added without changing the protocol.

## Restart and uncertain outcomes

The user selects **Restart selected service**. The request binds app/instance, current status revision and a unique operation ID. The agent rechecks status and refuses active operations, starting/busy services, changed revisions and app refusals. The callback must atomically fence new work and enforce ownership leases, unsaved documents, review plans, queued jobs and unresolved effects before scheduling a restart. A counter alone is insufficient. Return acceptance before the supervisor closes the management endpoint.

A SQLite intent is durable before callback dispatch. Duplicate IDs return the earlier receipt; changed payloads under the same ID are rejected. Callback failure or process interruption leaves an uncertain barrier that blocks further restarts for that app, including other instances and later process lifetimes. Lost client receipts are never automatically retried. The manager cannot clear app journals or management barriers.

App/operator code can inspect unresolved IDs and receipts with `agent.pending_restarts()` without dispatching anything. After independently verifying the actual outcome, it can call `agent.reconcile_restart(request_id, RestartResult(...))`. This app-only API appends an attestation and preserves the original uncertain intent. It is not exposed through tray, HTTP or MCP. An accepted receipt means the supervisor accepted the request; observe a fresh registration/status to establish replacement health.

The journal is bounded to 10,000 intents; archival must preserve operation IDs and unresolved outcomes. Registry enumeration is bounded to 256 instance records with a visible capacity warning. Stopped records are not automatically erased.

## Local coordination protocol

Python apps use `ManagedService`, `ManagementClient` and `ManagementRegistry`. Other runtimes can implement the same contract using the [registration schema](../examples/management-registration.schema.json) and [status schema](../examples/management-status.schema.json). Restart bodies and receipts use the [request schema](../examples/management-restart-request.schema.json) and [receipt schema](../examples/management-restart-receipt.schema.json).

- Write an ordinary JSON file atomically at `instances/<app_id>.<instance_id>.json`. It contains identity, display PID, a literal `127.0.0.1` HTTP endpoint, random per-instance bearer token and initial status. Keep it private; UI/snapshots omit control tokens and endpoints.
- Authenticate `GET /v1/management/status` and `POST /v1/management/restart`, enforce the exact loopback Host and reject browser Origin headers. Restart JSON contains `instance_id`, `expected_revision` and `request_id`, with no arbitrary command or native target.
- Status includes `schema_version: 1`, app/instance identity, `status` and SHA-256 of canonical sorted JSON `{instance_id, status}`. Match `canonical_digest` escaping/separators and numeric types when implementing another language.
- Return bounded `accepted`, `blocked` or `uncertain` restart receipts. App code alone implements effects and reconciliation; approval, retry or alternate routing is not implied.

This is same-user local coordination. Windows registry ACLs admit only the user and SYSTEM; POSIX test directories use mode 0700. HTTP binds loopback, bounds requests/responses and concurrent handlers, and omits credentials/app exceptions from logs. Same-user code remains inside this trust boundary. Remote OAuth/Redis is a separate profile.

## Verification

Follow the [management verification profile](../VERIFICATION.md#tray-management-profile). Synthetic tests cover real HTTP authentication, versions, changed/busy status, duplicate/concurrent requests, durable restart barriers, app-only reconciliation and private snapshots. Native Forms and installed-wheel checks remain separate from real app restart acceptance. Installing the manager does not migrate an app.

Upstream references: [Windows NotifyIcon](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.notifyicon), [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins). The CLI JSON adapter is version-sensitive and must be checked when the installed CLI changes.
