"""J3.x Surveillance messages — air, surface, subsurface, and space tracks.

These messages report tracks detected by sensors (radar, EW, etc.).
Unlike PPLI (J2.x) which reports own position, J3.x reports OTHER entities.

Message types:
  J3.0 — Reference Point
  J3.1 — Emergency Point
  J3.2 — Air Track
  J3.3 — Surface Track
  J3.4 — Subsurface Track
  J3.5 — Land Point/Track
  J3.6 — Space Track
  J3.7 — Electronic Warfare Product Information

Reference: MIL-STD-6016E Section 5, Part 1 — J3 Surveillance Word Applicability Table

NOTE: Bit positions marked (TBV) need verification from the standard.
"""

from geomonitor.plugins.link16.jwords import (
    FieldDef,
    MessageDef,
    decode_heading,
    decode_identity,
    decode_lat_coarse,
    decode_lon_coarse,
    decode_altitude_25ft,
    decode_quality,
    decode_speed,
    decode_uint,
    register_message,
)

# ---------------------------------------------------------------------------
# J3.2 Air Track — THE key surveillance message
#
# Reports an air track detected by sensors. Contains:
# - Track number, identity, track quality
# - Position (lat/lon coarse)
# - Altitude
# - Speed and heading
# - Platform type, activity
#
# The initial word carries identity and position.
# Extension words carry speed, heading, IFF, amplification.
# ---------------------------------------------------------------------------

_j3_2_fields = [
    FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
             decoder=decode_quality, desc="Surveillance Track Quality"),
    FieldDef("Identity", word=0, bit_start=13, bit_len=3,
             decoder=decode_identity, desc="Standard Identity"),
    FieldDef("Latitude Coarse", word=0, bit_start=0, bit_len=23,
             decoder=decode_lat_coarse, abs_bit=16,
             desc="Latitude (23 bits, ~2.4m resolution)"),
    FieldDef("Longitude Coarse", word=0, bit_start=0, bit_len=24,
             decoder=decode_lon_coarse, abs_bit=39,
             desc="Longitude (24 bits, ~2.4m resolution)"),
]

register_message(MessageDef(
    label=3, sub_label=2,
    name="Air Track",
    j_name="J3.2",
    category="Surveillance",
    fields=_j3_2_fields,
))


# ---------------------------------------------------------------------------
# J3.0 Reference Point
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=0,
    name="Reference Point",
    j_name="J3.0",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Latitude Coarse", word=0, bit_start=0, bit_len=23,
                 decoder=decode_lat_coarse, abs_bit=16),
        FieldDef("Longitude Coarse", word=0, bit_start=0, bit_len=24,
                 decoder=decode_lon_coarse, abs_bit=39),
    ],
))


# ---------------------------------------------------------------------------
# J3.1 Emergency Point
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=1,
    name="Emergency Point",
    j_name="J3.1",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))


# ---------------------------------------------------------------------------
# J3.3 Surface Track
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=3,
    name="Surface Track",
    j_name="J3.3",
    category="Surveillance",
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
# J3.4 Subsurface Track
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=4,
    name="Subsurface Track",
    j_name="J3.4",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))


# ---------------------------------------------------------------------------
# J3.5 Land Point/Track
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=5,
    name="Land Point/Track",
    j_name="J3.5",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))


# ---------------------------------------------------------------------------
# J3.6 Space Track
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=6,
    name="Space Track",
    j_name="J3.6",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))


# ---------------------------------------------------------------------------
# J3.7 Electronic Warfare Product Information
# ---------------------------------------------------------------------------

register_message(MessageDef(
    label=3, sub_label=7,
    name="EW Product Info",
    j_name="J3.7",
    category="Surveillance",
    fields=[
        FieldDef("Track Quality", word=0, bit_start=10, bit_len=3,
                 decoder=decode_quality),
        FieldDef("Identity", word=0, bit_start=13, bit_len=3,
                 decoder=decode_identity),
    ],
))
