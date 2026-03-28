"""WebSocket connection manager with non-blocking broadcast.

Messages are queued and flushed asynchronously so plugin ingest loops
never block on slow WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from fastapi import WebSocket

from geomonitor.core.models import Message

log = logging.getLogger("geomonitor.ws")


class WSManager:
    """Manages WebSocket connections with queued, non-blocking broadcasts."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        # Outbound message queue — emit() appends, flush task drains
        self._queue: deque[str] = deque(maxlen=5000)
        self._flush_task: asyncio.Task | None = None
        # Performance counters
        self._sent_count = 0
        self._drop_count = 0
        self._flush_time_sum = 0.0
        self._flush_count = 0
        self._queue_peak = 0

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        log.info("WebSocket client disconnected (%d total)", len(self._connections))

    @property
    def count(self) -> int:
        return len(self._connections)

    def start_flush_loop(self) -> None:
        """Start the background queue flush task."""
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    def stop_flush_loop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()

    async def _flush_loop(self) -> None:
        """Drain the outbound queue and batch-send to all clients."""
        while True:
            # Wait a short interval to accumulate messages into batches
            await asyncio.sleep(0.05)  # 50ms — up to 20 flushes/sec
            if not self._queue or not self._connections:
                continue

            # Drain entire queue into a batch
            batch = []
            while self._queue:
                batch.append(self._queue.popleft())

            t0 = time.monotonic()

            # Batch multiple messages into a single JSON array send
            if len(batch) == 1:
                payload = batch[0]
            else:
                # Merge individual {"type":"new","messages":[...]} into one
                all_msgs = []
                other = []
                for text in batch:
                    try:
                        obj = json.loads(text)
                        if obj.get("type") == "new":
                            all_msgs.extend(obj.get("messages", []))
                        else:
                            other.append(text)
                    except Exception:
                        other.append(text)

                # Send non-message payloads individually, batch messages
                for text in other:
                    await self._send_all(text)
                if all_msgs:
                    payload = json.dumps({"type": "new", "messages": all_msgs}, default=str)
                else:
                    continue

            await self._send_all(payload)

            dt = time.monotonic() - t0
            self._flush_time_sum += dt
            self._flush_count += 1

    async def _send_all(self, text: str) -> None:
        """Send text to all connected clients, dropping dead connections."""
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(text)
                self._sent_count += 1
            except Exception:
                dead.append(ws)
                self._drop_count += 1
        for ws in dead:
            self._connections.discard(ws)

    def broadcast_message(self, msg: Message) -> None:
        """Queue a message for broadcast (non-blocking, called from emit)."""
        text = json.dumps({"type": "new", "messages": [msg.to_dict()]}, default=str)
        qlen = len(self._queue)
        if qlen >= self._queue.maxlen:
            self._drop_count += 1
            return  # Drop oldest will happen via deque maxlen
        self._queue.append(text)
        if qlen + 1 > self._queue_peak:
            self._queue_peak = qlen + 1

    async def broadcast_tracks(self, updates: dict[str, Any]) -> None:
        """Broadcast track store updates to all clients."""
        text = json.dumps({"type": "tracks", "updates": updates}, default=str)
        await self._send_all(text)

    async def send_snapshot(
        self,
        ws: WebSocket,
        plugins: dict[str, Any],
        tracks: dict[str, Any],
    ) -> None:
        """Send plugin metadata and track state to a newly connected client."""
        text = json.dumps({
            "type": "snapshot",
            "plugins": plugins,
            "tracks": tracks,
        }, default=str)
        await ws.send_text(text)

    def get_perf_stats(self) -> dict[str, Any]:
        """Return performance counters for the /api/stats endpoint."""
        avg_flush = (self._flush_time_sum / self._flush_count * 1000) if self._flush_count else 0
        return {
            "ws_queue_depth": len(self._queue),
            "ws_queue_peak": self._queue_peak,
            "ws_sent_total": self._sent_count,
            "ws_drop_total": self._drop_count,
            "ws_avg_flush_ms": round(avg_flush, 2),
            "ws_flush_count": self._flush_count,
        }
