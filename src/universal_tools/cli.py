"""Local lifecycle and registration CLI. No global Codex configuration is changed."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .daemon import InstanceLock, serve
from .demo import create_demo
from .models import (
    ID_PATTERN,
    BrokerError,
    Settings,
    atomic_json,
    load_manifest,
    load_settings,
)
from .ownership import OwnershipJournal
from .skill_bundles import add_parser as skill_parser
from .skill_bundles import execute as skill_command
from .surface import RemoteBackend, create_surface
from .transport import http_session


def initialize(state: Path, port: int) -> Settings:
    state.mkdir(parents=True, exist_ok=True)
    (state / "services").mkdir(exist_ok=True)
    path = state / "settings.json"
    if path.exists():
        settings = load_settings(state)
        if settings.port != port:
            raise BrokerError(
                "state already has a different port; existing settings were preserved"
            )
        return settings
    settings = Settings(port=port, token=secrets.token_urlsafe(48))
    atomic_json(path, settings.model_dump(mode="json", by_alias=True))
    if os.name != "nt":
        path.chmod(0o600)
    return settings


def register(state: Path, manifest_path: Path, replace: bool = False) -> str:
    manifest = load_manifest(manifest_path)
    destination = state / "services" / f"{manifest.id}.json"
    with InstanceLock(state / "registration.lock"):
        if destination.exists() and not replace:
            raise BrokerError("service already registered; use --replace for an intentional update")
        atomic_json(destination, manifest.model_dump(mode="json", by_alias=True))
    return manifest.id


def configure_request_limit(state: Path, maximum_bytes: int) -> dict:
    # Coordinate both ordinary startup and direct `serve` without probing or
    # stopping a running broker. This command never touches app registrations.
    with InstanceLock(state / "startup.lock"), InstanceLock(state / "broker.lock"):
        settings = load_settings(state)
        replacement = Settings.model_validate(
            {
                **settings.model_dump(mode="json", by_alias=True),
                "max_request_body_bytes": maximum_bytes,
            }
        )
        atomic_json(state / "settings.json", replacement.model_dump(mode="json", by_alias=True))
    return {
        "max_request_body_bytes": replacement.max_request_body_bytes,
        "next": "start the broker; per-service argument budgets and app limits still apply",
    }


def probe(settings: Settings) -> dict | None:
    try:
        with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
            result = client.get(
                settings.url + "/health", headers={"Authorization": f"Bearer {settings.token}"}
            )
        if result.status_code != 200:
            raise BrokerError("configured port is occupied or broker credentials do not match")
        value = result.json()
        if value.get("name") != "universal-tools":
            raise BrokerError("configured port belongs to another service")
        return value
    except (httpx.ConnectError, httpx.TimeoutException):
        return None


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def spawn(state: Path, command: str):
    with (state / f"{command}.log").open("ab") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "universal_tools", "--state", str(state), command],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **process_options(),
        )


def ensure_running(state: Path, *, demo: bool = False) -> dict:
    load_settings(state)
    deadline = time.monotonic() + 35
    startup_lock = InstanceLock(state / "startup.lock")
    while True:
        try:
            startup_lock.__enter__()
            break
        except BrokerError:
            if time.monotonic() >= deadline:
                raise BrokerError("another client is still starting the broker") from None
            time.sleep(0.1)
    try:
        return _start_locked(state, demo=demo)
    finally:
        startup_lock.__exit__()


def _start_locked(state: Path, *, demo: bool) -> dict:
    settings = load_settings(state)
    health = probe(settings)
    if health:
        if demo:
            raise BrokerError("stop the running broker before starting the example stack")
        return health
    if demo and settings.port > 65533:
        raise BrokerError("the example stack needs two available ports above the broker port")
    # Bind check avoids spawning against another application that doesn't implement /health.
    ports = [settings.port, settings.port + 1, settings.port + 2] if demo else [settings.port]
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind((settings.host, port))
            except OSError:
                raise BrokerError(f"required loopback port {port} is occupied") from None
    child = spawn(state, "demo-stack" if demo else "serve")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        health = probe(settings)
        if health:
            return health
        if child.poll() is not None:
            # Another client may have won the singleton lock during simultaneous startup.
            health = probe(settings)
            if health:
                return health
            raise BrokerError("broker startup failed; inspect the local lifecycle log")
        time.sleep(0.1)
    raise BrokerError("broker startup timed out; inspect the local lifecycle log")


async def remote_operation(state: Path, name: str, arguments: dict | None = None):
    result = await RemoteBackend(load_settings(state)).invoke(name, arguments or {})
    if result.is_error:
        raise BrokerError(" ".join(item.text for item in result.content if item.type == "text"))
    return result.structured_content or result.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def stop(state: Path) -> dict:
    settings = load_settings(state)
    if not probe(settings):
        return {"stopped": True}
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.post(
            settings.url + "/shutdown", headers={"Authorization": f"Bearer {settings.token}"}
        )
        response.raise_for_status()
    for _ in range(100):
        if not probe(settings):
            return {"stopped": True}
        time.sleep(0.1)
    raise BrokerError("broker is still shutting down; no process was forcibly terminated")


def list_contexts(state: Path) -> dict:
    load_settings(state)
    fields = {"service_id", "context_id", "state", "reason", "expires_at", "updated_at"}
    return {
        "contexts": [
            {key: value for key, value in row.items() if key in fields}
            for row in OwnershipJournal(state / "ownership.sqlite3").entries()
        ],
        "note": "Recorded reservations do not prove native work is stopped or recoverable.",
    }


def recover_context(state: Path, service_id: str, context_id: str, *, acknowledged: bool) -> dict:
    load_settings(state)
    if not re.fullmatch(ID_PATTERN, service_id):
        raise BrokerError("invalid service_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", context_id):
        raise BrokerError("invalid context_id")
    if not acknowledged:
        raise BrokerError(
            "recovery requires --acknowledge-outcome after inspecting/reconciling app state"
        )
    # This is a local administrative operation, never a model-facing MCP tool.
    # The OS lock, not a health probe, proves no daemon owns this state during recovery.
    with InstanceLock(state / "broker.lock"):
        OwnershipJournal(state / "ownership.sqlite3").finish(
            service_id, context_id, "operator_reconciled", recovery=True
        )
    return {
        "service_id": service_id,
        "context_id": context_id,
        "recovered": True,
        "note": "Reservation cleared; no native operation, cancellation or rollback was performed.",
    }


async def wait_for_app(port: int):
    for _ in range(100):
        try:
            async with http_session(f"http://127.0.0.1:{port}/mcp", timeout=1):
                return
        except Exception:
            await asyncio.sleep(0.1)
    raise BrokerError("example app did not start")


def demo_stack(state: Path):
    settings = load_settings(state)
    children = []
    with InstanceLock(state / "demo-stack.lock"):
        if probe(settings):
            raise BrokerError("a broker is already running")
        try:
            for offset, kind in [(1, "math"), (2, "notes")]:
                service_id = f"example-{kind}"
                manifest_path = state / "services" / f"{service_id}.json"
                expected = {
                    "schema_version": 1,
                    "id": service_id,
                    "display_name": f"Example {kind}",
                    "description": "Isolated example app; no native engineering application",
                    "transport": {
                        "type": "streamable-http",
                        "url": f"http://127.0.0.1:{settings.port + offset}/mcp",
                    },
                    "allow_writes": kind == "notes",
                }
                if manifest_path.exists():
                    current = load_manifest(manifest_path)
                    if current.transport.model_dump(exclude={"max_sse_event_bytes"}) != {
                        **expected["transport"],
                        "bearer_token_env": None,
                    }:
                        raise BrokerError("example ID is already registered to another endpoint")
                else:
                    atomic_json(manifest_path, expected)
                children.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "universal_tools",
                            "--state",
                            str(state),
                            "demo",
                            "--kind",
                            kind,
                            "--port",
                            str(settings.port + offset),
                        ],
                        stdin=subprocess.DEVNULL,
                        **process_options(),
                    )
                )
                asyncio.run(wait_for_app(settings.port + offset))
            broker = subprocess.Popen(
                [sys.executable, "-m", "universal_tools", "--state", str(state), "serve"],
                stdin=subprocess.DEVNULL,
                **process_options(),
            )
            children.append(broker)
            broker.wait()
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)


def main(argv: list[str] | None = None):
    def skill_options(command):
        command.add_argument("--skill-package", type=Path, action="append", default=[])
        command.add_argument("--skill-profile", choices=["mcp", "openai-import"], default="mcp")
        command.add_argument("--skill-page-size", type=int, choices=range(1, 65), default=32)
        command.add_argument("--skill-directory-read", action="store_true")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.cwd() / ".local")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    skill_parser(commands)
    application = commands.add_parser("app-serve", help="run the shared framework for one app")
    application.add_argument("--publication", type=Path, required=True)
    skill_options(application)
    initialize_parser = commands.add_parser("init", help="create private local broker settings")
    initialize_parser.add_argument("--port", type=int, default=8768)
    request_limit = commands.add_parser(
        "configure-request-limit", help="set the HTTP body limit while the broker is stopped"
    )
    request_limit.add_argument("--bytes", type=int, required=True)
    for name in [
        "start",
        "serve",
        "status",
        "stop",
        "refresh",
        "list",
        "proxy",
        "start-demo",
        "demo-stack",
        "contexts",
    ]:
        command = commands.add_parser(name)
        if name in {"serve", "proxy"}:
            skill_options(command)
    registration = commands.add_parser("register", help="trust an app's local service manifest")
    registration.add_argument("manifest", type=Path)
    registration.add_argument("--replace", action="store_true")
    remove = commands.add_parser("unregister")
    remove.add_argument("service_id")
    configure = commands.add_parser("configure-plugin")
    configure.add_argument("plugin", type=Path)
    recovery = commands.add_parser(
        "recover-context", help="clear a reconciled context while stopped"
    )
    recovery.add_argument("service_id")
    recovery.add_argument("--context-id", required=True)
    recovery.add_argument("--acknowledge-outcome", action="store_true")
    demo = commands.add_parser("demo")
    demo.add_argument("--kind", choices=["math", "notes"], required=True)
    demo.add_argument("--port", type=int, required=True)
    demo.add_argument("--stdio", action="store_true")
    args = parser.parse_args(argv)
    state = args.state.resolve()
    try:
        skills = None
        if getattr(args, "skill_package", None):
            from .skill_catalog import SkillCatalog
            from .skill_manifest import MCP_PROFILE, OPENAI_IMPORT_PROFILE

            skills = SkillCatalog(
                args.skill_package,
                profile=OPENAI_IMPORT_PROFILE
                if args.skill_profile == "openai-import"
                else MCP_PROFILE,
                page_size=args.skill_page_size,
                directory_read=args.skill_directory_read,
            )
        match args.command:
            case "app-serve":
                from .framework import serve_application

                serve_application(state, args.publication, skills=skills)
                return
            case "skills":
                result = skill_command(state, args)
            case "init":
                settings = initialize(state, args.port)
                result = {"state": str(state), "url": settings.url, "version": __version__}
            case "configure-request-limit":
                result = configure_request_limit(state, args.bytes)
            case "start" | "start-demo":
                result = ensure_running(state, demo=args.command == "start-demo")
            case "serve":
                serve(state, skills=skills)
                return
            case "proxy":
                ensure_running(state)
                create_surface(RemoteBackend(load_settings(state)), skills=skills).run(
                    transport="stdio"
                )
                return
            case "demo-stack":
                demo_stack(state)
                return
            case "demo":
                app = create_demo(args.kind, args.port, state / "example-notes.json")
                app.run(
                    transport="stdio" if args.stdio else "streamable-http",
                    **({} if args.stdio else app.http_options),
                )
                return
            case "status":
                result = probe(load_settings(state)) or {"running": False}
            case "stop":
                result = stop(state)
            case "contexts":
                result = list_contexts(state)
            case "recover-context":
                result = recover_context(
                    state,
                    args.service_id,
                    args.context_id,
                    acknowledged=args.acknowledge_outcome,
                )
            case "register":
                load_settings(state)
                result = {
                    "registered": register(state, args.manifest, args.replace),
                    "next": "refresh_services or CLI refresh",
                }
            case "unregister":
                if not re.fullmatch(ID_PATTERN, args.service_id):
                    raise BrokerError("invalid service_id")
                (state / "services" / f"{args.service_id}.json").unlink(missing_ok=True)
                result = {"unregistered": args.service_id, "next": "refresh_services"}
            case "refresh" | "list":
                result = asyncio.run(
                    remote_operation(
                        state, "refresh_services" if args.command == "refresh" else "list_services"
                    )
                )
            case "configure-plugin":
                load_settings(state)
                plugin = args.plugin.resolve()
                if not (plugin / "plugin.json").is_file():
                    raise BrokerError("plugin.json is missing in the target plugin folder")
                atomic_json(
                    plugin / "runtime.json", {"python": sys.executable, "state": str(state)}
                )
                result = {"configured_plugin": str(plugin)}
        print(json.dumps(result, indent=2))
    except (BrokerError, ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        # Validation messages from third-party libraries may quote inputs; keep them local/generic.
        message = str(error) if isinstance(error, BrokerError) else type(error).__name__
        print(f"universal-tools: {message}", file=sys.stderr)
        raise SystemExit(2) from None
