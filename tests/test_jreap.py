"""Tests for JREAP-C / Link 16 protocol decoder.

Test vectors sourced from 3party/TDL message builder-interpretor:
  - JTIDS Output Samples.txt (real captured messages)
  - worksheet.txt (manually constructed test vectors with expected field values)
"""

import pytest

from geomonitor.plugins.link16.jreap import (
    AH0_SIZE,
    X10_SECTION_BITS,
    bytes_to_bits,
    bits_to_uint,
    bits_to_uint_range,
    convert_j_series_word,
    decode_jreap_message,
    extract_messages_from_stream,
    format_track_octal,
    parse_ah0,
    parse_management,
    parse_x10_section,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def csv_to_bytes(csv: str) -> bytes:
    """Convert comma-separated decimal string to bytes, as used in test vectors."""
    return bytes(int(x.strip()) for x in csv.split(",") if x.strip())


# ---------------------------------------------------------------------------
# Test vectors from JTIDS Output Samples.txt and worksheet.txt
# ---------------------------------------------------------------------------

# First sample from JTIDS Output Samples.txt — J-Series message (type 1)
SAMPLE_JSERIES_1 = csv_to_bytes(
    "49,1,0,45,0,1,84,102,228,103,0,41,31,103,0,0,0,3,8,8,74,1,112,255,"
    "31,128,32,202,133,181,110,105,14,252,255,63,5,0,0,0,0,1,67,0,0"
)

# Management X0.0.0 Echo from worksheet.txt
SAMPLE_MGMT_ECHO = csv_to_bytes(
    "48,1,0,30,18,52,16,0,0,0,0,16,0,20,1,5,18,52,0,0,0,0,0,0,1,35,222,173,190,239"
)

# Management X0.1.0 Common Time Reference from worksheet.txt
SAMPLE_MGMT_CTR = csv_to_bytes(
    "48,1,0,30,18,52,16,0,0,0,1,33,0,20,1,10,69,103,1,0,0,0,0,0,1,35,0,27,0,13"
)

# Management X0.2.0 Round-Trip Time Delay from worksheet.txt
SAMPLE_MGMT_RTT = csv_to_bytes(
    "48,1,0,38,18,52,16,0,0,0,2,17,0,28,1,5,18,52,0,0,0,0,0,0,0,119,"
    "16,56,64,0,32,56,68,0,48,56,72,0"
)

# Management X0.4.0 Acknowledgement from worksheet.txt
SAMPLE_MGMT_ACK = csv_to_bytes(
    "48,1,0,30,18,52,16,0,0,0,4,33,0,20,1,8,52,86,2,1,0,2,34,34,1,35,4,86,120,154"
)

# Management X0.7.0 Operator-to-Operator from worksheet.txt (single ASCII 'A')
SAMPLE_MGMT_OTO = csv_to_bytes(
    "48,17,0,27,18,52,0,0,0,0,7,34,0,17,1,10,52,86,2,0,0,0,0,0,1,35,65"
)

# Management X0.9.0 Terminate Link from worksheet.txt
SAMPLE_MGMT_TERM = csv_to_bytes(
    "48,17,0,26,18,52,16,0,0,0,9,34,0,16,1,4,52,96,1,0,0,0,0,0,1,35"
)

# Management subtype 0 from worksheet: another echo
SAMPLE_MGMT_ECHO2 = csv_to_bytes(
    "48,1,0,30,18,52,16,0,0,0,0,16,0,20,1,5,18,52,0,0,0,0,0,0,1,35,222,173,190,239"
)

# Second sample from JTIDS Output Samples — Management message (type 0)
SAMPLE_MGMT_RAW = csv_to_bytes(
    "48,1,0,38,0,1,4,102,231,246,2,0,0,28,1,30,2,90,0,0,0,0,0,0,"
    "140,143,84,102,231,246,0,0,0,0,0,0,0,0"
)


# ---------------------------------------------------------------------------
# Bit manipulation tests
# ---------------------------------------------------------------------------

class TestBitHelpers:
    def test_bytes_to_bits_length(self):
        assert len(bytes_to_bits(b"\x00")) == 8
        assert len(bytes_to_bits(b"\x00\xFF")) == 16

    def test_bytes_to_bits_values(self):
        bits = bytes_to_bits(b"\xA5")  # 10100101
        assert bits == [1, 0, 1, 0, 0, 1, 0, 1]

    def test_bits_to_uint(self):
        assert bits_to_uint([1, 0, 1, 0]) == 10
        assert bits_to_uint([0, 0, 0, 0]) == 0
        assert bits_to_uint([1, 1, 1, 1]) == 15
        assert bits_to_uint([1]) == 1
        assert bits_to_uint([]) == 0

    def test_roundtrip(self):
        """bytes → bits → uint should reconstruct original values."""
        data = b"\x31\x01"
        bits = bytes_to_bits(data)
        assert bits_to_uint(bits[:8]) == 0x31
        assert bits_to_uint(bits[8:16]) == 0x01


class TestJSeriesWordSwap:
    def test_identity_for_zeros(self):
        assert convert_j_series_word([0] * 16) == [0] * 16

    def test_identity_for_ones(self):
        assert convert_j_series_word([1] * 16) == [1] * 16

    def test_byte_reversal(self):
        """First byte 10000000 should become 00000001 after swap."""
        inp = [1, 0, 0, 0, 0, 0, 0, 0,  0, 0, 0, 0, 0, 0, 0, 0]
        out = convert_j_series_word(inp)
        assert out[:8] == [0, 0, 0, 0, 0, 0, 0, 1]
        assert out[8:] == [0, 0, 0, 0, 0, 0, 0, 0]

    def test_double_swap_is_identity(self):
        """Applying the swap twice should return the original."""
        inp = [1, 0, 1, 1, 0, 0, 1, 0,  1, 1, 0, 0, 0, 1, 1, 0]
        assert convert_j_series_word(convert_j_series_word(inp)) == inp


class TestTrackOctal:
    def test_zero(self):
        assert format_track_octal(0) == "000000"

    def test_one(self):
        assert format_track_octal(1) == "000001"

    def test_large(self):
        assert format_track_octal(0o177777) == "177777"


# ---------------------------------------------------------------------------
# AH.0 Header tests
# ---------------------------------------------------------------------------

class TestAH0:
    def test_jseries_sample(self):
        """First JTIDS Output Sample: J-Series message."""
        ah0 = parse_ah0(SAMPLE_JSERIES_1)
        assert ah0.header_type == 3  # JREAP-C
        assert ah0.message_type == 1  # J-Series
        assert ah0.app_proto_version == 1
        assert ah0.abml == 45
        assert ah0.sender_id == 1
        assert ah0.sender_id_octal == "000001"

    def test_mgmt_echo(self):
        """Management Echo (X0.0.0) from worksheet."""
        ah0 = parse_ah0(SAMPLE_MGMT_ECHO)
        assert ah0.header_type == 3  # JREAP-C
        assert ah0.message_type == 0  # Management
        assert ah0.abml == 30
        assert ah0.sender_id == 0x1234
        assert ah0.sender_id_octal == format_track_octal(0x1234)

    def test_mgmt_rtt(self):
        """Management RTT (X0.2.0) from worksheet."""
        ah0 = parse_ah0(SAMPLE_MGMT_RTT)
        assert ah0.message_type == 0
        assert ah0.abml == 38
        assert ah0.sender_id == 0x1234

    def test_time_accuracy_sample1(self):
        """First sample: time accuracy field."""
        ah0 = parse_ah0(SAMPLE_JSERIES_1)
        assert ah0.time_accuracy == 5  # > 8ms, ≤ 16ms
        assert "16 ms" in ah0.time_accuracy_name

    def test_too_short(self):
        with pytest.raises(ValueError):
            parse_ah0(b"\x00" * 5)


# ---------------------------------------------------------------------------
# X1.0 Section tests
# ---------------------------------------------------------------------------

class TestX10Section:
    def test_first_sample_sections(self):
        """First JTIDS sample has 45 bytes: 10 AH + 35 payload = 2 × 136-bit sections + remainder."""
        msg = decode_jreap_message(SAMPLE_JSERIES_1)
        assert msg.error is None
        assert msg.ah0.message_type == 1
        assert len(msg.x10_sections) == 2

    def test_first_section_fields(self):
        """Verify extracted fields from first section of first sample."""
        msg = decode_jreap_message(SAMPLE_JSERIES_1)
        s = msg.x10_sections[0]
        # JSTN should be a valid 16-bit value
        assert 0 <= s.jstn <= 0xFFFF
        assert len(s.jstn_octal) == 6
        # Data age is 13-bit / 32.0
        assert s.data_age >= 0
        # J-words: 4 × 16-bit + 6-bit word5
        assert len(s.j_words) == 4
        for w in s.j_words:
            assert len(w) == 16
        assert len(s.word5) == 6
        # Reconstructed 70-bit block
        assert len(s.j_word_bits_70) == 70

    def test_section_count_from_payload_size(self):
        """Payload = 35 bytes = 280 bits. 280 // 136 = 2 sections."""
        msg = decode_jreap_message(SAMPLE_JSERIES_1)
        assert len(msg.x10_sections) == 2


# ---------------------------------------------------------------------------
# Management message tests
# ---------------------------------------------------------------------------

class TestManagement:
    def test_echo_subtype(self):
        """X0.0.0 Echo: subtype=0, 1 destination address."""
        msg = decode_jreap_message(SAMPLE_MGMT_ECHO)
        assert msg.management is not None
        assert msg.management.subtype == 0
        assert msg.management.subtype_name == "Echo (X0.0.0)"
        assert msg.management.n_dest_addresses == 1
        assert len(msg.management.dest_addresses) == 1

    def test_echo_sender(self):
        """X0.0.0 Echo: sender = 0x1234."""
        msg = decode_jreap_message(SAMPLE_MGMT_ECHO)
        assert msg.ah0.sender_id == 0x1234

    def test_ctr_subtype(self):
        """X0.1.0 Common Time Reference: subtype=1."""
        msg = decode_jreap_message(SAMPLE_MGMT_CTR)
        assert msg.management is not None
        assert msg.management.subtype == 1
        assert "Common Time Reference" in msg.management.subtype_name

    def test_rtt_subtype(self):
        """X0.2.0 Round-Trip Time Delay."""
        msg = decode_jreap_message(SAMPLE_MGMT_RTT)
        assert msg.management is not None
        assert msg.management.subtype == 2
        assert "Round-Trip" in msg.management.subtype_name

    def test_ack_subtype(self):
        """X0.4.0 Acknowledgement."""
        msg = decode_jreap_message(SAMPLE_MGMT_ACK)
        assert msg.management is not None
        assert msg.management.subtype == 4

    def test_oto_subtype(self):
        """X0.7.0 Operator-to-Operator with ASCII 'A'."""
        msg = decode_jreap_message(SAMPLE_MGMT_OTO)
        assert msg.management is not None
        assert msg.management.subtype == 7
        assert msg.management.n_dest_addresses == 1

    def test_terminate_subtype(self):
        """X0.9.0 Terminate Link."""
        msg = decode_jreap_message(SAMPLE_MGMT_TERM)
        assert msg.management is not None
        assert msg.management.subtype == 9

    def test_management_dest_addresses(self):
        """Echo has 1 dest address = 0x0123."""
        msg = decode_jreap_message(SAMPLE_MGMT_ECHO)
        assert msg.management.dest_addresses == [0x0123]

    def test_management_cri_and_error(self):
        """X0.4.0: CRI=2 (INFORMATION per worksheet)."""
        msg = decode_jreap_message(SAMPLE_MGMT_ACK)
        m = msg.management
        # worksheet says: Control/Response Indicator, 3 = 2 (INFORMATION)
        # Error Code = 1 from worksheet vector
        assert m.control_response == 2
        assert m.error_code == 1


# ---------------------------------------------------------------------------
# Full decode pipeline tests
# ---------------------------------------------------------------------------

class TestFullDecode:
    def test_non_jreap_c_header(self):
        """Header type != 3 should set error."""
        data = bytearray(SAMPLE_JSERIES_1)
        data[0] = 0x11  # header_type = 1 (JREAP-A)
        msg = decode_jreap_message(bytes(data))
        assert msg.error is not None
        assert "Not JREAP-C" in msg.error

    def test_all_jtids_samples_decode(self):
        """All 16 samples from JTIDS Output Samples.txt should decode without error."""
        samples = _load_jtids_samples()
        for i, raw in enumerate(samples):
            msg = decode_jreap_message(raw)
            assert msg.error is None, f"Sample {i+1} failed: {msg.error}"
            assert msg.ah0.header_type == 3

    def test_all_worksheet_samples_decode(self):
        """All worksheet test vectors should decode without error."""
        vectors = [
            SAMPLE_MGMT_ECHO, SAMPLE_MGMT_CTR, SAMPLE_MGMT_RTT,
            SAMPLE_MGMT_ACK, SAMPLE_MGMT_OTO, SAMPLE_MGMT_TERM,
            SAMPLE_MGMT_RAW, SAMPLE_MGMT_ECHO2,
        ]
        for i, raw in enumerate(vectors):
            msg = decode_jreap_message(raw)
            assert msg.error is None, f"Worksheet vector {i+1} failed: {msg.error}"


# ---------------------------------------------------------------------------
# TCP stream framing tests
# ---------------------------------------------------------------------------

class TestStreamFraming:
    def test_single_message(self):
        msgs, rem = extract_messages_from_stream(SAMPLE_JSERIES_1)
        assert len(msgs) == 1
        assert msgs[0] == SAMPLE_JSERIES_1
        assert rem == b""

    def test_two_concatenated_messages(self):
        stream = SAMPLE_JSERIES_1 + SAMPLE_MGMT_ECHO
        msgs, rem = extract_messages_from_stream(stream)
        assert len(msgs) == 2
        assert msgs[0] == SAMPLE_JSERIES_1
        assert msgs[1] == SAMPLE_MGMT_ECHO
        assert rem == b""

    def test_incomplete_message(self):
        """Truncated message should be returned as remainder."""
        partial = SAMPLE_JSERIES_1[:20]
        msgs, rem = extract_messages_from_stream(partial)
        assert len(msgs) == 0
        assert rem == partial

    def test_complete_plus_incomplete(self):
        stream = SAMPLE_JSERIES_1 + SAMPLE_MGMT_ECHO[:5]
        msgs, rem = extract_messages_from_stream(stream)
        assert len(msgs) == 1
        assert msgs[0] == SAMPLE_JSERIES_1
        assert len(rem) == 5

    def test_empty_input(self):
        msgs, rem = extract_messages_from_stream(b"")
        assert len(msgs) == 0
        assert rem == b""


# ---------------------------------------------------------------------------
# Cross-validation: verify bit expansion matches sample file
# ---------------------------------------------------------------------------

class TestBitExpansionCrossValidation:
    def test_all_samples_bits_match(self):
        """For every sample in JTIDS Output Samples.txt, our bytes_to_bits()
        output must exactly match the binary expansion string in the file."""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..",
            "3party", "TDL message builder-interpretor", "JTIDS Output Samples.txt"
        )
        if not os.path.exists(path):
            pytest.skip("JTIDS Output Samples.txt not found")

        with open(path) as f:
            content = f.read()

        blocks = content.split("~~~Begin~~~~")
        checked = 0
        for block in blocks:
            if "~~~End~~~" not in block:
                continue
            lines = [l.strip() for l in block.split("\n") if l.strip() and not l.strip().startswith("~~~")]
            if len(lines) < 2:
                continue
            csv_line = lines[0]
            bits_line = lines[1]
            # Validate this is a csv/bits pair
            if "," not in csv_line or not all(c in "01" for c in bits_line):
                continue
            raw = csv_to_bytes(csv_line)
            expected = [int(c) for c in bits_line]
            actual = bytes_to_bits(raw)
            assert actual == expected, f"Bit mismatch for sample: {csv_line[:40]}..."
            checked += 1

        assert checked >= 10, f"Only validated {checked} samples, expected >= 10"


# ---------------------------------------------------------------------------
# Helpers to load samples from the 3party test file
# ---------------------------------------------------------------------------

def _load_jtids_samples() -> list[bytes]:
    """Load all samples from JTIDS Output Samples.txt."""
    import os
    path = os.path.join(
        os.path.dirname(__file__), "..",
        "3party", "TDL message builder-interpretor", "JTIDS Output Samples.txt"
    )
    if not os.path.exists(path):
        pytest.skip("JTIDS Output Samples.txt not found")

    samples = []
    with open(path) as f:
        in_block = False
        for line in f:
            line = line.strip()
            if line.startswith("~~~Begin"):
                in_block = True
                continue
            if line.startswith("~~~End"):
                in_block = False
                continue
            if in_block and "," in line and not line.startswith("0") or (
                in_block and "," in line and not all(c in "01" for c in line.replace(",", ""))
            ):
                # This is the CSV byte line (not the binary expansion)
                try:
                    raw = csv_to_bytes(line)
                    if len(raw) >= 10:
                        samples.append(raw)
                except (ValueError, IndexError):
                    pass
    return samples
