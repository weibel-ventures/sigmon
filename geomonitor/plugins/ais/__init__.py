"""AIS plugin for GeoMonitor — maritime vessel tracking via NMEA 0183."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase

log = logging.getLogger("geomonitor.plugin.ais")

# AIS message types
AIS_MSG_TYPES = {
    1: "Position Report (Class A)",
    2: "Position Report (Class A)",
    3: "Position Report (Class A)",
    4: "Base Station Report",
    5: "Static & Voyage (Class A)",
    8: "Binary Broadcast",
    9: "SAR Aircraft Position",
    11: "UTC/Date Response",
    14: "Safety Related Broadcast",
    18: "Position Report (Class B)",
    19: "Extended Position (Class B)",
    21: "Aid to Navigation",
    24: "Class B Static Data",
    27: "Long Range Position",
}

NAV_STATUS = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught",
    5: "Moored", 6: "Aground", 7: "Engaged in fishing",
    8: "Under way sailing", 14: "AIS-SART", 15: "Not defined",
}

SHIP_TYPES = {
    30: "Fishing", 31: "Towing", 32: "Towing (large)", 33: "Dredger",
    34: "Diving ops", 35: "Military ops", 36: "Sailing", 37: "Pleasure craft",
    40: "HSC", 50: "Pilot vessel", 51: "SAR vessel", 52: "Tug",
    53: "Port tender", 55: "Law enforcement", 60: "Passenger",
    70: "Cargo", 71: "Cargo (Hazmat A)", 72: "Cargo (Hazmat B)",
    80: "Tanker", 81: "Tanker (Hazmat A)", 89: "Tanker (no info)",
    90: "Other",
}


def _decode_ais_payload(payload_str: str, fill_bits: int = 0) -> list[int]:
    """Decode 6-bit ASCII AIS payload to bit array."""
    bits = []
    for c in payload_str:
        v = ord(c) - 48
        if v > 40:
            v -= 8
        for i in range(5, -1, -1):
            bits.append((v >> i) & 1)
    if fill_bits:
        bits = bits[:-fill_bits] if fill_bits else bits
    return bits


def _bits_to_uint(bits: list[int], start: int, length: int) -> int:
    val = 0
    for i in range(length):
        val = (val << 1) | bits[start + i]
    return val


def _bits_to_int(bits: list[int], start: int, length: int) -> int:
    val = _bits_to_uint(bits, start, length)
    if bits[start]:  # negative
        val -= (1 << length)
    return val


def _bits_to_string(bits: list[int], start: int, length: int) -> str:
    chars = []
    for i in range(0, length, 6):
        v = _bits_to_uint(bits, start + i, 6)
        if v == 0:
            break
        if v < 32:
            chars.append(chr(v + 64))  # A-Z etc
        else:
            chars.append(chr(v))  # space, digits
    return "".join(chars).strip("@").strip()


def _parse_aivdm(line: str) -> dict[str, Any] | None:
    """Parse a single AIVDM/AIVDO sentence."""
    # Strip any metadata prefix (Norwegian feed has \s:...\)
    if line.startswith("\\"):
        parts = line.split("\\")
        for p in parts:
            if p.startswith("!"):
                line = p
                break
        else:
            return None

    if not line.startswith("!"):
        return None

    fields = line.split(",")
    if len(fields) < 7:
        return None

    sentence_type = fields[0]  # !AIVDM or !AIVDO
    frag_count = int(fields[1]) if fields[1] else 1
    frag_num = int(fields[2]) if fields[2] else 1
    # fields[3] = sequential message ID (for multi-sentence)
    channel = fields[4]  # A or B
    payload = fields[5]
    fill_and_check = fields[6]
    fill_bits = int(fill_and_check[0]) if fill_and_check else 0

    if frag_count > 1:
        # Multi-sentence — for now only decode single-sentence messages
        # TODO: fragment reassembly
        return {"fragment": True, "frag_count": frag_count, "frag_num": frag_num,
                "channel": channel, "payload": payload}

    bits = _decode_ais_payload(payload, fill_bits)
    if len(bits) < 38:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    repeat = _bits_to_uint(bits, 6, 2)
    mmsi = _bits_to_uint(bits, 8, 30)

    result: dict[str, Any] = {
        "msg_type": msg_type,
        "msg_type_name": AIS_MSG_TYPES.get(msg_type, f"Type {msg_type}"),
        "repeat": repeat,
        "mmsi": str(mmsi).zfill(9),
        "channel": channel,
    }

    # Position reports: types 1, 2, 3
    if msg_type in (1, 2, 3) and len(bits) >= 168:
        nav_status = _bits_to_uint(bits, 38, 4)
        rot = _bits_to_int(bits, 42, 8)
        sog = _bits_to_uint(bits, 50, 10) / 10.0  # knots
        accuracy = _bits_to_uint(bits, 60, 1)
        lon = _bits_to_int(bits, 61, 28) / 600000.0
        lat = _bits_to_int(bits, 89, 27) / 600000.0
        cog = _bits_to_uint(bits, 116, 12) / 10.0
        hdg = _bits_to_uint(bits, 128, 9)

        if abs(lon) > 180 or abs(lat) > 90:
            lon, lat = None, None

        result.update({
            "nav_status": nav_status,
            "nav_status_name": NAV_STATUS.get(nav_status, "Unknown"),
            "rot": rot, "sog": sog, "accuracy": accuracy,
            "lon": lon, "lat": lat, "cog": cog,
            "hdg": hdg if hdg < 511 else None,
        })

    # Class B position: type 18
    elif msg_type == 18 and len(bits) >= 168:
        sog = _bits_to_uint(bits, 46, 10) / 10.0
        accuracy = _bits_to_uint(bits, 56, 1)
        lon = _bits_to_int(bits, 57, 28) / 600000.0
        lat = _bits_to_int(bits, 85, 27) / 600000.0
        cog = _bits_to_uint(bits, 112, 12) / 10.0
        hdg = _bits_to_uint(bits, 124, 9)

        if abs(lon) > 180 or abs(lat) > 90:
            lon, lat = None, None

        result.update({
            "sog": sog, "accuracy": accuracy,
            "lon": lon, "lat": lat, "cog": cog,
            "hdg": hdg if hdg < 511 else None,
        })

    # Static data: type 5
    elif msg_type == 5 and len(bits) >= 424:
        imo = _bits_to_uint(bits, 40, 30)
        callsign = _bits_to_string(bits, 70, 42)
        vessel_name = _bits_to_string(bits, 112, 120)
        ship_type = _bits_to_uint(bits, 232, 8)
        dest = _bits_to_string(bits, 302, 120)
        draught = _bits_to_uint(bits, 294, 8) / 10.0

        result.update({
            "imo": imo, "callsign": callsign, "vessel_name": vessel_name,
            "ship_type": ship_type,
            "ship_type_name": SHIP_TYPES.get(ship_type, SHIP_TYPES.get(ship_type // 10 * 10, "Unknown")),
            "destination": dest, "draught": draught,
        })

    # Base station: type 4
    elif msg_type == 4 and len(bits) >= 168:
        lon = _bits_to_int(bits, 79, 28) / 600000.0
        lat = _bits_to_int(bits, 107, 27) / 600000.0
        if abs(lon) > 180 or abs(lat) > 90:
            lon, lat = None, None
        result.update({"lon": lon, "lat": lat})

    return result


class AisPlugin(PluginBase):
    """AIS maritime vessel tracking plugin — NMEA 0183 via TCP or UDP."""

    plugin_id = "ais"
    name = "AIS"

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._running = False
        # Track vessel names (type 5 messages enrich later position reports)
        self._vessel_info: dict[str, dict] = {}

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        mode = settings.get("mode", "tcp_listen")
        self._running = True

        if mode == "tcp_listen":
            port = settings.get("tcp_port", 5631)
            self._task = asyncio.create_task(self._tcp_listen(port, emit))
        elif mode == "tcp_connect":
            host = settings.get("host", "127.0.0.1")
            port = settings.get("tcp_port", 5631)
            delay = settings.get("reconnect_delay", 5.0)
            self._task = asyncio.create_task(self._tcp_connect(host, port, delay, emit))
        elif mode == "udp":
            port = settings.get("udp_port", 5631)
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
        log.info("AIS plugin stopped")

    async def _tcp_listen(self, port: int, emit: EmitCallback) -> None:
        self._server = await asyncio.start_server(
            lambda r, w: self._handle_tcp_client(r, w, emit), "0.0.0.0", port)
        log.info("AIS listening on tcp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    async def _handle_tcp_client(self, reader, writer, emit):
        addr = writer.get_extra_info("peername")
        log.info("AIS client connected: %s", addr)
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
            log.info("AIS client disconnected: %s", addr)

    async def _tcp_connect(self, host: str, port: int, delay: float, emit: EmitCallback) -> None:
        while self._running:
            try:
                log.info("AIS connecting to %s:%d...", host, port)
                reader, writer = await asyncio.open_connection(host, port)
                log.info("AIS connected to %s:%d", host, port)
                while self._running:
                    line = await reader.readline()
                    if not line:
                        break
                    await emit(line, (host, port))
                writer.close()
            except asyncio.CancelledError:
                break
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                log.warning("AIS connection to %s:%d failed: %s", host, port, exc)
            if self._running:
                await asyncio.sleep(delay)

    async def _udp_listen(self, port: int, emit: EmitCallback) -> None:
        loop = asyncio.get_running_loop()

        class Protocol(asyncio.DatagramProtocol):
            def __init__(self, emit_fn):
                self._emit = emit_fn
            def datagram_received(self, data, addr):
                asyncio.ensure_future(self._emit(data, addr))

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: Protocol(emit), local_addr=("0.0.0.0", port))
        log.info("AIS listening on udp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    def self_test(self) -> list[tuple[str, bool, str]]:
        results = []
        # Type 1 position report
        try:
            line = "!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23"
            parsed = _parse_aivdm(line)
            ok = parsed is not None and parsed.get("msg_type") == 1 and parsed.get("mmsi") is not None
            results.append(("AIS Type 1", ok, f"mmsi={parsed.get('mmsi') if parsed else 'None'}"))
        except Exception as e:
            results.append(("AIS Type 1", False, str(e)))
        # Full decode pipeline
        try:
            raw = line.encode()
            result = self.decode(raw, ("127.0.0.1", 5631))
            ok = result.error is None
            results.append(("AIS decode", ok, result.summary[:60]))
        except Exception as e:
            results.append(("AIS decode", False, str(e)))
        return results

    def get_endpoints(self, settings: dict[str, Any]) -> list[str]:
        mode = settings.get("mode", "tcp_listen")
        if mode == "tcp_listen":
            return [f"tcp://0.0.0.0:{settings.get('tcp_port', 5631)}"]
        elif mode == "tcp_connect":
            return [f"tcp→{settings.get('host', '127.0.0.1')}:{settings.get('tcp_port', 5631)}"]
        elif mode == "udp":
            return [f"udp://0.0.0.0:{settings.get('udp_port', 5631)}"]
        return []

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        line = raw.decode("ascii", errors="replace").strip()
        parsed = _parse_aivdm(line)

        if not parsed:
            return DecodeResult(decoded={"raw_line": line}, summary="(unparseable)", meta={}, error="Invalid NMEA")

        if parsed.get("fragment"):
            return DecodeResult(
                decoded={"Fragment": {"Count": {"val": parsed["frag_count"]}, "Number": {"val": parsed["frag_num"]},
                         "Channel": {"val": parsed["channel"]}}},
                summary=f"MMSI fragment {parsed['frag_num']}/{parsed['frag_count']}",
                meta={"msg_type": "fragment"},
            )

        mmsi = parsed["mmsi"]
        msg_type = parsed["msg_type"]

        # Cache vessel info from type 5
        if msg_type == 5:
            self._vessel_info[mmsi] = {
                "name": parsed.get("vessel_name", ""),
                "callsign": parsed.get("callsign", ""),
                "ship_type": parsed.get("ship_type_name", ""),
                "destination": parsed.get("destination", ""),
            }

        # Enrich with cached vessel name
        vinfo = self._vessel_info.get(mmsi, {})
        vessel_name = parsed.get("vessel_name", "") or vinfo.get("name", "")

        # Build summary
        parts = [mmsi]
        if vessel_name:
            parts.append(vessel_name)
        if parsed.get("sog") is not None and parsed["sog"] > 0:
            parts.append(f"{parsed['sog']:.1f}kn")
        parts.append(parsed["msg_type_name"])

        # Decoded tree
        decoded_tree = {
            "Message": {
                "Type": {"val": msg_type, "desc": "AIS Message Type", "meaning": parsed["msg_type_name"]},
                "Repeat": {"val": parsed.get("repeat", 0), "desc": "Repeat indicator"},
                "Channel": {"val": parsed.get("channel", ""), "desc": "VHF channel (A/B)"},
            },
            "Identity": {
                "MMSI": {"val": mmsi, "desc": "Maritime Mobile Service Identity"},
            },
        }

        if vessel_name or vinfo:
            decoded_tree["Identity"]["Vessel Name"] = {"val": vessel_name or vinfo.get("name", "")}
            if parsed.get("callsign") or vinfo.get("callsign"):
                decoded_tree["Identity"]["Callsign"] = {"val": parsed.get("callsign", "") or vinfo.get("callsign", "")}
            if parsed.get("imo"):
                decoded_tree["Identity"]["IMO"] = {"val": parsed["imo"], "desc": "IMO Number"}

        if parsed.get("lat") is not None:
            decoded_tree["Position"] = {
                "Latitude": {"val": parsed["lat"], "desc": "WGS-84"},
                "Longitude": {"val": parsed["lon"], "desc": "WGS-84"},
            }
            if parsed.get("accuracy") is not None:
                decoded_tree["Position"]["Accuracy"] = {"val": parsed["accuracy"], "meaning": "High" if parsed["accuracy"] else "Low"}

        if parsed.get("sog") is not None:
            decoded_tree["Navigation"] = {}
            decoded_tree["Navigation"]["SOG"] = {"val": parsed["sog"], "desc": "Speed Over Ground (knots)"}
            if parsed.get("cog") is not None:
                decoded_tree["Navigation"]["COG"] = {"val": parsed["cog"], "desc": "Course Over Ground (degrees)"}
            if parsed.get("hdg") is not None:
                decoded_tree["Navigation"]["Heading"] = {"val": parsed["hdg"], "desc": "True heading (degrees)"}
            if parsed.get("nav_status_name"):
                decoded_tree["Navigation"]["Status"] = {"val": parsed["nav_status"], "meaning": parsed["nav_status_name"]}

        if parsed.get("ship_type_name"):
            decoded_tree["Vessel"] = {
                "Ship Type": {"val": parsed.get("ship_type", ""), "meaning": parsed["ship_type_name"]},
            }
            if parsed.get("destination"):
                decoded_tree["Vessel"]["Destination"] = {"val": parsed["destination"]}
            if parsed.get("draught"):
                decoded_tree["Vessel"]["Draught"] = {"val": parsed["draught"], "desc": "metres"}

        return DecodeResult(
            decoded=decoded_tree,
            summary=" ".join(parts),
            meta={"mmsi": mmsi, "vessel_name": vessel_name, "msg_type": msg_type},
        )

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        if not decoded or not isinstance(decoded, dict):
            return []

        pos = decoded.get("Position", {})
        lat_f = pos.get("Latitude", {})
        lon_f = pos.get("Longitude", {})
        lat = lat_f.get("val") if isinstance(lat_f, dict) else None
        lon = lon_f.get("val") if isinstance(lon_f, dict) else None

        if lat is None or lon is None:
            return []

        mmsi = meta.get("mmsi", "")
        vessel_name = meta.get("vessel_name", "")
        nav = decoded.get("Navigation", {})
        sog_kn = nav.get("SOG", {}).get("val")
        cog = nav.get("COG", {}).get("val")

        label = vessel_name if vessel_name else mmsi

        return [NormalizedEntity(
            entity_type=EntityType.TRACK,
            entity_id=mmsi,
            lat=lat, lon=lon, alt_m=None,
            heading_deg=cog,
            speed_mps=sog_kn * 0.514444 if sog_kn else None,
            timestamp=None,
            label=label,
            symbol_code=None, confidence=None,
            source_plugin=self.plugin_id,
            properties={"mmsi": mmsi, "vessel_name": vessel_name},
        )]
