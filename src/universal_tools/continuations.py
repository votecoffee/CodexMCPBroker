"""Bounded modern approval rounds retain one operation; they never replay a write."""

from __future__ import annotations

import asyncio
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace

from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_REQUEST,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequiredResult,
)

from .elicitation import ElicitationTarget
from .identity import client_identity
from .models import canonical_digest

active_route: ContextVar[ElicitationTarget | None] = ContextVar(
    "modern_approval_route", default=None
)


@dataclass(eq=False)
class Pending:
    owner: str
    fingerprint: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    answer: asyncio.Future | None = None
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None
    token: str | None = None
    busy: bool = False
    wire_session: object | None = None
    wire_request_id: object | None = None
    wire_progress_token: object | None = None

    async def send_progress_notification(self, **options):
        if self.wire_session is not None and self.wire_progress_token is not None:
            await self.wire_session.send_progress_notification(
                **{
                    **options,
                    "related_request_id": self.wire_request_id,
                    "progress_token": self.wire_progress_token,
                }
            )

    async def elicit_form(self, message, requested_schema, **_):
        self.answer = asyncio.get_running_loop().create_future()
        await self.queue.put(
            ElicitRequest(
                params=ElicitRequestFormParams(message=message, requested_schema=requested_schema)
            )
        )
        return await self.answer

    async def elicit_url(self, message, url, elicitation_id, **_):
        self.answer = asyncio.get_running_loop().create_future()
        await self.queue.put(
            ElicitRequest(
                params=ElicitRequestURLParams(
                    message=message, url=url, elicitation_id=elicitation_id
                )
            )
        )
        return await self.answer


class Continuations:
    def __init__(self, *, timeout=60.0, capacity=256):
        self.timeout, self.capacity = timeout, capacity
        self.tokens: dict[str, Pending] = {}
        self.operations: set[Pending] = set()

    def discard(self, pending, *, cancel=False):
        if pending.token:
            self.tokens.pop(pending.token, None)
        if pending.timer:
            pending.timer.cancel()
        if cancel and pending.task and not pending.task.done():
            pending.task.cancel()
        else:
            self.operations.discard(pending)

    async def run(self, ctx, params, invoke):
        owner = client_identity.get()
        if owner is None:
            raise MCPError(INVALID_REQUEST, "modern app calls require an authenticated frontend")
        fingerprint = canonical_digest({"name": params.name, "arguments": params.arguments or {}})
        if params.request_state:
            pending = self.tokens.get(params.request_state)
            if (
                pending is None
                or pending.owner != owner
                or pending.fingerprint != fingerprint
                or pending.busy
                or pending.answer is None
                or pending.answer.done()
            ):
                raise MCPError(
                    INVALID_REQUEST, "approval continuation is expired or does not match"
                )
            answers = params.input_responses or {}
            if set(answers) != {"confirmation"} or not isinstance(
                answers["confirmation"], ElicitResult
            ):
                raise MCPError(INVALID_REQUEST, "a matching form response is required")
            self.tokens.pop(params.request_state)
            pending.token = None
            if pending.timer:
                pending.timer.cancel()
            pending.answer.set_result(answers["confirmation"])
        else:
            if params.input_responses:
                raise MCPError(INVALID_REQUEST, "unsolicited approval responses are not accepted")
            if (
                len(self.operations) >= self.capacity
                or sum(p.owner == owner for p in self.operations) >= 8
            ):
                raise MCPError(INVALID_REQUEST, "too many pending app operations")
            pending = Pending(owner, fingerprint)
            # Preserve the actual caller's advertised form capability. Never invent it.
            pending.client_params = SimpleNamespace(capabilities=ctx.session.client_capabilities)
            route = ElicitationTarget(
                pending, ctx.request_id, progress_token=(ctx.meta or {}).get("progress_token")
            )

            async def work():
                token = active_route.set(route)
                try:
                    return await invoke(ctx, params)
                finally:
                    await route.aclose()
                    active_route.reset(token)

            pending.task = asyncio.create_task(work(), name="framework-approval-operation")

            def finished(task):
                if not task.cancelled():
                    task.exception()
                self.discard(pending)

            pending.task.add_done_callback(finished)
            self.operations.add(pending)
        pending.busy = True
        pending.wire_session = ctx.session
        pending.wire_request_id = ctx.request_id
        pending.wire_progress_token = (ctx.meta or {}).get("progress_token")
        question = asyncio.create_task(pending.queue.get())
        try:
            done, _ = await asyncio.wait(
                (pending.task, question), return_when=asyncio.FIRST_COMPLETED
            )
            if pending.task in done:
                self.discard(pending)
                return pending.task.result()
            pending.token = secrets.token_urlsafe(48)
            self.tokens[pending.token] = pending
            pending.timer = asyncio.get_running_loop().call_later(
                self.timeout, lambda: self.discard(pending, cancel=True)
            )
            return InputRequiredResult(
                input_requests={"confirmation": question.result()}, request_state=pending.token
            )
        except BaseException:
            self.discard(pending, cancel=True)
            raise
        finally:
            question.cancel()
            await asyncio.gather(question, return_exceptions=True)
            pending.busy = False
            pending.wire_session = None

    async def aclose(self):
        tasks = [p.task for p in self.operations]
        for pending in list(self.operations):
            self.discard(pending, cancel=True)
        await asyncio.gather(*tasks, return_exceptions=True)
