"""Administrative write scopes and byte budgets must act before app dispatch."""

import asyncio
import math

import pytest
from pydantic import ValidationError
from test_core import fake as fake
from test_core import manifest, tool

from universal_tools.broker import CatalogTool
from universal_tools.models import (
    DEFAULT_ARGUMENT_BYTES,
    MAX_ARGUMENT_BYTES,
    BrokerError,
    atomic_json,
)


@pytest.mark.parametrize(
    "names", [["*"], ["app/echo"], [""], ["x" * 129], ["echo", "echo"], "echo", [1]]
)
def test_write_allowlist_requires_unique_exact_tool_names(names):
    with pytest.raises(ValidationError):
        manifest(allowed_write_tools=names)


@pytest.mark.parametrize("limit", [0, MAX_ARGUMENT_BYTES + 1, True, 1.5])
def test_argument_budget_is_a_bounded_integer(limit):
    with pytest.raises(ValidationError):
        manifest(max_argument_bytes=limit)


@pytest.mark.parametrize(
    "enabled,names,expected",
    [
        (False, ["echo"], False),
        (True, None, True),
        (True, [], False),
        (True, ["echo"], True),
        (True, ["Echo"], False),
        (True, ["other"], False),
    ],
)
def test_write_scope_is_exact_and_subordinate_to_service_enablement(enabled, names, expected):
    entry = CatalogTool(manifest(allow_writes=enabled, allowed_write_tools=names), tool(False))
    assert entry.callable is expected
    assert entry.describe()["callable"] is expected
    assert entry.describe()["max_argument_bytes"] == DEFAULT_ARGUMENT_BYTES


def test_write_allowlist_does_not_reclassify_effects_or_block_reads():
    registration = manifest(allow_writes=False, allowed_write_tools=[], read_only_tools=["echo"])
    assert CatalogTool(registration, tool(True)).callable
    assert not CatalogTool(registration, tool(False)).read_only
    assert not CatalogTool(registration, tool(False)).callable


async def test_search_describe_and_dispatch_agree_on_write_scope(fake):
    broker, session = fake
    allowed, blocked, read = tool(False), tool(None), tool(True)
    allowed.name, blocked.name, read.name = "edit", "future_edit", "inspect"
    session.tools = [allowed, blocked, read]
    atomic_json(
        broker.registry / "app.json",
        manifest(allow_writes=True, allowed_write_tools=["edit"]).model_dump(mode="json"),
    )
    await broker.refresh()
    matches = {item["tool_id"]: item["callable"] for item in broker.search()["matches"]}
    assert matches == {"app/edit": True, "app/future_edit": False, "app/inspect": True}
    for name, expected in [("edit", True), ("future_edit", False)]:
        description = broker.describe("app/" + name)
        assert description["callable"] is expected
        if expected:
            await broker.call(
                "app/" + name,
                description["catalog_revision"],
                {"value": "reviewed"},
                write=True,
                request_id="scoped-write-" + name,
            )
        else:
            with pytest.raises(BrokerError, match="allowed_write_tools"):
                await broker.call(
                    "app/" + name,
                    description["catalog_revision"],
                    {"value": "blocked"},
                    write=True,
                    request_id="scoped-write-" + name,
                )
            with pytest.raises(BrokerError, match="unknown request_id"):
                broker.request_status("scoped-write-" + name)
    assert session.calls == 1


async def test_allowlist_revocation_blocks_old_revisions_without_refresh(fake):
    broker, session = fake
    session.tools = [tool(False)]
    registration = manifest(allow_writes=True, allowed_write_tools=["echo"])
    atomic_json(broker.registry / "app.json", registration.model_dump(mode="json"))
    await broker.refresh()
    before = broker.describe("app/echo")["catalog_revision"]
    atomic_json(
        broker.registry / "app.json",
        registration.model_copy(update={"allowed_write_tools": []}).model_dump(mode="json"),
    )
    with pytest.raises(BrokerError, match="registration changed"):
        await broker.call(
            "app/echo", before, {"value": "blocked"}, write=True, request_id="revoked-scope-001"
        )
    assert session.calls == 0
    with pytest.raises(BrokerError, match="unknown request_id"):
        broker.request_status("revoked-scope-001")
    await broker.refresh()
    assert broker.describe("app/echo")["catalog_revision"] != before
    assert not broker.describe("app/echo")["callable"]


@pytest.mark.parametrize("change", [{"allowed_write_tools": []}, {"max_argument_bytes": 2}])
async def test_queued_write_rechecks_policy_and_budget_changes_before_claiming(fake, change):
    broker, session = fake
    session.tools = [tool(False)]
    registration = manifest(allow_writes=True, allowed_write_tools=["echo"])
    atomic_json(broker.registry / "app.json", registration.model_dump(mode="json"))
    await broker.refresh()
    revision = broker.describe("app/echo")["catalog_revision"]
    async with broker.lock("app"):
        pending = asyncio.create_task(
            broker.call(
                "app/echo",
                revision,
                {"value": "queued"},
                write=True,
                request_id="queued-policy-001",
            )
        )
        await asyncio.sleep(0)
        assert not pending.done()
        atomic_json(
            broker.registry / "app.json",
            registration.model_copy(update=change).model_dump(mode="json"),
        )
    with pytest.raises(BrokerError, match="registration changed"):
        await pending
    assert session.calls == 0
    with pytest.raises(BrokerError, match="unknown request_id"):
        broker.request_status("queued-policy-001")


async def test_utf8_budget_includes_the_complete_argument_object_and_accepts_exact_limit(fake):
    broker, session = fake
    registration = manifest(max_argument_bytes=14)
    atomic_json(broker.registry / "app.json", registration.model_dump(mode="json"))
    await broker.refresh()
    # Twelve bytes for {"value":""}, plus two UTF-8 bytes for the value.
    revision = broker.describe("app/echo")["catalog_revision"]
    result = await broker.call("app/echo", revision, {"value": "é"}, write=False)
    assert result.content[0].text == "é"
    with pytest.raises(BrokerError, match="max_argument_bytes.*14"):
        await broker.call("app/echo", revision, {"value": "éx"}, write=False)
    assert session.calls == 1


async def test_oversized_write_is_not_claimed_and_another_app_keeps_its_default_budget(fake):
    broker, session = fake
    session.tools = [tool(False)]
    atomic_json(
        broker.registry / "app.json",
        manifest(allow_writes=True, max_argument_bytes=16).model_dump(mode="json"),
    )
    atomic_json(
        broker.registry / "other.json", manifest("other", allow_writes=True).model_dump(mode="json")
    )
    await broker.refresh()
    arguments = {"value": "x" * 1000}
    with pytest.raises(BrokerError, match="max_argument_bytes"):
        await broker.call(
            "app/echo",
            broker.describe("app/echo")["catalog_revision"],
            arguments,
            write=True,
            request_id="oversized-write-001",
        )
    with pytest.raises(BrokerError, match="unknown request_id"):
        broker.request_status("oversized-write-001")
    await broker.call(
        "other/echo",
        broker.describe("other/echo")["catalog_revision"],
        arguments,
        write=True,
        request_id="other-budget-001",
    )
    assert session.calls == 1


@pytest.mark.parametrize("value", [math.nan, math.inf, object(), "\ud800"])
async def test_non_json_argument_failures_are_redacted_before_dispatch(fake, value):
    broker, session = fake
    with pytest.raises(BrokerError, match="valid finite JSON"):
        await broker.call(
            "app/echo",
            broker.describe("app/echo")["catalog_revision"],
            {"value": value},
            write=False,
        )
    assert session.calls == 0
