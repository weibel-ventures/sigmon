"""CoT/TAK plugin for GeoMonitor — Cursor on Target situational awareness events."""

from __future__ import annotations

import asyncio
import logging
import struct
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase

log = logging.getLogger("geomonitor.plugin.cot")

# ---------------------------------------------------------------------------
# CoT type string → human-readable affiliation + role
# Format: a-<affiliation>-<battle dimension>-<function>
#   affiliation: f=friendly, h=hostile, n=neutral, u=unknown
#   battle dimension: A=air, G=ground, S=sea, U=subsurface, P=space
# Non-atom types start with b- (bits), t- (tasking), etc.
# ---------------------------------------------------------------------------

AFFILIATIONS = {
    "f": "Friendly", "h": "Hostile", "n": "Neutral", "u": "Unknown",
    "a": "Assumed Friend", "s": "Suspect", "j": "Joker", "k": "Faker",
    "o": "None", "p": "Pending",
}

BATTLE_DIMS = {
    "A": "Air", "G": "Ground", "S": "Sea", "U": "Subsurface",
    "P": "Space", "I": "Installation", "X": "Other",
}

HOW_CODES = {
    "h-e": "Human Estimated", "h-g-i-g-o": "Human GPS",
    "m-g": "Machine GPS", "m-r": "Machine Radar",
    "m-s": "Machine Sensor", "m-f": "Machine Fused",
    "m-a": "Machine Algorithmic",
}


def _parse_cot_type(cot_type: str) -> dict[str, str]:
    """Parse CoT type string into components."""
    parts = cot_type.split("-")
    result: dict[str, str] = {"raw": cot_type}

    if not parts:
        return result

    # Atom (a-) vs bits (b-) vs tasking (t-)
    kind = parts[0]
    if kind == "a" and len(parts) >= 3:
        result["kind"] = "Atom"
        result["affiliation"] = AFFILIATIONS.get(parts[1], parts[1])
        result["battle_dim"] = BATTLE_DIMS.get(parts[2], parts[2])
        if len(parts) > 3:
            result["function"] = "-".join(parts[3:])
    elif kind == "b":
        result["kind"] = "Bits"
        if len(parts) > 1:
            result["subtype"] = "-".join(parts[1:])
    elif kind == "t":
        result["kind"] = "Tasking"
        if len(parts) > 1:
            result["subtype"] = "-".join(parts[1:])
    else:
        result["kind"] = cot_type

    return result


def _parse_cot_xml(xml_bytes: bytes) -> dict[str, Any] | None:
    """Parse a single CoT XML event element."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    if root.tag != "event":
        return None

    # Event attributes
    event = {
        "version": root.get("version", ""),
        "uid": root.get("uid", ""),
        "type": root.get("type", ""),
        "time": root.get("time", ""),
        "start": root.get("start", ""),
        "stale": root.get("stale", ""),
        "how": root.get("how", ""),
    }

    # Point element
    point_el = root.find("point")
    if point_el is not None:
        try:
            event["lat"] = float(point_el.get("lat", "0"))
            event["lon"] = float(point_el.get("lon", "0"))
        except ValueError:
            event["lat"] = event["lon"] = None
        event["hae"] = _safe_float(point_el.get("hae"))
        event["ce"] = _safe_float(point_el.get("ce"))
        event["le"] = _safe_float(point_el.get("le"))

    # Detail element — extract known sub-elements
    detail_el = root.find("detail")
    if detail_el is not None:
        event["detail"] = _parse_detail(detail_el)

    return event


def _safe_float(val: str | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _parse_detail(detail: ET.Element) -> dict[str, Any]:
    """Extract known CoT detail sub-elements."""
    d: dict[str, Any] = {}

    # <contact callsign='...' endpoint='...'/>
    contact = detail.find("contact")
    if contact is not None:
        d["callsign"] = contact.get("callsign", "")
        if contact.get("endpoint"):
            d["endpoint"] = contact.get("endpoint")

    # <uid Droid='...'/>
    uid_el = detail.find("uid")
    if uid_el is not None:
        d["droid"] = uid_el.get("Droid", "")

    # <__group name='Blue' role='HQ'/>
    group = detail.find("__group")
    if group is not None:
        d["group"] = group.get("name", "")
        d["role"] = group.get("role", "")

    # <track course='...' speed='...'/>
    track = detail.find("track")
    if track is not None:
        d["course"] = _safe_float(track.get("course"))
        d["speed"] = _safe_float(track.get("speed"))

    # <status battery='...'/>
    status = detail.find("status")
    if status is not None:
        d["battery"] = status.get("battery", "")

    # <takv platform='...' device='...' version='...'/>
    takv = detail.find("takv")
    if takv is not None:
        d["platform"] = takv.get("platform", "")
        d["device"] = takv.get("device", "")
        d["tak_version"] = takv.get("version", "")

    # <remarks>...</remarks>
    remarks = detail.find("remarks")
    if remarks is not None and remarks.text:
        d["remarks"] = remarks.text.strip()

    # Collect any other sub-elements as generic attribs
    known_tags = {"contact", "uid", "__group", "track", "status", "takv", "remarks"}
    for child in detail:
        if child.tag not in known_tags:
            attrs = dict(child.items())
            if child.text and child.text.strip():
                attrs["_text"] = child.text.strip()
            if attrs:
                d[child.tag] = attrs

    return d


class CotPlugin(PluginBase):
    """Cursor on Target / TAK plugin — UDP multicast and TCP."""

    plugin_id = "cot"
    name = "CoT/TAK"

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._server: asyncio.Server | None = None
        self._running = False
        self._mode = "udp"
        self._udp_port = 6969
        self._tcp_port = 8087
        self._multicast_group = "239.2.3.1"

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        self._mode = settings.get("mode", "udp")
        self._udp_port = settings.get("udp_port", 6969)
        self._tcp_port = settings.get("tcp_port", 8087)
        self._multicast_group = settings.get("multicast_group", "239.2.3.1")
        self._running = True

        if self._mode == "tcp":
            self._task = asyncio.create_task(self._tcp_listen(emit))
            log.info("CoT plugin started (TCP :%d)", self._tcp_port)
        else:
            self._task = asyncio.create_task(self._udp_listen(emit))
            log.info("CoT plugin started (UDP :%d, multicast=%s)",
                     self._udp_port, self._multicast_group or "off")

    async def stop(self) -> None:
        self._running = False
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._server:
            self._server.close()
            self._server = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("CoT plugin stopped")

    # ----- UDP listener -----

    async def _udp_listen(self, emit: EmitCallback) -> None:
        loop = asyncio.get_running_loop()

        class Protocol(asyncio.DatagramProtocol):
            def __init__(self, emit_fn):
                self._emit = emit_fn

            def datagram_received(self, data, addr):
                asyncio.ensure_future(self._emit(data, addr))

        try:
            # Create UDP socket with multicast support
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self._udp_port))

            if self._multicast_group:
                group = socket.inet_aton(self._multicast_group)
                mreq = struct.pack("4sL", group, socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                log.info("CoT joined multicast group %s", self._multicast_group)

            sock.setblocking(False)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: Protocol(emit),
                sock=sock,
            )
            self._transport = transport

            # Keep alive until stopped
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("CoT UDP listener error: %s", exc)

    # ----- TCP server (TAK clients connect to us) -----

    async def _tcp_listen(self, emit: EmitCallback) -> None:
        try:
            self._server = await asyncio.start_server(
                lambda r, w: self._handle_tcp_client(r, w, emit),
                "0.0.0.0", self._tcp_port,
            )
            log.info("CoT TCP server listening on :%d", self._tcp_port)
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("CoT TCP server error: %s", exc)

    async def _handle_tcp_client(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter,
                                  emit: EmitCallback) -> None:
        addr = writer.get_extra_info("peername")
        log.info("CoT TCP client connected: %s", addr)
        buf = b""
        try:
            while self._running:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                # CoT events are XML — split on </event>
                while b"</event>" in buf:
                    idx = buf.index(b"</event>") + len(b"</event>")
                    event_data = buf[:idx]
                    buf = buf[idx:].lstrip()
                    # Strip any leading whitespace/newlines before <event
                    start = event_data.find(b"<event")
                    if start >= 0:
                        event_data = event_data[start:]
                    src = addr if addr else ("0.0.0.0", 0)
                    await emit(event_data, src)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            log.info("CoT TCP client disconnected: %s", addr)

    # ----- Decode -----

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        event = _parse_cot_xml(raw)

        if not event:
            text = raw.decode("utf-8", errors="replace").strip()
            return DecodeResult(decoded={"raw": text}, summary="(invalid XML)",
                                meta={}, error="XML parse error")

        uid = event["uid"]
        cot_type = event["type"]
        type_info = _parse_cot_type(cot_type)
        detail = event.get("detail", {})
        callsign = detail.get("callsign", "") or detail.get("droid", "") or uid
        how_desc = HOW_CODES.get(event["how"], event["how"])

        # Summary
        parts = [callsign]
        aff = type_info.get("affiliation")
        if aff:
            parts.append(aff)
        bdim = type_info.get("battle_dim")
        if bdim:
            parts.append(bdim)
        if detail.get("speed") and detail["speed"] > 0:
            parts.append(f"{detail['speed']:.1f}m/s")
        summary = " ".join(parts)

        # Build decoded tree
        decoded = {
            "Event": {
                "UID": {"val": uid, "desc": "Unique identifier"},
                "Type": {"val": cot_type, "desc": "CoT type string",
                         "meaning": " / ".join(v for k, v in type_info.items() if k != "raw")},
                "How": {"val": event["how"], "meaning": how_desc},
                "Time": {"val": event["time"]},
                "Stale": {"val": event["stale"]},
            },
        }

        if event.get("lat") is not None:
            pos_tree: dict[str, Any] = {
                "Latitude": {"val": event["lat"]},
                "Longitude": {"val": event["lon"]},
            }
            if event.get("hae") is not None:
                pos_tree["HAE"] = {"val": event["hae"], "desc": "Height Above Ellipsoid (m)"}
            if event.get("ce") is not None:
                pos_tree["CE"] = {"val": event["ce"], "desc": "Circular Error (m)"}
            if event.get("le") is not None:
                pos_tree["LE"] = {"val": event["le"], "desc": "Linear Error (m)"}
            decoded["Point"] = pos_tree

        # Detail sub-tree
        if detail:
            detail_tree: dict[str, Any] = {}
            if detail.get("callsign"):
                detail_tree["Callsign"] = {"val": detail["callsign"]}
            if detail.get("droid"):
                detail_tree["Droid"] = {"val": detail["droid"]}
            if detail.get("group"):
                detail_tree["Group"] = {"val": detail["group"]}
            if detail.get("role"):
                detail_tree["Role"] = {"val": detail["role"]}
            if detail.get("course") is not None:
                detail_tree["Course"] = {"val": detail["course"], "desc": "degrees"}
            if detail.get("speed") is not None:
                detail_tree["Speed"] = {"val": detail["speed"], "desc": "m/s"}
            if detail.get("battery"):
                detail_tree["Battery"] = {"val": detail["battery"] + "%"}
            if detail.get("platform"):
                detail_tree["Platform"] = {"val": detail["platform"]}
            if detail.get("device"):
                detail_tree["Device"] = {"val": detail["device"]}
            if detail.get("tak_version"):
                detail_tree["TAK Version"] = {"val": detail["tak_version"]}
            if detail.get("endpoint"):
                detail_tree["Endpoint"] = {"val": detail["endpoint"]}
            if detail.get("remarks"):
                detail_tree["Remarks"] = {"val": detail["remarks"]}
            # Any extra elements
            known = {"callsign", "droid", "group", "role", "course", "speed",
                     "battery", "platform", "device", "tak_version", "endpoint", "remarks"}
            for k, v in detail.items():
                if k not in known and isinstance(v, dict):
                    detail_tree[k] = {sk: {"val": sv} for sk, sv in v.items()}
            if detail_tree:
                decoded["Detail"] = detail_tree

        return DecodeResult(
            decoded=decoded,
            summary=summary,
            meta={
                "uid": uid,
                "cot_type": cot_type,
                "type_info": type_info,
                "callsign": callsign,
                "detail": detail,
            },
        )

    # ----- Normalize -----

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        if not decoded or not isinstance(decoded, dict):
            return []

        pos = decoded.get("Point", {})
        lat = pos.get("Latitude", {}).get("val")
        lon = pos.get("Longitude", {}).get("val")

        if lat is None or lon is None:
            return []

        hae = pos.get("HAE", {}).get("val")
        uid = meta.get("uid", "")
        callsign = meta.get("callsign", uid)
        type_info = meta.get("type_info", {})
        detail = meta.get("detail", {})

        # Determine entity type from CoT type
        kind = type_info.get("kind", "")
        aff = type_info.get("affiliation", "")
        if kind == "Atom":
            entity_type = EntityType.TRACK
        else:
            entity_type = EntityType.WAYPOINT

        # Course and speed from detail
        course = detail.get("course")
        speed = detail.get("speed")

        props: dict[str, Any] = {"uid": uid}
        if aff:
            props["affiliation"] = aff
        if type_info.get("battle_dim"):
            props["battle_dim"] = type_info["battle_dim"]
        if detail.get("group"):
            props["group"] = detail["group"]
        if detail.get("role"):
            props["role"] = detail["role"]
        if detail.get("remarks"):
            props["remarks"] = detail["remarks"]

        return [NormalizedEntity(
            entity_type=entity_type,
            entity_id=uid,
            lat=lat,
            lon=lon,
            alt_m=hae,
            heading_deg=course,
            speed_mps=speed,
            timestamp=None,
            label=callsign,
            symbol_code=None,
            confidence=None,
            source_plugin=self.plugin_id,
            properties=props,
        )]
