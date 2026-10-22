"""A Git checkout and its local runtime must never become release contents."""

import runpy
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_source_inventory_excludes_git_metadata_runtime_and_secrets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source_files = runpy.run_path(str(root / "scripts/verify_manifest.py"))["source_files"]
    for relative in [
        ".git/config",
        ".local/settings.json",
        ".local/runtime/secrets.txt",
        ".venv/pyvenv.cfg",
        "dist/previous.zip",
        "build/lib/module.py",
        "plugins/universal-tools/runtime.json",
        "__pycache__/module.pyc",
        "README.md",
        "LICENSE",
        "src/module.py",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("example", encoding="utf-8")
    assert {path.relative_to(tmp_path).as_posix() for path in source_files(tmp_path)} == {
        "README.md",
        "LICENSE",
        "src/module.py",
    }


def packaging_fixture(tmp_path):
    root = tmp_path / "source project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    original = Path(__file__).resolve().parents[1]
    for name in ("package_release.py", "verify_manifest.py"):
        shutil.copyfile(original / "scripts" / name, scripts / name)
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    (root / "LICENSE").write_text("synthetic license")
    (root / "dist").mkdir()
    (root / "dist" / "old.whl").write_bytes(b"preserve these old release bytes")
    return root


def test_clean_release_preserves_old_dist_and_excludes_archive(tmp_path):
    root = packaging_fixture(tmp_path)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/package_release.py"), "--clean"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    previous = list((root / ".local/release-archive").glob("*/dist/old.whl"))
    assert len(previous) == 1
    assert previous[0].read_bytes() == b"preserve these old release bytes"
    assert "archived_previous_dist" in result.stdout
    assert {p.name for p in (root / "dist").iterdir()} == {
        "CodexMCPBroker-1.2.3.zip",
        "CodexMCPBroker-1.2.3.zip.sha256",
    }
    with zipfile.ZipFile(root / "dist/CodexMCPBroker-1.2.3.zip") as archive:
        assert "CodexMCPBroker/LICENSE" in archive.namelist()
        assert not any(".local" in p or "/dist/" in p for p in archive.namelist())


def test_clean_refuses_an_external_output_without_moving_existing_files(tmp_path):
    root = packaging_fixture(tmp_path)
    external = tmp_path / "other releases"
    external.mkdir()
    (external / "keep.txt").write_text("untouched")
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/package_release.py"),
            "--clean",
            "--output-dir",
            str(external),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "ordinary project dist" in result.stderr
    assert (external / "keep.txt").read_text() == "untouched"
    assert (root / "dist/old.whl").is_file()
    assert not (root / ".local").exists()
