"""Link 16 J-word payload decoder.

Decodes the 70-bit J-word block extracted from JREAP X1.0 sections into
structured message data based on Label/Sub-label dispatch.

Architecture:
  - This module provides the core decode engine and common field decoders.
  - Message-specific field definitions live in jtables/*.py as declarative
    table entries registered via register_message().
  - Adding a new J-Series message type = adding a dict entry, not new parsing logic.

Reference: MIL-STD-6016E, SISO-STD-002-2021
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from geomonitor.plugins.link16.jreap import bits_to_uint


# ---------------------------------------------------------------------------
# Common field decoders — conversion from raw bits to engineering units
# ---------------------------------------------------------------------------

def decode_uint(bits: list[int]) -> int:
    """Unsigned integer from MSB-first bits."""
    return bits_to_uint(bits)


def decode_signed(bits: list[int]) -> int:
    """Signed integer (two's complement) from MSB-first bits."""
    val = bits_to_uint(bits)
    if bits[0]:  # negative
        val -= (1 << len(bits))
    return val


def decode_track_number(bits: list[int]) -> dict[str, Any]:
    """Decode a track number field (various widths).

    Track numbers in Link 16 are displayed as octal.
    """
    val = bits_to_uint(bits)
    return {"raw": val, "octal": oct(val)[2:].zfill(5), "display": oct(val)[2:].zfill(5)}


def decode_identity(bits: list[int]) -> dict[str, Any]:
    """Decode identity/affiliation field (2-5 bits depending on message).

    Standard Identity values (MIL-STD-6016E Table 4.4-2):
    """
    val = bits_to_uint(bits)
    IDENTITIES = {
        0: "Pending",
        1: "Unknown",
        2: "Assumed Friend",
        3: "Friend",
        4: "Neutral",
        5: "Suspect",
        6: "Hostile",
        7: "Joker",       # Exercise only
        8: "Faker",       # Exercise only
    }
    return {"raw": val, "name": IDENTITIES.get(val, f"Reserved({val})")}


def decode_lat_coarse(bits: list[int]) -> float:
    """Decode coarse latitude field.

    Standard encoding (MIL-STD-6016E Section 4.18):
    Signed, N bits, range -90 to +90 degrees.
    Resolution: 90 / 2^(N-1) degrees per LSB.
    """
    n = len(bits)
    val = decode_signed(bits)
    return val * 90.0 / (1 << (n - 1))


def decode_lon_coarse(bits: list[int]) -> float:
    """Decode coarse longitude field.

    Signed, N bits, range -180 to +180 degrees.
    Resolution: 180 / 2^(N-1) degrees per LSB.
    """
    n = len(bits)
    val = decode_signed(bits)
    return val * 180.0 / (1 << (n - 1))


def decode_altitude_25ft(bits: list[int]) -> dict[str, Any]:
    """Decode altitude in 25-foot increments.

    13 bits signed → range -204,800 to +204,775 ft.
    """
    val = decode_signed(bits)
    ft = val * 25
    return {"raw": val, "feet": ft, "meters": round(ft * 0.3048, 1)}


def decode_altitude_fine(coarse_bits: list[int], fine_bits: list[int]) -> dict[str, Any]:
    """Decode combined coarse+fine altitude (1.5625 ft resolution)."""
    coarse = decode_signed(coarse_bits)
    fine = bits_to_uint(fine_bits)
    combined = (coarse << len(fine_bits)) | fine
    ft = combined * 1.5625
    return {"raw": combined, "feet": round(ft, 1), "meters": round(ft * 0.3048, 1)}


def decode_speed(bits: list[int]) -> dict[str, Any]:
    """Decode speed field.

    Multiple encodings exist; this handles the common unsigned format.
    Resolution depends on message type.
    """
    val = bits_to_uint(bits)
    return {"raw": val}


def decode_heading(bits: list[int]) -> dict[str, Any]:
    """Decode heading/course field.

    Unsigned, N bits, range 0-360 degrees.
    Resolution: 360 / 2^N degrees per LSB.
    """
    n = len(bits)
    val = bits_to_uint(bits)
    deg = val * 360.0 / (1 << n)
    return {"raw": val, "degrees": round(deg, 2)}


def decode_quality(bits: list[int]) -> dict[str, Any]:
    """Decode track/position quality field (3 bits typically)."""
    val = bits_to_uint(bits)
    QUALITY = {
        0: "No Statement",
        1: "Degraded",
        2: "Level 6",
        3: "Level 5",
        4: "Level 4",
        5: "Level 3",
        6: "Level 2",
        7: "Level 1 (Best)",
    }
    return {"raw": val, "name": QUALITY.get(val, f"Q{val}")}


def decode_platform_type(bits: list[int]) -> dict[str, Any]:
    """Decode platform type field (5 bits)."""
    val = bits_to_uint(bits)
    PLATFORMS = {
        0: "No Statement",
        1: "Fighter/Bomber/Attack",
        2: "Reconnaissance/Patrol",
        3: "Cargo/Tanker",
        4: "EW/C2/AEW",
        5: "ASW",
        6: "SOF",
        7: "Trainer/Utility",
        8: "Helicopter (Attack)",
        9: "Helicopter (Utility)",
        10: "Helicopter (ASW)",
        11: "Unmanned Aerial Vehicle",
        12: "Space Vehicle",
        13: "Surface Combatant",
        14: "Submarine",
        15: "Merchant/Auxiliary",
        16: "Air Defense Unit",
        17: "Ground Unit",
        18: "Missile",
        19: "Surface Decoy",
        20: "Air Decoy",
    }
    return {"raw": val, "name": PLATFORMS.get(val, f"Type({val})")}


def decode_exercise_indicator(bits: list[int]) -> dict[str, Any]:
    """Decode exercise indicator (1 bit)."""
    val = bits[0]
    return {"raw": val, "name": "Exercise" if val else "Real"}


def decode_enum(table: dict[int, str]) -> Callable[[list[int]], dict[str, Any]]:
    """Create a decoder for an enumerated field using a lookup table."""
    def _decode(bits: list[int]) -> dict[str, Any]:
        val = bits_to_uint(bits)
        return {"raw": val, "name": table.get(val, f"Unknown({val})")}
    return _decode


# ---------------------------------------------------------------------------
# Field definition — declarative description of one field within a word
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    """Definition of a single field within a J-word message.

    Position can be specified two ways:
    1. Word-relative: word + bit_start (for fields within a single word)
    2. Absolute: abs_bit (for fields spanning word boundaries)

    If abs_bit is set, word and bit_start are ignored.

    Attributes:
        name:     Human-readable field name
        word:     Which word (0-4) — ignored if abs_bit is set
        bit_start: Starting bit within the word — ignored if abs_bit is set
        bit_len:  Number of bits
        decoder:  Function(bits) -> value. Default: decode_uint
        desc:     Optional description
        abs_bit:  Absolute bit position in the 70-bit block (None = use word+bit_start)
    """
    name: str
    word: int       # 0-4, ignored if abs_bit set
    bit_start: int  # within word, ignored if abs_bit set
    bit_len: int
    decoder: Callable[[list[int]], Any] = decode_uint
    desc: str = ""
    abs_bit: int | None = None


@dataclass
class MessageDef:
    """Definition of a J-Series message type.

    Attributes:
        label:      J-Series label number (0-31)
        sub_label:  Sub-label number (0-7)
        name:       Human-readable name (e.g. "J2.2 Air PPLI")
        j_name:     Standard J-designation (e.g. "J2.2")
        category:   Functional category ("PPLI", "Surveillance", "C2", etc.)
        fields:     List of FieldDef for the initial word fields
        ext_fields: List of FieldDef for extension word fields (future)
        cont_fields: List of FieldDef for continuation word fields (future)
    """
    label: int
    sub_label: int
    name: str
    j_name: str
    category: str
    fields: list[FieldDef] = field(default_factory=list)
    ext_fields: list[FieldDef] = field(default_factory=list)
    cont_fields: list[FieldDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Message registry
# ---------------------------------------------------------------------------

_message_registry: dict[tuple[int, int], MessageDef] = {}


def register_message(msg_def: MessageDef) -> None:
    """Register a J-Series message definition."""
    key = (msg_def.label, msg_def.sub_label)
    _message_registry[key] = msg_def


def get_message_def(label: int, sub_label: int) -> MessageDef | None:
    """Look up a registered message definition."""
    return _message_registry.get((label, sub_label))


def get_all_message_defs() -> dict[tuple[int, int], MessageDef]:
    """Return all registered message definitions."""
    return dict(_message_registry)


# ---------------------------------------------------------------------------
# J-word initial word header — common to all messages
# ---------------------------------------------------------------------------

# Label categories (MIL-STD-6016E Table 5.4-1)
LABEL_NAMES = {
    0: "Network Management (J0)",
    1: "Network Management (J1)",
    2: "PPLI (J2)",
    3: "Surveillance (J3)",
    5: "ASW (J5)",
    6: "Intelligence (J6)",
    7: "Info Management (J7)",
    8: "Info Management (J8)",
    9: "Weapons Coord (J9)",
    10: "Weapons Coord (J10)",
    12: "Control (J12)",
    13: "Platform Status (J13)",
    14: "Control (J14)",
    15: "Threat Warning (J15)",
    16: "Mission Support (J16)",
    17: "Miscellaneous (J17)",
    27: "National Use (J27)",
    28: "National Use (J28)",
    29: "National Use (J29)",
    30: "National Use (J30)",
    31: "National Use (J31)",
}


@dataclass
class JWordHeader:
    """Common header from the initial word (first 10 bits)."""
    word_format: int      # 2 bits: 0=Initial, 1=Extension, 2=Continuation
    word_format_name: str
    label: int            # 5 bits
    label_name: str
    sub_label: int        # 3 bits
    j_name: str           # e.g. "J2.2", "J3.0"


@dataclass
class DecodedJWord:
    """Result of decoding a 70-bit J-word block."""
    header: JWordHeader
    message_def: MessageDef | None  # None if unregistered message type
    fields: dict[str, Any]          # Decoded field values
    raw_bits: list[int]             # The 70-bit block


WORD_FORMATS = {0: "Initial", 1: "Extension", 2: "Continuation", 3: "Reserved"}


def decode_jword_header(bits_70: list[int]) -> JWordHeader:
    """Extract the common header from a 70-bit J-word block.

    Bits 0-1: Word Format
    Bits 2-6: Label
    Bits 7-9: Sub-label
    """
    wf = bits_to_uint(bits_70[0:2])
    label = bits_to_uint(bits_70[2:7])
    sub_label = bits_to_uint(bits_70[7:10])

    label_name = LABEL_NAMES.get(label, f"Label {label}")
    j_name = f"J{label}.{sub_label}"

    return JWordHeader(
        word_format=wf,
        word_format_name=WORD_FORMATS.get(wf, f"Unknown({wf})"),
        label=label,
        label_name=label_name,
        sub_label=sub_label,
        j_name=j_name,
    )


def decode_jword(bits_70: list[int]) -> DecodedJWord:
    """Decode a 70-bit J-word block into structured data.

    1. Extract the header (word format, label, sub-label)
    2. Look up the message definition in the registry
    3. Extract each registered field using its decoder
    """
    if len(bits_70) < 70:
        bits_70 = bits_70 + [0] * (70 - len(bits_70))

    header = decode_jword_header(bits_70)
    msg_def = get_message_def(header.label, header.sub_label)

    fields: dict[str, Any] = {}

    if msg_def:
        for fdef in msg_def.fields:
            # Calculate absolute bit position in the 70-bit block
            if fdef.abs_bit is not None:
                abs_start = fdef.abs_bit
            else:
                abs_start = fdef.word * 16 + fdef.bit_start
                if fdef.word == 4:
                    abs_start = 64 + fdef.bit_start

            abs_end = abs_start + fdef.bit_len
            if abs_end > 70:
                continue  # field extends beyond available bits

            field_bits = bits_70[abs_start:abs_end]
            try:
                value = fdef.decoder(field_bits)
            except Exception:
                value = {"raw": bits_to_uint(field_bits), "error": "decode failed"}

            fields[fdef.name] = value

    return DecodedJWord(
        header=header,
        message_def=msg_def,
        fields=fields,
        raw_bits=bits_70,
    )


# ---------------------------------------------------------------------------
# Load all message table modules (triggers registration)
# ---------------------------------------------------------------------------

def load_all_tables() -> None:
    """Import all jtables modules to register their message definitions."""
    try:
        from geomonitor.plugins.link16.jtables import register_all
        register_all()
    except ImportError:
        pass  # No tables available yet


# Auto-load on import
load_all_tables()
