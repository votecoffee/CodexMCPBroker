"""Exercise the Windows API argument/environment boundary without any MCP host."""

import json
import os
import sys
import time

import pytest

from universal_tools.resources import ResourceLedger


@pytest.mark.skipif(os.name != "nt", reason="Windows native process arguments")
def test_unicode_empty_quoted_arguments_and_environment_stay_out_of_the_ledger(tmp_path):
    directory = tmp_path / "worker space λ"
    directory.mkdir()
    script, marker = directory / "worker.py", directory / "result.json"
    script.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({'args':sys.argv[2:],"
        "'environment':os.environ.get('RESOURCE_FIXTURE')}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    arguments = ["", 'quoted"value', "C:\\ending\\", "工場💡", "space ; & literal"]
    private_value = "fixture λ; & value not recorded"
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("synthetic") as owner:
        launch = owner.launch(
            [sys.executable, "-B", str(script), str(marker), *arguments],
            "Argument fixture",
            allow_termination=True,
            env={**os.environ, "RESOURCE_FIXTURE": private_value},
        )
        try:
            deadline = time.monotonic() + 10
            result = None
            while time.monotonic() < deadline:
                try:
                    result = json.loads(marker.read_text())
                    break
                except (OSError, ValueError):
                    time.sleep(0.02)
            assert result == {"args": arguments, "environment": private_value}
            record = launch.refresh()
            assert record.members
            raw = (ledger.directory / f"{record.resource_id}.json").read_text()
            assert "RESOURCE_FIXTURE" not in raw and private_value not in raw
            assert "space ; & literal" not in raw
        finally:
            launch.close()
