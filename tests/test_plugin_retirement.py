"""The shared framework/manager must not install a universal Codex plugin."""

from pathlib import Path

import pytest

from universal_tools.cli import main


@pytest.mark.parametrize("command", [["configure-plugin", "unused"], ["skills", "status"]])
def test_retired_commands_cannot_write_plugin_state(tmp_path, command):
    state = tmp_path / "unused-state"
    with pytest.raises(SystemExit) as error:
        main(["--state", str(state), *command])
    assert error.value.code == 2
    assert not state.exists()


def test_no_universal_plugin_or_installer_remains_in_public_source():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "plugins/universal-tools",
        ".agents/plugins/marketplace.json",
        "scripts/Install-Plugin.ps1",
        "scripts/Install-Broker.ps1",
        "scripts/Sync-Skills.ps1",
        "src/universal_tools/skill_bundles.py",
    ):
        assert not (root / relative).exists(), relative
    assert (root / "assets/icon.ico").is_file()
    assert (root / "src/universal_tools/manager_assets/icon.ico").read_bytes() == (
        root / "assets/icon.ico"
    ).read_bytes()
