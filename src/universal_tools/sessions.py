"""A dedicated task owns each SDK context and its serialized operations until close.

SDK transports contain AnyIO task groups/cancel scopes. Entering one in a request
task and exiting it in another task is invalid; retaining just ClientSession is
insufficient. All transport entry, work and teardown happen in this actor task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from mcp import ClientSession

from .models import BrokerError


class SessionConfig(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def timeout_seconds(self) -> float: ...


@dataclass
class Work:
    operation: Callable[[ClientSession], Awaitable[Any]]
    result: asyncio.Future


class SessionWorker:
    def __init__(
        self, manifest: SessionConfig, connect, *, initialization_timeout: float | None = None
    ):
        self.manifest = manifest
        self.connect = connect
        self.initialization_timeout = (
            manifest.timeout_seconds if initialization_timeout is None else initialization_timeout
        )
        self.queue: asyncio.Queue[Work | None] = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.failed = False
        self.closing = False
        self.initialization: dict | None = None
        self.task = asyncio.create_task(self._run(), name=f"mcp-session:{manifest.id}")
        self.ready.add_done_callback(self._consume_exception)

    @staticmethod
    def _consume_exception(future):
        if not future.cancelled():
            future.exception()

    @property
    def alive(self) -> bool:
        return not self.failed and not self.closing and not self.task.done()

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.initialization_timeout):
                    session = await stack.enter_async_context(self.connect(self.manifest))
                    self.initialization = deepcopy(session.initialization)
                self.ready.set_result(None)
                while (work := await self.queue.get()) is not None:
                    try:
                        async with asyncio.timeout(self.manifest.timeout_seconds):
                            value = await work.operation(session)
                    except BrokerError as error:
                        work.result.set_exception(error)
                    except BaseException as error:
                        self.failed = True
                        work.result.set_exception(
                            error if isinstance(error, Exception) else BrokerError("context lost")
                        )
                        break
                    else:
                        work.result.set_result(value)
        except BaseException:
            self.failed = True
        finally:
            if not self.ready.done():
                self.ready.set_exception(BrokerError("app session initialization failed"))
            while not self.queue.empty():
                work = self.queue.get_nowait()
                if work is not None and not work.result.done():
                    work.result.set_exception(BrokerError("service context is lost"))

    async def execute(self, operation, *, on_cancel=None):
        try:
            await asyncio.shield(self.ready)
            if not self.alive:
                raise BrokerError("service context is lost; release_service before reacquiring")
            result = asyncio.get_running_loop().create_future()
            result.add_done_callback(self._consume_exception)
            self.queue.put_nowait(Work(operation, result))
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            # Do not transfer ownership while an abandoned operation is still running.
            # Its own bounded deadline and SDK cleanup run before the caller unlocks.
            await self.close()
            raise

    async def close(self):
        if not self.closing:
            self.closing = True
            self.queue.put_nowait(None)
        # SDK request responders use level-triggered AnyIO cancellation. Shield
        # cleanup from that scope as well as shielding the actor from Task.cancel.
        with anyio.CancelScope(shield=True):
            await asyncio.shield(self.task)


class SessionPool:
    """Lazily reuse bounded actors; each borrower owns one channel until cleanup.

    A failed or abandoned operation is never replayed. Only a later explicit
    invocation can create a replacement channel. This pool owns connections,
    not downstream app leases or their durable recovery barriers.
    """

    def __init__(
        self, config: SessionConfig, connect, *, size: int = 4, initialization_timeout: float = 20
    ):
        if size < 1:
            raise ValueError("session pool size must be positive")
        self.config = config
        self.connect = connect
        self.size = size
        self.initialization_timeout = initialization_timeout
        self._condition = asyncio.Condition()
        self._workers: dict[SessionWorker, bool] = {}
        self._closed = False

    async def _take(self) -> SessionWorker:
        async with self._condition:
            while not self._closed:
                for worker, busy in self._workers.items():
                    if not busy:
                        self._workers[worker] = True
                        return worker
                if len(self._workers) < self.size:
                    worker = SessionWorker(
                        self.config,
                        self.connect,
                        initialization_timeout=self.initialization_timeout,
                    )
                    self._workers[worker] = True
                    return worker
                await self._condition.wait()
        raise BrokerError("frontend connection pool is closed")

    async def execute(self, operation, *, on_cancel=None):
        worker = await self._take()
        try:
            return await worker.execute(operation, on_cancel=on_cancel)
        finally:
            # AnyIO request cancellation must not strand a borrowed channel.
            with anyio.CancelScope(shield=True):
                if not worker.alive:
                    await worker.close()
                async with self._condition:
                    if self._closed or not worker.alive:
                        self._workers.pop(worker, None)
                    else:
                        self._workers[worker] = False
                    self._condition.notify_all()

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            async with self._condition:
                self._closed = True
                workers = list(self._workers)
                self._condition.notify_all()
            # Actors drain bounded work and exit their own SDK contexts.
            await asyncio.shield(asyncio.gather(*(worker.close() for worker in workers)))
            async with self._condition:
                self._workers.clear()
