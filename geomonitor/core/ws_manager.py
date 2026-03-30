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

# Per-client send timeout — drop the client if a single send takes longer
_SEND_TIMEOUT = 2.0


class WSManager:
    """Manages WebSocket connections with queued, non-blocking broadcasts."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._msg_queue: deque[dict] = deque(maxlen=500)
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
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    def stop_flush_loop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()

    async def _flush_loop(self) -> None:
        """Drain the outbound queue and batch-send to all clients."""
        while True:
            await asyncio.sleep(0.05)  # 50ms — up to 20 flushes/sec
            if not self._msg_queue or not self._connections:
                continue

            # Drain queue — already dicts, no re-parsing needed
            batch = []
            while self._msg_queue:
                batch.append(self._msg_queue.popleft())

            t0 = time.monotonic()

            # Build a single JSON payload with all messages
            payload = json.dumps({"type": "new", "messages": batch}, default=str)
            await self._send_all(payload)

            dt = time.monotonic() - t0
            self._flush_time_sum += dt
            self._flush_count += 1

    async def _send_all(self, text: str) -> None:
        """Send text to all clients with per-client timeout."""
        if not self._connections:
            return
        dead: list[WebSocket] = []
        # Send to all clients concurrently with timeout
        tasks = {}
        for ws in self._connections:
            tasks[ws] = asyncio.create_task(self._send_one(ws, text))

        if tasks:
            done, pending = await asyncio.wait(
                tasks.values(), timeout=_SEND_TIMEOUT)
            for ws, task in tasks.items():
                if task in pending:
                    task.cancel()
                    dead.append(ws)
                    self._drop_count += 1
                    log.warning("WebSocket client timed out, dropping")
                elif task.exception():
                    dead.append(ws)
                    self._drop_count += 1
                else:
                    self._sent_count += 1

        for ws in dead:
            self._connections.discard(ws)

    @staticmethod
    async def _send_one(ws: WebSocket, text: str) -> None:
        await ws.send_text(text)

    def broadcast_message(self, msg: Message) -> None:
        """Queue a message for broadcast (non-blocking, called from emit)."""
        qlen = len(self._msg_queue)
        if qlen >= self._msg_queue.maxlen:
            self._drop_count += 1
            return
        self._msg_queue.append(msg.to_dict())
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
        avg_flush = (self._flush_time_sum / self._flush_count * 1000) if self._flush_count else 0
        return {
            "ws_queue_depth": len(self._msg_queue),
            "ws_queue_peak": self._queue_peak,
            "ws_sent_total": self._sent_count,
            "ws_drop_total": self._drop_count,
            "ws_avg_flush_ms": round(avg_flush, 2),
            "ws_flush_count": self._flush_count,
        }
