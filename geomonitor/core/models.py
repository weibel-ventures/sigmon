"""Core data models for the GeoMonitor platform."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Entity types for normalized model
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    TRACK = "track"
    DETECTION = "detection"
    SENSOR = "sensor"
    WAYPOINT = "waypoint"
    AREA = "area"


# ---------------------------------------------------------------------------
# Normalized entity — common model for map / correlation / fusion
# ---------------------------------------------------------------------------

@dataclass
class NormalizedEntity:
    entity_type: EntityType
    entity_id: str | None
    lat: float | None
    lon: float | None
    alt_m: float | None
    heading_deg: float | None
    speed_mps: float | None
    timestamp: float | None
    label: str
    symbol_code: str | None       # MIL-STD-2525 SIDC
    confidence: float | None      # 0.0–1.0
    source_plugin: str
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type.value,
            "entity_id": self.entity_id,
            "lat": self.lat,
            "lon": self.lon,
            "alt_m": self.alt_m,
            "heading_deg": self.heading_deg,
            "speed_mps": self.speed_mps,
            "timestamp": self.timestamp,
            "label": self.label,
            "symbol_code": self.symbol_code,
            "confidence": self.confidence,
            "source_plugin": self.source_plugin,
            "properties": self.properties,
        }


# ---------------------------------------------------------------------------
# Decode result — returned by plugin.decode()
# ---------------------------------------------------------------------------

@dataclass
class DecodeResult:
    decoded: dict | list[dict] | None
    summary: str
    meta: dict
    error: str | None = None


# ---------------------------------------------------------------------------
# Message — unified model stored in ring buffer
# ---------------------------------------------------------------------------

@dataclass
class Message:
    seq: int
    ts: float
    plugin_id: str
    src_ip: str
    src_port: int
    raw: bytes
    decoded: dict | list[dict] | None
    decode_error: str | None
    summary: str
    normalized: list[NormalizedEntity] | None
    meta: dict
    size: int = 0

    def __post_init__(self):
        if self.size == 0:
            self.size = len(self.raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "plugin_id": self.plugin_id,
            "src_ip": self.src_ip,
            "src_port": self.src_port,
            "raw_hex": self.raw.hex(),
            "decoded": self.decoded,
            "decode_error": self.decode_error,
            "summary": self.summary,
            "normalized": [e.to_dict() for e in self.normalized] if self.normalized else None,
            "meta": self.meta,
            "size": self.size,
        }


# ---------------------------------------------------------------------------
# Track state — maintained in the track store
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    entity: NormalizedEntity
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    first_seen: float = 0.0
    last_seen: float = 0.0
    message_count: int = 0

    def update(self, entity: NormalizedEntity, ts: float) -> None:
        self.entity = entity
        if entity.lat is not None and entity.lon is not None:
            self.history.append(entity)
        self.last_seen = ts
        if self.first_seen == 0.0:
            self.first_seen = ts
        self.message_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity.to_dict(),
            "history": [e.to_dict() for e in self.history],
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "message_count": self.message_count,
        }
