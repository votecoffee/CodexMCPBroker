"""Opt-in supervisor-owned Windows byte pipes and SDK-compatible stdio transport.

The message bridge follows the MCP Python SDK 2.3 stdio lifecycle. See the
packaged SDK_STDIO_LICENSE.txt. No SDK globals/private functions are patched.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import shutil
from contextlib import asynccontextmanager, suppress

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream
from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter


class _ReadProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader):
        super().__init__(reader)
        self.closed = asyncio.Event()

    def connection_lost(self, error):
        super().connection_lost(error)
        self.closed.set()


class _WriteProtocol(asyncio.Protocol):
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.closed = asyncio.Event()
        self.error = None

    def pause_writing(self):
        self.ready.clear()

    def resume_writing(self):
        self.ready.set()

    def connection_lost(self, error):
        self.error = error
        self.closed.set()
        self.ready.set()


class _ReceivePipe(ByteReceiveStream):
    def __init__(self, reader, transport, protocol):
        self.reader, self.transport, self.protocol = reader, transport, protocol

    async def receive(self, max_bytes=65536):
        if max_bytes < 1:
            raise ValueError("receive size must be positive")
        data = await self.reader.read(max_bytes)
        if not data:
            raise anyio.EndOfStream
        return data

    async def aclose(self):
        self.transport.close()
        await self.protocol.closed.wait()


class _SendPipe(ByteSendStream):
    def __init__(self, transport, protocol):
        self.transport, self.protocol = transport, protocol

    async def send(self, item):
        view = memoryview(item)
        for offset in range(0, len(view), 65536):
            if self.transport.is_closing():
                raise anyio.ClosedResourceError
            self.transport.write(view[offset : offset + 65536])
            await self.protocol.ready.wait()
            if self.protocol.closed.is_set():
                raise anyio.BrokenResourceError from self.protocol.error

    async def aclose(self):
        self.transport.close()
        await self.protocol.closed.wait()


class OwnedByteProcess:
    """Byte streams plus the exact, durable owned launch; no MCP server is implied."""

    def __init__(self, launch, stdin, stdout, grace_seconds):
        self.launch, self.stdin, self.stdout = launch, stdin, stdout
        self.grace_seconds = grace_seconds
        self._cleanup_task = None

    @property
    def pid(self):
        return self.launch.pid

    @property
    def identity(self):
        return dict(self.launch.group.identity)

    @property
    def resource_id(self):
        return self.launch.resource_id

    @property
    def returncode(self):
        return self.launch.group.returncode

    async def wait(self):
        while self.returncode is None:
            await anyio.sleep(0.01)
        return self.returncode

    async def _cleanup(self):
        try:
            with anyio.move_on_after(0.5):
                await self.stdin.aclose()
            with anyio.move_on_after(self.grace_seconds):
                await self.wait()
            # The launch retains the checked creation handle and the job. Even an
            # exited root can have live descendants. Cleanup is journalled once.
            receipt = await anyio.to_thread.run_sync(self.launch.close)
            if receipt["outcome"] not in {"terminated", "already_exited"}:
                raise RuntimeError("owned stdio cleanup is uncertain; inspect its retained receipt")
            return receipt
        finally:
            self.stdin.transport.abort()
            self.stdout.transport.close()
            with anyio.move_on_after(1):
                await self.stdin.protocol.closed.wait()
                await self.stdout.protocol.closed.wait()

    async def aclose(self):
        # A second caller observes the same operation, including a failed one.
        # This task owns only OS pipe cleanup, never an SDK/AnyIO session scope.
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        cancelled = False
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    result = await asyncio.shield(self._cleanup_task)
                    break
                except asyncio.CancelledError:
                    if self._cleanup_task.cancelled():
                        raise
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result


@asynccontextmanager
async def owned_byte_process(
    owner,
    argv,
    *,
    errlog,
    label="App stdio worker",
    cwd=None,
    env=None,
    max_processes=32,
    grace_seconds=2,
    allow_termination=False,
):
    """Create restricted overlapped pipes and reserve/bind before any worker code runs.

    The caller supplies its real long-lived ResourceOwner. Only the selected
    stdin/stdout/stderr handles are inherited, never the job or other live pipes.
    No command, argument, environment or credential is persisted in the ledger.
    """
    if allow_termination is not True:
        raise ValueError("the app must authorize private stdio-tree termination")
    if os.name != "nt":
        raise OSError("owned stdio currently requires Windows")
    loop = asyncio.get_running_loop()
    if not isinstance(loop, asyncio.ProactorEventLoop):
        raise OSError("owned stdio requires the Windows Proactor event loop; no fallback")
    if (
        type(max_processes) is not int
        or not 1 <= max_processes <= 256
        or isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, (int, float))
        or not math.isfinite(grace_seconds)
        or not 0 <= grace_seconds <= 10
    ):
        raise ValueError("invalid owned stdio process limit or grace period")
    import msvcrt
    from asyncio.windows_utils import PipeHandle, pipe

    import win32api
    import win32con

    handles, children = [], []
    read_transport = write_transport = None
    process = None
    try:
        child_read, parent_write = pipe(overlapped=(False, True), duplex=True)
        handles.extend([PipeHandle(child_read), PipeHandle(parent_write)])
        child_stdin, parent_stdin = handles[-2:]
        children.append(child_stdin)
        parent_read, child_write = pipe(overlapped=(True, False))
        handles.extend([PipeHandle(parent_read), PipeHandle(child_write)])
        parent_stdout, child_stdout = handles[-2:]
        children.append(child_stdout)
        current = win32api.GetCurrentProcess()
        child_stderr = win32api.DuplicateHandle(
            current,
            msvcrt.get_osfhandle(errlog.fileno()),
            current,
            0,
            True,
            win32con.DUPLICATE_SAME_ACCESS,
        )
        children.append(child_stderr)
        selected = (child_stdin.fileno(), child_stdout.fileno(), int(child_stderr))
        for handle in selected:
            os.set_handle_inheritable(handle, True)
        reader = asyncio.StreamReader(limit=65536)
        read_protocol = _ReadProtocol(reader)
        read_transport, _ = await loop.connect_read_pipe(lambda: read_protocol, parent_stdout)
        write_protocol = _WriteProtocol()
        write_transport, _ = await loop.connect_write_pipe(lambda: write_protocol, parent_stdin)
        launch = owner.launch(
            argv,
            label,
            allow_termination=True,
            cwd=cwd,
            env=env,
            max_processes=max_processes,
            stdio_handles=selected,
        )
        # No await after creation until the finally-protected process is installed.
        process = OwnedByteProcess(
            launch,
            _SendPipe(write_transport, write_protocol),
            _ReceivePipe(reader, read_transport, read_protocol),
            grace_seconds,
        )
        for handle in children:
            handle.close() if hasattr(handle, "close") else handle.Close()
        children.clear()
        yield process
    finally:
        try:
            if process is not None:
                await process.aclose()
        finally:
            if write_transport is not None:
                write_transport.abort()
            if read_transport is not None:
                read_transport.close()
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(1):
                    if write_transport is not None:
                        await write_protocol.closed.wait()
                    if read_transport is not None:
                        await read_protocol.closed.wait()
            for handle in children:
                handle.close() if hasattr(handle, "close") else handle.Close()
            for handle in handles:
                handle.close()


class OwnedStdioTransport:
    """Host-local stdio_client_factory using one app supervisor's durable owner.

    Opt-in only. Message encoding follows the pinned SDK's public types. Native
    state, shutdown refusal and barriers still belong to the embedding app.
    """

    def __init__(
        self,
        owner,
        *,
        allow_termination=False,
        label="App stdio backend",
        max_processes=32,
        grace_seconds=2,
        max_message_bytes=64 * 1024 * 1024,
    ):
        if allow_termination is not True:
            raise ValueError("the app must authorize private stdio-tree termination")
        if type(max_message_bytes) is not int or not 1 <= max_message_bytes <= 512 * 1024 * 1024:
            raise ValueError("invalid stdio message budget")
        self.owner, self.label = owner, label
        self.max_processes, self.grace_seconds = max_processes, grace_seconds
        self.max_message_bytes = max_message_bytes

    @asynccontextmanager
    async def __call__(self, server, *, errlog):
        env = get_default_environment() | (server.env or {})
        command = shutil.which(server.command, path=env.get("PATH"))
        if command is None:
            raise ValueError("owned stdio executable could not be resolved")
        async with owned_byte_process(
            self.owner,
            [os.path.abspath(command), *server.args],
            errlog=errlog,
            label=self.label,
            cwd=server.cwd,
            env=env,
            max_processes=self.max_processes,
            grace_seconds=self.grace_seconds,
            allow_termination=True,
        ) as process:
            async with message_streams(
                process, server, max_message_bytes=self.max_message_bytes
            ) as streams:
                yield streams


@asynccontextmanager
async def message_streams(process, server, *, max_message_bytes):
    """SDK-type bridge, also testable with harmless non-MCP byte echo workers.

    Adapted from the SDK 2.3 stdio message pumps; SDK_STDIO_LICENSE.txt retains
    its MIT notice. Parse errors are redacted and frame budgets are explicit.
    """
    read_sender, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_receiver = anyio.create_memory_object_stream[SessionMessage](0)
    writer_done = anyio.Event()

    async def stdout_reader():
        fragments = io.StringIO()
        try:
            async with read_sender:
                size = 0
                text = TextReceiveStream(
                    process.stdout, encoding=server.encoding, errors=server.encoding_error_handler
                )
                async for chunk in text:
                    parts = chunk.split("\n")
                    for index, part in enumerate(parts):
                        size += len(
                            part.encode(server.encoding, errors=server.encoding_error_handler)
                        )
                        if size > max_message_bytes:
                            await read_sender.send(
                                ValueError("stdio frame exceeds its byte budget")
                            )
                            return
                        fragments.write(part)
                        if index == len(parts) - 1:
                            continue
                        line = fragments.getvalue()
                        fragments.close()
                        fragments, size = io.StringIO(), 0
                        try:
                            value = SessionMessage(
                                jsonrpc_message_adapter.validate_json(line, by_name=False)
                            )
                        except ValueError:
                            value = ValueError("invalid SDK JSON-RPC frame")
                        await read_sender.send(value)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError, OSError):
            pass
        finally:
            fragments.close()
            # Once session delivery closes, drain so a flushing worker can read EOF.
            with anyio.CancelScope(shield=True):
                with suppress(
                    anyio.EndOfStream,
                    anyio.ClosedResourceError,
                    anyio.BrokenResourceError,
                    ConnectionError,
                    OSError,
                ):
                    while True:
                        await process.stdout.receive()

    async def stdin_writer():
        try:
            async with write_receiver:
                async for value in write_receiver:
                    data = (
                        value.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                    ).encode(server.encoding, errors=server.encoding_error_handler)
                    if len(data) - 1 > max_message_bytes:
                        raise ValueError("stdio frame exceeds its byte budget")
                    await process.stdin.send(data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError, ValueError):
            await read_sender.aclose()
        finally:
            writer_done.set()

    async with anyio.create_task_group() as group:
        group.start_soon(stdout_reader)
        group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            with anyio.CancelScope(shield=True):
                read_stream.close()
                write_stream.close()
                with anyio.move_on_after(0.5):
                    await writer_done.wait()
                await process.aclose()
                for stream in (read_stream, write_stream, read_sender, write_receiver):
                    await stream.aclose()
            group.cancel_scope.cancel()
