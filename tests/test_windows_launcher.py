"""Exercise Windows launchers with stub installers only; never start an MCP host."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELLS = [shell for name in ("powershell.exe", "pwsh.exe") if (shell := shutil.which(name))]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launchers")


@pytest.fixture
def launcher(tmp_path):
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "InstallAndRun.cmd", project / "InstallAndRun.cmd")
    shutil.copyfile(ROOT / "scripts/InstallAndRun.ps1", scripts / "InstallAndRun.ps1")
    for target in ("Manager", "Broker"):
        (scripts / f"Install-{target}.ps1").write_text(
            """param(
                [string]$PythonVersion, [int]$Port,
                [string]$InstallDirectory, [string]$RegistryDirectory,
                [switch]$StartAtLogon, [switch]$InstallOnly, [switch]$NoShortcuts,
                [switch]$Reinstall, [switch]$ShowWindow, [switch]$NoPause
            )
            $PSBoundParameters | ConvertTo-Json | Set-Content -LiteralPath $env:LAUNCH_CAPTURE
            exit ([int]$env:LAUNCH_EXIT)
            """,
            encoding="utf-8",
        )
    env = {**os.environ, "LAUNCH_CAPTURE": str(tmp_path / "capture.json"), "LAUNCH_EXIT": "0"}
    return project, env


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("broker", [False, True])
def test_dispatch_parameters_from_another_directory(launcher, tmp_path, shell, broker):
    project, env = launcher
    options = (
        ["-Broker", "-Port", "9888"]
        if broker
        else ["-InstallDirectory", str(tmp_path / "manager app"), "-InstallOnly", "-Reinstall"]
    )
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "scripts/InstallAndRun.ps1"),
            "-NoPause",
            *options,
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    captured = json.loads(Path(env["LAUNCH_CAPTURE"]).read_text(encoding="utf-8-sig"))
    assert captured["NoPause"]["IsPresent"]
    assert captured["PythonVersion"] == "3.13"
    if broker:
        assert captured["Port"] == 9888
        assert "ShowWindow" not in captured
    else:
        assert captured["ShowWindow"]["IsPresent"]
        assert captured["Reinstall"]["IsPresent"]
        assert captured["InstallDirectory"] == str(tmp_path / "manager app")


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("options", [["-Port", "9888"], ["-Broker", "-InstallOnly"]])
def test_incompatible_options_never_invoke_installer(launcher, tmp_path, shell, options):
    project, env = launcher
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "scripts/InstallAndRun.ps1"),
            "-NoPause",
            *options,
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not Path(env["LAUNCH_CAPTURE"]).exists()


@pytest.mark.parametrize("broker", [False, True])
def test_root_cmd_preserves_installer_exit_code(launcher, tmp_path, broker):
    project, env = launcher
    env["LAUNCH_EXIT"] = "23"
    options = " -Broker" if broker else ""
    command = (
        f'"{os.environ["COMSPEC"]}" /d /s /c ""{project / "InstallAndRun.cmd"}" -NoPause{options}"'
    )
    result = subprocess.run(
        command,
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 23, result.stderr
    assert Path(env["LAUNCH_CAPTURE"]).is_file()


@pytest.mark.parametrize("shell", SHELLS)
def test_existing_manager_install_is_reused_without_launch_or_reinstall(tmp_path, shell):
    project = tmp_path / "source project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/Install-Manager.ps1", scripts / "Install-Manager.ps1")
    shutil.copyfile(ROOT / "pyproject.toml", project / "pyproject.toml")
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    app = tmp_path / "manager app"
    python = app / "runtimes/retained/Scripts/python.cmd"
    python.parent.mkdir(parents=True)
    python.write_text(
        '@echo off\nif not "%~4"=="doctor" exit /b 92\n'
        f'echo {{"version":"{version}","windows":true,"tray_assets_present":true}}\n'
        "exit /b 0\n"
    )
    registry = tmp_path / "private registry"
    registry.mkdir()
    pointer = app / "current.json"
    original = json.dumps(
        {"schema_version": 1, "python": str(python), "registry": str(registry), "version": version}
    )
    pointer.write_text(original)
    # A running manager's lock must not force reinstall of the same verified version.
    with (registry / "manager.lock").open("w"):
        result = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "Install-Manager.ps1"),
                "-InstallDirectory",
                str(app),
                "-RegistryDirectory",
                str(registry),
                "-InstallOnly",
                "-NoShortcuts",
                "-NoPause",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout
    assert pointer.read_text() == original
    assert list((app / "runtimes").iterdir()) == [app / "runtimes/retained"]
