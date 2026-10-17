"""A dedicated task owns each SDK context and its serialized operations until close.

SDK transports contain AnyIO task groups/cancel scopes. Entering one in a request
task and exiting it in another task is invalid; retaining just ClientSession is
insufficient. All transport entry, work and teardown happen in this actor task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession

from .models import BrokerError, ServiceManifest


@dataclass
class Work:
    operation: Callable[[ClientSession], Awaitable[Any]]
    result: asyncio.Future


class SessionWorker:
    def __init__(self, manifest: ServiceManifest, connect):
        self.manifest = manifest
        self.connect = connect
        self.queue: asyncio.Queue[Work | None] = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.failed = False
        self.closing = False
        self.task = asyncio.create_task(self._run(), name=f"app-session:{manifest.id}")
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
                async with asyncio.timeout(self.manifest.timeout_seconds):
                    session = await stack.enter_async_context(self.connect(self.manifest))
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

    async def execute(self, operation):
        await asyncio.shield(self.ready)
        if not self.alive:
            raise BrokerError("service context is lost; release_service before reacquiring")
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(self._consume_exception)
        self.queue.put_nowait(Work(operation, result))
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            # Do not transfer ownership while an abandoned operation is still running.
            # Its own bounded deadline and SDK cleanup run before the caller unlocks.
            await self.close()
            raise

    async def close(self):
        if not self.closing:
            self.closing = True
            self.queue.put_nowait(None)
        await asyncio.shield(self.task)
