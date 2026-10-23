"""Real SDK protocol fixtures over contained pipes; no engineering software or devices."""

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from universal_tools.owned_stdio import OwnedStdioTransport
from universal_tools.resources import ResourceLedger

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows contained SDK stdio")
FIXTURES = Path(__file__).parent / "fixtures"


def tool_value(result):
    if result.structured_content is not None:
        return result.structured_content
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def exercise_payload(factory, errlog):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-B", str(FIXTURES / "payload_mcp.py")]
    )
    payload = {"text": "日本語 café 😀" * 8192, "integer": 2**63 - 1, "empty": ""}
    with anyio.fail_after(30):
        async with factory(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("roundtrip", {"payload": payload})
                state = await session.call_tool("state")
                rejected = await session.call_tool("missing_fixture_tool")
                assert tool_value(result) == payload
                assert tool_value(state) == {"commits": 0, "reads": 1}
                assert rejected.is_error
                return {
                    "protocol": initialized.protocol_version,
                    "tools": [t.model_dump(mode="json", by_alias=True) for t in tools.tools],
                    "result": result.model_dump(mode="json", by_alias=True),
                    "state": state.model_dump(mode="json", by_alias=True),
                    "rejected": rejected.model_dump(mode="json", by_alias=True),
                }


async def test_actual_sdk_initialize_catalog_unicode_results_errors_and_eof_match_stock(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("fixture") as owner, (tmp_path / "stderr.log").open("w") as errlog:
        expected = await exercise_payload(stdio_client, errlog)
        selected = OwnedStdioTransport(owner, allow_termination=True, max_processes=4)
        actual = await exercise_payload(selected, errlog)
        assert actual == expected
        records = ledger.records()
        assert len(records) == 1 and records[0].owner == owner.identity
        assert records[0].state == "terminated" and records[0].receipt is not None
        assert ledger.driver.state(records[0].process.model_dump()) in {"absent", "different"}


async def test_actual_sdk_worker_crash_closes_pending_request_and_checked_job_members(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    with (
        ledger.owner("fixture") as owner,
        (tmp_path / "stderr.log").open("w") as errlog,
        ExitStack() as held,
    ):
        selected = OwnedStdioTransport(owner, allow_termination=True, max_processes=4)
        parameters = StdioServerParameters(
            command=sys.executable, args=["-B", str(FIXTURES / "stateful_mcp.py")]
        )
        handles = []
        with anyio.fail_after(30):
            async with selected(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("inspect")
                    assert tool_value(result)["value"] == ""
                    launch = next(iter(owner._launches.values()))
                    record = launch.refresh()
                    handles = [
                        held.enter_context(ledger.driver.open(i.model_dump()))
                        for i in record.members
                    ]
                    assert handles and all(handle.alive() for handle in handles)
                    with pytest.raises(Exception, match="closed|Closed|stream|Stream"):
                        await session.call_tool("disconnect")
        assert all(handle.wait(5000) for handle in handles)
        records = ledger.records()
        assert len(records) == 1 and records[0].state == "terminated"
        assert records[0].receipt is not None


async def test_actual_sdk_failed_start_is_terminal_without_default_transport_retry(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("fixture") as owner, (tmp_path / "stderr.log").open("w") as errlog:
        selected = OwnedStdioTransport(owner, allow_termination=True, max_processes=4)
        parameters = StdioServerParameters(
            command=sys.executable, args=["-B", "-c", "raise SystemExit(19)"]
        )
        with anyio.fail_after(15):
            async with selected(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    with pytest.raises(Exception, match="closed|Closed|stream|Stream"):
                        await session.initialize()
        records = ledger.records()
        assert len(records) == 1 and records[0].state == "terminated"
        assert records[0].receipt is not None
        assert ledger.driver.state(records[0].process.model_dump()) in {"absent", "different"}
