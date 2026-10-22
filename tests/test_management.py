"""Real loopback management and restart barriers; no native apps or controllers."""

import concurrent.futures
import json
import secrets
import sqlite3

import httpx
import pytest

from universal_tools.management import (
    ComponentVersion,
    ManagedService,
    ManagedStatus,
    ManagementClient,
    Registration,
    RestartResult,
)
from universal_tools.manager_cli import main
from universal_tools.manager_inventory import parse_inventory, present_snapshot


def fixture_service(tmp_path, *, restart=None, **status):
    current = {
        "versions": {
            "app": {
                "installed": "2.0.0",
                "running": "1.0.0",
                "available": "3.0.0",
                "update_pending": True,
            }
        },
        "restart_allowed": True,
        "restart_reason": "",
        **status,
    }
    effects = []

    def perform(request):
        effects.append(request.request_id)
        return RestartResult(outcome="accepted", message="Supervisor accepted the restart")

    service = ManagedService(
        "example",
        "Example service",
        lambda: current,
        restart=restart or perform,
        registry=tmp_path / "registry",
        plugin_id="example@local",
    )
    return service, current, effects, ManagementClient(tmp_path / "registry")


def restart(client, row, request_id=None):
    return client.restart(
        row["app_id"], row["instance_id"], row["revision"], request_id or secrets.token_hex(16)
    )


def test_live_status_version_identity_and_clean_stop(tmp_path):
    service, _, _, client = fixture_service(tmp_path)
    with service:
        snapshot = client.snapshot()
        row = snapshot["services"][0]
        assert row["status"]["state"] == "running"
        assert row["status"]["versions"]["app"]["update_pending"]
        assert row["revision"] == service.snapshot()["revision"]
        assert "token" not in json.dumps(snapshot)
        assert "endpoint" not in json.dumps(snapshot)
        assert row["status"]["versions"]["framework"]["running"]
    assert client.snapshot()["services"][0]["status"]["state"] == "stopped"


def test_restart_duplicate_and_changed_payload_dispatch_once(tmp_path):
    service, _, effects, client = fixture_service(tmp_path)
    with service:
        row = client.snapshot()["services"][0]
        key = secrets.token_hex(16)
        first = restart(client, row, key)
        assert first["outcome"] == "accepted"
        assert restart(client, row, key) == first
        changed = dict(row, revision="f" * 64)
        assert restart(client, changed, key)["outcome"] == "uncertain"
        assert effects == [key]


def test_busy_race_and_stale_revision_never_restart(tmp_path):
    service, current, effects, client = fixture_service(tmp_path)
    with service:
        row = client.snapshot()["services"][0]
        current["active_operations"] = 1
        assert restart(client, row)["outcome"] == "blocked"
        busy = client.snapshot()["services"][0]
        assert not busy["status"]["restart_allowed"]
        assert restart(client, busy)["outcome"] == "blocked"
        assert effects == []


def test_failed_callback_retains_durable_cross_instance_barrier(tmp_path):
    effects = []

    def failed(request):
        effects.append(request.request_id)
        raise RuntimeError("private native exception")

    service, _, _, client = fixture_service(tmp_path, restart=failed)
    key = secrets.token_hex(16)
    with service:
        assert restart(client, client.snapshot()["services"][0], key)["outcome"] == "uncertain"
        assert service.pending_restarts()[0]["request_id"] == key
        row = client.snapshot()["services"][0]
        assert not row["status"]["restart_allowed"]
        assert restart(client, row)["outcome"] == "blocked"
    replacement, _, new_effects, _ = fixture_service(tmp_path)
    with replacement:
        row = next(
            r for r in client.snapshot()["services"] if r["instance_id"] == replacement.instance_id
        )
        assert restart(client, row)["outcome"] == "blocked"
        assert new_effects == []
        replacement.reconcile_restart(
            key,
            RestartResult(outcome="accepted", message="App supervisor verified restart receipt"),
        )
        assert replacement.pending_restarts() == []
        row = next(
            r for r in client.snapshot()["services"] if r["instance_id"] == replacement.instance_id
        )
        assert restart(client, row)["outcome"] == "accepted"
    assert effects == [key]
    with sqlite3.connect(tmp_path / "registry/restart-journal.sqlite3") as connection:
        assert (
            json.loads(
                connection.execute(
                    "SELECT result FROM restarts WHERE request_id=?", (key,)
                ).fetchone()[0]
            )["outcome"]
            == "uncertain"
        )
        assert connection.execute("SELECT count(*) FROM reconciliations").fetchone()[0] == 1


def test_concurrent_identical_restart_dispatches_once(tmp_path):
    service, _, effects, client = fixture_service(tmp_path)
    with service:
        row = client.snapshot()["services"][0]
        key = secrets.token_hex(16)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: restart(client, row, key), range(4)))
        assert all(result["outcome"] == "accepted" for result in results)
        assert effects == [key]


def test_authorization_host_origin_body_and_instance_guards(tmp_path):
    service, _, effects, _ = fixture_service(tmp_path)
    with service, httpx.Client(trust_env=False) as http:
        url = service.record.endpoint
        auth = {"Authorization": "Bearer " + service.token}
        assert http.get(url + "/status").status_code == 401
        assert (
            http.get(url + "/status", headers={**auth, "Origin": "http://example.com"}).status_code
            == 401
        )
        assert http.get(url + "/status", headers={**auth, "Host": "example.com"}).status_code == 401
        assert http.post(url + "/restart", headers=auth, content=b"x" * 8193).status_code == 413
        assert http.post(url + "/restart", headers=auth, content=b"not-json").status_code == 400
        response = http.post(
            url + "/restart",
            headers=auth,
            json={
                "instance_id": "f" * 32,
                "expected_revision": service.snapshot()["revision"],
                "request_id": secrets.token_hex(16),
            },
        )
        assert response.status_code == 400
        assert effects == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9000/v1/management",
        "https://127.0.0.1:9000/v1/management",
        "http://127.0.0.1:9000/other",
        "http://127.0.0.1:9000/v1/management?token=x",
        "http://user:password@127.0.0.1:9000/v1/management",
        "http://192.0.2.1:9000/v1/management",
    ],
)
def test_registration_cannot_target_other_services(tmp_path, endpoint):
    service, _, _, _ = fixture_service(tmp_path)
    with service:
        value = service.record.model_dump(mode="json")
        value["endpoint"] = endpoint
        with pytest.raises(ValueError):
            Registration.model_validate(value)


def test_tampered_registration_and_offline_instance_do_not_gain_restart(tmp_path):
    service, _, _, client = fixture_service(tmp_path)
    with service:
        record = service.record
        path = service.registry.path(record.app_id, record.instance_id)
    # A closed endpoint with a formerly live registration is offline, not healthy/stopped.
    path.write_text(record.model_dump_json(), encoding="utf-8")
    assert client.snapshot()["services"][0]["status"]["state"] == "offline"
    value = record.model_dump(mode="json")
    value["app_id"] = "other"
    path.write_text(json.dumps(value), encoding="utf-8")
    snapshot = client.snapshot()
    assert snapshot["services"] == [] and snapshot["warnings"]


def test_inventory_is_read_only_and_preserves_unknown_versions(tmp_path):
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "plugin.json").write_text('{"version":"2.0.0"}', encoding="utf-8")
    items = {
        "installed": [
            {
                "pluginId": "example@local",
                "name": "Example",
                "version": "1.0.0",
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(source)},
            }
        ]
    }
    rows = parse_inventory(items)
    assert rows[0]["update_pending"]
    ui = present_snapshot({"services": [], "warnings": [], "updated_at": 1}, rows)
    assert ui["rows"][0]["state"] == "unmonitored"
    assert not ui["rows"][0]["restart_allowed"]
    assert ui["rows"][0]["running"] == "Unknown"
    assert ui["rows"][0]["update"] == "Update pending"


def test_inventory_does_not_double_count_registered_plugin(tmp_path):
    service, _, _, client = fixture_service(tmp_path)
    plugin = {
        "plugin_id": "example@local",
        "display_name": "Example",
        "installed": "2.0.0",
        "available": "3.0.0",
        "enabled": True,
        "update_pending": True,
        "source_differs": True,
    }
    with service:
        ui = present_snapshot(client.snapshot(), [plugin])
        assert len(ui["rows"]) == 1
        assert ui["rows"][0]["restart_allowed"]
        assert ui["updates"] == 1


def test_watch_command_writes_credential_free_snapshot(tmp_path):
    service, _, _, _ = fixture_service(tmp_path)
    with service:
        target = tmp_path / "snapshot.json"
        assert (
            main(
                [
                    "--registry",
                    str(tmp_path / "registry"),
                    "watch",
                    "--once",
                    "--no-codex",
                    "--snapshot-file",
                    str(target),
                    "--stop-file",
                    str(tmp_path / "stop"),
                ]
            )
            == 0
        )
        raw = target.read_text(encoding="utf-8")
        assert service.token not in raw
        assert json.loads(raw)["rows"][0]["state"] == "running"


def test_default_read_only_service_never_offers_restart(tmp_path):
    with ManagedService(
        "reader", "Read only", lambda: ManagedStatus(), registry=tmp_path / "registry"
    ):
        row = ManagementClient(tmp_path / "registry").snapshot()["services"][0]
        assert not row["status"]["restart_allowed"]
        assert restart(ManagementClient(tmp_path / "registry"), row)["outcome"] == "blocked"


def test_versions_keep_distinct_install_running_and_available():
    value = ComponentVersion(installed="2.0.0", running="1.0.0", available="3.0.0")
    assert not value.update_pending  # App declares its update decision; no lexical guesses.
    assert value.running != value.installed


def test_framework_version_drift_is_reported_from_installed_metadata(tmp_path, monkeypatch):
    import universal_tools.management as management

    monkeypatch.setattr(management.importlib.metadata, "version", lambda _: "99.0.0")
    service, _, _, client = fixture_service(tmp_path)
    with service:
        row = client.snapshot()["services"][0]
        framework = row["status"]["versions"]["framework"]
        assert framework["installed"] == "99.0.0"
        assert framework["running"] == management.__version__
        ui = present_snapshot(client.snapshot(), [])
        assert ui["rows"][0]["details"]["versions"]["framework"] == framework


def test_invalid_restart_receipt_is_uncertain(tmp_path, monkeypatch):
    service, _, effects, client = fixture_service(tmp_path)
    with service:
        row = client.snapshot()["services"][0]
        monkeypatch.setattr(client, "_get", lambda *a, **k: {"outcome": "done"})
        assert restart(client, row)["outcome"] == "uncertain"
        assert effects == []


def test_windows_registry_ignores_host_localappdata_override(monkeypatch, tmp_path):
    import os

    from universal_tools.management import default_registry

    original = default_registry()
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "host-private"))
        assert default_registry() == original


def test_live_supervisor_can_restart_a_stopped_worker(tmp_path):
    service, _, effects, client = fixture_service(tmp_path, state="stopped")
    with service:
        row = client.snapshot()["services"][0]
        assert row["status"]["restart_allowed"]
        assert restart(client, row)["outcome"] == "accepted"
        assert len(effects) == 1
    closed = client.snapshot()["services"][0]
    assert closed["revision"] is None and not closed["status"]["restart_allowed"]
