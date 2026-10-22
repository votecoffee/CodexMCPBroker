"""Calendar boundaries and package-padding aliases must not invent manager updates."""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from universal_tools.manager_inventory import (
    newer_numeric_version,
    parse_inventory,
    present_snapshot,
)
from universal_tools.versioning import (
    file_version,
    package_version,
    timestamp_version,
    validate_timestamp_version,
    versions_equal,
)


@pytest.mark.parametrize(
    "instant,label,package,assembly",
    [
        ("2026-10-22T17:36:24-04:00", "26.1022.173624", "26.1022.173624", "26.1022.1736.24"),
        ("2026-01-01T00:01:02-05:00", "26.0101.000102", "26.101.102", "26.101.1.2"),
        ("2028-02-29T23:59:59-05:00", "28.0229.235959", "28.229.235959", "28.229.2359.59"),
    ],
)
def test_aware_release_has_fixed_display_and_bounded_platform_identities(
    instant, label, package, assembly
):
    assert timestamp_version(datetime.fromisoformat(instant)) == label
    assert package_version(label) == package
    assert file_version(label) == assembly
    assert all(int(part) <= 65535 for part in assembly.split("."))
    assert versions_equal(label, package)


@pytest.mark.parametrize("value", ["26.0230.000000", "26.1022.240000", "26.101.1", "bad"])
def test_invalid_calendar_or_unpadded_display_is_rejected(value):
    with pytest.raises(ValueError):
        validate_timestamp_version(value)


def test_release_time_cannot_silently_use_host_zone_or_ambiguous_century():
    for instant in (datetime(2026, 1, 1), datetime.fromisoformat("2100-01-01T00:00:00+00:00")):
        with pytest.raises(ValueError):
            timestamp_version(instant)


def test_package_alias_does_not_invent_updates_or_source_drift(tmp_path):
    assert newer_numeric_version("26.0102.000001", "26.101.235959")
    assert newer_numeric_version("27.0101.000000", "26.1231.235959")
    assert not newer_numeric_version("26.0101.000102", "26.101.102")
    assert not versions_equal("custom-a", "custom-b")
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "plugin.json").write_text('{"version":"26.0101.000102"}')
    rows = parse_inventory(
        {
            "installed": [
                {
                    "pluginId": "example@local",
                    "name": "Example",
                    "version": "26.101.102",
                    "installed": True,
                    "enabled": True,
                    "source": {"source": "local", "path": str(source)},
                }
            ]
        }
    )
    assert not rows[0]["update_pending"] and not rows[0]["source_differs"]


def test_manager_padding_aliases_do_not_request_restart_or_report_mixed():
    services = []
    for number, running in enumerate(("26.0101.000102", "26.101.102")):
        services.append(
            {
                "app_id": "example",
                "display_name": "Example",
                "plugin_id": "example@local",
                "instance_id": str(number),
                "pid": 100 + number,
                "started_at": number,
                "revision": "r" + str(number),
                "status": {
                    "state": "running",
                    "message": "",
                    "versions": {
                        "app": {
                            "installed": "26.101.102",
                            "running": running,
                            "available": None,
                            "update_pending": False,
                        }
                    },
                    "active_operations": 0,
                    "restart_allowed": False,
                    "restart_reason": "Synthetic",
                },
            }
        )
    ui = present_snapshot({"services": services, "warnings": [], "updated_at": 1}, [])
    assert all(row["update"] == "" for row in ui["rows"])
    assert ui["applications"][0]["running"] == "26.0101.000102"


def test_stamp_is_fixed_and_rejects_backwards_releases(tmp_path):
    source = Path(__file__).resolve().parents[1]
    for name in ["scripts/stamp_version.py", "src/universal_tools/versioning.py"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.20.1"\n')
    module = tmp_path / "src/universal_tools/__init__.py"
    module.write_text('__version__ = "0.20.1"\n')
    command = [sys.executable, str(tmp_path / "scripts/stamp_version.py")]
    stamp = subprocess.run(
        [*command, "--at", "2026-01-01T00:01:02-05:00"], capture_output=True, text=True, check=True
    )
    assert json.loads(stamp.stdout)["version"] == "26.0101.000102"
    check = subprocess.run([*command, "--check"], capture_output=True, text=True, check=True)
    assert json.loads(check.stdout)["package_version"] == "26.101.102"
    before = module.read_bytes()
    rejected = subprocess.run([*command, "--at", "2025-12-31T23:59:59-05:00"], capture_output=True)
    assert rejected.returncode != 0 and module.read_bytes() == before
