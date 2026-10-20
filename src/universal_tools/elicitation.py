"""Request-scoped form relay. No saved approvals, fallback answers or ambient routing."""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from urllib.parse import urlsplit

import anyio
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_REQUEST,
    CallToolResult,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    ErrorData,
    TextContent,
)

from .budgets import DEFAULT_JSON_BUDGET
from .diagnostics import measured
from .progress import ProgressRelay

FORM_TIMEOUT = 60


def rejected(message: str) -> ErrorData:
    return ErrorData(code=INVALID_REQUEST, message=message)


def form_validator(params: ElicitRequestFormParams) -> Draft202012Validator:
    DEFAULT_JSON_BUDGET.check(params.requested_schema)
    if len(params.model_dump_json().encode("utf-8")) > 32 * 1024:
        raise ValueError("form exceeds relay budget")
    schema = params.requested_schema

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


def input_validator(params):
    """Validate an ordinary form or URL without fetching or opening the URL."""
    if not isinstance(params, ElicitRequestFormParams | ElicitRequestURLParams) or params.task:
        raise ValueError("only ordinary form or URL elicitation is supported")
    if isinstance(params, ElicitRequestFormParams):
        return form_validator(params)
    parts = urlsplit(params.url)
    if (
        len(params.model_dump_json().encode("utf-8")) > 32 * 1024
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ord(c) < 33 for c in params.url)
        or (
            parts.scheme != "https"
            and not (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"})
        )
        or (params.elicitation_id is not None and not 1 <= len(params.elicitation_id) <= 128)
    ):
        raise ValueError("URL elicitation requires HTTPS or literal loopback and a bounded ID")
    return None


def validate_answer(params, answer):
    """URL acceptance acknowledges navigation, never credential or job completion."""
    validator = input_validator(params)
    answer = ElicitResult.model_validate(answer)
    if len(answer.model_dump_json().encode("utf-8")) > 16 * 1024:
        raise ValueError("elicitation response exceeds relay budget")
    if answer.action != "accept":
        return ElicitResult(action=answer.action)
    if validator is None:
        if answer.content:
            raise ValueError("URL answers cannot carry form content or credentials")
        return ElicitResult(action="accept")
    json.dumps(answer.content, allow_nan=False)
    validator.validate(answer.content if answer.content is not None else {})
    return ElicitResult(action="accept", content=deepcopy(answer.content))


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
        capabilities = client.capabilities if client else None
        capability = capabilities.elicitation if capabilities else None
        return capability is not None and (
            capability.form is not None or capability.url is None  # Legacy {} means form.
        )

    def supports_url(self) -> bool:
        client = self.session.client_params
        capabilities = client.capabilities if client else None
        return (
            capabilities is not None
            and capabilities.elicitation is not None
            and (capabilities.elicitation.url is not None)
        )

    async def forward(self, params):
        if self.closed.is_set() or self.pending:
            return rejected("no active confirmation route, or a form is already pending")
        is_url = isinstance(params, ElicitRequestURLParams)
        if not (self.supports_url() if is_url else self.supports_form()):
            return rejected("the calling client does not support this elicitation mode")
        self.requests += 1
        if self.requests > 8:
            return rejected("confirmation count exceeds this tool call's relay budget")
        try:
            input_validator(params)
        except Exception:
            return rejected("unsupported or oversized elicitation form")
        self.pending = True
        if is_url:
            operation = self.session.elicit_url(
                self.label + params.message,
                params.url,
                params.elicitation_id or secrets.token_urlsafe(24),
                related_request_id=self.request_id,
            )
        else:
            operation = self.session.elicit_form(
                self.label + params.message,
                deepcopy(params.requested_schema),
                related_request_id=self.request_id,
            )
        request = asyncio.create_task(operation)
        invalidated = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (request, invalidated),
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self.closed.is_set() or request not in done:
                return rejected("confirmation expired or its originating call ended")
            return validate_answer(params, request.result())
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
        self.rejection: ErrorData | None = None
        self._received_target: ContextVar[ElicitationTarget | None] = ContextVar(
            "received_elicitation_target"
        )

    @contextmanager
    def capture(self):
        # SDK callbacks run in child tasks. Preserve the route at message
        # receipt, including an absent route, before scheduling that task.
        token = self._received_target.set(self.target)
        try:
            yield
        finally:
            self._received_target.reset(token)

    async def callback(self, _, params):
        target = self._received_target.get(self.target)
        if target is None or target is not self.target or target.closed.is_set():
            error = rejected("elicitation is only available during its originating tool call")
            if target is self.target:
                self.rejection = error
            return error
        result = await target.forward(params)
        if self.target is not target or target.closed.is_set():
            return rejected("the originating tool call ended")
        if isinstance(result, ErrorData):
            self.rejection = result
        return result

    @asynccontextmanager
    async def bind(self, target):
        if self.target is not None:
            raise RuntimeError("a confirmation route is already bound")
        self.rejection = None
        self.target = target
        try:
            yield
        finally:
            self.target = None
            if target is not None:
                target.close()


async def routed_call(session, name, arguments, target: ElicitationTarget | None):
    with measured("backend_call"):
        return await _routed_call(session, name, arguments, target)


async def _routed_call(session, name, arguments, target):
    router = getattr(session, "elicitation_router", None)
    options = {}
    if target is not None and target.progress is not None:
        options["progress_callback"] = target.progress.offer
    try:
        if router is None:  # Synthetic sessions and clients without form relay support.
            return await session.call_tool(name, arguments, **options)
        async with router.bind(target):
            try:
                return await session.call_tool(name, arguments, **options)
            except MCPError as error:
                # SDK 2 propagates an app handler's MCPError as a JSON-RPC error.
                # Normalize only the exact refusal generated by this call's form
                # relay; never turn transport failures or unrelated RPC errors
                # into ordinary app errors or fabricate an elicitation answer.
                refusal = router.rejection
                if refusal is None or (error.code, error.message) != (
                    refusal.code,
                    refusal.message,
                ):
                    raise
                return CallToolResult(
                    content=[TextContent(type="text", text=refusal.message)],
                    is_error=True,
                    meta={"universal_tools/elicitation_error": {"code": refusal.code}},
                )
    finally:
        if target is not None:
            await target.aclose()
