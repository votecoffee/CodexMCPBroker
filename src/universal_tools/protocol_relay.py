"""Explicit, principal-scoped federation of native MCP discovery/read methods."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import secrets
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass

from mcp.client.subscriptions import listen
from mcp.server.extension import Extension
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import (
    SUBSCRIPTION_ID_META_KEY,
    PromptsListChanged,
    ResourcesListChanged,
    ResourceUpdated,
    event_to_notification,
)
from mcp.types import (
    INVALID_PARAMS,
    Completion,
    PaginatedRequestParams,
    PromptReference,
    ResourceTemplateReference,
)

from .budgets import DEFAULT_JSON_BUDGET
from .identity import client_identity
from .models import BrokerError, canonical_digest


@dataclass(frozen=True)
class RelaySource:
    """Trusted app configuration, never a source supplied by model arguments.

    connect(principal) returns an async ClientSession context manager with
    app-selected credentials and lifecycle. authorize(principal, method, target)
    must raise on denial, including subscription delivery after revocation.
    """

    id: str
    revision: str
    connect: object
    authorize: object

    def __post_init__(self):
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.id):
            raise ValueError("invalid relay source ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.revision):
            raise ValueError("invalid relay source revision")
        if not callable(self.connect) or not callable(self.authorize):
            raise ValueError(
                "relay sources require explicit connection and authorization callbacks"
            )


class ProtocolRelay(Extension):
    identifier = "io.github.votecoffee/protocol-relay"
    LISTS = {
        "resources/list": ("list_resources", "resources", "uri"),
        "resources/templates/list": ("list_resource_templates", "resourceTemplates", "uriTemplate"),
        "prompts/list": ("list_prompts", "prompts", "name"),
    }

    def __init__(self, sources, *, page_size=100, max_entries=2000, max_bytes=4 * 1024 * 1024):
        sources = tuple(sources)
        self.sources = {s.id: s for s in sources}
        if len(sources) != len(self.sources):
            raise ValueError("duplicate relay source IDs")
        if not 1 <= len(self.sources) <= 32 or not 1 <= page_size <= max_entries <= 10_000:
            raise ValueError("invalid relay source/list limits")
        if not 1024 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid relay payload budget")
        self.page_size, self.max_entries, self.max_bytes = page_size, max_entries, max_bytes
        self._cursor_key = secrets.token_bytes(32)
        self.request_middleware = self.dispatch

    def _bounded(self, value):
        DEFAULT_JSON_BUDGET.check(value)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > self.max_bytes:
            raise BrokerError("relayed response exceeds its configured budget")
        return value

    @staticmethod
    def uri(source, original):
        prefix = "ui://relay/" if original.startswith("ui://") else "relay://"
        return prefix + source.id + "/" + source.revision + "/" + original

    @staticmethod
    def prompt(source, original):
        return source.id + ":" + source.revision + ":" + original

    def _route(self, target, *, prompt=False):
        if prompt:
            parts = target.split(":", 2)
        else:
            prefix = "ui://relay/" if target.startswith("ui://relay/") else "relay://"
            if not target.startswith(prefix):
                return None
            parts = target.removeprefix(prefix).split("/", 2)
        if len(parts) != 3 or parts[0] not in self.sources:
            return None if prompt else self._bad_route()
        source = self.sources[parts[0]]
        if parts[1] != source.revision or not parts[2]:
            raise BrokerError("relay source revision changed; rediscover the source")
        return source, parts[2]

    @staticmethod
    def _bad_route():
        raise BrokerError("unknown relay resource")

    async def _authorize(self, source, owner, method, target):
        await source.authorize(owner, method, target)

    def _cursor(self, owner, method, digest, offset):
        raw = json.dumps([owner, method, digest, offset], separators=(",", ":")).encode()
        signature = hmac.digest(self._cursor_key, raw, "sha256")
        return base64.urlsafe_b64encode(signature + raw).decode()

    def _offset(self, cursor, owner, method, digest):
        if cursor is None:
            return 0
        try:
            if len(cursor) > 8192:
                raise ValueError
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            values = json.loads(payload)
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, "sha256")):
                raise ValueError
            if values[:3] != [owner, method, digest] or type(values[3]) is not int:
                raise ValueError
            return values[3]
        except Exception:
            raise BrokerError("relay cursor expired or changed; list again") from None

    async def _listing(self, ctx, call_next, owner):
        method, key, field = self.LISTS[ctx.method]
        # The native SDK decorators have no pagination; our cursor applies to
        # the combined, bounded inventory rather than a downstream cursor.
        from dataclasses import replace

        local_params = dict(ctx.params or {})
        local_params.pop("cursor", None)
        local = await call_next(replace(ctx, params=local_params))
        value = (
            local.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(local, "model_dump")
            else local
        )
        entries = list(value.get(key, []))
        for source in self.sources.values():
            await self._authorize(source, owner, ctx.method, "")
            async with source.connect(owner) as session:
                cursor = None
                seen = set()
                for _ in range(32):
                    result = await getattr(session, method)(
                        params=PaginatedRequestParams(cursor=cursor)
                    )
                    raw = self._bounded(
                        result.model_dump(mode="json", by_alias=True, exclude_none=True)
                    )
                    for item in raw[key]:
                        item[field] = (
                            self.prompt(source, item[field])
                            if field == "name"
                            else self.uri(source, item[field])
                        )
                        item.setdefault("_meta", {})["io.github.votecoffee/origin"] = {
                            "source": source.id,
                            "revision": source.revision,
                        }
                        entries.append(item)
                    if len(entries) > self.max_entries:
                        raise BrokerError("relayed inventory exceeds its entry budget")
                    cursor = raw.get("nextCursor")
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise BrokerError("downstream pagination repeated a cursor")
                    seen.add(cursor)
                else:
                    raise BrokerError("downstream pagination exceeds its page budget")
        self._bounded(entries)
        if len({item[field] for item in entries}) != len(entries):
            raise BrokerError("relayed inventory has conflicting names")
        digest = canonical_digest(entries)
        offset = self._offset((ctx.params or {}).get("cursor"), owner, ctx.method, digest)
        if offset < 0 or offset > len(entries):
            raise BrokerError("invalid relay cursor offset")
        result = {key: entries[offset : offset + self.page_size], "resultType": "complete"}
        if ctx.protocol_version == "2026-07-28":
            result.update(ttlMs=0, cacheScope="private")
        if offset + self.page_size < len(entries):
            result["nextCursor"] = self._cursor(owner, ctx.method, digest, offset + self.page_size)
        return result

    async def dispatch(self, ctx, call_next):
        methods = {
            *self.LISTS,
            "resources/read",
            "prompts/get",
            "completion/complete",
            "subscriptions/listen",
        }
        if ctx.method not in methods:
            return await call_next(ctx)
        owner = client_identity.get()
        if not owner:
            raise MCPError(INVALID_PARAMS, "protocol relay requires an authenticated principal")
        try:
            if ctx.method in self.LISTS:
                return await self._listing(ctx, call_next, owner)
            if ctx.method == "subscriptions/listen":
                return await self._listen(ctx, call_next, owner)
            params = deepcopy(ctx.params or {})
            if params.get("requestState") or params.get("inputResponses"):
                raise BrokerError(
                    "relayed reads must use app-owned authentication before retrieval"
                )
            if ctx.method == "completion/complete":
                ref = params["ref"]
                is_prompt = ref.get("type") == "ref/prompt"
                field = "name" if is_prompt else "uri"
                target = ref[field]
            else:
                is_prompt = ctx.method == "prompts/get"
                field = "name" if is_prompt else "uri"
                target = params[field]
            route = self._route(target, prompt=is_prompt)
            if route is None:
                return await call_next(ctx)
            source, original = route
            await self._authorize(source, owner, ctx.method, original)
            async with source.connect(owner) as session:
                if ctx.method == "resources/read":
                    result = await session.read_resource(original)
                    result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for content in result["contents"]:
                        content["uri"] = self.uri(source, content["uri"])
                elif ctx.method == "prompts/get":
                    result = await session.get_prompt(original, arguments=params.get("arguments"))
                    result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                else:
                    reference = (
                        PromptReference(name=original)
                        if is_prompt
                        else ResourceTemplateReference(uri=original)
                    )
                    result = await session.complete(
                        reference,
                        argument=params["argument"],
                        context_arguments=params.get("context", {}).get("arguments"),
                    )
                    result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if (
                ctx.method in {"resources/read", "prompts/get"}
                and ctx.protocol_version == "2026-07-28"
            ):
                result.update(ttlMs=0, cacheScope="private")
            return self._bounded(result)
        except MCPError:
            raise
        except Exception:
            raise MCPError(
                INVALID_PARAMS,
                "relay request unavailable or denied; rediscover and check source authorization",
            ) from None

    async def _listen(self, ctx, call_next, owner):
        from dataclasses import replace

        requested = (ctx.params or {}).get("notifications", {})
        uris = requested.get("resourceSubscriptions", [])
        groups = {s.id: [] for s in self.sources.values()}
        for uri in uris:
            route = self._route(uri)
            if route:
                groups[route[0].id].append(route[1])
        tasks = []
        acknowledged = asyncio.Event()

        class AckSession:
            def __getattr__(self, name):
                return getattr(ctx.session, name)

            async def send_notification(self, notification, **options):
                await ctx.session.send_notification(notification, **options)
                if notification.method == "notifications/subscriptions/acknowledged":
                    acknowledged.set()

        async with AsyncExitStack() as stack:
            for source in self.sources.values():
                selected = groups[source.id]
                prompt_changes = requested.get("promptsListChanged") is True
                resource_changes = requested.get("resourcesListChanged") is True
                if not (selected or prompt_changes or resource_changes):
                    continue
                await self._authorize(source, owner, "subscriptions/listen", "")
                for uri in selected:
                    await self._authorize(source, owner, "resources/read", uri)
                session = await stack.enter_async_context(source.connect(owner))
                subscription = await stack.enter_async_context(
                    listen(
                        session,
                        prompts_list_changed=prompt_changes,
                        resources_list_changed=resource_changes,
                        resource_subscriptions=selected,
                    )
                )

                async def forward(source=source, subscription=subscription):
                    await acknowledged.wait()
                    async for event in subscription:
                        await self._authorize(source, owner, "subscriptions/listen", "")
                        if isinstance(event, ResourceUpdated):
                            await self._authorize(source, owner, "resources/read", event.uri)
                            event = ResourceUpdated(self.uri(source, event.uri))
                        elif not isinstance(event, PromptsListChanged | ResourcesListChanged):
                            continue
                        # Delivered only on this principal's stream, never a global bus.
                        await ctx.session.send_notification(
                            event_to_notification(
                                event, {SUBSCRIPTION_ID_META_KEY: ctx.request_id}
                            ),
                            related_request_id=ctx.request_id,
                        )

                tasks.append(asyncio.create_task(forward()))
            main = asyncio.create_task(call_next(replace(ctx, session=AckSession())))
            try:
                done, _ = await asyncio.wait([main, *tasks], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if main not in done:
                    raise BrokerError("source notification stream ended; reconnect and refetch")
                return main.result()
            finally:
                for task in [main, *tasks]:
                    task.cancel()
                await asyncio.gather(main, *tasks, return_exceptions=True)

    def install(self, server):
        # Advertise completions even when this server has only relayed references.
        if server._lowlevel_server.get_request_handler("completion/complete") is None:

            @server.completion()
            async def complete(ref, argument, context):
                return Completion(values=[])
