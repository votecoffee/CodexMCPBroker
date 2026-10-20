"""Iterative JSON shape limits, independent of byte and transport budgets."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import BrokerError


@dataclass(frozen=True)
class JsonBudget:
    max_depth: int = 64
    max_nodes: int = 100_000

    def __post_init__(self):
        if not 1 <= self.max_depth <= 128 or not 1 <= self.max_nodes <= 1_000_000:
            raise ValueError("invalid JSON shape budget")

    def check(self, value):
        # Iterator frames avoid copying a wide array or recursing on hostile input.
        stack = [(iter((value,)), 0)]
        nodes = 0
        while stack:
            iterator, depth = stack[-1]
            try:
                item = next(iterator)
            except StopIteration:
                stack.pop()
                continue
            nodes += 1
            if nodes > self.max_nodes or depth > self.max_depth:
                raise BrokerError("JSON exceeds its depth or node budget")
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    raise BrokerError("JSON object keys must be strings")
                # Keys count too, independently of the byte limit.
                nodes += len(item)
                if nodes > self.max_nodes:
                    raise BrokerError("JSON exceeds its node budget")
                stack.append((iter(item.values()), depth + 1))
            elif isinstance(item, list):
                stack.append((iter(item), depth + 1))
            elif item is not None and type(item) not in (str, int, float, bool):
                raise BrokerError("value must contain valid finite JSON")
            elif isinstance(item, float) and not math.isfinite(item):
                raise BrokerError("value must contain valid finite JSON")


DEFAULT_JSON_BUDGET = JsonBudget()
