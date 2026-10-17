"""Frontend-instance affinity; never accept an owner identity as a tool argument."""

from contextvars import ContextVar

CLIENT_HEADER = "x-universal-tools-client"
CLIENT_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"
client_identity: ContextVar[str | None] = ContextVar("broker_client_identity", default=None)
