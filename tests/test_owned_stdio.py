"""Harmless byte echo workers and fake frames; never start an MCP server/native app."""

import asyncio
import json
import os
import sys
import time
from contextlib import ExitStack

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from universal_tools.owned_stdio import OwnedStdioTransport, owned_byte_process
from universal_tools.resources import ResourceLedger

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows contained stdio")

ECHO = (
    "import os\n"
    "while True:\n"
    " data=os.read(0,65536)\n"
    " if not data:break\n"
    " while data:\n"
    "  count=os.write(1,data);data=data[count:]\n"
)


async def collect(stream):
    parts = []
    while True:
        try:
            parts.append(await stream.receive())
        except anyio.EndOfStream:
            return b"".join(parts)


async def test_owned_overlapped_byte_pipes_preserve_large_binary_and_eof(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    payload = bytes(range(256)) * 16384 + "深 μ café".encode()
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        async with owned_byte_process(
            owner,
            [sys.executable, "-B", "-c", ECHO],
            errlog=errlog,
            allow_termination=True,
        ) as process:
            identity = process.identity
            received = asyncio.create_task(collect(process.stdout))
            with anyio.fail_after(15):
                await process.stdin.send(payload)
                await process.stdin.aclose()
                assert await received == payload
                assert await process.wait() == 0
        assert process.returncode == 0
        record = ledger.get(process.resource_id)
        assert record.state == "terminated" and record.process.model_dump() == identity
        receipt = record.receipt.model_dump()
        assert await process.aclose() == receipt
        assert len(ledger.records()) == 1


async def test_owned_stdio_uses_sdk_message_types_with_only_a_byte_echo_worker(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    message = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "fixture/echo",
        "params": {"text": "深 μ café", "integer": 2**63 - 1, "empty": ""},
    }
    value = SessionMessage(jsonrpc_message_adapter.validate_python(message, by_name=False))
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        selected = OwnedStdioTransport(owner, allow_termination=True)
        async with selected(
            StdioServerParameters(command=sys.executable, args=["-B", "-c", ECHO]), errlog=errlog
        ) as (read, write):
            with anyio.fail_after(10):
                await write.send(value)
                reply = await read.receive()
            assert isinstance(reply, SessionMessage)
            assert (
                reply.message.model_dump(mode="json", by_alias=True, exclude_unset=True) == message
            )
    assert all(r.state == "terminated" for r in ledger.records())


async def test_owned_stdio_closes_recorded_grandchild_and_preserves_attachment(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    outside = ResourceLedger(tmp_path / "outside")
    code = (
        "import subprocess,sys,json\n"
        "child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'])\n"
        "print(json.dumps({'child':child.pid}),flush=True)\n"
        "sys.stdin.buffer.read()\n"
    )
    with outside.owner("outside-fixture") as unrelated, ledger.owner("fixture") as owner:
        with (
            unrelated.launch(
                [sys.executable, "-B", "-c", "import time;time.sleep(30)"],
                "Unrelated harmless attachment",
                allow_termination=True,
            ) as outsider,
            open(os.devnull, "w") as errlog,
            ExitStack() as held,
        ):
            outsider_handle = held.enter_context(outside.driver.open(outsider.group.identity))
            attachment = owner.remember_process(outsider.pid, "Pre-existing harmless attachment")
            async with owned_byte_process(
                owner,
                [sys.executable, "-B", "-c", code],
                errlog=errlog,
                allow_termination=True,
                grace_seconds=0,
            ) as process:
                with anyio.fail_after(10):
                    line = await process.stdout.reader.readline()
                child_pid = json.loads(line)["child"]
                record = process.launch.refresh()
                assert child_pid in [m.pid for m in record.members]
                member_handles = [
                    held.enter_context(ledger.driver.open(m.model_dump())) for m in record.members
                ]
            assert all(handle.wait(5000) for handle in member_handles)
            assert outsider_handle.alive()
            assert ledger.get(attachment.resource_id).state == "active"
            owner.release_attachment(attachment.resource_id)


async def test_creation_handle_is_bound_without_reopening_the_child_pid(tmp_path, monkeypatch):
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        capture = ledger.driver.capture

        def owner_only(pid):
            assert pid == os.getpid(), "startup must bind the retained creation handle"
            return capture(pid)

        with monkeypatch.context() as selected:
            selected.setattr(ledger.driver, "capture", owner_only)
            context = owned_byte_process(
                owner,
                [sys.executable, "-B", "-c", ECHO],
                errlog=errlog,
                allow_termination=True,
                grace_seconds=0,
            )
            process = await context.__aenter__()
            assert ledger.get(process.resource_id).process.model_dump() == process.identity
        await context.__aexit__(None, None, None)


async def test_failed_identity_binding_never_executes_byte_worker(tmp_path, monkeypatch):
    from universal_tools import resources

    ledger = ResourceLedger(tmp_path / "registry")
    owner = ledger.owner("fixture")
    marker = tmp_path / "must-not-execute"
    original = resources.durable_json
    writes = 0

    def fail_bind(path, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("isolated bind journal failure")
        return original(path, value)

    monkeypatch.setattr(resources, "durable_json", fail_bind)
    with open(os.devnull, "w") as errlog, pytest.raises(OSError, match="bind journal"):
        async with owned_byte_process(
            owner,
            [
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path;import sys;Path(sys.argv[1]).touch()",
                str(marker),
            ],
            errlog=errlog,
            allow_termination=True,
        ):
            pytest.fail("a failed binding cannot yield an executing worker")
    assert not marker.exists()
    assert ledger.records()[0].state == "failed"


async def test_cancel_during_backpressure_stops_owned_process(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        with pytest.raises(TimeoutError):
            async with owned_byte_process(
                owner,
                [sys.executable, "-B", "-c", "import time;time.sleep(30)"],
                errlog=errlog,
                allow_termination=True,
                grace_seconds=0,
            ) as process:
                with anyio.fail_after(0.2):
                    await process.stdin.send(b"x" * (4 * 1024 * 1024))
        assert ledger.get(process.resource_id).state == "terminated"
        assert process.returncode is not None


async def test_uncertain_cleanup_keeps_one_receipt_without_replay(tmp_path, monkeypatch):
    ledger = ResourceLedger(tmp_path / "registry")
    owner = ledger.owner("fixture")
    with open(os.devnull, "w") as errlog:
        context = owned_byte_process(
            owner,
            [sys.executable, "-B", "-c", ECHO],
            errlog=errlog,
            allow_termination=True,
            grace_seconds=0,
        )
        process = await context.__aenter__()
        stop = process.launch.group.stop
        calls = []

        def lose_reply():
            calls.append(1)
            stop()
            raise OSError("isolated lost stop reply")

        monkeypatch.setattr(process.launch.group, "stop", lose_reply)
        with pytest.raises(RuntimeError, match="uncertain"):
            await process.aclose()
        receipt = ledger.get(process.resource_id).receipt
        with pytest.raises(RuntimeError, match="uncertain"):
            await context.__aexit__(None, None, None)
        assert calls == [1]
        assert ledger.get(process.resource_id).receipt == receipt
        assert ledger.get(process.resource_id).state == "uncertain"


async def test_unrelated_inheritable_pipe_is_not_inherited(tmp_path):
    ledger = ResourceLedger(tmp_path / "registry")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    pending = None
    try:
        with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
            async with owned_byte_process(
                owner,
                [sys.executable, "-B", "-c", "import time;time.sleep(30)"],
                errlog=errlog,
                allow_termination=True,
                grace_seconds=0,
            ):
                os.close(write_fd)
                write_fd = None
                pending = asyncio.create_task(asyncio.to_thread(os.read, read_fd, 1))
                # An accidentally inherited writer prevents EOF until that worker exits.
                assert await asyncio.wait_for(asyncio.shield(pending), 3) == b""
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), 5)
        os.close(read_fd)


async def test_owner_crash_closes_owned_byte_worker_without_a_live_endpoint(tmp_path):
    script = tmp_path / "owner.py"
    registry, marker = tmp_path / "child-registry", tmp_path / "identity.json"
    script.write_text(
        "import asyncio,json,os,sys\nfrom pathlib import Path\n"
        "from universal_tools.resources import ResourceLedger\n"
        "from universal_tools.owned_stdio import owned_byte_process\n"
        "async def main():\n"
        " ledger=ResourceLedger(sys.argv[1]);owner=ledger.owner('byte-fixture')\n"
        " with open(os.devnull,'w') as errlog:\n"
        "  async with owned_byte_process(owner,"
        "[sys.executable,'-B','-c','import time;time.sleep(30)'],"
        "errlog=errlog,allow_termination=True) as p:\n"
        "   Path(sys.argv[2]).write_text(json.dumps(p.identity))\n"
        "   os._exit(41)\n"
        "asyncio.run(main())\n"
    )
    outer = ResourceLedger(tmp_path / "outer-registry")
    with outer.owner("fixture-harness") as harness:
        with harness.launch(
            [sys.executable, "-B", str(script), str(registry), str(marker)],
            "Harmless crash fixture",
            allow_termination=True,
        ) as helper:
            with anyio.fail_after(15):
                while helper.group.returncode is None:
                    await anyio.sleep(0.02)
            assert helper.group.returncode == 41
            identity = json.loads(marker.read_text())
            ledger = ResourceLedger(registry)
            deadline = time.monotonic() + 5
            while ledger.driver.state(identity) not in {"absent", "different"}:
                assert time.monotonic() < deadline
                await anyio.sleep(0.02)
            (record,) = ledger.records()
            assert record.state == "active"  # Crash leaves original evidence for review.
            assert record.process.model_dump() == identity
            plan = ledger.plan(record.resource_id)
            assert plan["eligible"]
            import uuid

            receipt = ledger.cleanup(record.resource_id, plan["revision"], uuid.uuid4().hex)
            assert receipt["outcome"] == "already_exited"


@pytest.mark.parametrize("large", [False, True])
async def test_sdk_message_echo_preserves_backpressure_and_transaction_identifiers(tmp_path, large):
    ledger = ResourceLedger(tmp_path / "registry")
    text = "café 深 μ\u0000" * (65536 if large else 1)
    expected = {
        "jsonrpc": "2.0",
        "id": "fixture-α",
        "method": "fixture/echo",
        "params": {"text": text},
    }
    value = SessionMessage(jsonrpc_message_adapter.validate_python(expected, by_name=False))
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        async with OwnedStdioTransport(owner, allow_termination=True)(
            StdioServerParameters(command=sys.executable, args=["-B", "-c", ECHO]), errlog=errlog
        ) as (read, write):
            with anyio.fail_after(15):
                await write.send(value)
                reply = await read.receive()
            assert (
                reply.message.model_dump(mode="json", by_alias=True, exclude_unset=True) == expected
            )


@pytest.mark.parametrize("mode", ["malformed", "oversize"])
async def test_untrusted_input_frames_are_bounded_or_redacted(tmp_path, mode):
    ledger = ResourceLedger(tmp_path / "registry")
    raw = "private-fixture-not-json\n" if mode == "malformed" else "x" * 4096
    code = "import sys,time;sys.stdout.write(sys.argv[1]);sys.stdout.flush();time.sleep(30)"
    with ledger.owner("fixture") as owner, open(os.devnull, "w") as errlog:
        async with OwnedStdioTransport(
            owner, allow_termination=True, grace_seconds=0, max_message_bytes=128
        )(
            StdioServerParameters(command=sys.executable, args=["-B", "-c", code, raw]),
            errlog=errlog,
        ) as (read, _):
            with anyio.fail_after(10):
                error = await read.receive()
            assert isinstance(error, ValueError)
            assert "private-fixture" not in str(error)
            assert ("budget" in str(error)) is (mode == "oversize")
