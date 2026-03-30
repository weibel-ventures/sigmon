"""Tests for J-word payload decoder and message registry."""

import pytest

from geomonitor.plugins.link16.jwords import (
    DecodedJWord,
    FieldDef,
    MessageDef,
    decode_heading,
    decode_identity,
    decode_jword,
    decode_jword_header,
    decode_lat_coarse,
    decode_lon_coarse,
    decode_altitude_25ft,
    decode_quality,
    decode_signed,
    decode_uint,
    get_all_message_defs,
    get_message_def,
    load_all_tables,
)
from geomonitor.plugins.link16.jreap import bits_to_uint


class TestFieldDecoders:
    """Test individual field decoder functions."""

    def test_decode_uint(self):
        assert decode_uint([1, 0, 1, 0]) == 10
        assert decode_uint([0, 0, 0, 0]) == 0
        assert decode_uint([1, 1, 1, 1, 1, 1, 1, 1]) == 255

    def test_decode_signed_positive(self):
        assert decode_signed([0, 1, 0, 1]) == 5

    def test_decode_signed_negative(self):
        # 1111 = -1 in 4-bit two's complement
        assert decode_signed([1, 1, 1, 1]) == -1
        # 1000 = -8 in 4-bit two's complement
        assert decode_signed([1, 0, 0, 0]) == -8

    def test_decode_identity(self):
        # 3 bits: 000=Pending, 011=Friend, 110=Hostile
        assert decode_identity([0, 0, 0])["name"] == "Pending"
        assert decode_identity([0, 1, 1])["name"] == "Friend"
        assert decode_identity([1, 1, 0])["name"] == "Hostile"
        assert decode_identity([0, 0, 1])["name"] == "Unknown"

    def test_decode_quality(self):
        assert decode_quality([0, 0, 0])["name"] == "No Statement"
        assert decode_quality([1, 1, 1])["name"] == "Level 1 (Best)"

    def test_decode_heading(self):
        # 9-bit heading: 0 = 0°, 256 = 180°, 511 ≈ 359.3°
        result = decode_heading([0] * 9)
        assert result["degrees"] == 0.0
        # 256 = 0b100000000 → 256 * 360 / 512 = 180.0
        bits = [1, 0, 0, 0, 0, 0, 0, 0, 0]
        result = decode_heading(bits)
        assert result["degrees"] == 180.0

    def test_decode_lat_coarse(self):
        # 16-bit signed: 0 = 0°, max positive ≈ 90°
        # lat = val * 90 / 2^15 = val * 90 / 32768
        # val = 32767 → lat ≈ 89.997°
        bits = [0] + [1] * 15  # 0111111111111111 = 32767
        lat = decode_lat_coarse(bits)
        assert abs(lat - 89.997) < 0.01
        # Negative: 1000000000000000 = -32768 → -90°
        bits = [1] + [0] * 15
        lat = decode_lat_coarse(bits)
        assert abs(lat - (-90.0)) < 0.01

    def test_decode_lon_coarse(self):
        # 16-bit signed: val * 180 / 2^15
        # val = 16384 → lon = 16384 * 180 / 32768 = 90°
        bits = [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]  # 16384
        lon = decode_lon_coarse(bits)
        assert abs(lon - 90.0) < 0.01

    def test_decode_altitude_25ft(self):
        # 13-bit signed: val * 25 ft
        # val = 100 → 2500 ft
        val = 100
        bits = [(val >> (12 - i)) & 1 for i in range(13)]
        result = decode_altitude_25ft(bits)
        assert result["feet"] == 2500
        assert abs(result["meters"] - 762.0) < 1


class TestMessageRegistry:
    """Test that message definitions are registered correctly."""

    def test_tables_loaded(self):
        load_all_tables()
        defs = get_all_message_defs()
        assert len(defs) > 0, "No message definitions registered"

    def test_j2_2_registered(self):
        load_all_tables()
        msg = get_message_def(2, 2)
        assert msg is not None
        assert msg.j_name == "J2.2"
        assert msg.name == "Air PPLI"
        assert msg.category == "PPLI"

    def test_j3_2_registered(self):
        load_all_tables()
        msg = get_message_def(3, 2)
        assert msg is not None
        assert msg.j_name == "J3.2"
        assert msg.name == "Air Track"
        assert msg.category == "Surveillance"

    def test_j2_0_registered(self):
        load_all_tables()
        msg = get_message_def(2, 0)
        assert msg is not None
        assert msg.j_name == "J2.0"

    def test_all_ppli_types(self):
        load_all_tables()
        for sl in [0, 2, 3, 4, 5, 6]:
            msg = get_message_def(2, sl)
            assert msg is not None, f"J2.{sl} not registered"
            assert msg.category == "PPLI"

    def test_all_surveillance_types(self):
        load_all_tables()
        for sl in [0, 1, 2, 3, 4, 5, 6, 7]:
            msg = get_message_def(3, sl)
            assert msg is not None, f"J3.{sl} not registered"
            assert msg.category == "Surveillance"

    def test_unregistered_returns_none(self):
        load_all_tables()
        assert get_message_def(31, 7) is None  # unlikely to be registered


class TestJWordDecode:
    """Test full J-word decode pipeline."""

    def test_header_extraction(self):
        # Build a 70-bit block with known header:
        # WF=00 (Initial), Label=00010 (2), SubLabel=010 (2) → J2.2
        bits = [0, 0,  0, 0, 0, 1, 0,  0, 1, 0] + [0] * 60
        header = decode_jword_header(bits)
        assert header.word_format == 0
        assert header.word_format_name == "Initial"
        assert header.label == 2
        assert header.sub_label == 2
        assert header.j_name == "J2.2"

    def test_header_j3_2(self):
        # Label=00011 (3), SubLabel=010 (2) → J3.2
        bits = [0, 0,  0, 0, 0, 1, 1,  0, 1, 0] + [0] * 60
        header = decode_jword_header(bits)
        assert header.label == 3
        assert header.sub_label == 2
        assert header.j_name == "J3.2"

    def test_decode_j2_2_extracts_fields(self):
        load_all_tables()
        # Build J2.2 initial word with known field values
        bits = [0] * 70
        # Header: WF=00, Label=00010, SubLabel=010
        bits[0:2] = [0, 0]
        bits[2:7] = [0, 0, 0, 1, 0]
        bits[7:10] = [0, 1, 0]
        # Track Quality = 5 (011) at W0 bits 10-12
        bits[10:13] = [1, 0, 1]
        # Identity = 3 (Friend, 011) at W0 bits 13-15
        bits[13:16] = [0, 1, 1]

        result = decode_jword(bits)
        assert result.header.j_name == "J2.2"
        assert result.message_def is not None
        assert result.message_def.name == "Air PPLI"
        assert "Track Quality" in result.fields
        assert result.fields["Track Quality"]["raw"] == 5
        assert "Identity" in result.fields
        assert result.fields["Identity"]["name"] == "Friend"

    def test_decode_unknown_message(self):
        load_all_tables()
        # Label=11111 (31), SubLabel=111 (7) — likely unregistered
        bits = [0, 0,  1, 1, 1, 1, 1,  1, 1, 1] + [0] * 60
        result = decode_jword(bits)
        assert result.header.j_name == "J31.7"
        assert result.message_def is None
        assert len(result.fields) == 0

    def test_decode_preserves_raw_bits(self):
        bits = [1, 0] * 35  # 70 bits
        result = decode_jword(bits)
        assert result.raw_bits == bits

    def test_short_bits_padded(self):
        bits = [0, 0, 0, 0, 0, 1, 0, 0, 1, 0]  # only 10 bits
        result = decode_jword(bits)  # should not crash
        assert result.header.j_name == "J2.2"
        assert len(result.raw_bits) == 70
