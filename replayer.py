#!/usr/bin/env python3
"""Test data replayer for GeoMonitor plugins.

Usage:
  python replayer.py asterix [--file FILE] [--host HOST] [--port PORT] [--rate RATE] [--loop]
  python replayer.py adsb    [--file FILE] [--host HOST] [--port PORT] [--rate RATE] [--loop]
  python replayer.py ais     [--file FILE] [--host HOST] [--port PORT] [--rate RATE] [--loop]
  python replayer.py cot     [--file FILE] [--host HOST] [--port PORT] [--rate RATE] [--loop]
  python replayer.py gmti    [--file FILE] [--host HOST] [--port PORT] [--rate RATE] [--loop]

Examples:
  python replayer.py asterix                          # replay ASTERIX pcap to localhost:23401
  python replayer.py adsb --rate 50                   # replay ADS-B at 50 msg/s
  python replayer.py ais --rate 30 --loop             # replay AIS in a loop at 30 msg/s
  python replayer.py cot                              # replay CoT samples via UDP
  python replayer.py gmti --loop                      # replay STANAG 4607 sample in a loop
  python replayer.py adsb --file my_sbs_dump.txt      # replay custom SBS file
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Default test data paths
# ---------------------------------------------------------------------------
DEFAULTS = {
    "asterix": {
        "file": str(SCRIPT_DIR / "xenta-replayer" / "samples" / "weibel" / "2022_06_01_oslo.pcap"),
        "host": "127.0.0.1",
        "port": 23401,
        "rate": 0,  # 0 = original pcap timing
        "proto": "udp",
    },
    "adsb": {
        "file": str(SCRIPT_DIR / "test_data" / "adsb-sbs-sample.txt"),
        "host": "127.0.0.1",
        "port": 30003,
        "rate": 20,
        "proto": "tcp_client",  # Connect to monitor's listening port
    },
    "ais": {
        "file": str(SCRIPT_DIR / "test_data" / "ais-nmea-sample.txt"),
        "host": "127.0.0.1",
        "port": 5631,
        "rate": 30,
        "proto": "tcp_client",  # Connect to monitor's listening port
    },
    "cot": {
        "file": str(SCRIPT_DIR / "test_data" / "cot-samples.xml"),
        "host": "127.0.0.1",
        "port": 6969,
        "rate": 2,
        "proto": "udp",
    },
    "gmti": {
        "file": str(SCRIPT_DIR / "test_data" / "gmti-sample.4607"),
        "host": "127.0.0.1",
        "port": 7607,
        "rate": 2,
        "proto": "udp",
    },
}


# ---------------------------------------------------------------------------
# ASTERIX — pcap UDP replay
# ---------------------------------------------------------------------------
def replay_asterix(args):
    filepath = Path(args.file)
    if not filepath.exists():
        # Try alternative pcap locations
        alts = [
            SCRIPT_DIR / "xaas-radar-connector" / "test" / "sample_data" / "cat_034_048.pcap",
            SCRIPT_DIR / "xaas-radar-connector" / "test" / "weibel" / "ASTERIX_048_034.pcap",
        ]
        for alt in alts:
            if alt.exists():
                filepath = alt
                break
        if not filepath.exists():
            print(f"Error: No ASTERIX pcap found. Tried: {args.file}")
            print(f"  Place a pcap in: {DEFAULTS['asterix']['file']}")
            sys.exit(1)

    try:
        import dpkt
    except ImportError:
        print("Error: dpkt required for ASTERIX pcap replay.")
        print("  pip install dpkt")
        sys.exit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    fixed_delay = 1.0 / args.rate if args.rate > 0 else 0

    print(f"ASTERIX replayer: {filepath.name} → udp://{args.host}:{args.port}")
    if args.rate > 0:
        print(f"  Fixed rate: {args.rate} pkt/s")
    else:
        print(f"  Original pcap timing (max_delay=1s)")

    count = 0
    while True:
        with open(filepath, "rb") as f:
            pcap = dpkt.pcap.Reader(f)
            ts_last = None
            for ts, buf in pcap:
                try:
                    eth = dpkt.ethernet.Ethernet(buf)
                    data = eth.data.data.data  # eth > ip > udp > payload
                except (AttributeError, dpkt.UnpackError):
                    continue

                if fixed_delay > 0:
                    time.sleep(fixed_delay)
                elif ts_last is not None:
                    dt = min(ts - ts_last, 1.0)
                    if dt > 0:
                        time.sleep(dt)
                ts_last = ts

                sock.sendto(data, target)
                count += 1
                if count % 100 == 0:
                    print(f"\r  Sent: {count} packets", end="", flush=True)

        print(f"\n  Pass complete: {count} packets")
        if not args.loop:
            break
        print("  Looping...")

    sock.close()
    print(f"Done: {count} total packets")


# ---------------------------------------------------------------------------
# ADS-B — SBS BaseStation TCP server (plugin connects to us)
# ---------------------------------------------------------------------------
def _generate_sbs_data():
    """Generate synthetic SBS data for testing when no file exists."""
    import random
    from datetime import datetime

    aircraft = [
        ("4CA2E5", "SAS1234 ", 55.62, 12.65, 35000, 250, 45.0),
        ("3C6586", "DLH5678 ", 55.70, 12.55, 28000, 280, 120.0),
        ("A12345", "UAL999  ", 55.58, 12.70, 40000, 300, 270.0),
        ("400A1B", "BAW42   ", 55.75, 12.45, 32000, 260, 180.0),
        ("E48C01", "THY1919 ", 55.65, 12.60, 38000, 290, 90.0),
    ]

    while True:
        for icao, cs, lat, lon, alt, gs, trk in aircraft:
            now = datetime.now()
            date_s = now.strftime("%Y/%m/%d")
            time_s = now.strftime("%H:%M:%S.000")

            # MSG type 3 (airborne position)
            lat += random.uniform(-0.002, 0.002)
            lon += random.uniform(-0.002, 0.002)
            alt += random.randint(-100, 100)
            trk = (trk + random.uniform(-2, 2)) % 360
            vr = random.randint(-200, 200)

            line = f"MSG,3,1,1,{icao},1,{date_s},{time_s},{date_s},{time_s},{cs},{alt},{gs},{trk:.1f},{lat:.6f},{lon:.6f},{vr},,,0,0,0\n"
            yield line

            # Occasionally send MSG type 1 (callsign)
            if random.random() < 0.2:
                line = f"MSG,1,1,1,{icao},1,{date_s},{time_s},{date_s},{time_s},{cs},,,,,,,,,0,0,0\n"
                yield line


def replay_adsb(args):
    filepath = Path(args.file)
    use_file = filepath.exists()

    print(f"ADS-B replayer → tcp://{args.host}:{args.port}")
    if use_file:
        print(f"  File: {filepath.name}")
    else:
        print(f"  Synthetic data (5 aircraft over Copenhagen)")
    print(f"  Rate: {args.rate} msg/s")

    delay = 1.0 / args.rate if args.rate > 0 else 0.05

    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((args.host, args.port))
            print(f"  Connected to {args.host}:{args.port}")
        except ConnectionRefusedError:
            print(f"  Connection refused — retrying in 3s...")
            time.sleep(3)
            continue

        count = 0
        try:
            if use_file:
                while True:
                    with open(filepath) as f:
                        for line in f:
                            line = line.strip()
                            if not line or not line.startswith("MSG"):
                                continue
                            sock.sendall((line + "\n").encode())
                            count += 1
                            time.sleep(delay)
                            if count % 100 == 0:
                                print(f"\r  Sent: {count} messages", end="", flush=True)
                    if not args.loop:
                        break
                    print(f"\n  Pass complete, looping...")
            else:
                gen = _generate_sbs_data()
                while True:
                    line = next(gen)
                    sock.sendall(line.encode())
                    count += 1
                    time.sleep(delay)
                    if count % 100 == 0:
                        print(f"\r  Sent: {count} messages", end="", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print(f"\n  Disconnected after {count} messages")
        finally:
            sock.close()

        if not args.loop:
            break
        print(f"  Reconnecting in 2s...")
        time.sleep(2)

    print(f"Done: {count} messages")


# ---------------------------------------------------------------------------
# AIS — NMEA TCP client (connects to monitor's listening port)
# ---------------------------------------------------------------------------
def replay_ais(args):
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"Error: AIS NMEA file not found: {args.file}")
        sys.exit(1)

    line_count = sum(1 for _ in open(filepath))
    print(f"AIS replayer → tcp://{args.host}:{args.port}")
    print(f"  File: {filepath.name} ({line_count} lines)")
    print(f"  Rate: {args.rate} msg/s")

    delay = 1.0 / args.rate if args.rate > 0 else 0.03

    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((args.host, args.port))
            print(f"  Connected to {args.host}:{args.port}")
        except ConnectionRefusedError:
            print(f"  Connection refused — retrying in 3s...")
            time.sleep(3)
            continue

        count = 0
        try:
            while True:
                with open(filepath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        sock.sendall((line + "\r\n").encode())
                        count += 1
                        time.sleep(delay)
                        if count % 200 == 0:
                            print(f"\r  Sent: {count} messages", end="", flush=True)
                if not args.loop:
                    break
                print(f"\n  Pass complete ({count}), looping...")
        except (BrokenPipeError, ConnectionResetError):
            print(f"\n  Disconnected after {count} messages")
        finally:
            sock.close()

        if not args.loop:
            break
        print(f"  Reconnecting in 2s...")
        time.sleep(2)

    print(f"Done: {count} messages")


# ---------------------------------------------------------------------------
# CoT — UDP replay
# ---------------------------------------------------------------------------
def replay_cot(args):
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"Error: CoT XML file not found: {args.file}")
        sys.exit(1)

    # Parse events from file (may not have a root element)
    text = filepath.read_text()
    if not text.strip().startswith("<?xml") and not text.strip().startswith("<events"):
        text = "<root>" + text + "</root>"

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Try wrapping
        root = ET.fromstring("<root>" + filepath.read_text() + "</root>")

    events = []
    if root.tag == "event":
        events = [root]
    else:
        events = list(root.iter("event"))

    if not events:
        print(f"Error: No <event> elements found in {filepath}")
        sys.exit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    delay = 1.0 / args.rate if args.rate > 0 else 0.5

    print(f"CoT replayer: {filepath.name} → udp://{args.host}:{args.port}")
    print(f"  Events: {len(events)}")
    print(f"  Rate: {args.rate} evt/s")

    count = 0
    while True:
        for event_el in events:
            # Update timestamps to now for freshness
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            stale = now + timedelta(minutes=5)
            event_el.set("time", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            event_el.set("start", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            event_el.set("stale", stale.strftime("%Y-%m-%dT%H:%M:%SZ"))

            xml_bytes = ET.tostring(event_el, encoding="unicode").encode()
            sock.sendto(xml_bytes, target)
            count += 1
            uid = event_el.get("uid", "?")
            print(f"\r  Sent: {count} events (last: {uid})", end="", flush=True)
            time.sleep(delay)

        print(f"\n  Pass complete: {count} events")
        if not args.loop:
            break
        print("  Looping...")

    sock.close()
    print(f"Done: {count} total events")


# ---------------------------------------------------------------------------
# GMTI — STANAG 4607 UDP replay
# ---------------------------------------------------------------------------
def replay_gmti(args):
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"Error: STANAG 4607 file not found: {args.file}")
        sys.exit(1)

    raw = filepath.read_bytes()
    # Split into individual packets
    packets = []
    off = 0
    while off + 6 <= len(raw):
        pkt_size = struct.unpack(">I", raw[off + 2:off + 6])[0]
        if pkt_size < 32 or off + pkt_size > len(raw):
            break
        packets.append(raw[off:off + pkt_size])
        off += pkt_size

    if not packets:
        print(f"Error: No valid STANAG 4607 packets in {filepath}")
        sys.exit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    delay = 1.0 / args.rate if args.rate > 0 else 0.5

    print(f"GMTI replayer: {filepath.name} → udp://{args.host}:{args.port}")
    print(f"  Packets: {len(packets)} ({sum(len(p) for p in packets)} bytes)")
    print(f"  Rate: {args.rate} pkt/s")

    count = 0
    while True:
        for pkt in packets:
            sock.sendto(pkt, target)
            count += 1
            print(f"\r  Sent: {count} packets ({len(pkt)}B)", end="", flush=True)
            time.sleep(delay)

        print(f"\n  Pass complete: {count} packets")
        if not args.loop:
            break
        print("  Looping...")

    sock.close()
    print(f"Done: {count} total packets")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="GeoMonitor test data replayer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("format", choices=["asterix", "adsb", "ais", "cot", "gmti"],
                        help="Protocol format to replay")
    parser.add_argument("--file", "-f", help="Input file (default: built-in test data)")
    parser.add_argument("--host", "-H", help="Target host")
    parser.add_argument("--port", "-p", type=int, help="Target port")
    parser.add_argument("--rate", "-r", type=float, help="Messages per second (0=original timing)")
    parser.add_argument("--loop", "-l", action="store_true", help="Loop continuously")

    args = parser.parse_args()

    # Apply defaults
    defs = DEFAULTS[args.format]
    if args.file is None:
        args.file = defs["file"]
    if args.host is None:
        args.host = defs["host"]
    if args.port is None:
        args.port = defs["port"]
    if args.rate is None:
        args.rate = defs["rate"]

    print(f"=== GeoMonitor {args.format.upper()} Replayer ===\n")

    try:
        {"asterix": replay_asterix, "adsb": replay_adsb,
         "ais": replay_ais, "cot": replay_cot, "gmti": replay_gmti}[args.format](args)
    except KeyboardInterrupt:
        print("\n\nStopped.")


if __name__ == "__main__":
    main()
