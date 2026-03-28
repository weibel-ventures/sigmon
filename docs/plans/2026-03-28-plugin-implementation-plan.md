# Plugin Architecture — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the monolithic ASTERIX monitor into a plugin-based geospatial intelligence platform, then port ASTERIX as the first plugin and add ADS-B as the second.

**Architecture:** See [Architecture Spec](2026-03-28-plugin-architecture-spec.md) and [Plugin ICD](2026-03-28-plugin-icd.md).

**Tech Stack:** Python 3.12, FastAPI, uvicorn, asyncio, asterix-decoder, pyModeS, Leaflet.js

---

## Phase 1: Core Platform (refactor from monolith)

### Task 1: Project restructure and core models

**Files:**
- Create: `geomonitor/core/__init__.py`
- Create: `geomonitor/core/models.py`
- Create: `geomonitor/core/plugin_base.py`
- Create: `geomonitor/pyproject.toml` (or update root)

**What to do:**

1. Create the `geomonitor/core/` package directory structure
2. Implement the data models in `models.py`:
   - `EntityType` enum
   - `NormalizedEntity` dataclass
   - `Message` dataclass (extends current `AsterixEntry` to be protocol-agnostic)
   - `TrackState` dataclass
   - `DecodeResult` dataclass
3. Implement `PluginBase` ABC in `plugin_base.py` with all abstract methods
4. Update `pyproject.toml` with the new package structure

**Commit:** `refactor: create core models and plugin base class`

---

### Task 2: Ring buffer and track store

**Files:**
- Create: `geomonitor/core/buffer.py`
- Create: `geomonitor/core/track_store.py`

**What to do:**

1. Port `RingBuffer` from `app/main.py` to `core/buffer.py`
   - Change type from `AsterixEntry` to `Message`
   - Add per-plugin rate counters (dict of plugin_id → counter)
   - Keep the same deque-based design
2. Implement `TrackStore`:
   - Dict keyed by `(plugin_id, entity_id)` → `TrackState`
   - `update(entities: list[NormalizedEntity])` method
   - `snapshot()` for serialization to WebSocket
   - Configurable history depth per track (default 100 positions)
   - Stale track expiry (configurable, default 5 minutes)

**Commit:** `refactor: protocol-agnostic ring buffer and track store`

---

### Task 3: Plugin registry

**Files:**
- Create: `geomonitor/core/plugin_registry.py`

**What to do:**

1. Implement `PluginRegistry`:
   - `discover()` — scan entry points + local `plugins/` directory
   - `load_all(settings_manager)` — instantiate, validate, configure
   - `start_all(loop)` — call `plugin.start()` for each enabled plugin
   - `stop_all()` — graceful shutdown
   - `get_plugin(id)` — lookup by ID
   - `plugins` property — list all loaded plugins
2. The `emit` callback that the registry passes to each plugin:
   ```python
   async def _make_emit(self, plugin: PluginBase):
       async def emit(raw: bytes, src: tuple[str, int]):
           result = plugin.decode(raw, src)
           entities = plugin.normalize(result.decoded, result.meta) if result.decoded else []
           msg = Message(
               seq=self._buffer.next_seq(),
               ts=time.time(),
               plugin_id=plugin.plugin_id,
               src_ip=src[0], src_port=src[1],
               raw=raw,
               decoded=result.decoded,
               decode_error=result.error,
               summary=result.summary,
               normalized=entities,
               meta=result.meta,
           )
           self._buffer.append(msg)
           if entities:
               self._track_store.update(entities)
           await self._ws_manager.broadcast_message(msg)
       return emit
   ```
3. Load manifest.json from each plugin's package directory

**Commit:** `feat: plugin registry with discovery, lifecycle, and emit pipeline`

---

### Task 4: Settings manager

**Files:**
- Create: `geomonitor/core/settings.py`

**What to do:**

1. Implement `SettingsManager`:
   - Load from `settings.json` (create default if missing)
   - `get_global()` → global settings dict
   - `get_plugin(plugin_id)` → plugin settings dict (merged with defaults from manifest)
   - `set_plugin(plugin_id, settings)` → update + persist
   - `get_merged_schema()` → combined JSON Schema for the settings UI
2. Settings file structure:
   ```json
   {
       "global": {"buffer_max_messages": 50000, "web_port": 8080},
       "plugins": {
           "asterix": {"udp_port": 23401, "enabled": true},
           "adsb": {"host": "127.0.0.1", "port": 30005, "enabled": true}
       }
   }
   ```

**Commit:** `feat: settings manager with JSON persistence and schema merging`

---

### Task 5: WebSocket manager and FastAPI app

**Files:**
- Create: `geomonitor/core/ws_manager.py`
- Create: `geomonitor/core/app.py`

**What to do:**

1. Port `ConnectionManager` to `ws_manager.py`:
   - `broadcast_message(msg: Message)` — serialize and send to all clients
   - `broadcast_tracks(updates)` — debounced track store updates
   - `send_snapshot(ws)` — full buffer + plugin metadata + track state
2. Build `app.py` — the new FastAPI application:
   - Lifespan: init settings → init registry → discover plugins → start all → yield → stop all
   - Routes:
     - `GET /` → HTML UI
     - `GET /api/stats` → global + per-plugin stats
     - `GET /api/plugins` → plugin list with manifests
     - `GET /api/settings` → current settings
     - `PUT /api/settings/{plugin_id}` → update plugin settings
     - `GET /api/tracks` → track store snapshot
     - `WS /ws` → live stream (snapshot on connect, then new messages + track updates)
   - WebSocket v2 protocol as defined in architecture spec

**Commit:** `feat: new FastAPI app with plugin-aware WebSocket protocol`

---

### Task 6: Port ASTERIX as first plugin

**Files:**
- Create: `geomonitor/plugins/asterix/__init__.py`
- Create: `geomonitor/plugins/asterix/manifest.json`

**What to do:**

1. Extract ASTERIX-specific code from `app/main.py` into the plugin:
   - `AsterixPlugin(PluginBase)` with `start()`, `stop()`, `decode()`, `normalize()`
   - UDP ingestor (same `create_datagram_endpoint` pattern)
   - `decode()` wraps `asterix.parse()` and produces `DecodeResult`
   - `normalize()` extracts sensor positions (Cat 34 I120) and track positions (Cat 48 I040→polar to latlon)
   - `manifest.json` with settings schema, list columns, etc.
2. Register as entry point in `pyproject.toml`
3. Verify: existing ASTERIX data should work identically to current monolith

**Commit:** `feat: ASTERIX plugin — first protocol ported to plugin architecture`

---

## Phase 2: Frontend (refactor to plugin-aware)

### Task 7: Refactor HTML/JS for multi-plugin support

**Files:**
- Rewrite: `geomonitor/static/index.html`

**What to do:**

1. Message list changes:
   - Add "Plugin" column (with colored badge showing plugin icon/name)
   - Dynamic columns: read `list_columns` from plugin metadata received in snapshot
   - Render plugin-specific columns from `msg.meta` fields
   - Plugin filter chips in the filter bar (toggle per plugin)
2. Decoded tree: no changes needed — already renders arbitrary dicts
3. Hex dump: conditional on `supports_hex_dump` from manifest
4. Map changes:
   - Layer groups per plugin (with layer toggle control)
   - Read tracks from `tracks` updates instead of rebuilding from messages
   - Color tracks by plugin color from manifest
   - Sensor/track/detection icons based on entity type
5. Settings pane:
   - Slide-out panel or modal
   - Auto-generate form from merged settings JSON Schema
   - Send changes via WebSocket
   - Plugin enable/disable toggles
6. Stats bar: show per-plugin message rates

**Commit:** `feat: plugin-aware frontend with dynamic columns, map layers, and settings pane`

---

## Phase 3: ADS-B Plugin (second protocol)

### Task 8: ADS-B plugin

**Files:**
- Create: `geomonitor/plugins/adsb/__init__.py`
- Create: `geomonitor/plugins/adsb/manifest.json`

**Dependencies:** `pyModeS` added to requirements.txt

**What to do:**

1. Implement `AdsbPlugin(PluginBase)`:
   - **Ingestor:** TCP client connecting to Beast binary feed (default :30005). Reconnects on disconnect.
   - **Frame extraction:** Beast binary protocol — `0x1a` escape byte framing, message types 2 (Mode S short), 3 (Mode S long), 4 (status)
   - **Decode:** Use `pyModeS` to decode:
     - `pyModeS.adsb.position()` for lat/lon (CPR decoding needs even/odd frame pairs — maintain state)
     - `pyModeS.adsb.velocity()` for speed/heading
     - `pyModeS.adsb.callsign()` for flight ID
     - `pyModeS.adsb.altitude()` for altitude
     - Downlink format (DF) identification for message type classification
   - **DecodeResult:**
     - `decoded`: dict with DF, ICAO, typecode, and all decoded fields
     - `summary`: "RYR123 FL350 GS:450kn" style
     - `meta`: `{"icao": "4CA682", "callsign": "RYR123", "flight_level": 350}`
   - **Normalize:** Each position update → `NormalizedEntity(TRACK, entity_id=icao_hex, lat, lon, ...)`
2. `manifest.json` with Beast TCP settings, list columns (ICAO, Callsign, FL), color `#0077FD`

**Commit:** `feat: ADS-B plugin with Beast TCP ingestor and pyModeS decoder`

---

## Phase 4: AIS Plugin (third protocol)

### Task 9: AIS plugin

**Files:**
- Create: `geomonitor/plugins/ais/__init__.py`
- Create: `geomonitor/plugins/ais/manifest.json`

**Dependencies:** `pyais` added to requirements.txt

**What to do:**

1. Implement `AisPlugin(PluginBase)`:
   - **Ingestor:** UDP listener for NMEA sentences (default :10110). Also support TCP.
   - **Frame extraction:** NMEA 0183 line protocol (`!AIVDM`/`!AIVDO` sentences). Handle multi-sentence messages (fragment reassembly).
   - **Decode:** Use `pyais` to decode:
     - Message types 1-3 (position reports), 5 (static data), 18/19 (Class B), 21 (AtoN), 24 (Class B static)
     - MMSI, lat/lon, course, speed, heading, vessel name, ship type, destination
   - **DecodeResult:**
     - `decoded`: full pyais decoded dict
     - `summary`: "MMSI:219000606 MÆRSK SEBAROK 12.3kn"
     - `meta`: `{"mmsi": "219000606", "vessel_name": "MÆRSK SEBAROK", "ship_type": "Cargo"}`
   - **Normalize:** Position reports → `NormalizedEntity(TRACK, entity_id=mmsi, lat, lon, speed, heading, ...)`
2. `manifest.json` with UDP settings, list columns (MMSI, Name, Type), color `#34C759`

**Commit:** `feat: AIS plugin with NMEA ingestor and pyais decoder`

---

## Phase 5: CoT/TAK Plugin (fourth protocol)

### Task 10: Cursor on Target plugin

**Files:**
- Create: `geomonitor/plugins/cot/__init__.py`
- Create: `geomonitor/plugins/cot/manifest.json`

**What to do:**

1. Implement `CotPlugin(PluginBase)`:
   - **Ingestor:** UDP multicast (default 239.2.3.1:6969). Also support TCP for TAK server connections.
   - **Frame extraction:** XML messages (complete `<event>...</event>` elements). Handle TCP stream buffering to extract complete XML docs.
   - **Decode:** Standard library `xml.etree.ElementTree`:
     - Extract `event@type` (CoT type string, e.g. `a-f-G-U-C` = friendly ground unit)
     - Extract `point@lat`, `point@lon`, `point@hae`, `point@ce`, `point@le`
     - Extract `detail` sub-elements (contact callsign, group, track speed/course, remarks)
     - Map CoT type codes to MIL-STD-2525 SIDCs where possible
   - **DecodeResult:**
     - `decoded`: nested dict from XML
     - `summary`: "ALFA-1 a-f-G-U-C (friendly ground)"
     - `meta`: `{"uid": "ANDROID-359...", "cot_type": "a-f-G-U-C", "callsign": "ALFA-1"}`
   - **Normalize:** `NormalizedEntity(TRACK, entity_id=uid, lat, lon, alt, ...)` with `symbol_code` from type mapping
2. `manifest.json` with multicast settings, list columns (UID, Type, Callsign), color `#FF9F0A`

**Commit:** `feat: CoT/TAK plugin with multicast/TCP ingestor and XML decoder`

---

## Phase 6: Docker and Deployment

### Task 11: Update Docker and CI

**Files:**
- Rewrite: `Dockerfile`
- Rewrite: `docker-compose.yml`
- Update: `requirements.txt` or use `pyproject.toml` deps

**What to do:**

1. Dockerfile:
   - Install all plugin dependencies (asterix-decoder, pyModeS, pyais)
   - Copy `geomonitor/` package
   - Entry point: `uvicorn geomonitor.core.app:app`
2. docker-compose.yml:
   - Map all protocol ports (UDP + TCP)
   - Volume mount for settings.json and custom plugins
   - Environment variables for global config
3. Update README with new architecture, plugin list, and multi-port configuration

**Commit:** `feat: multi-protocol Docker deployment with all bundled plugins`

---

## Phase 7: Documentation and Cleanup

### Task 12: Final documentation

1. Update README.md — new architecture section, supported protocols table, plugin development guide
2. Add `docs/developing-plugins.md` — tutorial for writing a new plugin
3. Remove old `app/` directory (replaced by `geomonitor/`)
4. Tag release: `v2.0.0`

**Commit:** `docs: plugin development guide and updated README`

---

## Dependency Summary

| Phase | New Dependencies |
|-------|-----------------|
| Phase 1 (Core) | None (refactor only) |
| Phase 2 (Frontend) | None |
| Phase 3 (ADS-B) | `pyModeS>=2.7` |
| Phase 4 (AIS) | `pyais>=2.6` |
| Phase 5 (CoT) | None (stdlib XML) |

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Plugin decode() is slow, blocks event loop | Document performance contract; consider asyncio.to_thread() for heavy decoders |
| ADS-B CPR decoding needs state across messages | Plugin maintains internal position cache (even/odd frame pairs per ICAO) |
| Multiple plugins on same port conflict | Settings validation at startup; clear error message |
| Frontend complexity explosion with N plugins | Plugin metadata drives rendering; no per-plugin JS |
| Large track stores with many AIS vessels | Configurable stale expiry; lazy serialization |
