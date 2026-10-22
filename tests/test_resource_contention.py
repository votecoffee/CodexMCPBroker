"""Real file-lock contention with disposable helpers; no MCP or native applications."""

import errno
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from universal_tools.instance_lock import InstanceLock
from universal_tools.models import BrokerError
from universal_tools.resources import ResourceLedger


@contextmanager
def competing_lock(path):
    helper = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys\nfrom pathlib import Path\n"
            "from universal_tools.instance_lock import InstanceLock\n"
            "with InstanceLock(Path(sys.argv[1])):\n"
            " print('locked',flush=True)\n"
            " sys.stdin.buffer.read(1)\n",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert helper.stdout.readline().rstrip(b"\r\n") == b"locked"
        yield helper
    finally:
        helper.stdin.close()
        helper.wait(timeout=5)
        helper.stdout.close()
        error = helper.stderr.read()
        helper.stderr.close()
        assert helper.returncode == 0, error


class OwnerDriver:
    def capture(self, pid):
        return {
            "host_id": "f" * 64,
            "pid": pid,
            "created": "100",
            "executable": str(Path(sys.executable).absolute()),
        }


def test_default_singleton_still_refuses_a_second_owner(tmp_path):
    lock = InstanceLock(tmp_path / "instance.lock")
    with competing_lock(lock.path), pytest.raises(BrokerError, match="another process"):
        with lock:
            pytest.fail("singleton ownership must never be shared")
    assert lock.stream.closed
    with lock:
        pass


def test_ledger_waits_for_a_competing_process_before_one_reservation(tmp_path):
    ledger = ResourceLedger(tmp_path, driver=OwnerDriver())
    with competing_lock(ledger.directory / "ledger.lock") as helper:
        release = threading.Timer(0.1, lambda: (helper.stdin.write(b"x"), helper.stdin.flush()))
        release.start()
        try:
            record = ledger.owner("synthetic").begin_launch("One admission")
        finally:
            release.join()
    assert ledger.records() == [record]


def test_busy_ledger_has_no_reservation_or_effect_and_unlocks_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("universal_tools.resources._LOCK_TIMEOUT", 0.08)
    ledger = ResourceLedger(tmp_path, driver=OwnerDriver())
    owner = ledger.owner("synthetic")
    with competing_lock(ledger.directory / "ledger.lock"):
        with pytest.raises(BrokerError, match="remained busy"):
            owner.begin_launch("Never admitted")
        assert not ledger.records()
    admitted = owner.begin_launch("New explicit attempt after verified refusal")
    assert ledger.records() == [admitted]


def test_thread_lock_is_also_bounded_before_cross_process_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("universal_tools.resources._LOCK_TIMEOUT", 0.08)
    ledger = ResourceLedger(tmp_path, driver=OwnerDriver())
    ready, release = threading.Event(), threading.Event()

    def hold():
        with ledger._mutex:
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        with pytest.raises(BrokerError, match="resource ledger remained busy"):
            ledger.owner("synthetic").begin_launch("Never admitted")
        assert not ledger.records()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_cleanup_contention_does_not_claim_request_or_dispatch_termination(tmp_path, monkeypatch):
    monkeypatch.setattr("universal_tools.resources._LOCK_TIMEOUT", 0.08)
    driver = OwnerDriver()
    ledger = ResourceLedger(tmp_path, driver=driver)
    record = ledger.owner("synthetic").remember_process(
        100001, "Synthetic private worker", ownership="private", cleanup_mode="terminate"
    )
    effects = []
    driver.state = lambda identity: "absent" if identity["pid"] == os.getpid() else "alive"
    driver.open = lambda identity: effects.append(identity)
    assert ledger.plan(record.resource_id)["eligible"]
    with competing_lock(ledger.directory / "ledger.lock"):
        with pytest.raises(BrokerError, match="remained busy"):
            ledger.cleanup(record.resource_id, record.revision, "a" * 32)
        assert ledger.get(record.resource_id) == record
        assert not effects


def test_non_contention_error_is_not_retried_and_closes_handle(tmp_path, monkeypatch):
    calls = []
    lock = InstanceLock(tmp_path / "error.lock", timeout=1)

    def fail():
        calls.append(1)
        raise OSError(errno.EIO, "synthetic disk failure")

    monkeypatch.setattr(lock, "_acquire", fail)
    with pytest.raises(OSError, match="synthetic disk failure"), lock:
        pytest.fail("failed lock cannot enter the operation")
    assert calls == [1]
    assert lock.stream.closed


@pytest.mark.parametrize("timeout", [-1, 11, float("nan"), float("inf"), True, "1"])
def test_lock_wait_budget_is_bounded_and_validated(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite number"):
        InstanceLock(tmp_path / "bad.lock", timeout=timeout)
