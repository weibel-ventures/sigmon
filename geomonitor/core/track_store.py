"""Track store — maintains latest entity state for map and correlation."""

from __future__ import annotations

import time
from typing import Any

from geomonitor.core.models import NormalizedEntity, TrackState


class TrackStore:
    """Stores the latest state of every tracked entity across all plugins.

    Keyed by (plugin_id, entity_id). Provides snapshot for WebSocket
    clients and the map layer.
    """

    def __init__(self, history_depth: int = 100, stale_seconds: float = 300.0):
        self._tracks: dict[tuple[str, str], TrackState] = {}
        self._history_depth = history_depth
        self._stale_seconds = stale_seconds
        self._dirty: set[tuple[str, str]] = set()  # Changed since last flush

    def update(self, entities: list[NormalizedEntity]) -> None:
        """Update track store with new normalized entities."""
        ts = time.time()
        for entity in entities:
            if entity.entity_id is None:
                continue
            key = (entity.source_plugin, entity.entity_id)
            if key not in self._tracks:
                from collections import deque
                state = TrackState(
                    entity=entity,
                    history=deque(maxlen=self._history_depth),
                    first_seen=ts,
                    last_seen=ts,
                    message_count=0,
                )
                self._tracks[key] = state
            self._tracks[key].update(entity, ts)
            self._dirty.add(key)

    def flush_dirty(self) -> dict[str, Any] | None:
        """Return changed tracks since last flush, then clear dirty set.

        Returns None if nothing changed.
        """
        if not self._dirty:
            return None
        updates = {}
        for key in self._dirty:
            track = self._tracks.get(key)
            if track:
                str_key = f"{key[0]}:{key[1]}"
                updates[str_key] = track.to_dict()
        self._dirty.clear()
        return updates

    def expire_stale(self) -> int:
        """Remove tracks not updated within stale_seconds. Returns count removed."""
        now = time.time()
        expired = [
            key for key, track in self._tracks.items()
            if now - track.last_seen > self._stale_seconds
        ]
        for key in expired:
            del self._tracks[key]
        return len(expired)

    def snapshot(self) -> dict[str, Any]:
        """Full track store serialized for WebSocket snapshot."""
        result = {}
        for key, track in self._tracks.items():
            str_key = f"{key[0]}:{key[1]}"
            result[str_key] = track.to_dict()
        return result

    def __len__(self) -> int:
        return len(self._tracks)
