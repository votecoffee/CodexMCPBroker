"""Request-scoped form relay. No saved approvals, fallback answers or ambient routing."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import anyio
from jsonschema import Draft202012Validator
from mcp.types import (
    INVALID_REQUEST,
    ElicitRequestFormParams,
    ElicitResult,
    ErrorData,
)

from .progress import ProgressRelay

FORM_TIMEOUT = 60


def rejected(message: str) -> ErrorData:
    return ErrorData(code=INVALID_REQUEST, message=message)


def form_validator(params: ElicitRequestFormParams) -> Draft202012Validator:
    if len(params.model_dump_json().encode("utf-8")) > 32 * 1024:
        raise ValueError("form exceeds relay budget")
    schema = params.requestedSchema

    def visit(value):
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                raise ValueError("form references are not supported")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    if schema.get("type") != "object" or not isinstance(schema.get("properties", {}), dict):
        raise ValueError("form must describe an object")
    for field in schema.get("properties", {}).values():
        kind = field.get("type") if isinstance(field, dict) else None
        if kind == "array":
            item = field.get("items", {})
            if not isinstance(item, dict) or item.get("type", "string") != "string":
                raise ValueError("form arrays must contain enum choices")
            choices = item.get("enum")
            if choices is None:
                variants = item.get("anyOf", [])
                if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
                    raise ValueError("form arrays must contain enum choices")
                choices = [variant.get("const") for variant in variants]
            if (
                not isinstance(choices, list)
                or not choices
                or not all(isinstance(v, str) for v in choices)
            ):
                raise ValueError("form arrays must contain string enum choices")
        elif kind not in {"string", "integer", "number", "boolean"}:
            raise ValueError("form fields must be primitive")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class ElicitationTarget:
    def __init__(
        self,
        session,
        request_id,
        *,
        label: str = "",
        timeout: float = FORM_TIMEOUT,
        progress_token=None,
    ):
        self.session = session
        self.request_id = request_id
        self.label = label
        self.timeout = timeout
        self.closed = asyncio.Event()
        self.requests = 0
        self.pending = False
        self.progress = (
            ProgressRelay(session, request_id, progress_token)
            if progress_token is not None
            else None
        )

    def close(self):
        self.closed.set()
        if self.progress is not None:
            self.progress.close()

    async def aclose(self):
        self.close()
        if self.progress is not None:
            await self.progress.aclose()

    def supports_form(self) -> bool:
        client = self.session.client_params
        capability = client.capabilities.elicitation if client else None
        return capability is not None and (
            capability.form is not None or capability.url is None  # Legacy {} means form.
        )

    async def forward(self, params):
        if self.closed.is_set() or self.pending:
            return rejected("no active confirmation route, or a form is already pending")
        if not isinstance(params, ElicitRequestFormParams) or params.task is not None:
            return rejected("only ordinary form elicitation is supported")
        if not self.supports_form():
            return rejected("the calling client does not support form elicitation")
        self.requests += 1
        if self.requests > 8:
            return rejected("confirmation count exceeds this tool call's relay budget")
        try:
            validator = form_validator(params)
        except Exception:
            return rejected("unsupported or oversized elicitation form")
        self.pending = True
        request = asyncio.create_task(
            self.session.elicit_form(
                self.label + params.message,
                deepcopy(params.requestedSchema),
                related_request_id=self.request_id,
            )
        )
        invalidated = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (request, invalidated),
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self.closed.is_set() or request not in done:
                return rejected("confirmation expired or its originating call ended")
            result = request.result()
            if len(result.model_dump_json().encode("utf-8")) > 16 * 1024:
                return rejected("elicitation response exceeds relay budget")
            if result.action == "accept":
                content = result.content if result.content is not None else {}
                json.dumps(content, allow_nan=False)
                validator.validate(content)
                return ElicitResult(action="accept", content=deepcopy(result.content))
            # Do not turn a decline/cancel into approval through leftover content.
            return ElicitResult(action=result.action)
        except asyncio.CancelledError:
            self.close()
            raise
        except Exception:
            return rejected("elicitation failed or returned an invalid response")
        finally:
            for task in (request, invalidated):
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.shield(asyncio.gather(request, invalidated, return_exceptions=True))
            self.pending = False


class ElicitationRouter:
    def __init__(self):
        self.target: ElicitationTarget | None = None

    async def callback(self, _, params):
        target = self.target
        if target is None:
            return rejected("elicitation is only available during its originating tool call")
        result = await target.forward(params)
        if self.target is not target or target.closed.is_set():
            return rejected("the originating tool call ended")
        return result

    @asynccontextmanager
    async def bind(self, target):
        if self.target is not None:
            raise RuntimeError("a confirmation route is already bound")
        self.target = target
        try:
            yield
        finally:
            self.target = None
            if target is not None:
                target.close()


async def routed_call(session, name, arguments, target: ElicitationTarget | None):
    router = getattr(session, "elicitation_router", None)
    options = {}
    if target is not None and target.progress is not None:
        options["progress_callback"] = target.progress.offer
    try:
        if router is None:  # Synthetic sessions and clients without form relay support.
            return await session.call_tool(name, arguments, **options)
        async with router.bind(target):
            return await session.call_tool(name, arguments, **options)
    finally:
        if target is not None:
            await target.aclose()
