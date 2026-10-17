"""Bounded initialization snapshots; app capabilities are not broker relay support."""

import json

from mcp.types import InitializeResult

from .models import BrokerError

BROKER_SUPPORT = {
    "tool_calls": True,
    "resources": False,
    "prompts": False,
    "completions": False,
    "tasks": False,
    "server_requests": False,
    "elicitation_form": True,
    "elicitation_url": False,
    "notifications": False,
}


def initialization_metadata(result: InitializeResult) -> dict:
    raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    metadata = {
        name: raw[name]
        for name in ("protocolVersion", "serverInfo", "capabilities", "instructions")
        if name in raw
    }
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
        raise BrokerError("app initialization metadata exceeds 128 KiB")
    return metadata
