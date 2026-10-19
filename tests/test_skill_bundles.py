"""Native package composition stays local, hash-bound, reversible and independent of apps."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from universal_tools import __version__
from universal_tools.cli import initialize
from universal_tools.daemon import InstanceLock
from universal_tools.models import BrokerError, atomic_json
from universal_tools.skill_bundles import SkillBundle, SkillBundles

ROOT = Path(__file__).resolve().parents[1]


def write_bundle(root, bundle_id="text-tools", text="First guidance.", skill=None):
    name = skill or bundle_id + "-inspect"
    directory = root / bundle_id
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        f"skills/{name}/SKILL.md": (
            f"---\nname: {name}\ndescription: Inspect supplied text through Universal Tools.\n---\n"
            + text
            + "\n"
        ).encode(),
        f"skills/{name}/references/limits.md": "Unicode café 深 μ\n".encode(),
        f"skills/{name}/assets/template.bin": bytes(range(256)),
        f"skills/{name}/scripts/digest.py": b"print('not run by the synchronizer')\n",
        f"skills/{name}/agents/openai.yaml": (
            b"policy:\n  allow_implicit_invocation: false\n"
            b"dependencies:\n  tools:\n    - type: mcp\n      value: universal-tools\n"
        ),
        "LICENSE": b"MIT fixture license\n",
    }
    for name, raw in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest = directory / "skill-bundle.json"
    atomic_json(
        manifest,
        {
            "schema_version": 1,
            "id": bundle_id,
            "version": "1.0.0",
            "route": "universal-tools",
            "min_broker_version": __version__,
            "license": "MIT",
            "service_ids": ["workflow-example"],
            "skills": [skill or bundle_id + "-inspect"],
            "files": [
                dict(
                    path=name,
                    size=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    executable=name.endswith(".py"),
                )
                for name, raw in sorted(files.items())
            ],
        },
    )
    return manifest


def alter(manifest, change):
    value = json.loads(manifest.read_text(encoding="utf-8"))
    change(value)
    atomic_json(manifest, value)


def rehash(manifest):
    def update(value):
        for entry in value["files"]:
            raw = (manifest.parent / entry["path"]).read_bytes()
            entry.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    alter(manifest, update)


@pytest.fixture
def profile(tmp_path):
    state = tmp_path / "state"
    initialize(state, 8768)
    atomic_json(
        state / "services/workflow-example.json",
        {
            "id": "workflow-example",
            "display_name": "Never connected",
            "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["never-run-this-fixture"],
            },
        },
    )
    base = tmp_path / "base"
    shutil.copytree(
        ROOT / "plugins/universal-tools",
        base,
        ignore=shutil.ignore_patterns("runtime.json", "__pycache__"),
    )
    manager = SkillBundles(state)
    manifest = write_bundle(tmp_path / "inputs")
    return manager, base, manifest


def package(manager, release):
    return manager.marketplace / "releases" / release / "plugins/universal-tools"


def catalog(manager):
    return manager.marketplace / ".agents/plugins/marketplace.json"


def test_two_apps_update_remove_and_rollback_preserve_resources_and_normal_state(profile, tmp_path):
    manager, base, manifest = profile
    state_bytes = {
        path: path.read_bytes()
        for path in [
            manager.state / "settings.json",
            manager.state / "services/workflow-example.json",
        ]
    }
    initial = manager.inspect(manifest)
    assert not manager.root.exists()
    manager.add(manifest, initial["manifest_sha256"])
    another = write_bundle(tmp_path / "other-input", "other-app")
    manager.add(another)
    first = manager.sync(base)
    assert len(first["bundles"]) == 2
    assert first["selection_pending"] is False and first["host_activation_verified"] is False
    destination = package(manager, first["active_release"])
    for entry in initial["bundle"]["files"]:
        target = (
            entry["path"] if entry["path"] != "LICENSE" else "assets/bundle-licenses/text-tools.txt"
        )
        assert (destination / target).read_bytes() == (manifest.parent / entry["path"]).read_bytes()
    assert json.loads((destination / "runtime.json").read_text())["state"] == str(manager.state)
    assert set(json.loads((destination / "mcp.json").read_text())["mcpServers"]) == {
        "universal-tools"
    }
    assert manager.sync(base)["active_release"] == first["active_release"]
    # An app changing its source does not mutate a trusted snapshot or active plugin.
    write_bundle(manifest.parent.parent, text="Second guidance.")
    assert manager.sync(base)["active_release"] == first["active_release"]
    manager.add(manifest)
    assert manager.status()["selection_pending"] is True
    second = manager.sync(base)
    assert second["active_release"] != first["active_release"]
    manager.remove("text-tools")
    third = manager.sync(base)
    assert not (package(manager, third["active_release"]) / "skills/text-tools-inspect").exists()
    restored = manager.rollback(first["active_release"])
    assert restored["active_release"] == first["active_release"]
    assert restored["selection_pending"] is True  # rollback leaves the desired selection unchanged
    assert all(path.read_bytes() == raw for path, raw in state_bytes.items())
    assert not (base / "skills/text-tools-inspect").exists()


@pytest.mark.parametrize(
    "corruption",
    [
        "hash",
        "size",
        "extra",
        "route",
        "future",
        "unknown",
        "collision",
        "traversal",
        "ads",
        "case",
        "license",
        "name",
        "schema",
    ],
)
def test_invalid_bundles_never_change_selection_or_active_package(profile, corruption):
    manager, base, manifest = profile
    manager.add(manifest)
    manager.sync(base)
    before = catalog(manager).read_bytes(), (manager.root / "selection.json").read_bytes()
    match corruption:
        case "hash":
            alter(manifest, lambda v: v["files"][0].update(sha256="0" * 64))
        case "size":
            alter(manifest, lambda v: v["files"][0].update(size=999))
        case "extra":
            (manifest.parent / "secret.txt").write_text("not in the explicit inventory")
        case "route":
            alter(manifest, lambda v: v.update(route="standalone"))
        case "future":
            alter(manifest, lambda v: v.update(min_broker_version="999.0.0"))
        case "unknown":
            alter(manifest, lambda v: v.update(install_hook="never run"))
        case "collision":
            alter(manifest, lambda v: v["skills"].append(v["skills"][0]))
        case "traversal":
            alter(manifest, lambda v: v["files"][0].update(path="../outside.txt"))
        case "ads":
            alter(manifest, lambda v: v["files"][0].update(path="LICENSE:stream"))
        case "case":
            alter(manifest, lambda v: v["files"].append({**v["files"][0], "path": "license"}))
        case "license":
            (manifest.parent / "LICENSE").unlink()
            alter(
                manifest,
                lambda v: v.update(files=[e for e in v["files"] if e["path"] != "LICENSE"]),
            )
        case "name":
            skill = manifest.parent / "skills/text-tools-inspect/SKILL.md"
            skill.write_text(skill.read_text().replace("name: text-tools-inspect", "name: wrong"))
            rehash(manifest)
        case "schema":
            alter(manifest, lambda v: v.update(schema_version=2))
    with pytest.raises((ValueError, OSError)):
        manager.add(manifest)
    assert before == (catalog(manager).read_bytes(), (manager.root / "selection.json").read_bytes())


@pytest.mark.parametrize(
    "content",
    [
        "---\nname: text-tools-inspect\nname: text-tools-inspect\n"
        "description: duplicate\n---\nBody\n",
        "---\nname: text-tools-inspect\ndescription: &a [*a]\n---\nBody\n",
        "---\nname: text-tools-inspect\n"
        "description: !!python/object:builtins.object {}\n---\nBody\n",
        "---\nname: text-tools-inspect\ndescription: direct\n---\nCall mcp__other_app__write.\n",
        "---\nname: text-tools-inspect\ndescription: 5\n---\nBody\n",
    ],
)
def test_native_skill_metadata_and_direct_namespaces_are_checked(profile, content):
    manager, _, manifest = profile
    (manifest.parent / "skills/text-tools-inspect/SKILL.md").write_text(content, encoding="utf-8")
    rehash(manifest)
    with pytest.raises(ValueError):
        manager.add(manifest)
    assert not (manager.root / "selection.json").exists()


def test_direct_mcp_dependency_rejected_without_rewriting_policy(profile):
    manager, _, manifest = profile
    agents = manifest.parent / "skills/text-tools-inspect/agents/openai.yaml"
    agents.write_text(agents.read_text().replace("value: universal-tools", "value: direct-app"))
    rehash(manifest)
    with pytest.raises(BrokerError, match="dependencies"):
        manager.add(manifest)


def test_manifest_pin_and_stored_source_corruption_fail_closed(profile):
    manager, base, manifest = profile
    reviewed = manager.inspect(manifest)["manifest_sha256"]
    write_bundle(manifest.parent.parent, text="Changed after inspection.")
    with pytest.raises(BrokerError, match="changed after inspection"):
        manager.add(manifest, reviewed)
    result = manager.add(manifest)
    stored = manager.root / "sources" / result["manifest_sha256"] / "LICENSE"
    stored.write_text("changed stored source")
    with pytest.raises(BrokerError, match="hash/size"):
        manager.sync(base)
    assert not catalog(manager).exists()


def test_missing_service_and_cross_bundle_name_collision_preserve_release(profile, tmp_path):
    manager, base, manifest = profile
    manager.add(manifest)
    first = manager.sync(base)
    pointer = catalog(manager).read_bytes()
    registration = manager.state / "services/workflow-example.json"
    saved = registration.read_bytes()
    registration.unlink()
    with pytest.raises(OSError):
        manager.sync(base)
    registration.write_bytes(saved)
    assert catalog(manager).read_bytes() == pointer
    collision = write_bundle(tmp_path / "collision", "text", skill="text-tools-inspect")
    manager.add(collision)
    with pytest.raises(BrokerError, match="collides"):
        manager.sync(base)
    assert manager.status()["active_release"] == first["active_release"]


@pytest.mark.parametrize("target", ["plugin", "receipt", "extra", "pointer"])
def test_tampered_release_or_marketplace_is_never_silently_installed(profile, target):
    manager, base, manifest = profile
    manager.add(manifest)
    first = manager.sync(base)
    destination = package(manager, first["active_release"])
    match target:
        case "plugin":
            atomic_json(destination / "plugin.json", {"name": "another-plugin", "version": "0"})
        case "receipt":
            path = destination.parents[1] / "release.json"
            value = json.loads(path.read_text())
            value["bundles"][0]["version"] = "9.0.0"
            atomic_json(path, value)
        case "extra":
            (destination / "extra.txt").write_text("not in the release")
        case "pointer":
            value = json.loads(catalog(manager).read_text())
            value["plugins"].append(value["plugins"][0])
            atomic_json(catalog(manager), value)
    with pytest.raises(BrokerError):
        manager.status()


def test_publish_failure_keeps_old_pointer_and_next_sync_recovers(profile, monkeypatch):
    import universal_tools.skill_bundles as module

    manager, base, manifest = profile
    manager.add(manifest)
    original = manager.sync(base)
    pointer = catalog(manager).read_bytes()
    write_bundle(manifest.parent.parent, text="New version")
    manager.add(manifest)
    real = module.atomic_json

    def interrupted(path, value):
        if path == catalog(manager):
            raise OSError("simulated crash immediately before atomic pointer replacement")
        return real(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_json", interrupted)
        with pytest.raises(OSError):
            manager.sync(base)
    assert catalog(manager).read_bytes() == pointer
    assert manager.status()["active_release"] == original["active_release"]
    recovered = manager.sync(base)
    assert recovered["active_release"] != original["active_release"]


def test_competing_admin_is_excluded_by_os_lock(profile):
    manager, _, manifest = profile
    with InstanceLock(manager.root / "sync.lock"):
        with pytest.raises(BrokerError):
            manager.add(manifest)
    assert not (manager.root / "selection.json").exists()


def test_oversize_resource_and_boolean_schema_are_rejected(profile):
    manager, _, manifest = profile
    alter(manifest, lambda v: v.update(schema_version=True))
    with pytest.raises(ValueError):
        manager.add(manifest)
    alter(manifest, lambda v: v.update(schema_version=1))
    (manifest.parent / "skills/text-tools-inspect/assets/template.bin").write_bytes(
        b"x" * (4 * 1024 * 1024 + 1)
    )
    with pytest.raises(BrokerError, match="size budget"):
        manager.add(manifest)


def test_directory_links_cannot_import_files_outside_the_bundle(profile, tmp_path):
    manager, _, manifest = profile
    external = tmp_path / "outside"
    external.mkdir()
    (external / "private.txt").write_bytes(b"must never be copied")
    link = manifest.parent / "skills/text-tools-inspect/external"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(external, target_is_directory=True)
    with pytest.raises(BrokerError, match="reparse points"):
        manager.add(manifest)
    assert (external / "private.txt").read_bytes() == b"must never be copied"


def test_resource_spaces_and_python_package_files_are_preserved(profile):
    manager, base, manifest = profile
    value = json.loads(manifest.read_text())
    for name in ("scripts/__init__.py", "assets/sample template.txt"):
        name = "skills/text-tools-inspect/" + name
        raw = b"resource bytes\n"
        path = manifest.parent / name
        path.write_bytes(raw)
        value["files"].append(
            dict(path=name, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        )
    atomic_json(manifest, value)
    manager.add(manifest)
    release = manager.sync(base)["active_release"]
    for name in ("scripts/__init__.py", "assets/sample template.txt"):
        assert (
            package(manager, release) / "skills/text-tools-inspect" / name
        ).read_bytes() == b"resource bytes\n"


def test_public_bundle_schema_and_cli_smoke_use_only_isolated_state(profile):
    manager, base, _ = profile
    schema = json.loads((ROOT / "examples/skill-bundle.schema.json").read_text())
    assert schema == SkillBundle.model_json_schema()
    example = ROOT / "examples/skill-bundles/text-tools/skill-bundle.json"
    manager.inspect(example)
    command = [sys.executable, "-m", "universal_tools", "--state", str(manager.state), "skills"]
    for arguments in (["add", str(example)], ["sync", "--base-plugin", str(base)], ["status"]):
        completed = subprocess.run(command + arguments, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        assert isinstance(json.loads(completed.stdout), dict)
    bad = subprocess.run(
        command + ["rollback", "../escape"], capture_output=True, text=True, timeout=30
    )
    assert bad.returncode == 2 and "Traceback" not in bad.stderr
