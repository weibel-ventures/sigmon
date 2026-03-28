"""Rate counter, sequence generator, and emit-pipeline timing.

Message storage is handled client-side (browser ring buffer).
The server tracks message rates, emit latencies, and assigns sequence numbers.
"""

from __future__ import annotations

import time


class RateCounter:
    """Tracks message rates, emit latencies, and assigns sequence numbers."""

    def __init__(self):
        self._seq = 0

        # Global rate
        self._rate_counter = 0
        self._rate_ts = time.monotonic()
        self._rate_value = 0.0

        # Per-plugin rate
        self._plugin_counters: dict[str, int] = {}
        self._plugin_rates: dict[str, float] = {}

        # Per-plugin emit latency tracking (decode+normalize+store)
        self._plugin_emit_sum: dict[str, float] = {}
        self._plugin_emit_count: dict[str, int] = {}
        self._plugin_emit_avg: dict[str, float] = {}

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def count(self, plugin_id: str, emit_time: float = 0.0) -> None:
        """Record a message emitted by a plugin with optional emit duration."""
        self._rate_counter += 1
        self._plugin_counters[plugin_id] = self._plugin_counters.get(plugin_id, 0) + 1
        if emit_time > 0:
            self._plugin_emit_sum[plugin_id] = self._plugin_emit_sum.get(plugin_id, 0.0) + emit_time
            self._plugin_emit_count[plugin_id] = self._plugin_emit_count.get(plugin_id, 0) + 1

    def update_rate(self) -> None:
        now = time.monotonic()
        dt = now - self._rate_ts
        if dt >= 1.0:
            self._rate_value = self._rate_counter / dt
            self._rate_counter = 0
            for pid, cnt in self._plugin_counters.items():
                self._plugin_rates[pid] = cnt / dt
            self._plugin_counters = {pid: 0 for pid in self._plugin_counters}
            # Compute average emit times and reset
            for pid in self._plugin_emit_sum:
                cnt = self._plugin_emit_count.get(pid, 1)
                self._plugin_emit_avg[pid] = (self._plugin_emit_sum[pid] / cnt) * 1000  # ms
            self._plugin_emit_sum = {pid: 0.0 for pid in self._plugin_emit_sum}
            self._plugin_emit_count = {pid: 0 for pid in self._plugin_emit_count}
            self._rate_ts = now

    @property
    def rate(self) -> float:
        return self._rate_value

    def plugin_rate(self, plugin_id: str) -> float:
        return self._plugin_rates.get(plugin_id, 0.0)

    def plugin_emit_ms(self, plugin_id: str) -> float:
        return self._plugin_emit_avg.get(plugin_id, 0.0)

    @property
    def total_seq(self) -> int:
        return self._seq
