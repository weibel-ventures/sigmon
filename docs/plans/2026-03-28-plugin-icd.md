# Plugin Interface Control Document (ICD)

**Version:** 0.1 draft
**Date:** 2026-03-28
**Status:** Design proposal
**Parent:** [Architecture Specification](2026-03-28-plugin-architecture-spec.md)

---

## 1. Overview

A plugin is a Python package that teaches the GeoMonitor platform how to receive, decode, normalize, and display a specific surveillance or C2 protocol. This document defines the exact interfaces a plugin must implement.

## 2. Plugin Discovery

Plugins are discovered via one of two mechanisms:

### 2.1 Entry Points (packaged plugins)

```toml
# pyproject.toml
[project.entry-points."geomonitor.plugins"]
asterix = "geomonitor_asterix:AsterixPlugin"
```

### 2.2 Local Directory (development plugins)

Any Python package in `plugins/` with a `manifest.json` is auto-discovered. The `__init__.py` must export a class that implements `PluginBase`.

## 3. Manifest

Every plugin must include a `manifest.json` in its package directory:

```jsonc
{
    "id": "asterix",                           // Unique slug, lowercase, [a-z0-9_-]
    "name": "ASTERIX",                         // Human display name
    "version": "1.0.0",                        // Semver
    "description": "EUROCONTROL ASTERIX surveillance data (Cat 034, 048, 062, 065, ...)",
    "author": "Weibel Ventures",
    "icon": "radar",                           // Icon name (from built-in icon set)
    "color": "#FF2F00",                        // Brand color for map/UI elements

    "ingestor": {
        "type": "udp",                         // "udp" | "tcp_listen" | "tcp_connect" | "multicast" | "serial"
        "default_port": 23401,
        "multicast_group": null                // For multicast type
    },

    "list_columns": [
        {
            "key": "category",                 // Field path in meta dict
            "label": "Cat",
            "width": 50,
            "sortable": true
        },
        {
            "key": "summary",                  // Built-in: always available
            "label": "Summary",
            "width": null,                     // null = flex
            "sortable": true
        }
    ],

    "settings_schema": {
        "type": "object",
        "properties": {
            "udp_port": {
                "type": "integer",
                "title": "UDP Port",
                "default": 23401,
                "minimum": 1,
                "maximum": 65535
            },
            "multicast_group": {
                "type": ["string", "null"],
                "title": "Multicast Group",
                "default": null,
                "description": "Join this multicast group (e.g. 239.1.1.1). Leave empty for unicast."
            }
        }
    },

    "entity_types": ["track", "sensor"],       // Which NormalizedEntity types this plugin emits
    "map_layer": true,                         // Whether to create a map layer
    "supports_hex_dump": true                  // Whether raw bytes are meaningful for hex display
}
```

## 4. Python Interface

### 4.1 PluginBase (Abstract Base Class)

Every plugin's main class must inherit from `PluginBase`:

```python
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable
from geomonitor.core.models import NormalizedEntity

# Type alias for the callback the core provides
MessageCallback = Callable[[bytes, tuple[str, int]], Awaitable[None]]


class PluginBase(ABC):
    """Base class for all GeoMonitor plugins."""

    # --- Metadata (read from manifest.json by default) ---

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Unique plugin identifier (e.g. 'asterix')."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name (e.g. 'ASTERIX')."""

    # --- Lifecycle ---

    @abstractmethod
    async def start(self, settings: dict, emit: MessageCallback) -> None:
        """
        Start the plugin's ingestor(s).

        Called once at startup (or when re-enabled). The plugin should:
        1. Read its settings (ports, hosts, etc.)
        2. Start asyncio tasks/transports for data reception
        3. For each received datagram/message, call:
              await emit(raw_bytes, (src_ip, src_port))

        The `emit` callback is provided by the core. It triggers the
        decode → normalize → buffer → broadcast pipeline.

        Args:
            settings: Plugin-specific settings dict (from SettingsManager)
            emit: Async callback to push raw received data to the core
        """

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the plugin's ingestor(s). Release ports, cancel tasks.
        Must be idempotent — safe to call even if not started.
        """

    # --- Decode ---

    @abstractmethod
    def decode(self, raw: bytes, src: tuple[str, int]) -> "DecodeResult":
        """
        Decode raw bytes into protocol-native structure.

        This is the Wireshark-style decode — produce a rich, nested dict
        (or list of dicts) that represents the protocol's own view of the data.
        The result will be displayed in the decoded tree panel.

        This method is called synchronously in the event loop (must be fast).
        For expensive decodes, consider caching or lazy evaluation.

        Args:
            raw: Raw bytes as received from the network
            src: (ip, port) tuple of the sender

        Returns:
            DecodeResult with decoded data, summary, and metadata
        """

    # --- Normalize (optional) ---

    def normalize(self, decoded: Any, meta: dict) -> list[NormalizedEntity]:
        """
        Convert decoded protocol data to common NormalizedEntity model.

        Override this to place entities on the map and enable cross-protocol
        correlation. Return an empty list if this message has no geo content.

        Default implementation returns an empty list.

        Args:
            decoded: The protocol-native decoded structure (from decode())
            meta: The metadata dict (from decode())

        Returns:
            List of NormalizedEntity objects extracted from this message
        """
        return []

    # --- Settings ---

    def on_settings_changed(self, old: dict, new: dict) -> bool:
        """
        Called when the user changes this plugin's settings.

        Return True if the ingestor needs a restart (e.g. port changed).
        Return False if the change can be applied live.

        Default implementation always returns True (restart on any change).
        """
        return True
```

### 4.2 DecodeResult

The return type of `plugin.decode()`:

```python
@dataclass
class DecodeResult:
    decoded: dict | list[dict] | None
    """
    Protocol-native decoded structure. This is rendered as-is in the
    decoded tree panel. Can be a single dict or a list of dicts
    (e.g. ASTERIX data blocks, multiple AIS sentences in one datagram).

    Structure is arbitrary — the UI renders nested dicts recursively.
    For best display, use the convention:
        {"FieldName": {"desc": "...", "val": ..., "meaning": "..."}}
    """

    summary: str
    """One-line human-readable summary for the message list column."""

    meta: dict
    """
    Plugin-specific metadata for filtering and sorting.
    Keys should match the `key` fields in manifest.json list_columns.
    Example for ASTERIX: {"category": 48, "msg_type": "target_report"}
    Example for ADS-B:   {"icao": "4CA682", "callsign": "RYR123"}
    """

    error: str | None = None
    """If decoding partially or fully failed, describe the error here."""
```

## 5. Ingestor Patterns

The `start()` method is responsible for setting up data reception. Here are the patterns for each transport type:

### 5.1 UDP Unicast

```python
async def start(self, settings, emit):
    loop = asyncio.get_running_loop()
    self._transport, _ = await loop.create_datagram_endpoint(
        lambda: _UDPProtocol(emit),
        local_addr=("0.0.0.0", settings["udp_port"]),
    )
```

### 5.2 UDP Multicast

```python
async def start(self, settings, emit):
    loop = asyncio.get_running_loop()
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", settings["udp_port"]))
    group = socket.inet_aton(settings["multicast_group"])
    mreq = group + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    self._transport, _ = await loop.create_datagram_endpoint(
        lambda: _UDPProtocol(emit), sock=sock,
    )
```

### 5.3 TCP Connect (e.g. Beast ADS-B client)

```python
async def start(self, settings, emit):
    self._reader, self._writer = await asyncio.open_connection(
        settings["host"], settings["port"],
    )
    self._task = asyncio.create_task(self._read_loop(emit))

async def _read_loop(self, emit):
    while True:
        data = await self._reader.read(4096)
        if not data:
            break
        # Frame extraction logic here (protocol-specific)
        for frame in self._extract_frames(data):
            await emit(frame, (self._host, self._port))
```

### 5.4 TCP Listen (e.g. SBS BaseStation)

```python
async def start(self, settings, emit):
    self._server = await asyncio.start_server(
        lambda r, w: self._handle_client(r, w, emit),
        "0.0.0.0", settings["port"],
    )
```

### 5.5 Serial (future, e.g. NMEA over serial port)

```python
async def start(self, settings, emit):
    import serial_asyncio
    self._reader, self._writer = await serial_asyncio.open_serial_connection(
        url=settings["serial_device"], baudrate=settings["baudrate"],
    )
    self._task = asyncio.create_task(self._read_loop(emit))
```

## 6. Normalization Contract

Plugins that produce geo-locatable entities should implement `normalize()`. The contract:

| Entity Type | Required Fields | Optional Fields |
|-------------|----------------|-----------------|
| `TRACK` | `entity_id`, `lat`, `lon`, `label`, `source_plugin` | `alt_m`, `heading_deg`, `speed_mps`, `symbol_code`, `timestamp`, `confidence`, `properties` |
| `DETECTION` | `lat`, `lon`, `label`, `source_plugin` | Everything else |
| `SENSOR` | `entity_id`, `lat`, `lon`, `label`, `source_plugin` | `alt_m`, `properties` |
| `WAYPOINT` | `lat`, `lon`, `label`, `source_plugin` | `properties` |
| `AREA` | `label`, `source_plugin`, `properties` (must contain `geometry`) | — |

### 6.1 Units Convention

All normalized values use SI / aviation standard units:

| Field | Unit | Notes |
|-------|------|-------|
| `lat`, `lon` | Decimal degrees, WGS-84 | |
| `alt_m` | Meters above mean sea level | Convert from feet: `* 0.3048` |
| `heading_deg` | Degrees true north, 0-360 | |
| `speed_mps` | Meters per second | Convert from knots: `* 0.514444` |
| `timestamp` | Unix epoch (float, seconds) | Use source time if available |
| `confidence` | 0.0–1.0 | 1.0 = highest confidence |

### 6.2 Entity ID Conventions

| Protocol | entity_id format | Example |
|----------|-----------------|---------|
| ASTERIX Cat 48 | `"SAC:SIC:Tn"` | `"0:1:528"` |
| ADS-B | ICAO 24-bit hex | `"4CA682"` |
| AIS | MMSI (9-digit) | `"219000606"` |
| CoT | CoT UID | `"ANDROID-359..."` |
| STANAG 4607 | Target report index | `"dwell:3:tgt:7"` |
| MAVLink | System ID | `"sysid:1"` |
| DIS | Entity ID triple | `"1:2:3"` |

## 7. Example: ASTERIX Plugin

Minimal implementation showing all required interfaces:

```python
# plugins/asterix/__init__.py

import asyncio
import asterix as asterix_lib
from geomonitor.core.plugin_base import PluginBase, DecodeResult
from geomonitor.core.models import NormalizedEntity, EntityType


class AsterixPlugin(PluginBase):
    plugin_id = "asterix"
    name = "ASTERIX"

    def __init__(self):
        self._transport = None
        self._sensor_positions = {}  # SAC:SIC → (lat, lon, alt)

    async def start(self, settings, emit):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPReceiver(emit),
            local_addr=("0.0.0.0", settings.get("udp_port", 23401)),
        )

    async def stop(self):
        if self._transport:
            self._transport.close()
            self._transport = None

    def decode(self, raw, src):
        try:
            parsed = asterix_lib.parse(raw)
        except Exception as e:
            return DecodeResult(decoded=None, summary="PARSE ERROR", meta={}, error=str(e))

        cats = [b.get("category", "?") for b in parsed]
        summaries = []
        for block in parsed:
            cat = block.get("category", "?")
            info = f"Cat {cat}"
            if block.get("I240", {}).get("TId"):
                info += " " + block["I240"]["TId"]["val"].strip()
            elif block.get("I161", {}).get("Tn"):
                info += " Trk#" + str(block["I161"]["Tn"]["val"])
            if block.get("I000", {}).get("MsgTyp"):
                info += " " + (block["I000"]["MsgTyp"].get("meaning", ""))
            summaries.append(info)

        return DecodeResult(
            decoded=parsed,
            summary=" | ".join(summaries),
            meta={"category": cats[0] if len(cats) == 1 else cats},
        )

    def normalize(self, decoded, meta):
        entities = []
        if not decoded:
            return entities

        for block in (decoded if isinstance(decoded, list) else [decoded]):
            cat = block.get("category")

            # Cat 34: extract sensor position
            if cat == 34 and "I120" in block:
                sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                lat = block["I120"]["LAT"]["val"]
                lon = block["I120"]["Lon"]["val"]
                alt = block["I120"].get("Height", {}).get("val", 0)
                key = f"{sac}:{sic}"
                self._sensor_positions[key] = (lat, lon, alt)
                entities.append(NormalizedEntity(
                    entity_type=EntityType.SENSOR,
                    entity_id=key,
                    lat=lat, lon=lon, alt_m=alt,
                    heading_deg=None, speed_mps=None, timestamp=None,
                    label=f"Sensor {key}",
                    symbol_code=None, confidence=None,
                    source_plugin=self.plugin_id,
                    properties={"sac": sac, "sic": sic},
                ))

            # Cat 48: extract track position (polar → geodetic)
            if cat == 48 and "I040" in block:
                sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                sensor_key = f"{sac}:{sic}"
                sensor = self._sensor_positions.get(sensor_key)
                if not sensor:
                    continue
                rho = block["I040"]["RHO"]["val"]
                theta = block["I040"]["THETA"]["val"]
                lat, lon = polar_to_latlon(sensor[0], sensor[1], rho, theta)
                tn = block.get("I161", {}).get("Tn", {}).get("val")
                entities.append(NormalizedEntity(
                    entity_type=EntityType.TRACK,
                    entity_id=f"{sensor_key}:{tn}" if tn else None,
                    lat=lat, lon=lon,
                    alt_m=block.get("I110", {}).get("3D_Height", {}).get("val"),
                    heading_deg=block.get("I200", {}).get("CHdg", {}).get("val"),
                    speed_mps=_knots_to_mps(block.get("I200", {}).get("CGS", {}).get("val")),
                    timestamp=None,
                    label=f"Trk#{tn}" if tn else "Unknown",
                    symbol_code=None, confidence=None,
                    source_plugin=self.plugin_id,
                    properties={},
                ))

        return entities


class _UDPReceiver(asyncio.DatagramProtocol):
    def __init__(self, emit):
        self._emit = emit

    def datagram_received(self, data, addr):
        asyncio.ensure_future(self._emit(data, addr))
```

## 8. Example: manifest.json for ADS-B Plugin

```jsonc
{
    "id": "adsb",
    "name": "ADS-B",
    "version": "1.0.0",
    "description": "ADS-B / Mode S aircraft surveillance via Beast TCP feed",
    "author": "Weibel Ventures",
    "icon": "plane",
    "color": "#0077FD",

    "ingestor": {
        "type": "tcp_connect",
        "default_port": 30005
    },

    "list_columns": [
        {"key": "icao", "label": "ICAO", "width": 70, "sortable": true},
        {"key": "callsign", "label": "Callsign", "width": 80, "sortable": true},
        {"key": "flight_level", "label": "FL", "width": 50, "sortable": true},
        {"key": "summary", "label": "Summary", "width": null, "sortable": true}
    ],

    "settings_schema": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "title": "Beast Host",
                "default": "127.0.0.1",
                "description": "Hostname or IP of the Beast TCP source (dump1090, readsb, etc.)"
            },
            "port": {
                "type": "integer",
                "title": "Beast Port",
                "default": 30005,
                "minimum": 1,
                "maximum": 65535
            }
        }
    },

    "entity_types": ["track"],
    "map_layer": true,
    "supports_hex_dump": true
}
```

## 9. Versioning

The plugin interface is versioned. The core declares `PLUGIN_API_VERSION = 1`. Plugins declare `api_version = 1` in their manifest. If a plugin's API version doesn't match, the core logs a warning and skips it.

Breaking changes to the plugin interface increment the API version. The core may support loading older-version plugins with an adapter layer, but this is not guaranteed.
