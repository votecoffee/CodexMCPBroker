"""Stopped-broker configuration preserves existing private settings and policy."""

import json

import pytest
from pydantic import ValidationError

from universal_tools.broker import Broker
from universal_tools.cli import configure_request_limit, initialize, main
from universal_tools.daemon import InstanceLock
from universal_tools.models import (
    MAX_REQUEST_BODY_BYTES,
    MAX_REQUEST_BYTES,
    BrokerError,
    Settings,
    atomic_json,
    load_settings,
)
from universal_tools.surface import LocalBackend, RemoteBackend, create_surface


def test_old_settings_keep_default_and_init_preserves_an_explicit_limit(tmp_path):
    original = initialize(tmp_path, 19001)
    legacy = original.model_dump(mode="json", exclude={"max_request_body_bytes"})
    atomic_json(tmp_path / "settings.json", legacy)
    assert load_settings(tmp_path).max_request_body_bytes == MAX_REQUEST_BYTES
    result = configure_request_limit(tmp_path, MAX_REQUEST_BODY_BYTES)
    updated = load_settings(tmp_path)
    assert updated.token == original.token and updated.url == original.url
    assert updated.max_request_body_bytes == MAX_REQUEST_BODY_BYTES
    assert "token" not in result and "token" not in json.dumps(result)
    assert initialize(tmp_path, original.port) == updated


@pytest.mark.parametrize("value", [0, MAX_REQUEST_BODY_BYTES + 1, True, "100", 1.5])
def test_invalid_limit_preserves_exact_settings_bytes(tmp_path, value):
    initialize(tmp_path, 19001)
    original = (tmp_path / "settings.json").read_bytes()
    with pytest.raises(ValidationError):
        configure_request_limit(tmp_path, value)
    assert (tmp_path / "settings.json").read_bytes() == original


@pytest.mark.parametrize("held", ["startup.lock", "broker.lock"])
def test_request_limit_update_cannot_race_startup_or_change_a_running_broker(tmp_path, held):
    initialize(tmp_path, 19001)
    original = (tmp_path / "settings.json").read_bytes()
    with InstanceLock(tmp_path / held), pytest.raises(BrokerError):
        configure_request_limit(tmp_path, MAX_REQUEST_BODY_BYTES)
    assert (tmp_path / "settings.json").read_bytes() == original
    # Failed configuration also releases any earlier lock in its acquisition order.
    assert configure_request_limit(tmp_path, MAX_REQUEST_BODY_BYTES)["max_request_body_bytes"]


def test_cli_configures_only_body_limit_without_printing_credentials(tmp_path, capsys):
    original = initialize(tmp_path, 19001)
    manifest = tmp_path / "services/untouched.json"
    manifest.write_text('{"fixture": "not loaded by configuration"}', encoding="utf-8")
    raw = manifest.read_bytes()
    main(["--state", str(tmp_path), "configure-request-limit", "--bytes", "33554432"])
    output = capsys.readouterr().out
    assert json.loads(output)["max_request_body_bytes"] == 32 * 1024 * 1024
    assert original.token not in output and manifest.read_bytes() == raw


async def test_both_surface_types_use_the_configured_limit(tmp_path):
    broker = Broker(tmp_path, max_request_body_bytes=32 * 1024 * 1024)
    local = create_surface(LocalBackend(broker))
    assert local.http_options["max_request_body_size"] == 32 * 1024 * 1024
    settings = Settings(token="x" * 48, max_request_body_bytes=32 * 1024 * 1024)
    async with RemoteBackend(settings) as backend:
        frontend = create_surface(backend)
        assert frontend.http_options["max_request_body_size"] == 32 * 1024 * 1024
    await broker.aclose()
