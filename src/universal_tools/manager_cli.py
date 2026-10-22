"""Desktop manager commands. The manager never installs or kills app services."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .instance_lock import InstanceLock
from .management import ManagementClient, ManagementRegistry, default_registry
from .manager_inventory import CodexInventory, present_snapshot
from .models import atomic_json


def parent_alive(pid):
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch(args):
    registry = ManagementRegistry(args.registry)
    registry.prepare()
    client = ManagementClient(registry.directory)
    inventory = CodexInventory() if not args.no_codex else None
    with InstanceLock(registry.directory / "manager.lock"):
        refresh_seen = None
        while not args.stop_file.exists() and (
            not args.parent_pid or parent_alive(args.parent_pid)
        ):
            snapshot = client.snapshot()
            refresh_stamp = (
                args.refresh_file.stat().st_mtime_ns
                if args.refresh_file and args.refresh_file.exists()
                else None
            )
            plugins, warning = (
                inventory.refresh(force=refresh_stamp != refresh_seen) if inventory else ([], None)
            )
            refresh_seen = refresh_stamp
            if warning:
                snapshot["warnings"].append(warning)
            atomic_json(args.snapshot_file, present_snapshot(snapshot, plugins))
            if args.once:
                break
            # Small interruptible intervals keep manager exit responsive.
            for _ in range(30):
                if args.stop_file.exists():
                    return
                time.sleep(0.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="MCP service tray management")
    parser.add_argument("--registry", type=Path, default=default_registry())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("doctor")
    tray = commands.add_parser("tray")
    tray.add_argument("--show", action="store_true")
    watcher = commands.add_parser("watch")
    watcher.add_argument("--snapshot-file", required=True, type=Path)
    watcher.add_argument("--stop-file", required=True, type=Path)
    watcher.add_argument("--parent-pid", type=int)
    watcher.add_argument("--refresh-file", type=Path)
    watcher.add_argument("--once", action="store_true")
    watcher.add_argument("--no-codex", action="store_true")
    restart = commands.add_parser("restart")
    restart.add_argument("--app-id", required=True)
    restart.add_argument("--instance-id", required=True)
    restart.add_argument("--revision", required=True)
    restart.add_argument("--request-id", required=True)
    restart.add_argument("--result-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            watch(args)
            return 0
        if args.command == "tray":
            if os.name != "nt":
                raise ValueError("The tray application requires Windows")
            script = Path(__file__).parent / "manager_assets/Tray.ps1"
            command = [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Python",
                sys.executable,
                "-RegistryDirectory",
                str(args.registry.resolve()),
            ]
            if args.show:
                command.append("-ShowWindow")
            subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
            return 0
        if args.command == "doctor":
            assets = Path(__file__).parent / "manager_assets"
            result = {
                "version": __version__,
                "windows": os.name == "nt",
                "tray_assets_present": all(
                    (assets / n).is_file() for n in ("Tray.ps1", "icon.ico")
                ),
            }
        elif args.command == "status":
            result = ManagementClient(args.registry).snapshot()
        else:
            result = ManagementClient(args.registry, timeout=10).restart(
                args.app_id, args.instance_id, args.revision, args.request_id
            )
            if args.result_file:
                atomic_json(args.result_file, result)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        result = {
            "outcome": "uncertain",
            "message": "Management operation failed; inspect app state",
        }
        if getattr(args, "result_file", None):
            atomic_json(args.result_file, result)
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
