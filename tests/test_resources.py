"""Durable resource policy and disposable process trees; never run an MCP server."""

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager

import pytest

from universal_tools.resources import ResourceLedger


class FakeDriver:
    def __init__(self):
        self.owner = {
            "host_id": "f" * 64,
            "pid": os.getpid(),
            "created": "100",
            "executable": r"c:\fixture\owner.exe",
        }
        self.processes = {os.getpid(): self.owner}
        self.terminated = []
        self.fail_after_effect = False

    def add(self, pid=100001, created="200"):
        identity = {
            "host_id": "f" * 64,
            "pid": pid,
            "created": created,
            "executable": r"c:\fixture\worker.exe",
        }
        self.processes[pid] = identity
        return identity

    def capture(self, pid):
        if pid not in self.processes:
            raise ProcessLookupError("gone")
        return self.processes[pid]

    def state(self, identity):
        current = self.processes.get(identity["pid"])
        return "absent" if current is None else "alive" if current == identity else "different"

    @contextmanager
    def open(self, identity):
        assert self.state(identity) == "alive"
        driver = self

        class Handle:
            def terminate(self):
                driver.terminated.append(identity)
                driver.processes.pop(identity["pid"])
                if driver.fail_after_effect:
                    raise OSError("lost result after process exit")

            def wait(self, _milliseconds):
                return True

        yield Handle()


def fixture(tmp_path, *, ownership="private", cleanup="terminate", maximum=32):
    driver = FakeDriver()
    ledger = ResourceLedger(tmp_path, driver=driver, max_active_per_app=maximum)
    owner = ledger.owner("synthetic")
    identity = driver.add()
    record = owner.remember_process(
        identity["pid"], "Synthetic worker", ownership=ownership, cleanup_mode=cleanup
    )
    return ledger, owner, driver, record


def test_orphan_record_survives_new_ledger_and_exact_cleanup_is_idempotent(tmp_path):
    ledger, _, driver, record = fixture(tmp_path)
    assert not ledger.plan(record.resource_id)["eligible"]
    driver.processes.pop(os.getpid())
    reopened = ResourceLedger(tmp_path, driver=driver)
    plan = reopened.plan(record.resource_id)
    assert plan["eligible"]
    request = uuid.uuid4().hex
    result = reopened.cleanup(record.resource_id, plan["revision"], request)
    assert result["outcome"] == "terminated"
    assert reopened.cleanup(record.resource_id, plan["revision"], request) == result
    assert len(driver.terminated) == 1
    with pytest.raises(ValueError, match="already claimed"):
        reopened.cleanup(record.resource_id, plan["revision"], uuid.uuid4().hex)


@pytest.mark.parametrize(
    "ownership,cleanup", [("attached", "report"), ("shared", "report"), ("private", "report")]
)
def test_attached_shared_and_report_only_resources_never_terminate(tmp_path, ownership, cleanup):
    ledger, _, driver, record = fixture(tmp_path, ownership=ownership, cleanup=cleanup)
    driver.processes.pop(os.getpid())
    assert not ledger.plan(record.resource_id)["eligible"]
    with pytest.raises(ValueError):
        ledger.cleanup(record.resource_id, record.revision, uuid.uuid4().hex)
    assert driver.terminated == []


def test_pid_reuse_retires_the_old_claim_without_touching_replacement(tmp_path):
    ledger, _, driver, record = fixture(tmp_path)
    driver.processes.pop(os.getpid())
    replacement = driver.add(created="300")
    plan = ledger.plan(record.resource_id)
    assert plan["process_state"] == "different"
    assert (
        ledger.cleanup(record.resource_id, plan["revision"], uuid.uuid4().hex)["outcome"]
        == "already_exited"
    )
    assert driver.processes[replacement["pid"]] == replacement
    assert driver.terminated == []


def test_owner_and_resource_barriers_stale_plans_and_capacity(tmp_path):
    ledger, owner, driver, record = fixture(tmp_path, maximum=1)
    with pytest.raises(ValueError, match="Owner is live"):
        ledger.cleanup(record.resource_id, record.revision, uuid.uuid4().hex)
    with pytest.raises(ValueError, match="limit reached"):
        owner.begin_launch("second")
    updated = owner.set_recovery_barrier(record.resource_id, "Interrupted native write")
    driver.processes.pop(os.getpid())
    with pytest.raises(ValueError, match="changed"):
        ledger.cleanup(record.resource_id, record.revision, uuid.uuid4().hex)
    with pytest.raises(ValueError, match="recovery"):
        ledger.cleanup(record.resource_id, updated.revision, uuid.uuid4().hex)
    assert not driver.terminated


def test_uncertain_cleanup_preserves_claim_and_never_replays_after_reopen(tmp_path):
    ledger, _, driver, record = fixture(tmp_path)
    driver.processes.pop(os.getpid())
    driver.fail_after_effect = True
    request = uuid.uuid4().hex
    result = ledger.cleanup(record.resource_id, record.revision, request)
    assert result["outcome"] == "uncertain"
    reopened = ResourceLedger(tmp_path, driver=driver)
    assert reopened.cleanup(record.resource_id, record.revision, request) == result
    assert reopened.get(record.resource_id).state == "uncertain"
    assert not reopened.plan(record.resource_id)["eligible"]
    assert len(driver.terminated) == 1


def test_launch_intent_and_invalid_records_fail_closed(tmp_path):
    driver = FakeDriver()
    ledger = ResourceLedger(tmp_path, driver=driver)
    record = ledger.owner("synthetic").begin_launch("not yet created", cleanup_mode="terminate")
    driver.processes.pop(os.getpid())
    assert not ledger.plan(record.resource_id)["eligible"]
    (ledger.directory / (uuid.uuid4().hex + ".json")).write_text('{"invalid":true}')
    with pytest.raises(ValueError):
        ledger.records()


def test_cross_generation_reconciliation_retains_uncertain_receipt(tmp_path):
    ledger, _, driver, record = fixture(tmp_path)
    driver.processes.pop(os.getpid())
    driver.fail_after_effect = True
    result = ledger.cleanup(record.resource_id, record.revision, uuid.uuid4().hex)
    assert result["outcome"] == "uncertain"
    driver.owner = {**driver.owner, "created": "300"}
    driver.processes[os.getpid()] = driver.owner
    successor = ledger.owner("synthetic")
    uncertain = ledger.get(record.resource_id)
    reconciled = successor.reconcile(
        record.resource_id,
        uncertain.revision,
        "Inspected isolated worker exit and retained receipt",
    )
    assert reconciled.state == "released"
    assert reconciled.receipt.outcome == "uncertain"
    assert reconciled.reconciliation.previous_revision == uncertain.revision
    assert len(driver.terminated) == 1


def test_another_attached_claim_blocks_orphan_cleanup(tmp_path):
    ledger, _, driver, record = fixture(tmp_path)
    consumer = ledger.owner("consumer")
    consumer.remember_process(record.process.pid, "Attachment", ownership="attached")
    driver.processes.pop(os.getpid())
    assert not ledger.plan(record.resource_id)["eligible"]
    with pytest.raises(ValueError, match="Another retained"):
        ledger.cleanup(record.resource_id, record.revision, uuid.uuid4().hex)
    assert not driver.terminated


def test_owner_close_retires_remembered_private_process_and_fences_new_launches(tmp_path):
    ledger, owner, driver, record = fixture(tmp_path)
    owner.close()
    assert ledger.get(record.resource_id).state == "terminated"
    assert len(driver.terminated) == 1
    owner.close()
    with pytest.raises(ValueError, match="admission is fenced"):
        owner.begin_launch("too late")


def test_owner_close_refuses_attachments_and_report_only_software(tmp_path):
    ledger, owner, driver, record = fixture(tmp_path, ownership="attached", cleanup="report")
    with pytest.raises(ValueError, match="detach/release"):
        owner.close()
    owner.release_attachment(record.resource_id)
    owner.close()
    assert not driver.terminated
    assert driver.state(record.process.model_dump()) == "alive"


def test_app_only_barrier_resolution_keeps_an_attestation(tmp_path):
    ledger, owner, driver, record = fixture(tmp_path)
    blocked = owner.set_recovery_barrier(record.resource_id, "Inspect app outcome")
    with pytest.raises(ValueError, match="recovery"):
        owner.close()
    resolved = owner.resolve_recovery_barrier(
        record.resource_id, blocked.revision, "App journal and resource state inspected"
    )
    assert resolved.recovery_barrier is None
    assert resolved.barrier_resolution.previous_revision == blocked.revision
    owner.close()
    assert len(driver.terminated) == 1


def test_manager_cli_requires_review_and_never_exposes_app_reconciliation(
    tmp_path, monkeypatch, capsys
):
    from universal_tools import resources
    from universal_tools.manager_cli import main

    ledger, _, driver, record = fixture(tmp_path)
    driver.processes.pop(os.getpid())
    monkeypatch.setattr(resources, "ResourceLedger", lambda _registry: ledger)
    assert (
        main(["--registry", str(tmp_path), "resource-plan", "--resource-id", record.resource_id])
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["eligible"]
    arguments = [
        "--registry",
        str(tmp_path),
        "cleanup-resource",
        "--resource-id",
        record.resource_id,
        "--revision",
        plan["revision"],
        "--request-id",
        uuid.uuid4().hex,
    ]
    with pytest.raises(SystemExit) as missing_apply:
        main(arguments)
    assert missing_apply.value.code == 2
    assert not driver.terminated
    capsys.readouterr()
    assert main([*arguments, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "terminated"
    with pytest.raises(SystemExit):
        main(["--registry", str(tmp_path), "reconcile-resource"])
    assert len(driver.terminated) == 1


def test_checked_in_resource_schema_matches_runtime_contract():
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from universal_tools.resources import ResourceRecord

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/resource-record.schema.json").read_text()
    )
    assert schema == ResourceRecord.model_json_schema()
    Draft202012Validator.check_schema(schema)


def test_process_identity_canonicalization_and_foreign_private_claim_validation(tmp_path):
    from universal_tools.resources import ProcessIdentity, ResourceRecord

    identity = ProcessIdentity(
        host_id="f" * 64, pid=100001, created="000200", executable=r"C:\fixture\Worker.exe"
    )
    assert identity.created == "200"
    assert identity.executable == os.path.normcase(r"C:\fixture\Worker.exe")
    ledger, _, _, record = fixture(tmp_path)
    value = record.model_dump()
    value["owner"]["created"] = "300"
    with pytest.raises(ValueError, match="predate"):
        ResourceRecord.model_validate(value)
    value = record.model_dump()
    value["schema_version"] = True
    with pytest.raises(ValueError, match="integer 1"):
        ResourceRecord.model_validate(value)


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity")
def test_native_live_image_mismatch_is_unknown_not_proof_of_owner_death(tmp_path):
    ledger = ResourceLedger(tmp_path)
    identity = ledger.driver.capture(os.getpid())
    assert ledger.driver.state(identity) == "alive"
    assert (
        ledger.driver.state({**identity, "executable": r"c:\fixture\not-the-owner.exe"})
        == "unknown"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows host-bound process identity")
def test_foreign_host_fingerprint_never_opens_a_local_process(tmp_path, monkeypatch):
    ledger = ResourceLedger(tmp_path)
    identity = ledger.driver.capture(os.getpid())
    foreign = {**identity, "host_id": "0" * 64}

    def forbidden(*args, **kwargs):
        raise AssertionError("foreign resource must not look up a local PID")

    monkeypatch.setattr(ledger.driver, "_open", forbidden)
    assert ledger.driver.state(foreign) == "unknown"
    with pytest.raises(ValueError, match="host"):
        ledger.driver.open(foreign)


def wait_file(path, deadline=10):
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError("disposable process did not publish its ready marker")


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_native_windows_launch_is_persisted_and_normal_close_stops_descendants(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    owner = ledger.owner("synthetic")
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(json.dumps({'child':os.getpid(),'grandchild':p.pid}))\n"
        "time.sleep(60)\n"
    )
    marker = tmp_path / "ready.json"
    launch = owner.launch(
        [sys.executable, "-B", str(child_script), str(marker)],
        "Harmless tree",
        allow_termination=True,
    )
    try:
        ready = wait_file(marker)
        identities = [ledger.driver.capture(pid) for pid in ready.values()]
        record = ledger.get(launch.resource_id)
        assert record.state == "active" and record.containment == "windows_job"
        # Exiting objects can temporarily deny a fresh OpenProcess. Exact handles
        # held before teardown independently prove child/grandchild termination.
        with ExitStack() as stack:
            handles = [stack.enter_context(ledger.driver.open(i)) for i in identities]
            assert launch.close()["outcome"] == "terminated"
            assert all(handle.wait(5000) for handle in handles)
    finally:
        launch.group.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_native_windows_owner_crash_stops_tree_and_retains_orphan_record(tmp_path):
    registry, marker = tmp_path / "registry", tmp_path / "ready.json"
    script = tmp_path / "owner.py"
    script.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "from universal_tools.resources import ResourceLedger\n"
        "ledger=ResourceLedger(sys.argv[1]);owner=ledger.owner('synthetic')\n"
        "launch=owner.launch([sys.executable,'-B','-c','import time;time.sleep(60)'],"
        "'Harmless worker',allow_termination=True)\n"
        "Path(sys.argv[2]).write_text(json.dumps({'resource_id':launch.resource_id}))\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen([sys.executable, "-B", str(script), str(registry), str(marker)])
    try:
        ready = wait_file(marker)
        ledger = ResourceLedger(registry)
        record = ledger.get(ready["resource_id"])
        assert ledger.driver.state(record.process.model_dump()) == "alive"
        process.kill()
        process.wait(timeout=10)
        plan = ledger.plan(record.resource_id)
        until = time.monotonic() + 5
        while not plan["eligible"] and time.monotonic() < until:
            time.sleep(0.02)
            plan = ledger.plan(record.resource_id)
        assert plan["eligible"], plan
        assert ledger.get(record.resource_id).state == "active"
        assert (
            ledger.cleanup(record.resource_id, plan["revision"], uuid.uuid4().hex)["outcome"]
            == "already_exited"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_native_windows_identity_journal_failure_never_executes_worker(tmp_path, monkeypatch):
    from universal_tools import resources

    ledger = ResourceLedger(tmp_path / "registry")
    owner = ledger.owner("synthetic")
    marker = tmp_path / "must-not-execute.txt"
    captured = {}
    original_spawn = ledger.driver.create_suspended

    def spawn(*args, **kwargs):
        group = original_spawn(*args, **kwargs)
        captured["handle"] = ledger.driver.open(group.identity)
        return group

    original_write = resources.durable_json
    count = 0

    def fail_identity_once(path, value):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("isolated identity journal failure")
        return original_write(path, value)

    monkeypatch.setattr(ledger.driver, "create_suspended", spawn)
    monkeypatch.setattr(resources, "durable_json", fail_identity_once)
    try:
        with pytest.raises(OSError, match="journal failure"):
            owner.launch(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import sys;from pathlib import Path;Path(sys.argv[1]).write_text('executed')",
                    str(marker),
                ],
                "Never executed",
                allow_termination=True,
            )
        assert captured["handle"].wait(5000)
        assert not marker.exists()
        assert ledger.records()[0].state == "failed"
    finally:
        if captured.get("handle"):
            captured["handle"].close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_native_windows_eof_closes_owned_worker_and_retains_receipt(tmp_path):
    registry, marker = tmp_path / "registry", tmp_path / "ready.json"
    script = tmp_path / "eof_owner.py"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "from universal_tools.resources import ResourceLedger\n"
        "with ResourceLedger(sys.argv[1]).owner('synthetic') as owner:\n"
        " launch=owner.launch([sys.executable,'-B','-c','import time;time.sleep(60)'],"
        "'EOF fixture',allow_termination=True)\n"
        " Path(sys.argv[2]).write_text(json.dumps({'resource_id':launch.resource_id}))\n"
        " sys.stdin.buffer.read()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-B", str(script), str(registry), str(marker)], stdin=subprocess.PIPE
    )
    try:
        ready = wait_file(marker)
        ledger = ResourceLedger(registry)
        record = ledger.get(ready["resource_id"])
        with ledger.driver.open(record.process.model_dump()) as handle:
            process.communicate(timeout=10)
            assert process.returncode == 0
            assert handle.wait(5000)
        assert ledger.get(record.resource_id).receipt.outcome == "terminated"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
