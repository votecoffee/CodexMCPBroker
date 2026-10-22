"""Stamp one aware release instant into source metadata, or check an existing stamp."""

import argparse
import json
import re
import runpy
import tomllib
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(root / "src/universal_tools/versioning.py"))
parser = argparse.ArgumentParser(description=__doc__)
choice = parser.add_mutually_exclusive_group(required=True)
choice.add_argument("--at", help="fixed release instant, ISO 8601 with explicit time-zone offset")
choice.add_argument("--check", action="store_true", help="verify metadata without changing it")
args = parser.parse_args()
metadata = root / "pyproject.toml"
module = root / "src/universal_tools/__init__.py"
project_source, module_source = metadata.read_text("utf-8"), module.read_text("utf-8")
previous = tomllib.loads(project_source)["project"]["version"]
try:
    version = helpers["timestamp_version"](datetime.fromisoformat(args.at)) if args.at else previous
    helpers["validate_timestamp_version"](version)
    if args.at:
        if re.fullmatch(r"[0-9]{2}\.[0-9]{4}\.[0-9]{6}", previous) and version < previous:
            raise ValueError("release time predates the current stamp; use a later release instant")
        project_source, project_count = re.subn(
            r'^version = "[^"\r\n]+"$',
            f'version = "{version}"',
            project_source,
            count=1,
            flags=re.M,
        )
        module_source, module_count = re.subn(
            r'^__version__ = "[^"\r\n]+"$',
            f'__version__ = "{version}"',
            module_source,
            count=1,
            flags=re.M,
        )
        if project_count != 1 or module_count != 1:
            raise ValueError("source version declaration is missing or unsupported")
        metadata.write_text(project_source, encoding="utf-8", newline="\n")
        module.write_text(module_source, encoding="utf-8", newline="\n")
    else:
        declaration = re.search(r'^__version__ = "([^"\r\n]+)"$', module_source, flags=re.M)
        if declaration is None or declaration[1] != version:
            raise ValueError("source module and package metadata versions differ")
except ValueError as error:
    parser.error(str(error))
print(
    json.dumps(
        {
            "version": version,
            "package_version": helpers["package_version"](version),
            "file_version": helpers["file_version"](version),
        }
    )
)
