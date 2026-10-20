"""Record tested installed dependencies without embedding editable/source paths."""

import importlib.metadata
from pathlib import Path

from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1]


def closure(initial):
    queue = [(name, frozenset()) for name in initial]
    visited = set()
    versions = {}
    while queue:
        name, extras = queue.pop()
        key = (name.lower().replace("_", "-"), extras)
        if key in visited:
            continue
        visited.add(key)
        dist = importlib.metadata.distribution(name)
        versions[dist.metadata["Name"]] = dist.version
        for value in dist.requires or []:
            requirement = Requirement(value)
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in ["", *extras]
            ):
                continue
            queue.append((requirement.name, frozenset(requirement.extras)))
    return versions


for filename, packages in [
    (
        "requirements-lock.txt",
        ["mcp", "jsonschema", "anyio", "PyYAML", "httpx", "opentelemetry-api"],
    ),
    (
        "requirements-dev-lock.txt",
        [
            "mcp",
            "jsonschema",
            "anyio",
            "PyYAML",
            "httpx",
            "opentelemetry-api",
            "pytest",
            "pytest-asyncio",
            "ruff",
            "build",
        ],
    ),
]:
    versions = closure(packages)
    lines = ["# Tested Windows / Python 3.13 dependency snapshot. No credentials or local paths."]
    for name, version in sorted(versions.items(), key=lambda item: item[0].lower()):
        marker = '; sys_platform == "win32"' if name.lower() == "pywin32" else ""
        lines.append(f"{name}=={version}{marker}")
    (root / filename).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

print("Wrote runtime and development dependency locks.")
