"""GMTI/STANAG 4607 plugin for GeoMonitor — Ground Moving Target Indication."""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase

log = logging.getLogger("geomonitor.plugin.gmti")

# ---------------------------------------------------------------------------
# STANAG 4607 type conversions
# ---------------------------------------------------------------------------

def sa32_to_deg(v: int) -> float:
    """Signed binary angle 32-bit → degrees (-180..+180)."""
    return v * 180.0 / 2147483648.0

def ba32_to_deg(v: int) -> float:
    """Unsigned binary angle 32-bit → degrees (0..360)."""
    return (v & 0xFFFFFFFF) * 360.0 / 4294967296.0

def sa16_to_deg(v: int) -> float:
    """Signed binary angle 16-bit → degrees (-180..+180)."""
    return v * 180.0 / 32768.0

def ba16_to_deg(v: int) -> float:
    """Unsigned binary angle 16-bit → degrees (0..360)."""
    return (v & 0xFFFF) * 360.0 / 65536.0

def b16_to_float(v: int) -> float:
    """Signed binary decimal 16-bit → float."""
    sign = (v >> 15) & 1
    integer = (v >> 7) & 0xFF
    frac = v & 0x7F
    val = integer + frac / 128.0
    return -val if sign else val

def lon360_to_signed(lon: float) -> float:
    """Convert 0..360 longitude to -180..+180."""
    return lon - 360.0 if lon > 180.0 else lon


# ---------------------------------------------------------------------------
# Classification & platform codes
# ---------------------------------------------------------------------------

CLASSIFICATIONS = {
    1: "Top Secret", 2: "Secret", 3: "Confidential",
    4: "Restricted", 5: "Unclassified", 6: "No Classification",
}

EXERCISE_IND = {
    0: "Real Ops", 1: "Simulated Ops", 2: "Synthesized Ops",
    128: "Real Exercise", 129: "Sim Exercise", 130: "Synth Exercise",
}

PLATFORM_TYPES = {
    0: "Unidentified", 1: "ACS", 2: "ARL-M", 3: "Sentinel",
    4: "Rotary Wing", 5: "Global Hawk (EQ-4)", 6: "Horizon",
    7: "E-8C", 8: "P-3C", 9: "Predator", 10: "RADARSAT2",
    11: "U-2S", 12: "E-10", 13: "UGS (single)", 14: "UGS (cluster)",
    15: "Ground Based", 16: "SIDM", 17: "Reaper",
    18: "Warrior A", 19: "Warrior", 20: "Twin Otter",
    255: "Other",
}

SEGMENT_TYPES = {
    1: "Mission", 2: "Dwell", 3: "HRR", 5: "Job Definition",
    6: "Free Text", 7: "Low Reflectivity Index", 8: "Group",
    9: "Attached Target", 10: "Test & Status", 11: "System Specific",
    12: "Processing History", 13: "Platform Location",
    101: "Job Request", 102: "Job Acknowledge",
}

RADAR_MODES = {
    0: "Unspecified", 1: "MTI (generic)", 2: "HRR (generic)",
    3: "MTI + HRR", 4: "HRR (wide area)", 5: "HRR (track)",
    11: "Attack Control", 12: "Attack Control (HRR)",
    31: "Wide Area MTI", 32: "Coherent Area MTI",
}

TARGET_CLASSES = {
    0: "No Info", 1: "Tracked Vehicle", 2: "Wheeled Vehicle",
    3: "Rotary Wing A/C", 4: "Fixed Wing A/C", 5: "Stationary Rotator",
    6: "Maritime", 7: "Beacon", 8: "Decoy", 9: "Person",
    126: "Other Live", 127: "Unknown Live",
    255: "Unknown Simulated",
}


# ---------------------------------------------------------------------------
# Packet & segment parsing
# ---------------------------------------------------------------------------

def parse_packet_header(data: bytes) -> dict[str, Any] | None:
    """Parse 32-byte STANAG 4607 packet header."""
    if len(data) < 32:
        return None
    ver, pkt_size, nat, cls, cls_sys, pkt_code, ex_ind, plat_id, mission_id, job_id = \
        struct.unpack(">2sI2sB2sHB10sII", data[:32])
    return {
        "version": f"{ver[0] - 48}.{ver[1] - 48}",
        "packet_size": pkt_size,
        "nationality": nat.decode("ascii", errors="replace").strip(),
        "classification": CLASSIFICATIONS.get(cls, str(cls)),
        "class_system": cls_sys.decode("ascii", errors="replace").strip(),
        "packet_code": pkt_code,
        "exercise_indicator": EXERCISE_IND.get(ex_ind, str(ex_ind)),
        "platform_id": plat_id.decode("ascii", errors="replace").strip(),
        "mission_id": mission_id,
        "job_id": job_id,
    }


def parse_segment_header(data: bytes) -> tuple[int, int]:
    """Parse 5-byte segment header → (segment_type, segment_size)."""
    seg_type, seg_size = struct.unpack(">BI", data[:5])
    return seg_type, seg_size


def parse_mission(data: bytes) -> dict[str, Any]:
    """Parse 39-byte mission segment payload."""
    plan, flight, plat_type, plat_config, year, month, day = \
        struct.unpack(">12s12sB10sHBB", data[:39])
    return {
        "mission_plan": plan.decode("ascii", errors="replace").strip(),
        "flight_plan": flight.decode("ascii", errors="replace").strip(),
        "platform_type": PLATFORM_TYPES.get(plat_type, str(plat_type)),
        "platform_config": plat_config.decode("ascii", errors="replace").strip(),
        "reference_date": f"{year:04d}-{month:02d}-{day:02d}",
    }


def parse_job_def(data: bytes) -> dict[str, Any]:
    """Parse job definition segment payload (68 bytes)."""
    if len(data) < 68:
        return {"error": "truncated"}
    job_id = struct.unpack(">I", data[0:4])[0]
    sensor_type = data[4]
    sensor_model = data[5:11].decode("ascii", errors="replace").strip()
    radar_mode = data[45]
    # Bounding area corners
    corners = []
    for i in range(4):
        off = 13 + i * 8
        lat_raw, lon_raw = struct.unpack(">iI", data[off:off + 8])
        corners.append((sa32_to_deg(lat_raw), lon360_to_signed(ba32_to_deg(lon_raw))))

    return {
        "job_id": job_id,
        "sensor_model": sensor_model,
        "radar_mode": RADAR_MODES.get(radar_mode, str(radar_mode)),
        "bounding_area": corners,
    }


def parse_dwell(data: bytes) -> dict[str, Any]:
    """Parse dwell segment payload."""
    if len(data) < 31:
        return {"error": "truncated"}

    # Existence mask (8 bytes)
    em = struct.unpack(">Q", data[0:8])[0]

    # Fixed fields (always present): 8+2+2+1+2+4+4+4+4 = 31 bytes
    ri = struct.unpack(">H", data[8:10])[0]
    di = struct.unpack(">H", data[10:12])[0]
    ld = data[12]
    trc = struct.unpack(">H", data[13:15])[0]
    dt = struct.unpack(">I", data[15:19])[0]
    s_lat = struct.unpack(">i", data[19:23])[0]
    s_lon = struct.unpack(">I", data[23:27])[0]
    s_alt = struct.unpack(">i", data[27:31])[0]

    dwell = {
        "revisit_index": ri,
        "dwell_index": di,
        "last_dwell_of_revisit": bool(ld),
        "target_report_count": trc,
        "dwell_time_ms": dt,
        "sensor_lat": sa32_to_deg(s_lat),
        "sensor_lon": lon360_to_signed(ba32_to_deg(s_lon)),
        "sensor_alt_m": s_alt / 100.0,
    }

    # Parse optional fields based on existence mask
    off = 31

    def _has(bit): return bool(em & (1 << (63 - bit)))
    def _read_sa32():
        nonlocal off; v = struct.unpack(">i", data[off:off+4])[0]; off += 4; return sa32_to_deg(v)
    def _read_ba32():
        nonlocal off; v = struct.unpack(">I", data[off:off+4])[0]; off += 4; return lon360_to_signed(ba32_to_deg(v))
    def _read_ba16():
        nonlocal off; v = struct.unpack(">H", data[off:off+2])[0]; off += 2; return ba16_to_deg(v)
    def _read_sa16():
        nonlocal off; v = struct.unpack(">h", data[off:off+2])[0]; off += 2; return sa16_to_deg(v)
    def _read_i32():
        nonlocal off; v = struct.unpack(">I", data[off:off+4])[0]; off += 4; return v
    def _read_i16():
        nonlocal off; v = struct.unpack(">H", data[off:off+2])[0]; off += 2; return v
    def _read_s16():
        nonlocal off; v = struct.unpack(">h", data[off:off+2])[0]; off += 2; return v
    def _read_i8():
        nonlocal off; v = data[off]; off += 1; return v
    def _read_s8():
        nonlocal off; v = struct.unpack(">b", data[off:off+1])[0]; off += 1; return v
    def _read_b16():
        nonlocal off; v = struct.unpack(">H", data[off:off+2])[0]; off += 2; return b16_to_float(v)

    # Bits 8-17: optional dwell-level fields
    if _has(8):  dwell["lat_scale_factor"] = _read_sa32()
    if _has(9):  dwell["lon_scale_factor"] = _read_ba32()
    if _has(10): dwell["spu_along_track"] = _read_i32()
    if _has(11): dwell["spu_cross_track"] = _read_i32()
    if _has(12): dwell["spu_alt"] = _read_i16()
    if _has(13): dwell["sensor_track"] = _read_ba16()
    if _has(14): dwell["sensor_speed_mm_s"] = _read_i32()
    if _has(15): dwell["sensor_vert_vel"] = _read_s8()
    if _has(16): dwell["sensor_track_unc"] = _read_i8()
    if _has(17): dwell["sensor_speed_unc"] = _read_i16()
    if _has(18): dwell["sensor_vert_vel_unc"] = _read_i16()
    if _has(19): dwell["platform_heading"] = _read_ba16()
    if _has(20): dwell["platform_pitch"] = _read_sa16()
    if _has(21): dwell["platform_roll"] = _read_sa16()

    # Bits 22-23: mandatory dwell center
    if _has(22): dwell["dwell_center_lat"] = _read_sa32()
    if _has(23): dwell["dwell_center_lon"] = _read_ba32()
    # Bits 24-25: mandatory dwell extent
    if _has(24): dwell["dwell_range_half_extent_km"] = _read_b16()
    if _has(25): dwell["dwell_angle_half_extent"] = _read_ba16()
    # Bits 26-29: optional sensor orientation + mdv
    if _has(26): dwell["sensor_heading"] = _read_ba16()
    if _has(27): dwell["sensor_pitch"] = _read_sa16()
    if _has(28): dwell["sensor_roll"] = _read_sa16()
    if _has(29): dwell["mdv"] = _read_i8()

    # Parse target reports
    targets = []
    for _ in range(trc):
        tgt: dict[str, Any] = {}
        if _has(30): tgt["report_index"] = _read_i16()
        if _has(31): tgt["hr_lat"] = _read_sa32()
        if _has(32): tgt["hr_lon"] = _read_ba32()
        if _has(33): tgt["delta_lat"] = _read_s16()
        if _has(34): tgt["delta_lon"] = _read_s16()
        if _has(35): tgt["geodetic_height_m"] = _read_s16()
        if _has(36): tgt["vel_los_cm_s"] = _read_s16()
        if _has(37): tgt["wrap_velocity"] = _read_i16()
        if _has(38): tgt["snr_db"] = _read_s8()
        if _has(39):
            tc = _read_i8()
            tgt["classification"] = TARGET_CLASSES.get(tc, str(tc))
            tgt["classification_code"] = tc
        if _has(40): tgt["class_prob"] = _read_i8()
        if _has(41): tgt["slant_range_unc_cm"] = _read_i16()
        if _has(42): tgt["cross_range_unc_dm"] = _read_i16()
        if _has(43): tgt["height_unc_m"] = _read_i8()
        if _has(44): tgt["rad_vel_unc_cm_s"] = _read_i16()
        if _has(45): tgt["truth_tag_app"] = _read_i8()
        if _has(46): tgt["truth_tag_entity"] = _read_i32()
        if _has(47): tgt["rcs_dbm2"] = _read_s8()

        # Resolve target position
        if "hr_lat" in tgt and "hr_lon" in tgt:
            tgt["lat"] = tgt["hr_lat"]
            tgt["lon"] = tgt["hr_lon"]
        elif "delta_lat" in tgt and "dwell_center_lat" in dwell:
            # Delta from dwell center (simplified — ignores scale factors)
            lat_sf = dwell.get("lat_scale_factor", sa32_to_deg(1))
            lon_sf = dwell.get("lon_scale_factor", ba32_to_deg(1))
            tgt["lat"] = dwell["dwell_center_lat"] + tgt["delta_lat"] * lat_sf
            tgt["lon"] = dwell["dwell_center_lon"] + tgt["delta_lon"] * lon_sf

        targets.append(tgt)

    dwell["targets"] = targets
    return dwell


def parse_segments(data: bytes) -> list[dict[str, Any]]:
    """Parse all segments from packet payload (after 32-byte header)."""
    segments = []
    off = 0
    while off + 5 <= len(data):
        seg_type, seg_size = parse_segment_header(data[off:off + 5])
        payload = data[off + 5:off + seg_size]
        seg_name = SEGMENT_TYPES.get(seg_type, f"Unknown({seg_type})")

        seg: dict[str, Any] = {"type": seg_type, "type_name": seg_name, "size": seg_size}

        if seg_type == 1:
            seg["mission"] = parse_mission(payload)
        elif seg_type == 2:
            seg["dwell"] = parse_dwell(payload)
        elif seg_type == 5:
            seg["job_def"] = parse_job_def(payload)
        # Other segment types: store raw size only

        segments.append(seg)
        off += seg_size

    return segments


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class GmtiPlugin(PluginBase):
    """STANAG 4607 GMTI plugin — Ground Moving Target Indication."""

    plugin_id = "gmti"
    name = "GMTI"

    def __init__(self):
        self._transport: asyncio.DatagramTransport | None = None
        self._mission_cache: dict[int, dict] = {}

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        port = settings.get("udp_port", 7607)
        loop = asyncio.get_running_loop()

        class Protocol(asyncio.DatagramProtocol):
            def __init__(self, emit_fn):
                self._emit = emit_fn
            def datagram_received(self, data, addr):
                asyncio.ensure_future(self._emit(data, addr))

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: Protocol(emit),
            local_addr=("0.0.0.0", port),
        )
        log.info("GMTI UDP listener started on 0.0.0.0:%d", port)

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
            log.info("GMTI UDP listener stopped")

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        pkt_hdr = parse_packet_header(raw)
        if not pkt_hdr:
            return DecodeResult(decoded=None, summary="(invalid header)", meta={},
                                error="Packet too short for STANAG 4607 header")

        segments = parse_segments(raw[32:])

        # Build decoded tree
        decoded: dict[str, Any] = {
            "Packet Header": {
                "Version": {"val": pkt_hdr["version"]},
                "Nationality": {"val": pkt_hdr["nationality"]},
                "Classification": {"val": pkt_hdr["classification"]},
                "Exercise": {"val": pkt_hdr["exercise_indicator"]},
                "Platform": {"val": pkt_hdr["platform_id"]},
                "Mission ID": {"val": pkt_hdr["mission_id"]},
                "Job ID": {"val": pkt_hdr["job_id"]},
                "Size": {"val": pkt_hdr["packet_size"], "desc": "bytes"},
            }
        }

        # Summary parts
        parts = [pkt_hdr["platform_id"]]
        total_targets = 0
        meta: dict[str, Any] = {"platform_id": pkt_hdr["platform_id"]}

        for i, seg in enumerate(segments):
            seg_key = f"Segment {i+1}: {seg['type_name']}"

            if seg["type"] == 1:
                m = seg["mission"]
                self._mission_cache[pkt_hdr["mission_id"]] = m
                decoded[seg_key] = {
                    "Mission Plan": {"val": m["mission_plan"]},
                    "Flight Plan": {"val": m["flight_plan"]},
                    "Platform Type": {"val": m["platform_type"]},
                    "Reference Date": {"val": m["reference_date"]},
                }
                parts.append(f"Mission:{m['mission_plan']}")

            elif seg["type"] == 2:
                d = seg["dwell"]
                trc = d["target_report_count"]
                total_targets += trc
                dwell_tree: dict[str, Any] = {
                    "Revisit": {"val": d["revisit_index"]},
                    "Dwell": {"val": d["dwell_index"]},
                    "Last of Revisit": {"val": d["last_dwell_of_revisit"]},
                    "Targets": {"val": trc},
                    "Dwell Time": {"val": f"{d['dwell_time_ms']}ms"},
                    "Sensor Lat": {"val": round(d["sensor_lat"], 6)},
                    "Sensor Lon": {"val": round(d["sensor_lon"], 6)},
                    "Sensor Alt": {"val": f"{d['sensor_alt_m']:.0f}m"},
                }
                if "dwell_center_lat" in d:
                    dwell_tree["Dwell Center"] = {
                        "val": f"{d['dwell_center_lat']:.4f}, {d['dwell_center_lon']:.4f}"}
                if "platform_heading" in d:
                    dwell_tree["Platform Hdg"] = {"val": f"{d['platform_heading']:.1f}°"}
                if "sensor_speed_mm_s" in d:
                    dwell_tree["Sensor Speed"] = {"val": f"{d['sensor_speed_mm_s'] / 1000:.1f} m/s"}

                # Target reports sub-tree
                for ti, tgt in enumerate(d.get("targets", [])):
                    tgt_tree: dict[str, Any] = {}
                    if "lat" in tgt:
                        tgt_tree["Position"] = {"val": f"{tgt['lat']:.6f}, {tgt['lon']:.6f}"}
                    if "vel_los_cm_s" in tgt:
                        tgt_tree["Radial Vel"] = {"val": f"{tgt['vel_los_cm_s'] / 100:.1f} m/s"}
                    if "snr_db" in tgt:
                        tgt_tree["SNR"] = {"val": f"{tgt['snr_db']} dB"}
                    if "classification" in tgt:
                        tgt_tree["Class"] = {"val": tgt["classification"]}
                    if "geodetic_height_m" in tgt:
                        tgt_tree["Height"] = {"val": f"{tgt['geodetic_height_m']}m"}
                    if "rcs_dbm2" in tgt:
                        tgt_tree["RCS"] = {"val": f"{tgt['rcs_dbm2']} dBm²"}
                    dwell_tree[f"Target {ti+1}"] = tgt_tree

                decoded[seg_key] = dwell_tree
                meta["dwell"] = d

            elif seg["type"] == 5:
                jd = seg["job_def"]
                decoded[seg_key] = {
                    "Job ID": {"val": jd.get("job_id")},
                    "Sensor": {"val": jd.get("sensor_model", "")},
                    "Radar Mode": {"val": jd.get("radar_mode", "")},
                }
                if jd.get("radar_mode"):
                    parts.append(jd["radar_mode"])
            else:
                decoded[seg_key] = {"Size": {"val": f"{seg['size']} bytes"}}

        if total_targets > 0:
            parts.append(f"{total_targets} tgt{'s' if total_targets != 1 else ''}")

        parts.append(f"{len(segments)} seg")

        return DecodeResult(
            decoded=decoded,
            summary=" ".join(parts),
            meta=meta,
        )

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        if not decoded or not isinstance(decoded, dict):
            return []

        entities = []
        dwell = meta.get("dwell")
        platform_id = meta.get("platform_id", "")

        if not dwell:
            return []

        # Sensor as a detection/sensor entity
        s_lat = dwell.get("sensor_lat")
        s_lon = dwell.get("sensor_lon")
        if s_lat is not None and s_lon is not None:
            entities.append(NormalizedEntity(
                entity_type=EntityType.SENSOR,
                entity_id=f"sensor-{platform_id}",
                lat=s_lat, lon=s_lon,
                alt_m=dwell.get("sensor_alt_m"),
                heading_deg=dwell.get("platform_heading"),
                speed_mps=dwell.get("sensor_speed_mm_s", 0) / 1000.0 if dwell.get("sensor_speed_mm_s") else None,
                timestamp=None,
                label=platform_id or "GMTI Sensor",
                symbol_code=None, confidence=None,
                source_plugin=self.plugin_id,
                properties={"revisit": dwell.get("revisit_index"), "dwell": dwell.get("dwell_index")},
            ))

        # Target reports as track entities
        for tgt in dwell.get("targets", []):
            lat = tgt.get("lat")
            lon = tgt.get("lon")
            if lat is None or lon is None:
                continue

            tgt_idx = tgt.get("report_index", 0)
            vel = tgt.get("vel_los_cm_s")
            label = tgt.get("classification", "Target")
            if tgt_idx:
                label = f"Tgt {tgt_idx}"

            entities.append(NormalizedEntity(
                entity_type=EntityType.DETECTION,
                entity_id=f"tgt-{platform_id}-{dwell.get('revisit_index',0)}-{tgt_idx}",
                lat=lat, lon=lon,
                alt_m=tgt.get("geodetic_height_m"),
                heading_deg=None,
                speed_mps=abs(vel) / 100.0 if vel is not None else None,
                timestamp=None,
                label=label,
                symbol_code=None, confidence=None,
                source_plugin=self.plugin_id,
                properties={
                    k: v for k, v in {
                        "classification": tgt.get("classification"),
                        "snr_db": tgt.get("snr_db"),
                        "rcs_dbm2": tgt.get("rcs_dbm2"),
                        "vel_los_m_s": round(vel / 100, 1) if vel else None,
                    }.items() if v is not None
                },
            ))

        return entities
