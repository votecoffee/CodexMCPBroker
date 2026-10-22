"""Owner loss during suspended creation; only disposable non-MCP Python fixtures."""

import json
import os
import subprocess
import sys
import time

import pytest

from universal_tools.resources import ResourceLedger


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic process containment")
@pytest.mark.parametrize("phase", ["before_bind", "before_resume"])
def test_owner_loss_in_startup_gap_does_not_leave_a_worker_or_execute_it(tmp_path, phase):
    registry = tmp_path / "registry"
    marker, executed = tmp_path / "created.json", tmp_path / "must-not-execute.txt"
    script = tmp_path / "owner.py"
    script.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "from universal_tools.resources import ResourceLedger\n"
        "from universal_tools.windows_resources import ContainedProcess\n"
        "ledger=ResourceLedger(sys.argv[1]);owner=ledger.owner('synthetic')\n"
        "def stop_here(group):\n"
        " Path(sys.argv[2]).write_text(json.dumps(group.identity))\n"
        " os._exit(37)\n"
        "if sys.argv[4]=='before_bind':\n"
        " original=ledger.driver.create_suspended\n"
        " def create(*args,**kwargs):\n"
        "  group=original(*args,**kwargs);stop_here(group)\n"
        " ledger.driver.create_suspended=create\n"
        "else:\n"
        " ContainedProcess.resume=stop_here\n"
        "owner.launch([sys.executable,'-B','-c',"
        "\"import sys;from pathlib import Path;Path(sys.argv[1]).write_text('executed')\","
        "sys.argv[3]],'Never executed',allow_termination=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-B", str(script), str(registry), str(marker), str(executed), phase]
    )
    try:
        process.wait(timeout=15)
        assert process.returncode == 37
        fingerprint = json.loads(marker.read_text())
        ledger = ResourceLedger(registry)
        deadline = time.monotonic() + 5
        state = ledger.driver.state(fingerprint)
        while state not in {"absent", "different"} and time.monotonic() < deadline:
            time.sleep(0.02)
            state = ledger.driver.state(fingerprint)
        assert state in {"absent", "different"}
        assert not executed.exists()
        (record,) = ledger.records()
        assert record.state == ("launching" if phase == "before_bind" else "active")
        assert (record.process is None) == (phase == "before_bind")
        successor = ledger.owner("synthetic")
        reconciled = successor.reconcile(
            record.resource_id,
            record.revision,
            "Inspected startup record; verified suspended fixture exit without execution",
        )
        assert reconciled.state == "released"
        assert reconciled.reconciliation.previous_revision == record.revision
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
