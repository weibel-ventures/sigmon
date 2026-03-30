<p align="center">
  <a href="https://weibelventures.com">
    <img src="docs/img/wv-logo.svg" alt="Weibel Ventures" width="280">
  </a>
</p>

<h1 align="center">Signal Monitor</h1>

<p align="center">
  A real-time multi-protocol signal monitoring and debugging tool.<br>
  Wireshark-style message inspection meets a live tactical map — in a single Docker container.
</p>

<p align="center">
  <img src="docs/img/screenshot-dark.png" alt="Signal Monitor — Dark Theme" width="100%">
</p>

---

## What it does

Signal Monitor listens for data from multiple military and civilian protocols simultaneously, decodes messages in real time, and presents them in a browser-based debug interface. Engineers use it to verify that data feeds are reaching the system, inspect message contents at the bit level, and visualize entity positions on a map.

It is a **passive monitoring tool** — by default it listens on standard protocol ports and accepts inbound connections. It does not initiate connections unless explicitly configured to do so.

Built for the integration team at [Weibel Ventures](https://weibelventures.com) to accelerate debugging during system deployment, lab testing, and field exercises.

## Supported Protocols

| Protocol | Standard | Transport | Default Port | Map Support |
|----------|----------|-----------|-------------|-------------|
| **ASTERIX** | EUROCONTROL Cat 034/048/062/065 | UDP | 23401 | Tracks + Sensors |
| **ADS-B** | SBS BaseStation / AVR | TCP / UDP | 30003 | Aircraft Tracks |
| **AIS** | NMEA 0183 (AIVDM/AIVDO) | TCP / UDP | 5631 | Vessel Tracks |
| **CoT/TAK** | Cursor on Target MIL-STD | UDP multicast / TCP | 6969 | SA Entities |
| **GMTI** | STANAG 4607 | UDP | 7607 | Sensor + Targets |
| **Link 16** | MIL-STD-3011D JREAP-C | TCP / UDP | 5555 | PPLI + Tracks |

All plugins are **enabled by default** and pass built-in self-tests at startup. Each plugin supports `tcp_listen` (default), `tcp_connect` (opt-in), and `udp` modes — configurable via the Settings panel.

## Features

**Multi-protocol message inspector**
- Six-pane layout: scrolling message list, collapsible decoded field tree, and hex dump
- Full protocol decoding for all supported formats with human-readable field names
- Click any message to see every data item, sub-field value, and meaning
- Filter by protocol, source IP, time range, or free-text search across all fields

**Live tactical map**
- Leaflet-based map with CARTO dark/light tiles
- Track trails with per-plugin coloring and position history
- Diff-based updates — markers move in place, no flicker
- Sensor positions, track detections, CoT waypoints, and PPLI all on one map

**Plugin architecture**
- Each protocol is a self-contained plugin with manifest, settings schema, and self-test
- Settings panel shows listening endpoints, enable/disable toggles, and live status
- All configuration persisted to `/etc/sigmon/` (JSON files, bind-mountable)
- Plugin self-tests run at startup and verify the full decode pipeline

**Real-time performance**
- Non-blocking WebSocket broadcast with batched flush (50ms intervals)
- Client-side message buffer (configurable, default 5000 messages)
- Per-plugin emit timing and rate counters
- RAF-gated render loop — data rates of 100+ msg/s sustained

**Keyboard navigation**
- `Arrow Up / Down` — navigate messages (disengages tail)
- `Arrow Right / Left` — expand / collapse decoded tree
- `Space` — play / pause
- `M` — toggle map
- `S` — settings panel
- Scroll up to disengage tail, click Tail button to re-engage

## Quick Start

### Docker (recommended)

```bash
git clone <repo-url> signal-monitor
cd signal-monitor
docker compose up -d
```

Open [http://localhost:8080](http://localhost:8080). The monitor is immediately listening on all protocol ports.

### Verify it's running

```bash
curl http://localhost:8080/api/stats
```

### Send test data

```bash
# CoT events via UDP
python replayer.py cot --rate 2 --loop

# GMTI via UDP
python replayer.py gmti --rate 1 --loop

# ADS-B via TCP (connects to monitor's listening port 30003)
python replayer.py adsb --rate 10 --loop

# AIS via TCP (connects to monitor's listening port 5631)
python replayer.py ais --rate 30 --loop

# Link 16 JREAP-C simulator (connects to monitor's listening port 5555)
python simulators/link16_sim.py --tracks 8 --rate 5
```

## Installation on a Server

### Prerequisites

- Docker and Docker Compose (or Podman)
- Ports: 8080 (web), plus protocol ports as needed (see table above)
- Reverse proxy (nginx) for HTTPS — optional but recommended for production

### Step 1: Clone and configure

```bash
git clone <repo-url> /opt/signal-monitor
cd /opt/signal-monitor
```

Edit `config/sigmon.conf` for global settings:

```json
{
  "web_port": 8080,
  "track_history_depth": 100,
  "track_stale_seconds": 300
}
```

Edit per-plugin configs in `config/sigmon.d/`:

```bash
ls config/sigmon.d/
# adsb.conf  ais.conf  asterix.conf  cot.conf  gmti.conf  link16.conf
```

Each plugin config controls its mode and port. Example `config/sigmon.d/asterix.conf`:

```json
{
  "udp_port": 23401,
  "multicast_group": null,
  "enabled": true
}
```

### Step 2: Build and start

```bash
docker compose up -d --build
```

Verify startup:

```bash
docker compose logs | grep "self-test"
# All plugins should show "N/N passed"

docker compose logs | grep "listening"
# Shows which ports each plugin is listening on
```

### Step 3: Reverse proxy (nginx)

For HTTPS access, add an nginx site config. Example for `sigmon.example.com`:

```nginx
server {
    listen 80;
    server_name sigmon.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name sigmon.example.com;

    ssl_certificate     /etc/letsencrypt/live/sigmon.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sigmon.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8080/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

### Step 4: Point data sources

Configure your data sources to send to the monitor's listening ports:

| Data Source | Send To |
|-------------|---------|
| ASTERIX radar | `udp://<server>:23401` |
| dump1090 / readsb (ADS-B) | `tcp://<server>:30003` (connect to monitor) |
| AIS receiver / aggregator | `tcp://<server>:5631` (connect to monitor) |
| ATAK / WinTAK (CoT) | `udp://<server>:6969` (or multicast 239.2.3.1) |
| STANAG 4607 (GMTI) | `udp://<server>:7607` |
| JREAP-C gateway (Link 16) | `tcp://<server>:5555` (connect to monitor) |

For outbound connections (e.g., connecting to a remote AIS feed), change the plugin mode to `tcp_connect` in the Settings panel or config file:

```json
{
  "mode": "tcp_connect",
  "host": "153.44.253.27",
  "tcp_port": 5631,
  "enabled": true
}
```

## Listening Ports Reference

All ports are configurable via `config/sigmon.d/<plugin>.conf` or the Settings panel.

| Port | Protocol | Transport | Plugin | Notes |
|------|----------|-----------|--------|-------|
| **8080** | HTTP/WS | TCP | Web UI | Configurable via `WEB_PORT` |
| **23401** | ASTERIX | UDP | asterix | Standard ASTERIX reception port |
| **30003** | ADS-B SBS | TCP+UDP | adsb | BaseStation standard port |
| **5631** | AIS NMEA | TCP+UDP | ais | Norwegian Coastal Admin feed port |
| **6969** | CoT/TAK SA | UDP | cot | Multicast 239.2.3.1 default |
| **8087** | CoT/TAK | TCP | cot | TAK server default (alt mode) |
| **7607** | STANAG 4607 | UDP | gmti | GMTI data |
| **5555** | JREAP-C | TCP+UDP | link16 | Link 16 tactical data link |

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI (single HTML page) |
| `/api/stats` | GET | Message rates, track count, per-plugin stats, WS perf |
| `/api/plugins` | GET | Plugin metadata, endpoints, settings schemas |
| `/api/settings` | GET | Current global + per-plugin settings |
| `/api/settings/global` | PUT | Update global settings |
| `/api/settings/{plugin}` | PUT | Update plugin settings (auto-restarts if needed) |
| `/api/plugins/{plugin}/restart` | POST | Restart a plugin |
| `/api/plugins/{plugin}/stop` | POST | Stop a plugin |
| `/api/plugins/{plugin}/enable` | POST | Enable/disable a plugin |
| `/api/tracks` | GET | Current track store snapshot |
| `/ws` | WebSocket | Live message stream + track updates |

## Architecture

```
              Data Sources
     ┌──────────┬──────────┬──────────┐
     │ UDP      │ TCP      │ TCP      │
     │ :23401   │ :30003   │ :5555    │  ...
     ▼          ▼          ▼
 ┌─────────────────────────────────────────┐
 │  Python asyncio event loop              │
 │                                         │
 │  Plugin Registry                        │
 │    ├── ASTERIX  (UDP listener)          │
 │    ├── ADS-B    (TCP server)            │
 │    ├── AIS      (TCP server)            │
 │    ├── CoT/TAK  (UDP + multicast)       │
 │    ├── GMTI     (UDP listener)          │
 │    └── Link 16  (TCP server)            │
 │         │                               │
 │         ▼                               │
 │  Emit Pipeline                          │
 │    decode() → normalize() → track store │
 │                          → WS broadcast │
 │                                         │
 │  FastAPI (uvicorn)                      │
 │    /         → HTML UI                  │
 │    /api/*    → REST endpoints           │
 │    /ws       → WebSocket stream         │
 │                                         │
 │  Settings: /etc/sigmon/sigmon.d/*.conf   │
 └─────────────────────────────────────────┘
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# JREAP transport layer (37 tests)
python -m pytest tests/test_jreap.py -v

# J-word payload decoder (22 tests)
python -m pytest tests/test_jwords.py -v
```

Tests use real protocol samples from MIL-STD-3011D test vectors (JTIDS Output Samples) and verify bit-level correctness of the decode pipeline.

## Simulators and Replayers

Standalone tools for testing — completely separate from the monitor.

### Replayer (file-based)

```bash
python replayer.py <protocol> [--rate N] [--loop] [--file FILE]
```

| Protocol | Source | Transport |
|----------|--------|-----------|
| `asterix` | pcap file (needs `dpkt`) | UDP → :23401 |
| `adsb` | SBS file or synthetic | TCP → :30003 |
| `ais` | NMEA text file (85k lines) | TCP → :5631 |
| `cot` | CoT XML events | UDP → :6969 |
| `gmti` | STANAG 4607 binary | UDP → :7607 |

### Link 16 Simulator

```bash
python simulators/link16_sim.py --tracks 8 --rate 10 --port 5555
```

Generates JREAP-C traffic with moving J2.2 Air PPLI tracks, management messages, and proper J-word encoding. Connects as a TCP client to the monitor's Link 16 listening port.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Backend | Python 3.12, FastAPI, uvicorn, asyncio |
| ASTERIX | [`asterix-decoder`](https://pypi.org/project/asterix-decoder/) (C extension) |
| Link 16 | Pure Python JREAP-C + J-word decoder (MIL-STD-3011D) |
| GMTI | Pure Python STANAG 4607 decoder |
| AIS | Built-in NMEA 0183 / AIVDM parser (no external deps) |
| CoT | Built-in XML parser |
| Frontend | Single HTML file, vanilla JS, Leaflet.js |
| Map tiles | CARTO (dark/light) |
| Container | `python:3.12-slim` (~180MB image) |

## Configuration Reference

### Global settings (`config/sigmon.conf`)

| Key | Default | Description |
|-----|---------|-------------|
| `web_port` | `8080` | HTTP/WebSocket port |
| `track_history_depth` | `100` | Max position history per track |
| `track_stale_seconds` | `300` | Remove tracks after N seconds without update |

### Plugin modes

Every TCP-capable plugin supports three modes:

| Mode | Behavior | Use case |
|------|----------|----------|
| `tcp_listen` | Accept inbound connections (default) | Data sources connect to the monitor |
| `tcp_connect` | Connect outward to a remote host | Monitor connects to a feed server |
| `udp` | Listen for UDP datagrams | Broadcast/multicast reception |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `8080` | Web UI port |
| `SIGMON_CONFIG_DIR` | `/etc/sigmon` | Config directory path |
| `ASTERIX_UDP_PORT` | `23401` | ASTERIX UDP port (Docker mapping) |
| `COT_UDP_PORT` | `6969` | CoT UDP port |
| `COT_TCP_PORT` | `8087` | CoT TCP port |
| `GMTI_UDP_PORT` | `7607` | GMTI UDP port |

## About Weibel Ventures

[Weibel Ventures](https://weibelventures.com) builds and invests in deep-tech companies at the intersection of radar, sensing, and defense technology.

**We're hiring.** If you find this project interesting, check out our open positions at [weibelventures.com](https://weibelventures.com).

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
