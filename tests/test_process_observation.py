"""Minimal-rights pinned observations; synthetic kernels and harmless Python workers."""

import os
import sys

import pytest

from universal_tools.resources import ResourceLedger
from universal_tools.windows_resources import ProcessDriver

IDENTITY = {"host_id": "a" * 64, "pid": 10001, "created": "123", "executable": "fixture.exe"}


class Kernel:
    def __init__(self):
        self.access = []
        self.closed = []
        self.wait_result = 258
        self.terminated = []

    def OpenProcess(self, access, inherit, pid):
        self.access.append((access, inherit, pid))
        return 91

    def CloseHandle(self, handle):
        self.closed.append(handle)

    def WaitForSingleObject(self, handle, milliseconds):
        return self.wait_result

    def TerminateProcess(self, handle, code):
        self.terminated.append((handle, code))
        return 1


def driver():
    value = ProcessDriver.__new__(ProcessDriver)
    value.kernel, value.host_id = Kernel(), IDENTITY["host_id"]
    value.capture_handle = lambda _handle, _pid: dict(IDENTITY)
    return value


def test_observation_pins_identity_once_and_never_requests_termination_rights():
    selected = driver()
    observed = selected.observe(IDENTITY)
    assert selected.kernel.access == [(0x101000, False, IDENTITY["pid"])]

    def deny(*args):
        raise PermissionError("repeat image query denied during shutdown")

    selected.capture_handle = deny
    detached = observed.identity
    detached["created"] = "not-the-recorded-generation"
    assert observed.identity == IDENTITY and observed.alive() and not observed.wait(0)
    with pytest.raises(PermissionError, match="observation handles"):
        observed.terminate()
    assert not selected.kernel.terminated
    selected.kernel.wait_result = 0
    assert not observed.alive() and observed.wait(25) and observed.identity == IDENTITY
    observed.close()
    observed.close()
    assert selected.kernel.closed == [91]
    with pytest.raises(ValueError, match="closed"):
        observed.alive()
    with pytest.raises(ValueError, match="closed"):
        observed.wait(0)


def test_cleanup_handle_retains_explicit_termination_permission():
    selected = driver()
    with selected.open(IDENTITY) as cleanup:
        assert selected.kernel.access == [(0x101001, False, IDENTITY["pid"])]
        cleanup.terminate()
    assert selected.kernel.terminated == [(91, 1)]


@pytest.mark.parametrize("field,replacement", [("created", "999"), ("executable", "other.exe")])
def test_observation_refuses_reused_or_mismatched_generations(field, replacement):
    selected = driver()
    with pytest.raises(ProcessLookupError, match="identity changed"):
        selected.observe({**IDENTITY, field: replacement})
    assert selected.kernel.closed == [91] and not selected.kernel.terminated


def test_observation_refuses_foreign_host_before_open_and_unknown_initial_image():
    selected = driver()
    with pytest.raises(ValueError, match="host"):
        selected.observe({**IDENTITY, "host_id": "b" * 64})
    assert not selected.kernel.access

    def deny(*args):
        raise PermissionError("initial identity acquisition denied")

    selected.capture_handle = deny
    with pytest.raises(PermissionError, match="initial identity"):
        selected.observe(IDENTITY)
    assert selected.kernel.closed == [91]


def test_invalid_wait_is_not_classified_as_exited_or_alive():
    selected = driver()
    with selected.observe(IDENTITY) as observed:
        for invalid in (True, -1, 1.5, 2**32 - 1):
            with pytest.raises(ValueError, match="bounded"):
                observed.wait(invalid)
        selected.kernel.wait_result = 0xFFFFFFFF
        with pytest.raises(OSError, match="wait failed"):
            observed.alive()
        with pytest.raises(OSError, match="wait failed"):
            observed.wait(25)


@pytest.mark.skipif(os.name != "nt", reason="Windows contained observation")
def test_observation_retains_real_worker_handle_until_exact_job_exit(tmp_path, monkeypatch):
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("fixture") as owner:
        with owner.launch(
            [sys.executable, "-B", "-c", "import time;time.sleep(30)"],
            "Harmless observed worker",
            allow_termination=True,
            max_processes=4,
        ) as launch:
            with ledger.driver.observe(launch.group.identity) as observed:
                with monkeypatch.context() as changes:

                    def deny(*args):
                        raise AssertionError("known observations must not repeat identity queries")

                    changes.setattr(ledger.driver, "capture_handle", deny)
                    assert observed.identity == launch.group.identity
                    assert observed.alive() and not observed.wait(0)
                    with pytest.raises(PermissionError):
                        observed.terminate()
                launch.close()
                assert observed.wait(5000) and not observed.alive()
        assert ledger.get(launch.resource_id).state == "terminated"
