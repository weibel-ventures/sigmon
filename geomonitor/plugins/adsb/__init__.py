"""ADS-B plugin for GeoMonitor — SBS BaseStation and AVR raw TCP feeds."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase

log = logging.getLogger("geomonitor.plugin.adsb")

# SBS message types
SBS_MSG_TYPES = {
    1: "ES Identification",
    2: "ES Surface Position",
    3: "ES Airborne Position",
    4: "ES Airborne Velocity",
    5: "Surveillance Alt",
    6: "Surveillance ID",
    7: "Air-to-Air",
    8: "All Call Reply",
}


def _parse_sbs_line(line: str) -> dict[str, Any] | None:
    """Parse an SBS BaseStation CSV line into a dict."""
    parts = line.strip().split(",")
    if len(parts) < 22 or parts[0] != "MSG":
        return None

    msg_type = int(parts[1]) if parts[1] else 0
    icao = parts[4].strip() if parts[4] else ""
    if not icao:
        return None

    def _float(s: str) -> float | None:
        try:
            return float(s) if s.strip() else None
        except ValueError:
            return None

    def _int(s: str) -> int | None:
        try:
            return int(s) if s.strip() else None
        except ValueError:
            return None

    return {
        "msg_type": msg_type,
        "msg_type_name": SBS_MSG_TYPES.get(msg_type, f"Type {msg_type}"),
        "icao": icao,
        "flight_id": parts[5].strip() if len(parts) > 5 else "",
        "date_gen": parts[6].strip() if len(parts) > 6 else "",
        "time_gen": parts[7].strip() if len(parts) > 7 else "",
        "date_log": parts[8].strip() if len(parts) > 8 else "",
        "time_log": parts[9].strip() if len(parts) > 9 else "",
        "callsign": parts[10].strip() if len(parts) > 10 else "",
        "altitude": _int(parts[11]) if len(parts) > 11 else None,
        "ground_speed": _float(parts[12]) if len(parts) > 12 else None,
        "track": _float(parts[13]) if len(parts) > 13 else None,
        "lat": _float(parts[14]) if len(parts) > 14 else None,
        "lon": _float(parts[15]) if len(parts) > 15 else None,
        "vertical_rate": _int(parts[16]) if len(parts) > 16 else None,
        "squawk": parts[17].strip() if len(parts) > 17 else "",
        "alert": parts[18].strip() == "1" if len(parts) > 18 else False,
        "emergency": parts[19].strip() == "1" if len(parts) > 19 else False,
        "spi": parts[20].strip() == "1" if len(parts) > 20 else False,
        "on_ground": parts[21].strip() == "1" if len(parts) > 21 else False,
    }


def _parse_avr_line(line: str) -> dict[str, Any] | None:
    """Parse an AVR raw hex line (*hexdata;) into a basic dict.

    For full decode, pyModeS would be needed. This provides a basic
    hex view with message length classification.
    """
    line = line.strip()
    if not line.startswith("*") or not line.endswith(";"):
        return None

    hex_data = line[1:-1]
    msg_len = len(hex_data)

    # Extract ICAO from downlink formats that contain it
    icao = ""
    if msg_len == 28:  # Long (112-bit) message
        # DF17/18: ICAO is bytes 1-3
        df = int(hex_data[0:2], 16) >> 3
        if df in (17, 18):
            icao = hex_data[2:8].upper()
    elif msg_len == 14:  # Short (56-bit) message
        df = int(hex_data[0:2], 16) >> 3
        if df in (0, 4, 5, 11):
            icao = hex_data[2:8].upper()

    return {
        "format": "avr",
        "hex": hex_data,
        "length_bits": msg_len * 4,
        "icao": icao,
        "df": int(hex_data[0:2], 16) >> 3 if msg_len >= 2 else None,
    }


class AdsbPlugin(PluginBase):
    """ADS-B / Mode S plugin — SBS BaseStation / AVR feeds via TCP or UDP."""

    plugin_id = "adsb"
    name = "ADS-B"

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._running = False
        self._format = "sbs"
        # Track state: remember callsigns per ICAO for enrichment
        self._callsigns: dict[str, str] = {}

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        mode = settings.get("mode", "tcp_listen")
        self._format = settings.get("format", "sbs")
        self._running = True

        if mode == "tcp_listen":
            port = settings.get("tcp_port", 30003)
            self._task = asyncio.create_task(self._tcp_listen(port, emit))
        elif mode == "tcp_connect":
            host = settings.get("host", "127.0.0.1")
            port = settings.get("tcp_port", 30003)
            delay = settings.get("reconnect_delay", 5.0)
            self._task = asyncio.create_task(self._tcp_connect(host, port, delay, emit))
        elif mode == "udp":
            port = settings.get("udp_port", 30003)
            self._task = asyncio.create_task(self._udp_listen(port, emit))

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            self._server = None
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("ADS-B plugin stopped")

    async def _tcp_listen(self, port: int, emit: EmitCallback) -> None:
        """Listen for inbound TCP connections (default — data sources connect to us)."""
        self._server = await asyncio.start_server(
            lambda r, w: self._handle_tcp_client(r, w, emit), "0.0.0.0", port)
        log.info("ADS-B listening on tcp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    async def _handle_tcp_client(self, reader, writer, emit):
        addr = writer.get_extra_info("peername")
        log.info("ADS-B client connected: %s", addr)
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                await emit(line, addr)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            log.info("ADS-B client disconnected: %s", addr)

    async def _tcp_connect(self, host: str, port: int, delay: float, emit: EmitCallback) -> None:
        """Connect outbound to a remote SBS/AVR feed (opt-in)."""
        while self._running:
            try:
                log.info("ADS-B connecting to %s:%d...", host, port)
                reader, writer = await asyncio.open_connection(host, port)
                log.info("ADS-B connected to %s:%d", host, port)
                while self._running:
                    line = await reader.readline()
                    if not line:
                        break
                    await emit(line, (host, port))
                writer.close()
            except asyncio.CancelledError:
                break
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                log.warning("ADS-B connection to %s:%d failed: %s", host, port, exc)
            if self._running:
                await asyncio.sleep(delay)

    async def _udp_listen(self, port: int, emit: EmitCallback) -> None:
        """Listen for UDP datagrams."""
        loop = asyncio.get_running_loop()

        class Protocol(asyncio.DatagramProtocol):
            def __init__(self, emit_fn):
                self._emit = emit_fn
            def datagram_received(self, data, addr):
                asyncio.ensure_future(self._emit(data, addr))

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: Protocol(emit), local_addr=("0.0.0.0", port))
        log.info("ADS-B listening on udp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    def self_test(self) -> list[tuple[str, bool, str]]:
        results = []
        # SBS parse test
        try:
            line = "MSG,3,1,1,4CA2E5,1,2026/03/28,10:00:00.000,2026/03/28,10:00:00.000,SAS1234 ,35000,250,45.0,55.620000,12.650000,-200,,0,0,0,0"
            parsed = _parse_sbs_line(line)
            ok = (parsed is not None and parsed["icao"] == "4CA2E5"
                  and parsed["lat"] == 55.62 and parsed["callsign"] == "SAS1234")
            results.append(("SBS parse", ok, f"icao={parsed['icao'] if parsed else 'None'}"))
        except Exception as e:
            results.append(("SBS parse", False, str(e)))
        # Full decode test
        try:
            raw = line.encode()
            result = self.decode(raw, ("127.0.0.1", 30003))
            ok = result.error is None and result.summary != ""
            results.append(("SBS decode", ok, result.summary[:60]))
        except Exception as e:
            results.append(("SBS decode", False, str(e)))
        return results

    def get_endpoints(self, settings: dict[str, Any]) -> list[str]:
        mode = settings.get("mode", "tcp_listen")
        if mode == "tcp_listen":
            return [f"tcp://0.0.0.0:{settings.get('tcp_port', 30003)}"]
        elif mode == "tcp_connect":
            return [f"tcp→{settings.get('host', '127.0.0.1')}:{settings.get('tcp_port', 30003)}"]
        elif mode == "udp":
            return [f"udp://0.0.0.0:{settings.get('udp_port', 30003)}"]
        return []

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        line = raw.decode("ascii", errors="replace").strip()

        if self._format == "sbs":
            parsed = _parse_sbs_line(line)
            if not parsed:
                return DecodeResult(decoded={"raw_line": line}, summary="(unparseable)", meta={}, error="Invalid SBS line")

            icao = parsed["icao"]
            callsign = parsed.get("callsign", "")

            # Remember callsigns
            if callsign:
                self._callsigns[icao] = callsign
            elif icao in self._callsigns:
                callsign = self._callsigns[icao]
                parsed["callsign"] = callsign

            # Build summary
            parts = [icao]
            if callsign:
                parts.append(callsign)
            alt = parsed.get("altitude")
            if alt is not None:
                parts.append(f"FL{alt // 100}" if alt > 1000 else f"{alt}ft")
            gs = parsed.get("ground_speed")
            if gs:
                parts.append(f"{gs:.0f}kn")
            parts.append(parsed["msg_type_name"])

            # Wrap in tree-friendly structure for the detail view
            decoded_tree = {
                "Message": {
                    "Type": {"val": parsed["msg_type"], "desc": "SBS Message Type", "meaning": parsed["msg_type_name"]},
                },
                "Aircraft": {
                    "ICAO": {"val": icao, "desc": "ICAO 24-bit Address"},
                    "Callsign": {"val": callsign, "desc": "Flight Identification"},
                    "Squawk": {"val": parsed.get("squawk", ""), "desc": "Transponder Code"},
                },
                "Position": {
                    "Latitude": {"val": parsed["lat"], "desc": "WGS-84 Latitude"},
                    "Longitude": {"val": parsed["lon"], "desc": "WGS-84 Longitude"},
                    "Altitude": {"val": alt, "desc": "Altitude (feet)"},
                    "On Ground": {"val": parsed["on_ground"], "desc": "Aircraft on ground"},
                },
                "Velocity": {
                    "Ground Speed": {"val": gs, "desc": "Ground speed (knots)"},
                    "Track": {"val": parsed.get("track"), "desc": "Track angle (degrees)"},
                    "Vertical Rate": {"val": parsed.get("vertical_rate"), "desc": "Vertical rate (ft/min)"},
                },
                "Timing": {
                    "Date": {"val": parsed.get("date_gen", ""), "desc": "Generated date"},
                    "Time": {"val": parsed.get("time_gen", ""), "desc": "Generated time"},
                },
                "Flags": {
                    "Alert": {"val": parsed["alert"], "desc": "Alert flag"},
                    "Emergency": {"val": parsed["emergency"], "desc": "Emergency flag"},
                    "SPI": {"val": parsed["spi"], "desc": "Special Position Identification"},
                },
            }

            return DecodeResult(
                decoded=decoded_tree,
                summary=" ".join(parts),
                meta={
                    "icao": icao,
                    "callsign": callsign,
                    "altitude": alt,
                    "msg_type": parsed["msg_type"],
                },
            )

        elif self._format == "avr":
            parsed = _parse_avr_line(line)
            if not parsed:
                return DecodeResult(decoded={"raw_line": line}, summary="(unparseable AVR)", meta={}, error="Invalid AVR line")

            icao = parsed["icao"]
            summary = f"{icao} DF{parsed.get('df', '?')} ({parsed['length_bits']}bit)"

            decoded_tree = {
                "Frame": {
                    "Hex": {"val": parsed["hex"], "desc": "Raw hex data"},
                    "Length": {"val": parsed["length_bits"], "desc": "Message length (bits)"},
                    "DF": {"val": parsed.get("df"), "desc": "Downlink Format"},
                    "ICAO": {"val": icao, "desc": "ICAO 24-bit Address"},
                },
            }

            return DecodeResult(
                decoded=decoded_tree,
                summary=summary,
                meta={"icao": icao, "df": parsed.get("df")},
            )

        return DecodeResult(decoded={"raw": line}, summary=line[:60], meta={})

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        if not decoded or not isinstance(decoded, dict):
            return []

        # Only SBS messages with position produce entities
        pos = decoded.get("Position", {})
        lat_field = pos.get("Latitude", {})
        lon_field = pos.get("Longitude", {})

        lat = lat_field.get("val") if isinstance(lat_field, dict) else None
        lon = lon_field.get("val") if isinstance(lon_field, dict) else None

        if lat is None or lon is None:
            return []

        icao = meta.get("icao", "")
        callsign = meta.get("callsign", "")
        alt_ft = meta.get("altitude")

        vel = decoded.get("Velocity", {})
        gs_kn = vel.get("Ground Speed", {}).get("val")
        track_deg = vel.get("Track", {}).get("val")

        label = callsign if callsign else icao

        return [NormalizedEntity(
            entity_type=EntityType.TRACK,
            entity_id=icao,
            lat=lat, lon=lon,
            alt_m=alt_ft * 0.3048 if alt_ft else None,
            heading_deg=track_deg,
            speed_mps=gs_kn * 0.514444 if gs_kn else None,
            timestamp=None,
            label=label,
            symbol_code=None,
            confidence=None,
            source_plugin=self.plugin_id,
            properties={
                "callsign": callsign,
                "icao": icao,
                "squawk": decoded.get("Aircraft", {}).get("Squawk", {}).get("val", ""),
            },
        )]
