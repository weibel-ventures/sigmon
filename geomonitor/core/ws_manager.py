"""WebSocket connection manager with message and track broadcasting."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocket

from geomonitor.core.models import Message

log = logging.getLogger("geomonitor.ws")


class WSManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: set[WebSocket] = set()

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

    async def broadcast_raw(self, text: str) -> None:
        """Broadcast pre-serialized text to all clients."""
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    async def broadcast_message(self, msg: Message) -> None:
        """Broadcast a new message to all clients."""
        text = json.dumps({"type": "new", "messages": [msg.to_dict()]}, default=str)
        await self.broadcast_raw(text)

    async def broadcast_tracks(self, updates: dict[str, Any]) -> None:
        """Broadcast track store updates to all clients."""
        text = json.dumps({"type": "tracks", "updates": updates}, default=str)
        await self.broadcast_raw(text)

    async def send_snapshot(
        self,
        ws: WebSocket,
        messages: list[dict],
        plugins: dict[str, Any],
        tracks: dict[str, Any],
    ) -> None:
        """Send full snapshot to a newly connected client."""
        text = json.dumps({
            "type": "snapshot",
            "messages": messages,
            "plugins": plugins,
            "tracks": tracks,
        }, default=str)
        await ws.send_text(text)
