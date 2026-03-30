"""Link 16 / JREAP-C plugin for Signal Monitor.

Decodes JREAP-C framing (MIL-STD-3011D AH.0), X1.0 J-Series sections,
X7.0 NPG assignments, management messages, and free-text.

Ingest: TCP (JREAP-C) or UDP (JREAP-B style).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from geomonitor.core.models import DecodeResult, EntityType, NormalizedEntity
from geomonitor.core.plugin_base import EmitCallback, PluginBase
from geomonitor.plugins.link16.jreap import (
    AH0_SIZE,
    decode_jreap_message,
    extract_messages_from_stream,
    parse_ah0,
)

log = logging.getLogger("geomonitor.plugin.link16")


class Link16Plugin(PluginBase):
    """JREAP-C / Link 16 tactical data link plugin."""

    plugin_id = "link16"
    name = "Link 16"

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._running = False

    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        mode = settings.get("mode", "tcp_listen")
        self._running = True

        if mode == "tcp_listen":
            port = settings.get("tcp_port", 5555)
            self._task = asyncio.create_task(self._tcp_listen(port, emit))
        elif mode == "tcp_connect":
            host = settings.get("host", "127.0.0.1")
            port = settings.get("tcp_port", 5555)
            delay = settings.get("reconnect_delay", 5.0)
            self._task = asyncio.create_task(self._tcp_connect(host, port, delay, emit))
        elif mode == "udp":
            port = settings.get("udp_port", 5555)
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
        log.info("Link 16 plugin stopped")

    # ----- TCP listen (default — JREAP-C sources connect to us) -----

    async def _tcp_listen(self, port: int, emit: EmitCallback) -> None:
        self._server = await asyncio.start_server(
            lambda r, w: self._handle_tcp_client(r, w, emit), "0.0.0.0", port)
        log.info("Link 16 listening on tcp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    async def _handle_tcp_client(self, reader, writer, emit):
        addr = writer.get_extra_info("peername")
        log.info("Link 16 client connected: %s", addr)
        buf = b""
        try:
            while self._running:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                messages, buf = extract_messages_from_stream(buf)
                for msg_bytes in messages:
                    await emit(msg_bytes, addr)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            log.info("Link 16 client disconnected: %s", addr)

    # ----- TCP connect (opt-in — connect outward to JREAP-C source) -----

    async def _tcp_connect(self, host: str, port: int, delay: float, emit: EmitCallback) -> None:
        while self._running:
            try:
                log.info("Link 16 connecting to %s:%d...", host, port)
                reader, writer = await asyncio.open_connection(host, port)
                log.info("Link 16 connected to %s:%d", host, port)
                buf = b""
                while self._running:
                    data = await reader.read(65536)
                    if not data:
                        break
                    buf += data
                    messages, buf = extract_messages_from_stream(buf)
                    for msg_bytes in messages:
                        await emit(msg_bytes, (host, port))
                writer.close()
            except asyncio.CancelledError:
                break
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                log.warning("Link 16 connection to %s:%d failed: %s", host, port, exc)
            if self._running:
                await asyncio.sleep(delay)

    # ----- UDP listener -----

    async def _udp_listen(self, port: int, emit: EmitCallback) -> None:
        loop = asyncio.get_running_loop()

        class Protocol(asyncio.DatagramProtocol):
            def __init__(self, emit_fn):
                self._emit = emit_fn
            def datagram_received(self, data, addr):
                asyncio.ensure_future(self._emit(data, addr))

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: Protocol(emit), local_addr=("0.0.0.0", port))
        log.info("Link 16 listening on udp://0.0.0.0:%d", port)
        while self._running:
            await asyncio.sleep(1.0)

    # ----- Decode -----

    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        msg = decode_jreap_message(raw)

        if msg.error:
            return DecodeResult(
                decoded=None,
                summary=f"JREAP ERROR: {msg.error}",
                meta={},
                error=msg.error,
            )

        # Build decoded tree
        decoded: dict[str, Any] = {}

        # AH.0 header
        ah0 = msg.ah0
        decoded["Application Header"] = {
            "Header Type": {"val": ah0.header_type, "meaning": ah0.header_type_name},
            "Message Type": {"val": ah0.message_type, "meaning": ah0.message_type_name},
            "Protocol Version": {"val": ah0.app_proto_version},
            "Total Length": {"val": ah0.abml, "desc": "bytes"},
            "Sender ID": {"val": ah0.sender_id_octal, "desc": f"({ah0.sender_id})"},
            "Time Accuracy": {"val": ah0.time_accuracy, "meaning": ah0.time_accuracy_name},
            "Data Valid Time": {"val": f"{ah0.data_valid_time:.3f}s"},
        }

        # Summary parts
        parts = [ah0.message_type_name]
        meta: dict[str, Any] = {
            "message_type": ah0.message_type,
            "sender_id": ah0.sender_id,
        }

        # X1.0 J-Series sections
        if msg.x10_sections:
            for i, sec in enumerate(msg.x10_sections):
                key = f"J-Series Section {i+1}"
                decoded[key] = {
                    "Track Number": {"val": sec.jstn_octal, "desc": f"({sec.jstn})"},
                    "Sequence": {"val": sec.seq_num},
                    "Relay": {"val": sec.relay},
                    "Ack Request": {"val": sec.ack_req},
                    "Data Age": {"val": f"{sec.data_age:.3f}s"},
                    "J-Words": {"val": sec.n_jwords},
                    "Word 1": {"val": _bits_hex(sec.j_words[0])},
                    "Word 2": {"val": _bits_hex(sec.j_words[1])},
                    "Word 3": {"val": _bits_hex(sec.j_words[2])},
                    "Word 4": {"val": _bits_hex(sec.j_words[3])},
                    "Word 5": {"val": _bits_hex(sec.word5, pad=2)},
                }
            parts.append(f"Sender:{ah0.sender_id_octal}")
            tracks = [s.jstn_octal for s in msg.x10_sections]
            parts.append(f"Trk:{','.join(tracks)}")
            parts.append(f"{len(msg.x10_sections)} sec")
            meta["tracks"] = [s.jstn for s in msg.x10_sections]

        # X7.0 NPG Assignment sections
        if msg.x70_sections:
            for i, sec in enumerate(msg.x70_sections):
                key = f"NPG Section {i+1}"
                decoded[key] = {
                    "Track Number": {"val": sec.jstn_octal},
                    "Sequence": {"val": sec.seq_num},
                    "Source Link": {"val": sec.source_link_designator},
                    "Transmit Link": {"val": sec.transmit_link_designator},
                    "NPG": {"val": sec.npg},
                    "J-Words": {"val": sec.n_jwords},
                }
            parts.append(f"NPG {len(msg.x70_sections)} sec")

        # Management
        if msg.management:
            m = msg.management
            decoded["Management"] = {
                "Subtype": {"val": m.subtype, "meaning": m.subtype_name},
                "Version": {"val": m.mgmt_version},
                "Ack Protocol": {"val": m.ack_protocol},
                "Message Length": {"val": m.message_length},
                "Sequence": {"val": m.msg_seq_num},
                "CRI": {"val": m.control_response},
                "Error Code": {"val": m.error_code},
                "Destinations": {"val": m.n_dest_addresses},
            }
            if m.dest_addresses:
                for i, addr in enumerate(m.dest_addresses):
                    decoded["Management"][f"Dest {i+1}"] = {
                        "val": f"{oct(addr)[2:].zfill(6)} ({addr})"}
            parts = [f"Mgmt:{m.subtype_name}"]
            parts.append(f"Sender:{ah0.sender_id_octal}")

        # Free text
        if msg.free_text_sections:
            for i, sec in enumerate(msg.free_text_sections):
                decoded[f"Free Text {i+1}"] = {
                    "Track Number": {"val": sec["jstn_octal"]},
                    "Type": {"val": sec["type"]},
                }
            parts.append(f"FreeText {len(msg.free_text_sections)} sec")

        return DecodeResult(
            decoded=decoded,
            summary=" ".join(parts),
            meta=meta,
        )

    # ----- Normalize -----

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        # JREAP framing doesn't carry geographic data directly.
        # J-word payload decoding (J2.2 track lat/lon) requires MIL-STD-6020C
        # which is a future enhancement. For now, no map entities.
        return []

    # ----- Self-test -----

    def self_test(self) -> list[tuple[str, bool, str]]:
        results = []

        # Test 1: AH.0 parse on known J-Series sample
        try:
            sample = bytes([49,1,0,45,0,1,84,102,228,103])
            ah0 = parse_ah0(sample)
            ok = (ah0.header_type == 3 and ah0.message_type == 1
                  and ah0.abml == 45 and ah0.sender_id == 1)
            results.append(("AH.0 parse", ok,
                            f"type={ah0.header_type} msg={ah0.message_type} abml={ah0.abml}"))
        except Exception as e:
            results.append(("AH.0 parse", False, str(e)))

        # Test 2: Full J-Series decode
        try:
            sample = bytes([49,1,0,45,0,1,84,102,228,103,0,41,31,103,0,0,0,3,
                            8,8,74,1,112,255,31,128,32,202,133,181,110,105,14,
                            252,255,63,5,0,0,0,0,1,67,0,0])
            msg = decode_jreap_message(sample)
            ok = msg.error is None and len(msg.x10_sections) == 2
            results.append(("X1.0 decode", ok,
                            f"sections={len(msg.x10_sections)} err={msg.error}"))
        except Exception as e:
            results.append(("X1.0 decode", False, str(e)))

        # Test 3: Management decode
        try:
            sample = bytes([48,1,0,30,18,52,16,0,0,0,0,16,0,20,1,5,
                            18,52,0,0,0,0,0,0,1,35,222,173,190,239])
            msg = decode_jreap_message(sample)
            ok = (msg.error is None and msg.management is not None
                  and msg.management.subtype == 0)
            results.append(("Mgmt decode", ok,
                            f"subtype={msg.management.subtype if msg.management else 'None'}"))
        except Exception as e:
            results.append(("Mgmt decode", False, str(e)))

        # Test 4: TCP stream framing
        try:
            s1 = bytes([49,1,0,45,0,1,84,102,228,103,0,41,31,103,0,0,0,3,
                         8,8,74,1,112,255,31,128,32,202,133,181,110,105,14,
                         252,255,63,5,0,0,0,0,1,67,0,0])
            msgs, rem = extract_messages_from_stream(s1 + s1)
            ok = len(msgs) == 2 and rem == b""
            results.append(("TCP framing", ok, f"msgs={len(msgs)} rem={len(rem)}"))
        except Exception as e:
            results.append(("TCP framing", False, str(e)))

        # Test 5: J-word byte-swap roundtrip
        try:
            from geomonitor.plugins.link16.jreap import convert_j_series_word
            inp = [1,0,1,1,0,0,1,0, 1,1,0,0,0,1,1,0]
            ok = convert_j_series_word(convert_j_series_word(inp)) == inp
            results.append(("J-word swap", ok, "double-swap identity"))
        except Exception as e:
            results.append(("J-word swap", False, str(e)))

        return results

    def get_endpoints(self, settings: dict[str, Any]) -> list[str]:
        mode = settings.get("mode", "tcp_listen")
        if mode == "tcp_listen":
            return [f"tcp://0.0.0.0:{settings.get('tcp_port', 5555)}"]
        elif mode == "tcp_connect":
            return [f"tcp→{settings.get('host', '127.0.0.1')}:{settings.get('tcp_port', 5555)}"]
        elif mode == "udp":
            return [f"udp://0.0.0.0:{settings.get('udp_port', 5555)}"]
        return []


def _bits_hex(bits: list[int], pad: int = 4) -> str:
    """Convert a bit list to hex string for display."""
    val = 0
    for b in bits:
        val = (val << 1) | b
    return f"0x{val:0{pad}X}"
