"""Create a clean source ZIP and SHA-256 inventory; never include runtime state."""

import argparse
import hashlib
import json
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from verify_manifest import source_files, verify

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", type=Path, default=root / "dist")
parser.add_argument(
    "--clean",
    action="store_true",
    help="archive the existing default dist under ignored .local before packaging",
)
args = parser.parse_args()
output_dir = args.output_dir.resolve()
if output_dir.is_relative_to(root) and not output_dir.is_relative_to(root / "dist"):
    parser.error("output inside the source checkout must be under dist/")
if args.clean:
    intended = root / "dist"
    local = root / ".local"
    if (
        output_dir != intended
        or intended.is_symlink()
        or getattr(intended, "is_junction", lambda: False)()
    ):
        parser.error("--clean requires the ordinary project dist directory")
    if not local.resolve().is_relative_to(root):
        parser.error("local archive must stay inside the project")
    if output_dir.exists() and any(output_dir.iterdir()):
        archive_root = (
            local
            / "release-archive"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        )
        if not archive_root.resolve().is_relative_to(local.resolve()):
            parser.error("invalid archive location")
        archive_root.mkdir(parents=True)
        output_dir.rename(archive_root / "dist")
        print(json.dumps({"archived_previous_dist": str(archive_root / "dist")}))
output_dir.mkdir(parents=True, exist_ok=True)
entries = []
for path in source_files(root):
    raw = path.read_bytes()
    entries.append(
        {
            "path": path.relative_to(root).as_posix(),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
manifest = {"name": "CodexMCPBroker", "version": version, "files": entries}
(root / "manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
)
assert verify(root)["ok"]
archive = output_dir / f"CodexMCPBroker-{version}.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
    for path in [*source_files(root), root / "manifest.json"]:
        bundle.write(path, arcname="CodexMCPBroker/" + path.relative_to(root).as_posix())
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
print(json.dumps({"archive": str(archive), "files": len(entries), "sha256": digest}, indent=2))
