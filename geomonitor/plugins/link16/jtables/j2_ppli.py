"""J2.x PPLI (Precise Participant Location and Identification) messages.

These messages report own-force positions. Each Link 16 participant
periodically broadcasts its position via PPLI.

Message types:
  J2.0 — Indirect Interface Unit PPLI
  J2.2 — Air PPLI (airborne platforms)
  J2.3 — Surface PPLI (ships, ground vehicles)
  J2.4 — Subsurface PPLI (submarines)
  J2.5 — Land Point PPLI
  J2.6 — Land Track PPLI

Field positions reference the 70-bit J-word block (after byte-swap).
Word 0 bits 0-9 are the header (WF, Label, Sub-label).

Reference: MIL-STD-6016E Section 5, Part 1 — J2 PPLI Word Applicability Table

NOTE: Bit positions are derived from MIL-STD-6016E. Positions marked with
(TBV) need verification against the standard's word applicability tables.
The encoding formulas are confirmed from Section 4.18.
"""

from geomonitor.plugins.link16.jwords import (
    FieldDef,
    MessageDef,
    decode_heading,
    decode_identity,
    decode_lat_coarse,
    decode_lon_coarse,
    decode_altitude_25ft,
    decode_platform_type,
    decode_quality,
    decode_track_number,
    decode_uint,
    decode_exercise_indicator,
    register_message,
)

# ---------------------------------------------------------------------------
# J2.2 Air PPLI — Initial Word
#
# The initial word carries the core position and identity data.
# Bit positions within the 70-bit block (Word 0 = bits 0-15):
#
# W0 [0:2]   Word Format = 00 (Initial)
# W0 [2:7]   Label = 00010 (2)
# W0 [7:10]  Sub-label = 010 (2)
# W0 [10:13] Track Quality (3 bits)
# W0 [13:16] spare / message-specific
#
# Position fields span W1-W3 (bits 16-63):
# W1 [16:39] Latitude coarse (23 bits, signed, 0.0013 min ≈ 90/2^22 deg)  (TBV)
# W2+W3      Longitude coarse (24 bits, signed, 180/2^23 deg)  (TBV)
#            Altitude (13 bits, 25 ft)  (TBV)
#
# The exact field boundaries within W1-W3 vary by message type.
# Below are best-effort positions that will be refined from the standard.
# ---------------------------------------------------------------------------

_j2_2_fields = [
    # Header is automatically extracted by decode_jword_header()
    FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
             decoder=decode_quality, desc="PPLI Geodetic Position Quality"),
    FieldDef("Identity", word=0, bit_start=13, bit_len=3,
             decoder=decode_identity, desc="Standard Identity"),

    # Position fields span W1-W2-W3 using absolute bit addressing.
    # 23-bit latitude: bits 16-38 (W1 full + W2 bits 0-6)
    # Resolution: 90 / 2^22 = 0.0000214° ≈ 2.4 meters
    FieldDef("Latitude Coarse", word=0, bit_start=0, bit_len=23,
             decoder=decode_lat_coarse, abs_bit=16,
             desc="Latitude (23 bits, ~2.4m resolution)"),

    # 24-bit longitude: bits 39-62 (W2 bits 7-15 + W3 full)
    # Resolution: 180 / 2^23 = 0.0000214° ≈ 2.4 meters
    FieldDef("Longitude Coarse", word=0, bit_start=0, bit_len=24,
             decoder=decode_lon_coarse, abs_bit=39,
             desc="Longitude (24 bits, ~2.4m resolution)"),

    # Latitude uses bits 16-38, Longitude uses bits 39-62.
    # Remaining: bits 63-69 (7 bits). Not enough for full 13-bit altitude.
    # Full altitude is in extension words per MIL-STD-6016E.
]

register_message(MessageDef(
    label=2, sub_label=2,
    name="Air PPLI",
    j_name="J2.2",
    category="PPLI",
    fields=_j2_2_fields,
))


# ---------------------------------------------------------------------------
# J2.0 Indirect Interface Unit PPLI
# ---------------------------------------------------------------------------

_j2_0_fields = [
    FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
             decoder=decode_quality),
    FieldDef("Identity", word=0, bit_start=13, bit_len=3,
             decoder=decode_identity),
    FieldDef("Latitude Coarse", word=0, bit_start=0, bit_len=23,
             decoder=decode_lat_coarse, abs_bit=16),
    FieldDef("Longitude Coarse", word=0, bit_start=0, bit_len=24,
             decoder=decode_lon_coarse, abs_bit=39),
]

register_message(MessageDef(
    label=2, sub_label=0,
    name="IIU PPLI",
    j_name="J2.0",
    category="PPLI",
    fields=_j2_0_fields,
))


# ---------------------------------------------------------------------------
# J2.3 Surface PPLI
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=2, sub_label=3,
    name="Surface PPLI",
    j_name="J2.3",
    category="PPLI",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
        FieldDef("Latitude Coarse", word=0, bit_start=0, bit_len=23,
                 decoder=decode_lat_coarse, abs_bit=16),
        FieldDef("Longitude Coarse", word=0, bit_start=0, bit_len=24,
                 decoder=decode_lon_coarse, abs_bit=39),
    ],
))


# ---------------------------------------------------------------------------
# J2.4 Subsurface PPLI
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=2, sub_label=4,
    name="Subsurface PPLI",
    j_name="J2.4",
    category="PPLI",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))


# ---------------------------------------------------------------------------
# J2.5 Land Point PPLI, J2.6 Land Track PPLI
# ---------------------------------------------------------------------------

for sl, name in [(5, "Land Point PPLI"), (6, "Land Track PPLI")]:
    register_message(MessageDef(
        label=2, sub_label=sl,
        name=name,
        j_name=f"J2.{sl}",
        category="PPLI",
        fields=[
            FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                     decoder=decode_quality),
            FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                     decoder=decode_identity),
        ],
    ))
