# Geospatial Intelligence Monitor — Architecture Specification

**Version:** 0.1 draft
**Date:** 2026-03-28
**Status:** Design proposal

---

## 1. Vision

Transform the ASTERIX debug monitor into a multi-protocol geospatial intelligence platform. Each surveillance/C2 protocol (ASTERIX, ADS-B, AIS, CoT, STANAG 4609, etc.) lives as a self-contained **plugin** that the core platform discovers, loads, and hosts. Engineers can enable the protocols they need, debug their raw data, and see all sources correlated on a single map.

## 2. Design Principles

1. **Plugin isolation** — A broken or missing plugin never crashes the core. Each plugin is a Python package with defined entry points.
2. **Typed core, flexible edges** — The platform defines strict interfaces for ingest, decode, normalization, and settings. Plugins have full freedom in how they decode protocol-native data and what detail they expose.
3. **Normalize for correlation, preserve for debugging** — Every plugin can emit a common `Track` / `Detection` / `SensorStatus` model for the map and fusion layer, while keeping protocol-native decoded dicts for the Wireshark-style inspector.
4. **Single process, single event loop** — All plugins run as asyncio tasks in one Python process. No inter-process communication overhead.
5. **Frontend is plugin-aware** — The UI renders plugin-provided schemas for list columns, detail views, and map layers. Plugins don't ship custom JS — they declare what to show and the core renders it.
6. **Configuration as data** — All plugin settings are declarative (JSON schema). The core generates the settings UI automatically.

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Browser (Single Page)                │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐ │
│  │ Message  │  │ Decoded  │  │   Hex   │  │  Leaflet │ │
│  │  List    │  │  Tree    │  │  Dump   │  │   Map    │ │
│  │(per-plugin│  │(per-plugin│  │         │  │(merged   │ │
│  │ columns) │  │ detail)  │  │         │  │ layers)  │ │
│  └────┬─────┘  └────┬─────┘  └────┬────┘  └────┬─────┘ │
│       └──────────────┴─────────────┴─────────────┘       │
│                        WebSocket                         │
└────────────────────────────┬─────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────┐
│                    Core Platform (Python)                 │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Plugin Registry                                    │ │
│  │  Discovers + loads plugins at startup               │ │
│  └──────────┬──────────────────────────────────────────┘ │
│             │                                            │
│  ┌──────────▼──────────────────────────────────────────┐ │
│  │  Message Bus (asyncio.Queue per plugin)             │ │
│  │  Ingestors → Decoder → Normalizer → Buffer → WS    │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌──────────────────┐  ┌────────────┐  ┌──────────────┐ │
│  │  Ring Buffer      │  │  Track     │  │  Settings    │ │
│  │  (unified, all   │  │  Store     │  │  Manager     │ │
│  │   protocols)     │  │  (fused)   │  │  (per-plugin │ │
│  │                  │  │            │  │   + global)  │ │
│  └──────────────────┘  └────────────┘  └──────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  FastAPI + WebSocket + Static Files                 │ │
│  └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
         ▲            ▲            ▲            ▲
         │            │            │            │
    ┌────┴───┐   ┌────┴───┐  ┌────┴───┐  ┌────┴───┐
    │ASTERIX │   │ ADS-B  │  │  AIS   │  │  CoT   │  ...
    │ Plugin │   │ Plugin │  │ Plugin │  │ Plugin │
    │        │   │        │  │        │  │        │
    │UDP:2340│   │TCP:3000│  │UDP:1371│  │UDP:6969│
    └────────┘   └────────┘  └────────┘  └────────┘
```

## 4. Core Platform Components

### 4.1 Plugin Registry

Discovers plugins via Python entry points (`importlib.metadata`). Each plugin is a Python package that registers an entry point in the `geomonitor.plugins` group.

```toml
# In a plugin's pyproject.toml
[project.entry-points."geomonitor.plugins"]
asterix = "geomonitor_asterix:AsterixPlugin"
```

At startup, the registry:
1. Discovers all installed `geomonitor.plugins` entry points
2. Instantiates each plugin class
3. Validates it implements the required interfaces
4. Calls `plugin.configure(settings)` with saved settings
5. Starts ingestors as asyncio tasks via `plugin.start(loop, message_callback)`

Plugins can also be loaded from a local `plugins/` directory for development without packaging.

### 4.2 Unified Message Model

Every message from every plugin flows through one model before entering the ring buffer:

```python
@dataclass
class Message:
    seq: int                        # Global sequence number (core assigns)
    ts: float                       # Reception timestamp (time.time())
    plugin_id: str                  # "asterix", "adsb", "ais", etc.
    src_ip: str                     # Source IP address
    src_port: int                   # Source port
    raw: bytes                      # Raw bytes as received
    decoded: dict | list | None     # Protocol-native decoded structure
    decode_error: str | None        # Error string if decode failed
    summary: str                    # One-line human summary (plugin provides)
    normalized: list[NormalizedEntity] | None  # Common model for map/fusion
    meta: dict                      # Plugin-specific metadata (category, msg type, etc.)
```

### 4.3 Normalized Entity Model

The common model that enables cross-protocol correlation and unified map display:

```python
@dataclass
class NormalizedEntity:
    entity_type: EntityType         # TRACK, DETECTION, SENSOR, WAYPOINT, AREA
    entity_id: str | None           # Track number, MMSI, ICAO hex, callsign
    lat: float | None               # WGS-84 latitude
    lon: float | None               # WGS-84 longitude
    alt_m: float | None             # Altitude in meters (MSL)
    heading_deg: float | None       # True heading in degrees
    speed_mps: float | None         # Ground speed in m/s
    timestamp: float | None         # Observation time (source time if available)
    label: str                      # Display label
    symbol_code: str | None         # MIL-STD-2525 SIDC (optional)
    confidence: float | None        # 0.0-1.0 (optional)
    source_plugin: str              # Plugin that produced this
    properties: dict                # Additional key-value pairs for tooltip
```

`EntityType` enum:
```python
class EntityType(str, Enum):
    TRACK = "track"           # Moving entity with ID continuity (aircraft, ship, vehicle)
    DETECTION = "detection"   # Point detection without track continuity
    SENSOR = "sensor"         # Sensor/radar position
    WAYPOINT = "waypoint"     # Static point of interest
    AREA = "area"             # Polygon/circle area (future)
```

### 4.4 Ring Buffer (Unified)

One global ring buffer holds `Message` objects from all plugins. Same deque-based design as today but protocol-agnostic.

- Configurable max size (default 50,000 messages)
- Per-plugin rate counters
- Global sequence numbering

### 4.5 Track Store

A secondary data structure that maintains the latest state of each tracked entity across all plugins. Keyed by `(plugin_id, entity_id)`.

```python
class TrackStore:
    tracks: dict[tuple[str, str], TrackState]

@dataclass
class TrackState:
    entity: NormalizedEntity        # Latest normalized entity
    history: deque[NormalizedEntity] # Position history (configurable depth)
    first_seen: float
    last_seen: float
    message_count: int
```

The map reads from the TrackStore, not the ring buffer. This separates "message history" (ring buffer, for the list/inspector) from "entity state" (track store, for the map).

### 4.6 Settings Manager

Collects settings schemas from all plugins + global settings. Persists to a JSON file. Exposes via REST API for the frontend settings pane.

```python
# Global settings
{
    "buffer_max_messages": 50000,
    "web_port": 8080,
    "theme": "dark"
}

# Per-plugin settings (plugin declares JSON Schema)
{
    "asterix": {
        "udp_port": 23401,
        "enabled": true,
        "multicast_group": null
    },
    "adsb": {
        "beast_host": "127.0.0.1",
        "beast_port": 30005,
        "enabled": true
    }
}
```

### 4.7 WebSocket Protocol (v2)

Extends the current protocol to support multi-plugin awareness:

```jsonc
// Server → Client: initial snapshot
{
    "type": "snapshot",
    "messages": [...],              // Full buffer
    "plugins": {                    // Plugin metadata
        "asterix": {
            "name": "ASTERIX",
            "icon": "radar",
            "color": "#FF2F00",
            "list_columns": [...],  // Column definitions
            "enabled": true
        },
        "adsb": { ... }
    },
    "tracks": { ... }              // Current track store state
}

// Server → Client: new messages
{
    "type": "new",
    "messages": [...]
}

// Server → Client: track updates (debounced)
{
    "type": "tracks",
    "updates": { ... }             // Changed tracks only
}

// Client → Server: settings change
{
    "type": "settings",
    "plugin_id": "asterix",
    "settings": { "udp_port": 5555 }
}

// Client → Server: plugin enable/disable
{
    "type": "plugin_toggle",
    "plugin_id": "adsb",
    "enabled": false
}
```

### 4.8 REST API

```
GET  /                          → HTML UI
GET  /api/stats                 → Global + per-plugin statistics
GET  /api/plugins               → List of plugins with metadata + settings schemas
GET  /api/settings              → Current settings (global + per-plugin)
PUT  /api/settings/{plugin_id}  → Update plugin settings
GET  /api/tracks                → Current track store snapshot
WS   /ws                        → Live message + track stream
```

## 5. Frontend Architecture

### 5.1 Plugin-Driven Rendering

The frontend does not hardcode any protocol knowledge. It receives plugin metadata (columns, colors, icons) from the server and renders accordingly.

**Message list columns** — Each plugin declares its columns. The UI merges them:

| Source | Columns always shown | Plugin adds |
|--------|---------------------|-------------|
| Core | #, Time, Source, Plugin, Size | — |
| ASTERIX | — | Cat, Summary (track/msg type) |
| ADS-B | — | ICAO, Callsign, FL |
| AIS | — | MMSI, Name, Type |
| CoT | — | UID, Type, Detail |

When multiple plugins are active, the list shows the union of core columns + active plugin columns. Messages from plugins that don't have a given column show "—" in that cell.

**Detail view** — The decoded tree renderer is already protocol-agnostic (renders arbitrary nested dicts). Each plugin's `decoded` field is displayed as-is.

**Map layers** — Each plugin gets a Leaflet layer group. The core manages:
- Sensor markers (from `EntityType.SENSOR`)
- Track trails (from `EntityType.TRACK` with history)
- Detection dots (from `EntityType.DETECTION`)
- Layer toggle control per plugin

### 5.2 Settings Pane

A slide-out panel (or modal) that renders settings forms automatically from JSON Schema. Each plugin section is collapsible. Changes are sent via WebSocket and applied live (with plugin restart if the ingestor needs it).

### 5.3 Plugin Filter

A row of toggle chips in the filter bar, one per active plugin. Clicking a chip shows/hides that plugin's messages in the list and entities on the map.

## 6. Data Flow

```
1. Network data arrives at plugin ingestor (UDP/TCP/multicast)
2. Ingestor calls plugin.decode(raw_bytes, src_addr) → DecodedMessage
3. Plugin.normalize(decoded) → list[NormalizedEntity] (optional)
4. Plugin.summarize(decoded) → str (one-line summary)
5. Core wraps in Message(seq, ts, plugin_id, raw, decoded, normalized, summary)
6. Message appended to ring buffer
7. NormalizedEntities update the track store
8. Message broadcast to WebSocket clients
9. Track store changes broadcast (debounced) to WebSocket clients
```

## 7. File Structure

```
geomonitor/
├── core/
│   ├── __init__.py
│   ├── app.py              # FastAPI app, lifespan, routes, WebSocket
│   ├── buffer.py           # RingBuffer (protocol-agnostic)
│   ├── models.py           # Message, NormalizedEntity, EntityType, TrackState
│   ├── plugin_registry.py  # Discovery, loading, lifecycle
│   ├── track_store.py      # TrackStore (fused entity state)
│   ├── settings.py         # SettingsManager (load/save/schema merge)
│   └── ws_manager.py       # ConnectionManager (broadcast)
├── plugins/
│   ├── asterix/
│   │   ├── __init__.py     # AsterixPlugin class
│   │   ├── ingestor.py     # UDP listener
│   │   ├── decoder.py      # asterix.parse() wrapper
│   │   ├── normalizer.py   # Cat48 → NormalizedEntity
│   │   └── manifest.json   # Plugin metadata + settings schema
│   ├── adsb/
│   │   ├── __init__.py
│   │   ├── ingestor.py     # Beast TCP client
│   │   ├── decoder.py      # pyModeS wrapper
│   │   ├── normalizer.py   # ADS-B → NormalizedEntity
│   │   └── manifest.json
│   ├── ais/
│   │   └── ...
│   └── cot/
│       └── ...
├── static/
│   └── index.html          # Single-file UI (reads plugin metadata from WS)
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml           # Core package + plugin entry points
└── requirements.txt
```

## 8. Deployment

### Docker

One image ships with all bundled plugins. Custom plugins can be mounted:

```yaml
services:
  monitor:
    build: .
    ports:
      - "8080:8080"
      - "23401:23401/udp"     # ASTERIX
      - "30005:30005"         # ADS-B Beast
      - "10110:10110/udp"     # AIS
      - "6969:6969/udp"       # CoT
    volumes:
      - ./my-plugins:/opt/geomonitor/plugins/custom
      - ./settings.json:/opt/geomonitor/settings.json
    environment:
      - WEB_PORT=8080
      - BUFFER_MAX_MESSAGES=50000
```

### Plugin Installation

Bundled plugins: installed as part of the core package.
External plugins: `pip install geomonitor-plugin-xyz` (registers entry point).
Development plugins: drop folder in `plugins/` directory.

## 9. Correlation and Fusion (Future)

The normalized entity model enables:

- **Cross-source correlation** — An aircraft seen on both ASTERIX radar and ADS-B can be correlated by position proximity, track ID, or Mode S address. A correlator module (future) would merge these into a single fused track.
- **Multi-source display** — The map already shows all protocols. Color-coding by source plugin, with a "fused" layer that merges correlated entities.
- **Time alignment** — Source timestamps (when available) vs. reception timestamps allow cross-protocol timing analysis.

This is explicitly out of scope for v1 but the architecture supports it via the `NormalizedEntity` model and `TrackStore`.
