"""Exercise the actual PowerShell wrappers with isolated state and a fake Codex command."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from universal_tools.cli import initialize
from universal_tools.models import atomic_json
from universal_tools.skill_bundles import SkillBundles

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_sync_install_and_corruption_refusal_through_real_shells(tmp_path, shell):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is not installed")
    state = tmp_path / "state"
    initialize(state, 8768)
    atomic_json(
        state / "services/workflow-example.json",
        {
            "id": "workflow-example",
            "display_name": "Never connected",
            "transport": {"type": "stdio", "command": sys.executable, "args": ["never-run.py"]},
        },
    )
    project = tmp_path / "project with spaces"
    shutil.copytree(
        ROOT / "plugins/universal-tools",
        project / "plugins/universal-tools",
        ignore=shutil.ignore_patterns("runtime.json", "__pycache__"),
    )
    (project / "scripts").mkdir()
    for name in ("Sync-Skills.ps1", "Install-Plugin.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, project / "scripts" / name)
    atomic_json(
        project / "plugins/universal-tools/runtime.json",
        {"python": sys.executable, "state": str(state)},
    )
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        """param([string]$Project, [string]$Manifest, [string]$Log, [switch]$InstallOnly)
$ErrorActionPreference = 'Stop'
$global:SkillSyncCalls = @()
function global:codex {
    $global:SkillSyncCalls += ,@($args)
    $global:SkillSyncCalls | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Log
    $global:LASTEXITCODE = 0
    Write-Output '{"fixture": true}'
}
if ($InstallOnly) { & (Join-Path $Project 'scripts\\Install-Plugin.ps1') }
else { & (Join-Path $Project 'scripts\\Sync-Skills.ps1') -BundleManifest $Manifest -Install }
""",
        encoding="utf-8",
    )
    log = tmp_path / "codex-calls.json"
    command = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(driver),
        "-Project",
        str(project),
        "-Manifest",
        str(ROOT / "examples/skill-bundles/text-tools/skill-bundle.json"),
        "-Log",
        str(log),
    ]
    first = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert first.returncode == 0, first.stdout + first.stderr
    calls = json.loads(log.read_text(encoding="utf-8-sig"))
    # Windows PowerShell 5 serializes nested argument arrays as value/Count objects.
    calls = [entry["value"] if isinstance(entry, dict) else entry for entry in calls]
    manager = SkillBundles(state)
    assert calls == [
        ["plugin", "marketplace", "add", str(manager.marketplace), "--json"],
        ["plugin", "add", "universal-tools@universal-tools-local", "--json"],
    ]
    release = manager.status()["active_release"]
    (
        manager.marketplace
        / "releases"
        / release
        / "plugins/universal-tools/skills/text-tools-inspect-text/SKILL.md"
    ).write_text("corrupt")
    log.unlink()
    failed = subprocess.run(command + ["-InstallOnly"], capture_output=True, text=True, timeout=90)
    assert failed.returncode != 0 and "Cannot verify" in failed.stderr
    assert not log.exists(), "verification failure must not fall back to a base install"
