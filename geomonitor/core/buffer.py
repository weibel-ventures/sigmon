"""Protocol-agnostic ring buffer for the GeoMonitor platform."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from geomonitor.core.models import Message


class RingBuffer:
    """Bounded message buffer with per-plugin rate tracking."""

    def __init__(self, maxlen: int = 50_000):
        self._buf: deque[Message] = deque(maxlen=maxlen)
        self._seq = 0
        self._maxlen = maxlen

        # Global rate
        self._rate_counter = 0
        self._rate_ts = time.monotonic()
        self._rate_value = 0.0

        # Per-plugin rate
        self._plugin_counters: dict[str, int] = {}
        self._plugin_rates: dict[str, float] = {}

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def append(self, msg: Message) -> None:
        self._buf.append(msg)
        self._rate_counter += 1
        self._plugin_counters[msg.plugin_id] = self._plugin_counters.get(msg.plugin_id, 0) + 1

    def snapshot(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self._buf]

    def update_rate(self) -> None:
        now = time.monotonic()
        dt = now - self._rate_ts
        if dt >= 1.0:
            self._rate_value = self._rate_counter / dt
            self._rate_counter = 0
            for pid, count in self._plugin_counters.items():
                self._plugin_rates[pid] = count / dt
            self._plugin_counters = {pid: 0 for pid in self._plugin_counters}
            self._rate_ts = now

    @property
    def rate(self) -> float:
        return self._rate_value

    def plugin_rate(self, plugin_id: str) -> float:
        return self._plugin_rates.get(plugin_id, 0.0)

    @property
    def total_seq(self) -> int:
        return self._seq

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def __len__(self) -> int:
        return len(self._buf)
