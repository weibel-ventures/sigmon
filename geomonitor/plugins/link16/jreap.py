"""JREAP-C / Link 16 protocol decoder.

Implements MIL-STD-3011D Application Header (AH.0) parsing, X1.0 J-Series
section extraction, X7.0 NPG assignment, management messages, and free-text.

Reference: MIL-STD-3011D, MIL-STD-6016E, MIL-STD-6020C
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Bit manipulation helpers
# ---------------------------------------------------------------------------

def bytes_to_bits(data: bytes) -> list[int]:
    """Convert bytes to MSB-first bit array (network byte order)."""
    bits: list[int] = []
    for b in data:
        for j in range(7, -1, -1):
            bits.append((b >> j) & 1)
    return bits


def bits_to_uint(bits: list[int]) -> int:
    """MSB-first bit array to unsigned integer."""
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def bits_to_uint_range(bits: list[int], start: int, length: int) -> int:
    """Extract unsigned integer from bit array at [start:start+length]."""
    return bits_to_uint(bits[start:start + length])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_TYPES = {
    0: "Undefined",
    1: "ATP JREAP-A",
    2: "PTP JREAP-B",
    3: "Application JREAP-C",
}

MESSAGE_TYPES = {
    0: "Management",
    1: "J-Series (X1.0)",
    2: "Free Text Coded (X2.0)",
    3: "Free Text Uncoded (X3.0)",
    4: "VMF",
    5: "Link 22",
    6: "CMF IBS",
    7: "NPG Assignment (X7.0)",
    15: "Reserved",
}

MANAGEMENT_SUBTYPES = {
    0: "Echo (X0.0.0)",
    1: "Common Time Reference (X0.1.0)",
    2: "Round-Trip Time Delay (X0.2.0)",
    4: "Acknowledgement (X0.4.0)",
    5: "Latency Threshold (X0.5.0)",
    6: "Latency Exceeded (X0.6.0)",
    7: "Operator-to-Operator (X0.7.0)",
    8: "Special Event (X0.8.0)",
    9: "Terminate Link (X0.9.0)",
    10: "Filter Response (X0.10.0)",
    11: "Secondary Track Number List (X0.11.0)",
    12: "Direct Connection List (X0.12.0)",
    13: "Network Connectivity Matrix (X0.13.0)",
    14: "Connectivity Feedback (X0.14.0)",
}

TIME_ACCURACY = {
    0: "No statement",
    1: "≤ 1 ms",
    2: "> 1 ms, ≤ 2 ms",
    3: "> 2 ms, ≤ 4 ms",
    4: "> 4 ms, ≤ 8 ms",
    5: "> 8 ms, ≤ 16 ms",
    6: "> 16 ms, ≤ 32 ms",
    7: "> 32 ms, ≤ 64 ms",
    8: "> 64 ms, ≤ 128 ms",
    9: "> 128 ms, ≤ 256 ms",
    10: "> 256 ms, ≤ 512 ms",
    11: "> 512 ms, ≤ 1024 ms",
    12: "> 1024 ms, ≤ 2048 ms",
    13: "> 2048 ms, ≤ 4096 ms",
    14: "> 4096 ms, ≤ 8192 ms",
    15: "> 8192 ms, ≤ 16384 ms",
}

# AH.0 header size in bytes
AH0_SIZE = 10
# X1.0 section size in bits
X10_SECTION_BITS = 136
# X7.0 section size in bits
X70_SECTION_BITS = 184
# X2.0 section size in bits
X20_SECTION_BITS = 264
# X3.0 section size in bits
X30_SECTION_BITS = 512


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AH0Header:
    """JREAP Application Header (AH.0) — 10 bytes / 80 bits."""
    header_type: int          # 4 bits — 3 = JREAP-C
    header_type_name: str
    message_type: int         # 4 bits — 0=Mgmt, 1=J-Series, etc.
    message_type_name: str
    tx_time_ref: int          # 1 bit
    app_proto_version: int    # 4 bits
    abml: int                 # 16 bits — total message length in bytes
    sender_id: int            # 16 bits — displayed as octal
    sender_id_octal: str
    time_accuracy: int        # 4 bits
    time_accuracy_name: str
    data_valid_time: float    # 28 bits → seconds (raw / 1024.0)
    data_valid_time_raw: int


@dataclass
class X10Section:
    """One X1.0 J-Series section — 136 bits."""
    jstn: int                 # 16 bits — JRE Source Track Number
    jstn_octal: str
    seq_num: int              # 16 bits — J-Series message sequence number
    relay: int                # 1 bit
    ack_req: int              # 1 bit
    data_age: float           # 13 bits → seconds (raw / 32.0)
    data_age_raw: int
    n_jwords: int             # 12 bits
    j_words: list[list[int]]  # 4 × 16-bit words (byte-swapped)
    word5: list[int]          # 6 bits (no swap)
    j_word_bits_70: list[int] # reconstructed 70-bit J-word block


@dataclass
class X70Section:
    """One X7.0 NPG Assignment section — 184 bits."""
    jstn: int
    jstn_octal: str
    seq_num: int
    relay: int
    ack_req: int
    data_age: float
    data_age_raw: int
    n_jwords: int
    source_link_designator: int
    transmit_link_designator: int
    npg: int                  # 9 bits — Network Participation Group
    j_words: list[list[int]]
    word5: list[int]
    j_word_bits_70: list[int]


@dataclass
class ManagementHeader:
    """Management Message Subheader (MMSH.0)."""
    subtype: int
    subtype_name: str
    mgmt_version: int
    ack_protocol: int
    message_length: int
    n_dest_addresses: int
    completion_timeout: int
    msg_seq_num: int
    control_response: int
    error_code: int
    fragment_num: int
    total_fragments: int
    orig_msg_seq_num: int
    dest_addresses: list[int]


@dataclass
class JREAPMessage:
    """Complete decoded JREAP message."""
    ah0: AH0Header
    raw: bytes
    # Populated based on message_type
    x10_sections: list[X10Section] = field(default_factory=list)
    x70_sections: list[X70Section] = field(default_factory=list)
    management: ManagementHeader | None = None
    free_text_sections: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# J-Series word byte-swap (MIL-STD-3011D, DFI3028)
# ---------------------------------------------------------------------------

def convert_j_series_word(bits_16: list[int]) -> list[int]:
    """Apply byte-swap as specified in dfiset3028.cpp ConvertJSeriesWord.

    Within each byte of the 16-bit word, bit order is reversed.
    Input bits 0-7 map to output positions 7,6,5,4,3,2,1,0.
    Input bits 8-15 map to output positions 15,14,13,12,11,10,9,8.
    """
    out = [0] * 16
    for i in range(8):
        out[7 - i] = bits_16[i]
        out[15 - i] = bits_16[8 + i]
    return out


def reconstruct_j_word_70(words: list[list[int]], word5: list[int]) -> list[int]:
    """Reconstruct the 70-bit J-word block from 4 swapped words + 6-bit word5."""
    bits: list[int] = []
    for w in words:
        bits.extend(w)
    bits.extend(word5)
    return bits


# ---------------------------------------------------------------------------
# Track number formatting
# ---------------------------------------------------------------------------

def format_track_octal(tn: int) -> str:
    """Format a 16-bit track number as 6-digit octal (Link 16 convention)."""
    return oct(tn)[2:].zfill(6)


# ---------------------------------------------------------------------------
# AH.0 Parser
# ---------------------------------------------------------------------------

def parse_ah0(data: bytes) -> AH0Header:
    """Parse 10-byte JREAP-C Application Header.

    Raises ValueError if data is too short.
    """
    if len(data) < AH0_SIZE:
        raise ValueError(f"AH.0 requires {AH0_SIZE} bytes, got {len(data)}")

    bits = bytes_to_bits(data[:AH0_SIZE])

    header_type = bits_to_uint_range(bits, 0, 4)
    message_type = bits_to_uint_range(bits, 4, 4)
    tx_time_ref = bits[8]
    # bits 9-11 spare
    app_proto_ver = bits_to_uint_range(bits, 12, 4)
    abml = bits_to_uint_range(bits, 16, 16)
    sender_id = bits_to_uint_range(bits, 32, 16)
    time_accuracy = bits_to_uint_range(bits, 48, 4)
    dvt_raw = bits_to_uint_range(bits, 52, 28)

    return AH0Header(
        header_type=header_type,
        header_type_name=HEADER_TYPES.get(header_type, f"Unknown({header_type})"),
        message_type=message_type,
        message_type_name=MESSAGE_TYPES.get(message_type, f"Unknown({message_type})"),
        tx_time_ref=tx_time_ref,
        app_proto_version=app_proto_ver,
        abml=abml,
        sender_id=sender_id,
        sender_id_octal=format_track_octal(sender_id),
        time_accuracy=time_accuracy,
        time_accuracy_name=TIME_ACCURACY.get(time_accuracy, f"Unknown({time_accuracy})"),
        data_valid_time=dvt_raw / 1024.0,
        data_valid_time_raw=dvt_raw,
    )


# ---------------------------------------------------------------------------
# X1.0 Section Parser
# ---------------------------------------------------------------------------

def parse_x10_section(bits: list[int], offset: int = 0) -> X10Section:
    """Parse one 136-bit X1.0 J-Series section from a bit array."""
    b = offset

    jstn = bits_to_uint_range(bits, b, 16); b += 16
    seq_num = bits_to_uint_range(bits, b, 16); b += 16
    relay = bits[b]; b += 1
    ack_req = bits[b]; b += 1
    b += 1  # spare
    data_age_raw = bits_to_uint_range(bits, b, 13); b += 13
    b += 4  # spare nibble
    n_jwords = bits_to_uint_range(bits, b, 12); b += 12

    # 4 × 16-bit J-words (each byte-swapped)
    words = []
    for _ in range(4):
        w = convert_j_series_word(bits[b:b + 16]); b += 16
        words.append(w)

    b += 2  # spare
    word5 = bits[b:b + 6]; b += 6

    j70 = reconstruct_j_word_70(words, word5)

    return X10Section(
        jstn=jstn,
        jstn_octal=format_track_octal(jstn),
        seq_num=seq_num,
        relay=relay,
        ack_req=ack_req,
        data_age=data_age_raw / 32.0,
        data_age_raw=data_age_raw,
        n_jwords=n_jwords,
        j_words=words,
        word5=word5,
        j_word_bits_70=j70,
    )


# ---------------------------------------------------------------------------
# X7.0 Section Parser
# ---------------------------------------------------------------------------

def parse_x70_section(bits: list[int], offset: int = 0) -> X70Section:
    """Parse one 184-bit X7.0 NPG Assignment section from a bit array."""
    b = offset

    jstn = bits_to_uint_range(bits, b, 16); b += 16
    seq_num = bits_to_uint_range(bits, b, 16); b += 16
    relay = bits[b]; b += 1
    ack_req = bits[b]; b += 1
    b += 1  # spare
    data_age_raw = bits_to_uint_range(bits, b, 13); b += 13
    b += 4  # spare nibble
    n_jwords = bits_to_uint_range(bits, b, 12); b += 12

    # Extra X7.0 fields
    src_link = bits_to_uint_range(bits, b, 16); b += 16
    tx_link = bits_to_uint_range(bits, b, 16); b += 16
    npg = bits_to_uint_range(bits, b, 9); b += 9
    b += 7  # spare

    # 4 × 16-bit J-words + word5
    words = []
    for _ in range(4):
        w = convert_j_series_word(bits[b:b + 16]); b += 16
        words.append(w)
    b += 2  # spare
    word5 = bits[b:b + 6]; b += 6

    j70 = reconstruct_j_word_70(words, word5)

    return X70Section(
        jstn=jstn,
        jstn_octal=format_track_octal(jstn),
        seq_num=seq_num,
        relay=relay,
        ack_req=ack_req,
        data_age=data_age_raw / 32.0,
        data_age_raw=data_age_raw,
        n_jwords=n_jwords,
        source_link_designator=src_link,
        transmit_link_designator=tx_link,
        npg=npg,
        j_words=words,
        word5=word5,
        j_word_bits_70=j70,
    )


# ---------------------------------------------------------------------------
# Management Message Parser
# ---------------------------------------------------------------------------

def parse_management(bits: list[int]) -> ManagementHeader:
    """Parse management message subheader (MMSH.0) from payload bits."""
    b = 0
    subtype = bits_to_uint_range(bits, b, 8); b += 8
    mgmt_ver = bits_to_uint_range(bits, b, 4); b += 4
    ack_proto = bits_to_uint_range(bits, b, 4); b += 4
    msg_len = bits_to_uint_range(bits, b, 16); b += 16
    n_dest = bits_to_uint_range(bits, b, 8); b += 8
    timeout = bits_to_uint_range(bits, b, 8); b += 8
    msg_seq = bits_to_uint_range(bits, b, 16); b += 16
    cri = bits_to_uint_range(bits, b, 8); b += 8
    err_code = bits_to_uint_range(bits, b, 8); b += 8
    frag_num = bits_to_uint_range(bits, b, 8); b += 8
    total_frag = bits_to_uint_range(bits, b, 8); b += 8
    orig_seq = bits_to_uint_range(bits, b, 16); b += 16

    # Destination addresses (16 bits each)
    dests = []
    for _ in range(n_dest):
        if b + 16 <= len(bits):
            dests.append(bits_to_uint_range(bits, b, 16))
            b += 16

    return ManagementHeader(
        subtype=subtype,
        subtype_name=MANAGEMENT_SUBTYPES.get(subtype, f"Unknown({subtype})"),
        mgmt_version=mgmt_ver,
        ack_protocol=ack_proto,
        message_length=msg_len,
        n_dest_addresses=n_dest,
        completion_timeout=timeout,
        msg_seq_num=msg_seq,
        control_response=cri,
        error_code=err_code,
        fragment_num=frag_num,
        total_fragments=total_frag,
        orig_msg_seq_num=orig_seq,
        dest_addresses=dests,
    )


# ---------------------------------------------------------------------------
# Top-level decoder
# ---------------------------------------------------------------------------

def decode_jreap_message(data: bytes) -> JREAPMessage:
    """Decode a complete JREAP message from raw bytes.

    Returns a JREAPMessage with AH.0 header and type-specific payload.
    """
    try:
        ah0 = parse_ah0(data)
    except ValueError as e:
        return JREAPMessage(
            ah0=AH0Header(0, "Error", 0, "Error", 0, 0, 0, 0, "000000", 0, "Error", 0.0, 0),
            raw=data,
            error=str(e),
        )

    msg = JREAPMessage(ah0=ah0, raw=data)

    if ah0.header_type != 3:
        msg.error = f"Not JREAP-C (header_type={ah0.header_type})"
        return msg

    # Extract payload
    payload_len = min(ah0.abml - AH0_SIZE, len(data) - AH0_SIZE)
    if payload_len <= 0:
        return msg

    payload = data[AH0_SIZE:AH0_SIZE + payload_len]
    payload_bits = bytes_to_bits(payload)

    if ah0.message_type == 0:
        # Management
        msg.management = parse_management(payload_bits)

    elif ah0.message_type == 1:
        # X1.0 J-Series
        n_sections = len(payload_bits) // X10_SECTION_BITS
        for i in range(n_sections):
            msg.x10_sections.append(
                parse_x10_section(payload_bits, i * X10_SECTION_BITS)
            )

    elif ah0.message_type == 7:
        # X7.0 NPG Assignment
        n_sections = len(payload_bits) // X70_SECTION_BITS
        for i in range(n_sections):
            msg.x70_sections.append(
                parse_x70_section(payload_bits, i * X70_SECTION_BITS)
            )

    elif ah0.message_type in (2, 3):
        # Free text (coded / uncoded) — store raw payload
        section_bits = X20_SECTION_BITS if ah0.message_type == 2 else X30_SECTION_BITS
        n_sections = len(payload_bits) // section_bits
        for i in range(n_sections):
            off = i * section_bits
            jstn = bits_to_uint_range(payload_bits, off, 16)
            msg.free_text_sections.append({
                "jstn": jstn,
                "jstn_octal": format_track_octal(jstn),
                "type": "coded" if ah0.message_type == 2 else "uncoded",
            })

    return msg


# ---------------------------------------------------------------------------
# TCP stream framing
# ---------------------------------------------------------------------------

def extract_messages_from_stream(data: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete JREAP messages from a TCP byte stream.

    Returns (list of complete message byte buffers, remaining unconsumed bytes).
    """
    messages: list[bytes] = []
    offset = 0

    while offset + AH0_SIZE <= len(data):
        # Read ABML from bytes 2-3 (bits 16-31)
        abml = (data[offset + 2] << 8) | data[offset + 3]
        if abml < AH0_SIZE:
            # Invalid — skip one byte and try to resync
            offset += 1
            continue
        if offset + abml > len(data):
            # Incomplete message — return remainder as unconsumed
            break
        messages.append(data[offset:offset + abml])
        offset += abml

    return messages, data[offset:]
