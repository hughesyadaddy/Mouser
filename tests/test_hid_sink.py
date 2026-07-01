"""Tests for DFHR binary HID sink framing."""

import unittest

from core.hid_sink import (
    SINK_MAGIC,
    encode_report_frame,
    is_json_line_start,
    try_decode_report_frame,
)


class HidSinkFrameTests(unittest.TestCase):
    def test_round_trip(self):
        payload = bytes.fromhex("11ff0b0001a00000")
        frame = encode_report_frame(3, payload)
        self.assertTrue(frame.startswith(SINK_MAGIC))
        decoded = try_decode_report_frame(frame)
        self.assertIsNotNone(decoded)
        device_id, out, consumed = decoded
        self.assertEqual(device_id, 3)
        self.assertEqual(out, payload)
        self.assertEqual(consumed, len(frame))

    def test_incomplete_returns_none(self):
        frame = encode_report_frame(1, b"\x01\x02")
        self.assertIsNone(try_decode_report_frame(frame[:4]))

    def test_rejects_oversized_encode(self):
        with self.assertRaises(ValueError):
            encode_report_frame(1, b"\x00" * 5000)

    def test_malformed_magic_raises(self):
        frame = encode_report_frame(1, b"\x01\x02")
        bad = b"XXXX" + frame[4:]
        with self.assertRaises(ValueError):
            try_decode_report_frame(bad)

    def test_oversize_payload_len_raises(self):
        header = SINK_MAGIC + (1).to_bytes(2, "little") + (5000).to_bytes(4, "little")
        with self.assertRaises(ValueError):
            try_decode_report_frame(header + b"\x00" * 8)

    def test_multiple_frames_in_buffer(self):
        a = encode_report_frame(1, b"\x01")
        b = encode_report_frame(2, b"\x02\x03")
        buf = a + b
        first = try_decode_report_frame(buf)
        self.assertIsNotNone(first)
        _id, payload, consumed = first
        self.assertEqual(payload, b"\x01")
        second = try_decode_report_frame(buf[consumed:])
        self.assertIsNotNone(second)
        _id2, payload2, consumed2 = second
        self.assertEqual(payload2, b"\x02\x03")

    def test_is_json_line_start(self):
        self.assertTrue(is_json_line_start(ord("{")))
        self.assertTrue(is_json_line_start(ord("[")))
        self.assertFalse(is_json_line_start(ord("D")))


if __name__ == "__main__":
    unittest.main()
