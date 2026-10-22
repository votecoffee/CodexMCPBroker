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
                "instance_id": service["instance_id"],
                "revision": service["revision"],
                "details": {
                    "message": status["message"],
                    "restart_reason": status["restart_reason"],
                    "versions": components,
                    "plugin": plugin,
                    "pid": service["pid"],
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
    return {
        "schema_version": 1,
        "updated_at": snapshot["updated_at"],
        "rows": sorted(rows, key=lambda r: (r["name"].casefold(), r["key"])),
        "warnings": snapshot["warnings"],
        "running": sum(r["state"] == "running" for r in rows),
        "updates": sum(bool(r["update"]) for r in rows),
    }
