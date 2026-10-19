"""Bounded, observational progress for one active request; no background routing."""

from __future__ import annotations

import asyncio
import math

import anyio

MAX_UPDATES = 4096  # Covers a full 300-second app deadline at the 10 Hz send rate.
MAX_MESSAGE_BYTES = 4096
MIN_INTERVAL = 0.1
SEND_TIMEOUT = 0.5


class ProgressRelay:
    def __init__(self, session, request_id, token):
        self.session = session
        self.request_id = request_id
        self.token = token
        self.label = ""
        self.closed = False
        self.received = 0
        self.previous = None
        self.pending = None
        self.task = None
        self.sent_at = None

    async def offer(self, progress, total=None, message=None):
        # The SDK awaits this callback in its receive loop. Never wait on the
        # upstream client here: one pending update coalesces bursts in bounded space.
        if self.closed or self.received >= MAX_UPDATES:
            return
        self.received += 1
        try:
            for value in (progress, total):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    return
            if progress is None or (self.previous is not None and progress <= self.previous):
                return
            if message is not None:
                if not isinstance(message, str) or len(message) > MAX_MESSAGE_BYTES:
                    return
                message = self.label + message
                if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    return
        except (OverflowError, UnicodeError):
            return
        self.previous = progress
        self.pending = (progress, total, message)
        if self.task is None:
            self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while self.pending is not None and not self.closed:
                loop = asyncio.get_running_loop()
                if self.sent_at is not None:
                    await asyncio.sleep(max(0, self.sent_at + MIN_INTERVAL - loop.time()))
                if self.closed:
                    return
                progress, total, message = self.pending
                self.pending = None
                try:
                    async with asyncio.timeout(SEND_TIMEOUT):
                        await self.session.send_progress_notification(
                            progress_token=self.token,
                            progress=progress,
                            total=total,
                            message=message,
                            related_request_id=self.request_id,
                        )
                except Exception:
                    # A disconnected/slow receiver loses telemetry, never the
                    # app result or its write outcome. Do not log app content.
                    self.close()
                    return
                self.sent_at = loop.time()
        finally:
            self.task = None

    def close(self):
        self.closed = True
        self.pending = None
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()

    async def aclose(self):
        task = self.task
        self.close()
        if task is not None:
            with anyio.CancelScope(shield=True):
                await asyncio.shield(asyncio.gather(task, return_exceptions=True))
