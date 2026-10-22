"""Read-only Codex CLI inventory; never launch a plugin or run its update commands."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .management import MAX_BYTES, read_json


def newer_numeric_version(available, installed):
    pattern = r"[0-9]+(?:\.[0-9]+){1,3}"
    if not all(isinstance(v, str) and re.fullmatch(pattern, v) for v in (available, installed)):
        return False

    def parts(value):
        numbers = tuple(map(int, value.split(".")))
        return numbers + (0,) * (4 - len(numbers))

    return parts(available) > parts(installed)


def parse_inventory(data):
    installed = data.get("installed")
    if not isinstance(installed, list) or len(installed) > 1024:
        raise ValueError("unsupported Codex inventory format")
    rows = []
    for item in installed:
        if not isinstance(item, dict) or item.get("installed") is not True:
            continue
        plugin_id, name, version = (item.get(k) for k in ("pluginId", "name", "version"))
        if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (plugin_id, name, version)):
            raise ValueError("invalid Codex inventory entry")
        source = item.get("source", {})
        available = None
        if source.get("source") == "local" and isinstance(source.get("path"), str):
            root = Path(source["path"])
            if root.is_absolute():
                # Publisher metadata only. Do not inspect/execute MCP bindings or runtime pointers.
                for manifest in (root / "plugin.json", root / ".codex-plugin/plugin.json"):
                    try:
                        value = read_json(manifest).get("version")
                        if isinstance(value, str) and len(value) <= 128:
                            available = value
                            break
                    except (OSError, ValueError):
                        continue
        rows.append(
            {
                "plugin_id": plugin_id,
                "display_name": name,
                "installed": version,
                "available": available,
                "enabled": item.get("enabled") is True,
                "update_pending": newer_numeric_version(available, version),
                "source_differs": available is not None and available != version,
            }
        )
    if len({r["plugin_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate Codex inventory identity")
    return rows


class CodexInventory:
    def __init__(self, *, command=None, interval=60):
        self.command = command or shutil.which("codex")
        self.interval = interval
        self.checked_at = 0
        self.rows = []
        self.error = None

    def refresh(self, *, force=False):
        now = time.monotonic()
        if not force and self.checked_at and now - self.checked_at < self.interval:
            return self.rows, self.error
        self.checked_at = now
        try:
            if not self.command:
                raise ValueError("Codex CLI is unavailable")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run(
                [self.command, "plugin", "list", "--json"],
                capture_output=True,
                timeout=15,
                check=True,
                **options,
            )
            if len(result.stdout) > MAX_BYTES * 64:
                raise ValueError("Codex inventory is oversized")
            self.rows = parse_inventory(json.loads(result.stdout))
            self.error = None
        except Exception:
            self.error = (
                "Plugin inventory is unavailable; previously observed versions may be stale"
            )
        return self.rows, self.error


def application_rows(rows):
    """Summarize applications without concealing distinct responding instances.

    Exact-instance rows remain available. A summary of multiple responding instances
    cannot choose a restart target on the user's behalf.
    """
    groups = {}
    for row in rows:
        plugin_id = (row["details"].get("plugin") or {}).get("plugin_id")
        identity = plugin_id or row.get("plugin_id") or row["app_id"] or row["key"]
        groups.setdefault(identity, []).append(row)
    applications = []
    severity = {"error": 0, "degraded": 1, "busy": 2, "starting": 3, "running": 4, "stopped": 5}
    for identity, instances in groups.items():
        responding = [r for r in instances if r["revision"]]
        latest = max(instances, key=lambda r: r["details"].get("started_at", 0))
        selected = responding or [latest]
        primary = min(selected, key=lambda r: severity.get(r["state"], 6))
        summary = {**primary, "key": "application:" + identity, "details": dict(primary["details"])}
        for field in ("installed", "running", "available"):
            values = {r[field] for r in selected}
            summary[field] = next(iter(values)) if len(values) == 1 else "Mixed"
        summary["update"] = next(
            (
                label
                for label in ("Update pending", "Restart pending", "Source differs")
                if any(r["update"] == label for r in selected)
            ),
            "",
        )
        counts = {
            "responding": len(responding),
            "stopped": sum(r["state"] == "stopped" and not r["revision"] for r in instances),
            "offline": sum(r["state"] == "offline" for r in instances),
            "registered": sum(r["app_id"] is not None for r in instances),
        }
        summary["details"]["instance_counts"] = counts
        summary["details"]["responding_pids"] = sorted({r["details"]["pid"] for r in responding})
        summary["multiple_instances"] = len(responding) > 1
        if len(responding) != 1:
            summary.update(restart_allowed=False, instance_id=None, revision=None)
            if responding:
                summary["details"]["restart_reason"] = (
                    "Multiple instances are responding. Select Show instances and inspect the "
                    "exact process before requesting an app-approved restart."
                )
        applications.append(summary)
    return sorted(applications, key=lambda r: (r["name"].casefold(), r["key"]))


def present_snapshot(snapshot, inventory):
    """Build a nontechnical UI model without credentials or executable metadata."""
    rows = []
    matched = set()
    plugins = {p["plugin_id"]: p for p in inventory}
    for service in snapshot["services"]:
        status = service["status"]
        components = status["versions"]
        plugin = plugins.get(service["plugin_id"])
        if plugin:
            matched.add(plugin["plugin_id"])
        primary = components.get("app") or components.get("plugin") or {}
        update = any(c["update_pending"] for c in components.values()) or bool(
            plugin and plugin["update_pending"]
        )
        restart_pending = any(
            c["running"] is not None and c["installed"] != c["running"] for c in components.values()
        )
        rows.append(
            {
                "key": service["app_id"] + "." + service["instance_id"],
                "name": service["display_name"],
                "state": status["state"],
                "installed": primary.get("installed") or (plugin or {}).get("installed", "Unknown"),
                "running": (
                    primary.get("running") or "Unknown"
                    if status["state"] not in {"offline", "stopped"}
                    else "Unknown"
                ),
                "available": primary.get("available")
                or (plugin or {}).get("available")
                or "Unknown",
                "update": "Update pending"
                if update
                else "Restart pending"
                if restart_pending
                else "",
                "restart_allowed": bool(status["restart_allowed"] and service["revision"]),
                "app_id": service["app_id"],
                "plugin_id": service["plugin_id"],
                "instance_id": service["instance_id"],
                "revision": service["revision"],
                "details": {
                    "message": status["message"],
                    "restart_reason": status["restart_reason"],
                    "versions": components,
                    "plugin": plugin,
                    "pid": service["pid"],
                    "started_at": service.get("started_at", 0),
                },
            }
        )
    for plugin in inventory:
        if plugin["plugin_id"] in matched:
            continue
        rows.append(
            {
                "key": plugin["plugin_id"],
                "name": plugin["display_name"],
                "state": "unmonitored" if plugin["enabled"] else "disabled",
                "installed": plugin["installed"],
                "running": "Unknown",
                "available": plugin["available"] or "Unknown",
                "update": "Update pending"
                if plugin["update_pending"]
                else "Source differs"
                if plugin["source_differs"]
                else "",
                "restart_allowed": False,
                "app_id": None,
                "instance_id": None,
                "revision": None,
                "details": {
                    "message": "This plugin has not registered a management connection",
                    "restart_reason": "The app must opt into the management interface",
                    "plugin": plugin,
                },
            }
        )
    applications = application_rows(rows)
    return {
        "schema_version": 1,
        "updated_at": snapshot["updated_at"],
        "rows": sorted(rows, key=lambda r: (r["name"].casefold(), r["key"])),
        "applications": applications,
        "warnings": snapshot["warnings"],
        "running": sum(r["state"] == "running" for r in rows),
        "updates": sum(bool(r["update"]) for r in rows),
        "application_updates": sum(bool(r["update"]) for r in applications),
        "responding_instances": sum(bool(r["revision"]) for r in rows),
        "responding_applications": sum(
            r["details"]["instance_counts"]["responding"] > 0 for r in applications
        ),
        "multiple_instance_applications": sum(r["multiple_instances"] for r in applications),
        "inactive_instances": sum(
            r["state"] in {"offline", "stopped"} and not r["revision"] for r in rows
        ),
    }
