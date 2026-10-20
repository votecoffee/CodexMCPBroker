"""Process-local aggregate timings and exporter-neutral tracing without app payloads."""

import time
from collections import Counter
from contextlib import contextmanager
from threading import Lock

from opentelemetry import trace

_lock = Lock()
_counts = Counter()
_seconds = Counter()


def queue_wait(elapsed):
    with _lock:
        _counts["queue_wait.returned"] += 1
        _seconds["queue_wait"] += elapsed


@contextmanager
def measured(stage):
    # Only framework-defined labels enter metrics. Never include arguments, URIs,
    # credentials, principals, form answers, exception text or native results.
    if stage not in {"backend_call", "discovery", "queue_wait"}:
        raise ValueError("unknown timing stage")
    start = time.monotonic()
    outcome = "failed"
    with trace.get_tracer("universal_tools").start_as_current_span(
        "framework." + stage, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield
            outcome = "returned"
        finally:
            elapsed = time.monotonic() - start
            span.set_attribute("framework.outcome", outcome)
            with _lock:
                _counts[stage + "." + outcome] += 1
                _seconds[stage] += elapsed


def snapshot():
    with _lock:
        return {"calls": dict(_counts), "seconds": dict(_seconds)}
