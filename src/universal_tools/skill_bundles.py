"""Local, explicit native-skill packaging. Never called by the model-facing MCP surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from . import __version__
from .daemon import InstanceLock
from .models import (
    ID_PATTERN,
    BrokerError,
    StrictModel,
    atomic_json,
    canonical_digest,
    load_manifest,
    load_settings,
)

MAX_FILE = 4 * 1024 * 1024
MAX_PACKAGE = 32 * 1024 * 1024
MAX_COMPOSED = 64 * 1024 * 1024
NAME = r"^[a-z][a-z0-9-]{0,62}$"
HASH = r"^[0-9a-f]{64}$"
VERSION = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
MANIFEST = "skill-bundle.json"
MARKETPLACE = "universal-tools-local"


class BundleFile(StrictModel):
    path: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=HASH)
    executable: bool = False


class SkillBundle(StrictModel):
    schema_version: Literal[1]
    id: str = Field(pattern=NAME)
    version: str = Field(pattern=VERSION)
    route: Literal["universal-tools"]
    min_broker_version: str = Field(pattern=VERSION)
    license: str = Field(min_length=1, max_length=128)
    service_ids: list[Annotated[str, Field(pattern=ID_PATTERN)]] = Field(
        min_length=1, max_length=32
    )
    skills: list[Annotated[str, Field(pattern=NAME)]] = Field(min_length=1, max_length=32)
    files: list[BundleFile] = Field(min_length=2, max_length=256)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def unique(self):
        for values in (
            self.service_ids,
            self.skills,
            [entry.path.casefold() for entry in self.files],
        ):
            if len(set(values)) != len(values):
                raise ValueError("duplicate bundle entry")
        if any(not name.startswith(self.id + "-") for name in self.skills):
            raise ValueError("skill names must begin with the bundle ID and a hyphen")
        if sum(entry.size for entry in self.files) > MAX_PACKAGE:
            raise ValueError("bundle exceeds 32 MiB")
        return self


class Selection(StrictModel):
    schema_version: Literal[1] = 1
    bundles: dict[Annotated[str, Field(pattern=NAME)], Annotated[str, Field(pattern=HASH)]] = Field(
        default_factory=dict, max_length=64
    )


def _fail(message):
    raise BrokerError("skill bundles: " + message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)


def _raw(path, maximum=MAX_FILE):
    if path.stat().st_size > maximum:
        _fail("file exceeds its size budget")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        _fail("file exceeds its size budget")
    return raw


def _portable(name):
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"{p}{n}" for p in ("COM", "LPT") for n in range(10)]}
    if len(name) > 240 or any(
        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._ -]{0,99}", part)
        or part.endswith((".", " "))
        or part.split(".")[0].rstrip(" ").upper() in reserved
        for part in parts
    ):
        _fail("nonportable or escaping package path")
    return parts


def _plain(path):
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        _fail("symbolic links and reparse points are not package inputs")
    if not path.is_dir() and not path.is_file():
        _fail("package input is not an ordinary file or directory")


def _tree(root, maximum=MAX_PACKAGE):
    """Capture bytes once, rejecting links and case aliases on every platform."""
    _plain(root)
    if not root.is_dir():
        _fail("package root must be a directory")
    result, seen, total = {}, set(), 0
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            _plain(path)
            relative = path.relative_to(root).as_posix()
            _portable(relative)
            if relative.casefold() in seen:
                _fail("case-insensitive package path collision")
            seen.add(relative.casefold())
        for name in files:
            path = Path(directory) / name
            raw = _raw(path)
            total += len(raw)
            if total > maximum or len(result) >= 4096:
                _fail("package exceeds its aggregate budget")
            result[path.relative_to(root).as_posix()] = raw
    return result


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class _SkillLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            _fail("YAML aliases are not supported in skill metadata")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        pairs = [
            (self.construct_object(k, deep=deep), self.construct_object(v, deep=deep))
            for k, v in node.value
        ]
        if any(not isinstance(key, str) for key, _ in pairs):
            _fail("skill metadata keys must be strings")
        return _pairs(pairs)


def _yaml(text):
    try:
        result = yaml.load(text, Loader=_SkillLoader)
    except (yaml.YAMLError, RecursionError):
        _fail("invalid skill metadata")
    if not isinstance(result, dict):
        _fail("skill metadata must be an object")
    return result


def _skill(name, files):
    raw = files.get(f"skills/{name}/SKILL.md", b"")
    if len(raw) > 64 * 1024:
        _fail("SKILL.md exceeds 64 KiB")
    text = raw.decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.+)\Z", text, re.S)
    if not match:
        _fail("missing native skill frontmatter or instructions")
    metadata = _yaml(match[1])
    if metadata.get("name") != name or not isinstance(metadata.get("description"), str):
        _fail("skill name/description does not match its package")
    if not metadata["description"].strip() or len(metadata["description"]) > 1024:
        _fail("skill description is empty or too long")
    # This is an explicit route check, not semantic certification of skill instructions/scripts.
    for token in re.findall(r"mcp__([A-Za-z0-9_-]+?)__", text):
        if token != "universal_tools":
            _fail("skill still references a direct MCP namespace; publish a broker variant")
    agents = files.get(f"skills/{name}/agents/openai.yaml")
    if agents is not None:
        if len(agents) > 64 * 1024:
            _fail("skill agent metadata exceeds 64 KiB")
        value = _yaml(agents.decode("utf-8"))
        dependencies = value.get("dependencies", {})
        if not isinstance(dependencies, dict) or set(dependencies) - {"tools"}:
            _fail("unsupported skill dependencies")
        tools = dependencies.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict)
            or tool.get("type") != "mcp"
            or tool.get("value") != "universal-tools"
            or set(tool) - {"type", "value", "description"}
            for tool in tools
        ):
            _fail("skill dependencies must use the bundled universal-tools MCP server")


def _bundle(files):
    raw = files.get(MANIFEST, b"")
    if len(raw) > 256 * 1024:
        _fail("bundle manifest exceeds 256 KiB")
    bundle = SkillBundle.model_validate(_json(raw))
    if tuple(map(int, bundle.min_broker_version.split("."))) > tuple(
        map(int, __version__.split("."))
    ):
        _fail("bundle requires a newer broker")
    expected = {entry.path for entry in bundle.files}
    if set(files) != expected | {MANIFEST} or "LICENSE" not in expected:
        _fail("bundle inventory must be exact and include LICENSE")
    roots = {f"skills/{name}/" for name in bundle.skills}
    for entry in bundle.files:
        _portable(entry.path)
        if entry.path != "LICENSE" and not any(entry.path.startswith(root) for root in roots):
            _fail("bundle files must belong to their declared skills")
        if len(files[entry.path]) != entry.size or _digest(files[entry.path]) != entry.sha256:
            _fail("bundle content does not match its declared hash/size")
    for name in bundle.skills:
        _skill(name, files)
    return bundle


def _write_tree(destination, files, executable=()):
    destination.mkdir(parents=True, exist_ok=False)
    for name, raw in sorted(files.items()):
        path = destination.joinpath(*_portable(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if name in executable else 0o644)


def _inventory(files, executable=()):
    return [
        dict(path=name, size=len(raw), sha256=_digest(raw), executable=name in executable)
        for name, raw in sorted(files.items())
    ]


class SkillBundles:
    """Explicit selection, immutable sources/releases and one atomic catalog pointer."""

    def __init__(self, state):
        self.state = Path(state).resolve()
        self.root = self.state / "skill-bundles"
        self.marketplace = self.root / "marketplace"

    def _lock(self):
        return InstanceLock(self.root / "sync.lock")

    def _selection(self):
        path = self.root / "selection.json"
        return Selection.model_validate(_json(_raw(path))) if path.exists() else Selection()

    @staticmethod
    def inspect(manifest):
        files = SkillBundles._input(manifest)
        bundle = _bundle(files)
        return {"bundle": bundle.model_dump(), "manifest_sha256": _digest(files[MANIFEST])}

    @staticmethod
    def _input(manifest):
        manifest = Path(manifest).absolute()
        if manifest.name != MANIFEST:
            _fail("expected skill-bundle.json")
        return _tree(manifest.parent)

    def add(self, manifest, expected_sha256=None):
        files = self._input(manifest)
        bundle = _bundle(files)
        revision = _digest(files[MANIFEST])
        if expected_sha256 is not None and revision != expected_sha256:
            _fail("manifest changed after inspection")
        with self._lock():
            selection = self._selection()
            destination = self.root / "sources" / revision
            if destination.exists():
                if _tree(destination) != files:
                    _fail("stored source snapshot was modified")
            else:
                stage = self.root / "sources" / ("stage-" + uuid.uuid4().hex)
                _write_tree(
                    stage, files, {entry.path for entry in bundle.files if entry.executable}
                )
                stage.rename(destination)
            selection.bundles[bundle.id] = revision
            # Revalidate aggregate bounds before changing the selected bundle set.
            Selection.model_validate(selection.model_dump())
            atomic_json(self.root / "selection.json", selection.model_dump())
        return {"registered": bundle.id, "manifest_sha256": revision, "next": "skills sync"}

    def remove(self, bundle_id):
        with self._lock():
            selection = self._selection()
            if bundle_id not in selection.bundles:
                _fail("bundle is not selected")
            del selection.bundles[bundle_id]
            atomic_json(self.root / "selection.json", selection.model_dump())
        return {"removed": bundle_id, "next": "skills sync; previous releases remain available"}

    def _sources(self, selection):
        for name, revision in sorted(selection.bundles.items()):
            files = _tree(self.root / "sources" / revision)
            if _digest(files[MANIFEST]) != revision:
                _fail("stored manifest was modified")
            bundle = _bundle(files)
            if bundle.id != name:
                _fail("stored bundle identity mismatch")
            yield bundle, revision, files

    def list(self):
        with self._lock():
            return {
                "bundles": [
                    {"id": b.id, "version": b.version, "skills": b.skills, "manifest_sha256": r}
                    for b, r, _ in self._sources(self._selection())
                ]
            }

    def sync(self, base_plugin):
        with self._lock():
            load_settings(self.state)
            sources = self._sources(self._selection())
            files = _tree(Path(base_plugin).absolute())
            # A configured source runtime belongs to that checkout, never to the composed package.
            files.pop("runtime.json", None)
            plugin = _json(files["plugin.json"])
            mcp = _json(files["mcp.json"])
            if plugin.get("name") != "universal-tools" or plugin.get("version") != __version__:
                _fail("base plugin identity/version must match this broker")
            if set(mcp.get("mcpServers", {})) != {"universal-tools"}:
                _fail("base plugin must contain only the universal-tools MCP server")
            allowed = {"plugin.json", "mcp.json", "LICENSE"}
            if any(
                name not in allowed and not name.startswith(("server/", "skills/", "assets/"))
                for name in files
            ):
                _fail("unsupported base plugin component")
            files["plugin.json"] = (
                json.dumps(plugin, indent=2, ensure_ascii=False) + "\n"
            ).encode()
            files["runtime.json"] = (
                json.dumps({"python": sys.executable, "state": str(self.state)}, indent=2) + "\n"
            ).encode()
            bundles, executable = [], set()
            occupied = {name.casefold() for name in files}
            total = sum(map(len, files.values()))
            for bundle, revision, contents in sources:
                for service_id in bundle.service_ids:
                    registration = load_manifest(self.state / "services" / (service_id + ".json"))
                    if registration.id != service_id:
                        _fail("required service registration identity mismatch")
                bundles.append(
                    {
                        "id": bundle.id,
                        "version": bundle.version,
                        "license": bundle.license,
                        "manifest_sha256": revision,
                        "service_ids": bundle.service_ids,
                        "skills": bundle.skills,
                    }
                )
                for entry in bundle.files:
                    name = (
                        entry.path
                        if entry.path != "LICENSE"
                        else f"assets/bundle-licenses/{bundle.id}.txt"
                    )
                    if name.casefold() in occupied:
                        _fail("bundle collides with another bundle or base component")
                    total += len(contents[entry.path])
                    if total > MAX_COMPOSED or len(files) >= 4094:
                        _fail("composed plugin exceeds its aggregate budget")
                    occupied.add(name.casefold())
                    files[name] = contents[entry.path]
                    if entry.executable:
                        executable.add(name)
            if sum(map(len, files.values())) > MAX_COMPOSED:
                _fail("composed plugin exceeds 64 MiB")
            # The release identity binds the unmodified base manifest to avoid a digest cycle.
            recipe = {
                "schema_version": 1,
                "broker_version": __version__,
                "bundles": bundles,
                "files": _inventory(files, executable),
            }
            revision = canonical_digest(recipe)
            plugin["version"] = __version__ + "+skills." + revision[:16]
            files["plugin.json"] = (
                json.dumps(plugin, indent=2, ensure_ascii=False) + "\n"
            ).encode()
            receipt = {
                **recipe,
                "release_id": revision,
                "files": _inventory(files, executable),
                "recipe": recipe,
            }
            destination = self.marketplace / "releases" / revision
            if not destination.exists():
                stage = self.marketplace / "releases" / ("stage-" + uuid.uuid4().hex)
                _write_tree(stage / "plugins/universal-tools", files, executable)
                atomic_json(stage / "release.json", receipt)
                stage.rename(destination)
            self._release(revision)
            self._activate(revision)
            return self._status()

    def _release(self, revision):
        if not re.fullmatch(HASH, revision):
            _fail("invalid release ID")
        root = self.marketplace / "releases" / revision
        files = _tree(root, MAX_COMPOSED + MAX_FILE)
        receipt = _json(files.pop("release.json"))
        recipe = receipt["recipe"]
        if receipt["release_id"] != revision or canonical_digest(recipe) != revision:
            _fail("release receipt identity mismatch")
        prefix = "plugins/universal-tools/"
        if any(not name.startswith(prefix) for name in files):
            _fail("unexpected release file")
        plugin_files = {name[len(prefix) :]: raw for name, raw in files.items()}
        expected = receipt["files"]
        executable = {entry["path"] for entry in expected if entry["executable"]}
        if _inventory(plugin_files, executable) != expected:
            _fail("release files were modified")
        # Verify the identity recipe also binds every file after the version was synthesized.
        manifest = _json(plugin_files["plugin.json"])
        if manifest.get("version") != recipe["broker_version"] + "+skills." + revision[:16]:
            _fail("release plugin version mismatch")
        manifest["version"] = recipe["broker_version"]
        plugin_files["plugin.json"] = (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode()
        if _inventory(plugin_files, executable) != recipe["files"]:
            _fail("release recipe does not bind its contents")
        if receipt["bundles"] != recipe["bundles"]:
            _fail("release bundle provenance mismatch")
        if recipe["broker_version"] != __version__:
            _fail("release requires a different broker runtime; sync with the matching base plugin")
        return receipt

    def _activate(self, revision):
        atomic_json(self.marketplace / ".agents/plugins/marketplace.json", self._catalog(revision))

    @staticmethod
    def _catalog(revision):
        return {
            "name": MARKETPLACE,
            "interface": {"displayName": "Universal Tools Local"},
            "plugins": [
                {
                    "name": "universal-tools",
                    "source": {
                        "source": "local",
                        "path": f"./releases/{revision}/plugins/universal-tools",
                    },
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "Productivity",
                }
            ],
        }

    def _status(self):
        path = self.marketplace / ".agents/plugins/marketplace.json"
        result = {
            "marketplace_root": str(self.marketplace),
            "active_release": None,
            "restart_required": False,
        }
        selection = self._selection()
        if path.exists():
            value = _json(_raw(path))
            source = value["plugins"][0]["source"]["path"]
            match = re.fullmatch(r"\./releases/([0-9a-f]{64})/plugins/universal-tools", source)
            if value.get("name") != MARKETPLACE or not match:
                _fail("invalid generated marketplace pointer")
            if value != self._catalog(match[1]):
                _fail("generated marketplace metadata was modified")
            receipt = self._release(match[1])
            result.update(
                active_release=match[1], bundles=receipt["bundles"], restart_required=True
            )
            result["selection_pending"] = selection.bundles != {
                b["id"]: b["manifest_sha256"] for b in receipt["bundles"]
            }
        else:
            result["selection_pending"] = bool(selection.bundles)
        result["next"] = (
            "refresh/install universal-tools@universal-tools-local from marketplace_root; "
            "restart Codex and test a fresh chat"
        )
        result["host_activation_verified"] = False
        return result

    def status(self):
        with self._lock():
            return self._status()

    def rollback(self, revision):
        with self._lock():
            self._release(revision)
            self._activate(revision)
            return self._status()


def add_parser(commands):
    parser = commands.add_parser(
        "skills", help="manage native skill bundles locally; no app migration"
    )
    actions = parser.add_subparsers(dest="skill_action", required=True)
    register = actions.add_parser("add", help="explicitly trust and snapshot a broker skill bundle")
    register.add_argument("manifest", type=Path)
    register.add_argument("--expected-sha256", help="pin the manifest reviewed with skills inspect")
    inspect = actions.add_parser("inspect", help="validate and show a bundle without selecting it")
    inspect.add_argument("manifest", type=Path)
    remove = actions.add_parser("remove", help="deselect a bundle for the next sync")
    remove.add_argument("bundle_id")
    sync = actions.add_parser(
        "sync", help="compose and atomically select a local marketplace release"
    )
    sync.add_argument("--base-plugin", type=Path, required=True)
    rollback = actions.add_parser("rollback", help="select a verified previous release")
    rollback.add_argument("release_id")
    for name in ("list", "status", "schema"):
        actions.add_parser(name)


def execute(state, args):
    manager = SkillBundles(state)
    match args.skill_action:
        case "add":
            return manager.add(args.manifest, args.expected_sha256)
        case "inspect":
            return manager.inspect(args.manifest)
        case "remove":
            return manager.remove(args.bundle_id)
        case "sync":
            return manager.sync(args.base_plugin)
        case "rollback":
            return manager.rollback(args.release_id)
        case "list":
            return manager.list()
        case "status":
            return manager.status()
        case "schema":
            return SkillBundle.model_json_schema()
