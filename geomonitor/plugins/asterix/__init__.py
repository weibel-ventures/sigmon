"""ASTERIX plugin for GeoMonitor — EUROCONTROL surveillance data."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import asterix as asterix_lib

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase

log = logging.getLogger("geomonitor.plugin.asterix")


def _polar_to_latlon(sensor_lat: float, sensor_lon: float, rho_nm: float, theta_deg: float) -> tuple[float, float]:
    """Convert polar (range NM, azimuth deg) relative to sensor → WGS-84 lat/lon."""
    range_m = rho_nm * 1852.0
    az_rad = theta_deg * math.pi / 180.0
    lat_rad = sensor_lat * math.pi / 180.0
    d_lat = (range_m * math.cos(az_rad)) / 111320.0
    d_lon = (range_m * math.sin(az_rad)) / (111320.0 * math.cos(lat_rad))
    return sensor_lat + d_lat, sensor_lon + d_lon


def _knots_to_mps(knots: float | None) -> float | None:
    return knots * 0.514444 if knots is not None else None


def _feet_to_meters(feet: float | None) -> float | None:
    return feet * 0.3048 if feet is not None else None


class AsterixPlugin(PluginBase):
    """ASTERIX surveillance data plugin."""

    plugin_id = "asterix"
    name = "ASTERIX"

    def __init__(self):
        self._transport: asyncio.DatagramTransport | None = None
        self._sensor_positions: dict[str, tuple[float, float, float]] = {}

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        port = settings.get("udp_port", 23401)
        loop = asyncio.get_running_loop()

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _AsterixUDP(emit),
            local_addr=("0.0.0.0", port),
        )
        log.info("ASTERIX UDP listener started on 0.0.0.0:%d", port)

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
            log.info("ASTERIX UDP listener stopped")

    def self_test(self) -> list[tuple[str, bool, str]]:
        results = []
        try:
            import asterix as asterix_lib
            results.append(("import asterix", True, "asterix-decoder available"))
        except ImportError as e:
            results.append(("import asterix", False, str(e)))
            return results
        # Minimal Cat 48 decode test
        try:
            # Minimal valid ASTERIX Cat 48 (header only)
            test = bytes([0x30, 0x00, 0x05, 0x00, 0x00])
            parsed = asterix_lib.parse(test)
            ok = isinstance(parsed, list)
            results.append(("Cat 48 parse", ok, f"result_type={type(parsed).__name__}"))
        except Exception as e:
            results.append(("Cat 48 parse", False, str(e)))
        return results

    def get_endpoints(self, settings: dict[str, Any]) -> list[str]:
        port = settings.get("udp_port", 23401)
        mc = settings.get("multicast_group")
        ep = f"udp://0.0.0.0:{port}"
        if mc:
            ep += f" (multicast {mc})"
        return [ep]

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        try:
            parsed = asterix_lib.parse(raw)
        except Exception as e:
            return DecodeResult(decoded=None, summary="PARSE ERROR", meta={}, error=str(e))

        if not parsed:
            return DecodeResult(decoded=[], summary="(empty)", meta={})

        cats = [b.get("category", "?") for b in parsed]
        summaries = []
        for block in parsed:
            cat = block.get("category", "?")
            info = f"Cat {cat}"
            if block.get("I240", {}).get("TId"):
                info += " " + block["I240"]["TId"]["val"].strip()
            elif block.get("I161", {}).get("Tn"):
                info += " Trk#" + str(block["I161"]["Tn"]["val"])
            elif block.get("I220", {}).get("ACAddr"):
                info += " " + block["I220"]["ACAddr"]["val"]
            if block.get("I000", {}).get("MsgTyp"):
                mt = block["I000"]["MsgTyp"]
                info += " " + (mt.get("meaning", "") or f"Type {mt.get('val', '?')}")
            summaries.append(info)

        meta_cat = cats[0] if len(cats) == 1 else cats
        return DecodeResult(
            decoded=parsed,
            summary=" | ".join(summaries),
            meta={"category": meta_cat},
        )

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        entities: list[NormalizedEntity] = []
        if not decoded:
            return entities

        blocks = decoded if isinstance(decoded, list) else [decoded]
        for block in blocks:
            cat = block.get("category")

            # Cat 34: extract sensor position from I120
            if cat == 34 and "I120" in block:
                i120 = block["I120"]
                if "LAT" in i120 and "Lon" in i120:
                    sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                    sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                    lat = i120["LAT"]["val"]
                    lon = i120["Lon"]["val"]
                    alt = i120.get("Height", {}).get("val", 0)
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

            # Cat 48: extract track position from I040 (polar → geodetic)
            if cat == 48 and "I040" in block:
                sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                sensor_key = f"{sac}:{sic}"
                sensor = self._sensor_positions.get(sensor_key)
                if not sensor:
                    continue

                rho = block["I040"].get("RHO", {}).get("val")
                theta = block["I040"].get("THETA", {}).get("val")
                if rho is None or theta is None:
                    continue

                lat, lon = _polar_to_latlon(sensor[0], sensor[1], rho, theta)
                tn = block.get("I161", {}).get("Tn", {}).get("val")
                alt_ft = block.get("I110", {}).get("3D_Height", {}).get("val")
                hdg = block.get("I200", {}).get("CHdg", {}).get("val")
                spd_kn = block.get("I200", {}).get("CGS", {}).get("val")
                callsign = block.get("I240", {}).get("TId", {}).get("val", "").strip()

                entity_id = f"{sensor_key}:{tn}" if tn is not None else None
                label = callsign or (f"Trk#{tn}" if tn is not None else "Unknown")

                entities.append(NormalizedEntity(
                    entity_type=EntityType.TRACK,
                    entity_id=entity_id,
                    lat=lat, lon=lon,
                    alt_m=_feet_to_meters(alt_ft),
                    heading_deg=hdg,
                    speed_mps=_knots_to_mps(spd_kn),
                    timestamp=None,
                    label=label,
                    symbol_code=None, confidence=None,
                    source_plugin=self.plugin_id,
                    properties={"cat": 48, "track_num": tn},
                ))

            # Cat 62: SDPS system track — position in I105 (WGS-84) or I100 (Cartesian)
            if cat == 62:
                sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                tn = block.get("I040", {}).get("TrkN", {}).get("val")
                if tn is None:
                    tn = block.get("I040", {}).get("val")

                lat = lon = None
                # I105: calculated WGS-84 position (preferred)
                i105 = block.get("I105", {})
                if "Lat" in i105 and "Lon" in i105:
                    lat = i105["Lat"]["val"]
                    lon = i105["Lon"]["val"]
                elif "Lat" in i105 and "Lng" in i105:
                    lat = i105["Lat"]["val"]
                    lon = i105["Lng"]["val"]

                # I100: Cartesian X/Y (0.5m resolution) — needs sensor position for reference
                if lat is None and "I100" in block:
                    i100 = block["I100"]
                    x = i100.get("X", {}).get("val")
                    y = i100.get("Y", {}).get("val")
                    sensor_key = f"{sac}:{sic}"
                    sensor = self._sensor_positions.get(sensor_key)
                    if x is not None and y is not None and sensor:
                        # X/Y in metres from sensor, convert to lat/lon
                        lat = sensor[0] + y / 111320.0
                        lon = sensor[1] + x / (111320.0 * math.cos(math.radians(sensor[0])))

                if lat is None or lon is None:
                    continue

                # Altitude from I130 (geometric, 6.25 ft resolution) or I135 (barometric)
                alt_ft = block.get("I130", {}).get("Alt", {}).get("val")
                if alt_ft is None:
                    alt_ft = block.get("I135", {}).get("CTL", {}).get("val")
                    if alt_ft is None:
                        alt_ft = block.get("I135", {}).get("Alt", {}).get("val")

                # Speed and heading from I185 (Cartesian vx/vy) or I200
                hdg = None
                spd_kn = None
                i185 = block.get("I185", {})
                vx = i185.get("Vx", {}).get("val")
                vy = i185.get("Vy", {}).get("val")
                if vx is not None and vy is not None:
                    spd_mps = math.sqrt(vx ** 2 + vy ** 2)
                    hdg = math.degrees(math.atan2(vx, vy)) % 360
                else:
                    i200 = block.get("I200", {})
                    hdg = i200.get("CHdg", {}).get("val")
                    if hdg is None:
                        hdg = i200.get("TrkAng", {}).get("val")
                    spd_kn = i200.get("CGS", {}).get("val")
                    if spd_kn is None:
                        spd_kn = i200.get("GrdSpd", {}).get("val")
                    spd_mps = None

                callsign = block.get("I245", {}).get("TId", {}).get("val", "").strip()
                if not callsign:
                    callsign = block.get("I380", {}).get("ADR", {}).get("val", "").strip()

                entity_id = f"sdps:{sac}:{sic}:{tn}" if tn is not None else None
                label = callsign or (f"Sys#{tn}" if tn is not None else "Unknown")

                props: dict[str, Any] = {"cat": 62}
                if tn is not None:
                    props["track_num"] = tn
                mode3a = block.get("I060", {}).get("Mode3A", {}).get("val")
                if mode3a:
                    props["squawk"] = mode3a

                entities.append(NormalizedEntity(
                    entity_type=EntityType.TRACK,
                    entity_id=entity_id,
                    lat=lat, lon=lon,
                    alt_m=_feet_to_meters(alt_ft) if alt_ft is not None else None,
                    heading_deg=hdg,
                    speed_mps=spd_mps if spd_mps is not None else _knots_to_mps(spd_kn),
                    timestamp=None,
                    label=label,
                    symbol_code=None, confidence=None,
                    source_plugin=self.plugin_id,
                    properties=props,
                ))

            # Cat 65: SDPS service status — emit as sensor entity
            if cat == 65:
                sac = block.get("I010", {}).get("SAC", {}).get("val", 0)
                sic = block.get("I010", {}).get("SIC", {}).get("val", 0)
                sensor_key = f"sdps:{sac}:{sic}"
                sensor = self._sensor_positions.get(f"{sac}:{sic}")
                if not sensor:
                    continue

                msg_type = block.get("I000", {}).get("MsgTyp", {}).get("val", "")
                nogo = block.get("I040", {}).get("NOGO", {}).get("val")
                ovl = block.get("I040", {}).get("OVL", {}).get("val")

                props = {"cat": 65, "sac": sac, "sic": sic}
                if msg_type:
                    props["msg_type"] = msg_type
                if nogo is not None:
                    props["nogo"] = nogo
                if ovl is not None:
                    props["overload"] = ovl

                entities.append(NormalizedEntity(
                    entity_type=EntityType.SENSOR,
                    entity_id=sensor_key,
                    lat=sensor[0], lon=sensor[1], alt_m=sensor[2],
                    heading_deg=None, speed_mps=None, timestamp=None,
                    label=f"SDPS {sac}:{sic}",
                    symbol_code=None, confidence=None,
                    source_plugin=self.plugin_id,
                    properties=props,
                ))

        return entities


class _AsterixUDP(asyncio.DatagramProtocol):
    """asyncio UDP protocol that forwards datagrams to the emit callback."""

    def __init__(self, emit: EmitCallback):
        self._emit = emit

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.ensure_future(self._emit(data, addr))

    def error_received(self, exc: Exception) -> None:
        log.warning("ASTERIX UDP error: %s", exc)
