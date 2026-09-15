"""Bounded process-local accounting; never retain model inputs or outputs."""
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from threading import RLock
from typing import Literal

from suiteharness.models.types import ModelUsage


class UsageLedger:
    def __init__(self, routes: Iterable[str]) -> None:
        self._lock = RLock()
        self._routes = {route: dict(
            calls=0, succeeded=0, failed=0, cancelled=0, usage_unknown=0,
            input_tokens=0, output_tokens=0, cached_input_tokens=0,
            reasoning_tokens=0, elapsed_seconds=0.0,
        ) for route in routes}

    def record(self, route_id: str, status: Literal['succeeded', 'failed', 'cancelled'],
               elapsed_seconds: float, usage: ModelUsage | None) -> None:
        with self._lock:
            row = self._routes.get(route_id)
            if row is None:
                return
            row['calls'] += 1
            row[status] += 1
            row['elapsed_seconds'] += max(0.0, elapsed_seconds)
            if usage is None:
                row['usage_unknown'] += 1
            else:
                for name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens'):
                    row[name] += getattr(usage, name)

    def snapshot(self) -> dict:
        with self._lock:
            return {'scope': 'current_process', 'routes': deepcopy(self._routes),
                    'cost': None, 'cost_note': 'No configured tariff; token usage is not a billing statement.'}
