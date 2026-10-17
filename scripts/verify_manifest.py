"""Verify source file hashes and exact archive membership, excluding local runtime state."""

import hashlib
import json
import os
from pathlib import Path

EXCLUDED = {
    ".git",
    ".local",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}


def source_files(root):
    paths = []
    for directory, directories, filenames in os.walk(root):
        directories[:] = [
            name for name in directories if name not in EXCLUDED and not name.endswith(".egg-info")
        ]
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(root)
            if (
                path.is_symlink()
                or path.suffix == ".pyc"
                or relative.as_posix() in {"manifest.json", "plugins/universal-tools/runtime.json"}
            ):
                continue
            paths.append(path)
    yield from sorted(paths)


def verify(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual = {path.relative_to(root).as_posix(): path for path in source_files(root)}
    errors = []
    if expected.keys() != actual.keys():
        errors.append(
            {
                "missing": sorted(expected.keys() - actual.keys()),
                "extra": sorted(actual.keys() - expected.keys()),
            }
        )
    for name in expected.keys() & actual.keys():
        raw = actual[name].read_bytes()
        if (
            len(raw) != expected[name]["size"]
            or hashlib.sha256(raw).hexdigest() != expected[name]["sha256"]
        ):
            errors.append({"mismatch": name})
    result = {"ok": not errors, "files": len(expected), "errors": errors}
    return result


if __name__ == "__main__":
    result = verify(Path(__file__).resolve().parents[1])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
