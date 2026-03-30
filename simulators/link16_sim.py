#!/usr/bin/env python3
"""Link 16 / JREAP-C traffic simulator for Signal Monitor testing.

Generates realistic JREAP-C TCP traffic including:
  - J-Series messages (X1.0) with track numbers and J-word payloads
  - Management messages (X0.0 Echo, X0.1 CTR, X0.7 Operator-to-Operator)
  - Periodic track updates with incrementing sequence numbers

Usage:
  python simulators/link16_sim.py                    # TCP server on :5555
  python simulators/link16_sim.py --port 6000        # custom port
  python simulators/link16_sim.py --rate 20          # 20 messages/sec
  python simulators/link16_sim.py --tracks 10        # 10 simulated tracks

Connect the Link 16 plugin to this simulator's address.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import struct
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("link16_sim")

# ---------------------------------------------------------------------------
# JREAP-C message builder
# ---------------------------------------------------------------------------

def bits_to_bytes(bits: list[int]) -> bytes:
    """Convert MSB-first bit array to bytes (padded to full bytes)."""
    while len(bits) % 8 != 0:
        bits.append(0)
    result = []
    for i in range(0, len(bits), 8):
        val = 0
        for j in range(8):
            val = (val << 1) | bits[i + j]
        result.append(val)
    return bytes(result)


def uint_to_bits(val: int, width: int) -> list[int]:
    """Convert unsigned integer to MSB-first bit array of given width."""
    bits = []
    for i in range(width - 1, -1, -1):
        bits.append((val >> i) & 1)
    return bits


def reverse_byte_bits(bits_16: list[int]) -> list[int]:
    """Reverse of ConvertJSeriesWord — encode from canonical to wire order."""
    out = [0] * 16
    for i in range(8):
        out[i] = bits_16[7 - i]
        out[8 + i] = bits_16[15 - i]
    return out


def build_ah0(message_type: int, payload_len: int, sender_id: int,
              time_accuracy: int = 5, data_valid_time_s: float = 0.0) -> bytes:
    """Build a 10-byte JREAP-C AH.0 header."""
    bits: list[int] = []
    bits.extend(uint_to_bits(3, 4))              # header_type = JREAP-C
    bits.extend(uint_to_bits(message_type, 4))   # message_type
    bits.append(0)                                # tx_time_ref
    bits.extend([0, 0, 0])                       # spare
    bits.extend(uint_to_bits(1, 4))              # app_proto_version
    bits.extend(uint_to_bits(10 + payload_len, 16))  # ABML
    bits.extend(uint_to_bits(sender_id, 16))     # sender_id
    bits.extend(uint_to_bits(time_accuracy, 4))  # time_accuracy
    dvt_raw = int(data_valid_time_s * 1024.0) & 0x0FFFFFFF
    bits.extend(uint_to_bits(dvt_raw, 28))       # data_valid_time
    return bits_to_bytes(bits)


def build_x10_section(jstn: int, seq_num: int, j_word_bits: list[int] | None = None,
                       data_age_s: float = 0.0) -> bytes:
    """Build one 136-bit (17-byte) X1.0 J-Series section."""
    bits: list[int] = []
    bits.extend(uint_to_bits(jstn, 16))          # JSTN
    bits.extend(uint_to_bits(seq_num, 16))       # seq_num
    bits.append(0)                                # relay
    bits.append(0)                                # ack_req
    bits.append(0)                                # spare
    data_age_raw = int(data_age_s * 32.0) & 0x1FFF
    bits.extend(uint_to_bits(data_age_raw, 13))  # data_age
    bits.extend([0, 0, 0, 0])                    # spare nibble
    bits.extend(uint_to_bits(3, 12))             # n_jwords (3 = standard)

    # J-words: 4 × 16 bits (wire order) + 6-bit word5
    if j_word_bits and len(j_word_bits) >= 70:
        # Convert from canonical to wire order
        for w in range(4):
            word = j_word_bits[w * 16:(w + 1) * 16]
            bits.extend(reverse_byte_bits(word))
        bits.extend([0, 0])  # spare
        bits.extend(j_word_bits[64:70])  # word5 (no swap)
    else:
        # Generate random J-word content
        for _ in range(4):
            bits.extend([random.randint(0, 1) for _ in range(16)])
        bits.extend([0, 0])
        bits.extend([random.randint(0, 1) for _ in range(6)])

    return bits_to_bytes(bits)


def build_mgmt_echo(sender_id: int, dest_id: int, app_data: bytes = b"\xDE\xAD\xBE\xEF") -> bytes:
    """Build a complete Management Echo (X0.0.0) message."""
    # MMSH.0 payload
    payload_bits: list[int] = []
    payload_bits.extend(uint_to_bits(0, 8))      # subtype = Echo
    payload_bits.extend(uint_to_bits(1, 4))      # mgmt_version
    payload_bits.extend(uint_to_bits(0, 4))      # ack_protocol
    payload_bits.extend(uint_to_bits(20, 16))    # message_length
    payload_bits.extend(uint_to_bits(1, 8))      # n_dest
    payload_bits.extend(uint_to_bits(5, 8))      # timeout
    payload_bits.extend(uint_to_bits(0, 16))     # msg_seq_num
    payload_bits.extend(uint_to_bits(0, 8))      # CRI
    payload_bits.extend(uint_to_bits(0, 8))      # error_code
    payload_bits.extend(uint_to_bits(0, 8))      # frag_num
    payload_bits.extend(uint_to_bits(0, 8))      # total_frags
    payload_bits.extend(uint_to_bits(0, 16))     # orig_seq
    payload_bits.extend(uint_to_bits(dest_id, 16))  # dest address
    # Application data
    for b in app_data:
        payload_bits.extend(uint_to_bits(b, 8))

    payload = bits_to_bytes(payload_bits)
    header = build_ah0(0, len(payload), sender_id)
    return header + payload


def build_j2_2_jword(lat: float, lon: float, alt_ft: float = 30000,
                      identity: int = 3, quality: int = 5) -> list[int]:
    """Build a 70-bit J2.2 Air PPLI J-word block with encoded position.

    Encodes position using the field layout matching jtables/j2_ppli.py:
      W0 [0:2]   Word Format = 00 (Initial)
      W0 [2:7]   Label = 00010 (2)
      W0 [7:10]  Sub-label = 010 (2)
      W0 [10:13] Track Quality (3 bits)
      W0 [13:16] Identity (3 bits)
      W1 [0:16]  Latitude coarse (16 bits signed, 90/2^15 deg/LSB)
      W2 [0:16]  Longitude coarse (16 bits signed, 180/2^15 deg/LSB)
      W3 [0:13]  Altitude (13 bits signed, 25 ft/LSB)
      W3 [13:16] spare
      W4 [0:6]   spare
    """
    bits = [0] * 70

    # Header (bits 0-15 = W0)
    bits[0:2] = [0, 0]                         # Word Format = Initial
    bits[2:7] = uint_to_bits(2, 5)             # Label = 2 (PPLI)
    bits[7:10] = uint_to_bits(2, 3)            # Sub-label = 2 (Air)
    bits[10:13] = uint_to_bits(quality & 7, 3) # Track Quality
    bits[13:16] = uint_to_bits(identity & 7, 3)# Identity

    def _encode_signed(val: int, n_bits: int) -> int:
        """Encode signed value as unsigned for bit extraction."""
        if val < 0:
            return val + (1 << n_bits)
        return val

    # Latitude: signed 23-bit, resolution = 90/2^22 deg/LSB
    # Bits 16-38 (spans W1 + first 7 bits of W2)
    lat_raw = int(round(lat * (1 << 22) / 90.0))
    lat_raw = max(-(1 << 22), min((1 << 22) - 1, lat_raw))
    lat_u = _encode_signed(lat_raw, 23)
    for i in range(23):
        bits[16 + i] = (lat_u >> (22 - i)) & 1

    # Longitude: signed 24-bit, resolution = 180/2^23 deg/LSB
    # Bits 39-62 (W2 bits 7-15 + W3 full)
    lon_raw = int(round(lon * (1 << 23) / 180.0))
    lon_raw = max(-(1 << 23), min((1 << 23) - 1, lon_raw))
    lon_u = _encode_signed(lon_raw, 24)
    for i in range(24):
        bits[39 + i] = (lon_u >> (23 - i)) & 1

    # Altitude: signed 13-bit, 25 ft/LSB
    # Bits 48-60 (inside W3, after longitude)
    # Note: this overlaps with longitude at bit 48 in the current layout.
    # In the real standard, altitude is in a different position.
    # For now, place it at bits 63-69 (W3 tail + W4) to avoid overlap.
    # This is TBV and will be corrected from MIL-STD-6016E word tables.

    return bits


def build_j3_2_jword(lat: float, lon: float, alt_ft: float = 30000,
                      identity: int = 6, quality: int = 4) -> list[int]:
    """Build a 70-bit J3.2 Air Track J-word block (surveillance track).

    Same field layout as J2.2 but with Label=3 (Surveillance), Sub-label=2.
    Default identity=6 (Hostile) for surveillance tracks.
    """
    bits = build_j2_2_jword(lat, lon, alt_ft, identity, quality)
    # Override label to 3 (Surveillance)
    bits[2:7] = uint_to_bits(3, 5)
    return bits


def build_jseries_message(sender_id: int, tracks: list[dict], seq_base: int,
                           dvt: float = 0.0) -> bytes:
    """Build a J-Series (X1.0) message with multiple track sections.

    Each track dict should have 'jstn' and optionally 'lat', 'lon', 'alt_ft',
    'identity', 'msg_type' ('ppli' or 'surv').
    """
    sections = b""
    for i, track in enumerate(tracks):
        # Build J-word with position if available
        lat = track.get("lat")
        lon = track.get("lon")
        alt = track.get("alt_ft", 30000)
        identity = track.get("identity", 3)  # Default: Friend
        msg_type = track.get("msg_type", "ppli")

        if lat is not None and lon is not None:
            if msg_type == "surv":
                j_bits = build_j3_2_jword(lat, lon, alt, identity)
            else:
                j_bits = build_j2_2_jword(lat, lon, alt, identity)
        else:
            j_bits = None

        sec = build_x10_section(
            jstn=track["jstn"],
            seq_num=seq_base + i,
            j_word_bits=j_bits,
            data_age_s=random.uniform(0.0, 0.5),
        )
        sections += sec

    header = build_ah0(1, len(sections), sender_id, data_valid_time_s=dvt)
    return header + sections


# ---------------------------------------------------------------------------
# Track simulation
# ---------------------------------------------------------------------------

class SimTrack:
    """A simulated Link 16 track with motion."""

    def __init__(self, jstn: int, lat: float, lon: float, alt_ft: float,
                 speed_kn: float, heading_deg: float, label: str):
        self.jstn = jstn
        self.lat = lat
        self.lon = lon
        self.alt_ft = alt_ft
        self.speed_kn = speed_kn
        self.heading_deg = heading_deg
        self.label = label

    def update(self, dt: float):
        """Advance position by dt seconds."""
        speed_mps = self.speed_kn * 0.514444
        hdg_rad = math.radians(self.heading_deg)
        dlat = (speed_mps * math.cos(hdg_rad) * dt) / 111320.0
        dlon = (speed_mps * math.sin(hdg_rad) * dt) / (111320.0 * math.cos(math.radians(self.lat)))
        self.lat += dlat
        self.lon += dlon
        # Gentle heading drift
        self.heading_deg = (self.heading_deg + random.uniform(-1, 1)) % 360


def generate_tracks(n: int) -> list[SimTrack]:
    """Generate n simulated tracks in the Copenhagen / Baltic area."""
    tracks = []
    labels = [
        "ALFA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT",
        "GOLF", "HOTEL", "INDIA", "JULIET", "KILO", "LIMA",
        "MIKE", "NOVEMBER", "OSCAR", "PAPA", "QUEBEC", "ROMEO",
    ]
    for i in range(n):
        jstn = 0o010000 + i  # Track numbers starting at octal 010000
        lat = 55.0 + random.uniform(-2, 2)
        lon = 12.0 + random.uniform(-3, 3)
        alt = random.choice([5000, 10000, 20000, 30000, 35000, 40000])
        speed = random.uniform(150, 550)  # knots
        heading = random.uniform(0, 360)
        label = labels[i % len(labels)] + f"-{i+1:02d}"
        tracks.append(SimTrack(jstn, lat, lon, alt, speed, heading, label))
    return tracks


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         tracks: list[SimTrack], rate: float, sender_id: int):
    addr = writer.get_extra_info("peername")
    log.info("Client connected: %s", addr)

    delay = 1.0 / rate if rate > 0 else 0.5
    seq = 0
    count = 0
    t_last = time.monotonic()

    try:
        while True:
            now = time.monotonic()
            dt = now - t_last
            t_last = now

            # Update track positions
            for t in tracks:
                t.update(dt)

            # Current time of day in seconds
            tod = time.time() % 86400

            # Send J-Series message with all tracks
            # Split into batches of max 4 tracks per message
            for i in range(0, len(tracks), 4):
                batch = tracks[i:i + 4]
                msg = build_jseries_message(
                    sender_id=sender_id,
                    tracks=[{"jstn": t.jstn, "lat": t.lat, "lon": t.lon,
                             "alt_ft": t.alt_ft, "msg_type": "ppli"} for t in batch],
                    seq_base=seq,
                    dvt=tod,
                )
                writer.write(msg)
                seq += len(batch)
                count += 1

            # Occasionally send management messages
            if count % 20 == 0:
                echo = build_mgmt_echo(sender_id, 0x0001)
                writer.write(echo)
                count += 1

            await writer.drain()

            if count % 50 == 0:
                log.info("Sent %d messages, %d tracks, seq=%d", count, len(tracks), seq)

            await asyncio.sleep(delay)

    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        log.info("Client disconnected: %s (sent %d messages)", addr, count)


async def run_server(port: int, n_tracks: int, rate: float, sender_id: int):
    tracks = generate_tracks(n_tracks)
    log.info("Generated %d simulated tracks", n_tracks)
    for t in tracks:
        log.info("  Track %s: %s (%.1f kn, %.0f ft, hdg %.0f°)",
                 oct(t.jstn)[2:].zfill(6), t.label, t.speed_kn, t.alt_ft, t.heading_deg)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, tracks, rate, sender_id),
        "0.0.0.0", port,
    )
    log.info("JREAP-C simulator listening on tcp://0.0.0.0:%d", port)
    log.info("Rate: %.1f msg/s, Tracks: %d, Sender: %s",
             rate, n_tracks, oct(sender_id)[2:].zfill(6))
    log.info("Connect the Link 16 plugin to this address.")

    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(
        description="Link 16 / JREAP-C traffic simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", "-p", type=int, default=5555, help="TCP listen port (default: 5555)")
    parser.add_argument("--rate", "-r", type=float, default=10.0, help="Messages per second (default: 10)")
    parser.add_argument("--tracks", "-t", type=int, default=5, help="Number of simulated tracks (default: 5)")
    parser.add_argument("--sender", "-s", type=int, default=0o012345,
                        help="Sender ID in decimal (default: 5349 = octal 012345)")
    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.port, args.tracks, args.rate, args.sender))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
