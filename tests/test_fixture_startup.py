"""Regress setup windows without slow real processes or wider app deadlines."""

from types import SimpleNamespace

import pytest
import test_integration as integration


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(integration.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        integration.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )
    return elapsed


def test_slow_example_does_not_consume_brokers_startup_window(clock):
    child = SimpleNamespace(poll=lambda: None)
    integration.wait_for_fixture(child, lambda: clock[0] >= 17, "example app")
    broker_ready_at = clock[0] + 17
    integration.wait_for_fixture(child, lambda: clock[0] >= broker_ready_at, "broker")
    assert 34 <= clock[0] < 35


def test_fixture_timeout_remains_bounded_and_identifies_the_process(clock):
    with pytest.raises(pytest.fail.Exception, match="broker startup timed out"):
        integration.wait_for_fixture(SimpleNamespace(poll=lambda: None), lambda: False, "broker")
    assert 20 <= clock[0] < 21


def test_exited_fixture_fails_without_waiting_or_restarting(clock):
    def should_not_probe():
        pytest.fail("a terminated fixture must not be probed")

    with pytest.raises(pytest.fail.Exception, match="example app exited before readiness"):
        integration.wait_for_fixture(
            SimpleNamespace(poll=lambda: 1), should_not_probe, "example app"
        )
    assert clock[0] == 0
