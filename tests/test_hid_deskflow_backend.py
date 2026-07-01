"""Tests for the Deskflow sink queue adapter."""

import unittest

from core.hid_deskflow_backend import DeskflowSinkDevice, flush_deskflow_sink, get_deskflow_sink


class DeskflowSinkDeviceTests(unittest.TestCase):
    def test_feed_and_read(self):
        sink = DeskflowSinkDevice(read_timeout_ms=50)
        payload = b"\x11\xff\x0b"
        sink.feed_report(payload)
        out = sink.read(64)
        self.assertEqual(out, payload)

    def test_flush_drops_queued_reports(self):
        sink = DeskflowSinkDevice(read_timeout_ms=50)
        sink.feed_report(b"\x01")
        sink.flush()
        self.assertIsNone(sink.read(64, timeout_ms=10))

    def test_global_flush(self):
        sink = get_deskflow_sink()
        sink.feed_report(b"\x02")
        flush_deskflow_sink()
        self.assertIsNone(sink.read(64, timeout_ms=10))


if __name__ == "__main__":
    unittest.main()
