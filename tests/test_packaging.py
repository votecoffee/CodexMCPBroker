"""A Git checkout and its local runtime must never become release contents."""

import runpy
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
