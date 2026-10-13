"""Receiver device-connection notice (HID++ 1.0 sub_id 0x41): a mouse that
re-links to its receiver after sleep must trigger a full settings re-apply."""
import unittest
from unittest.mock import Mock, patch

from core import hid_gesture


def _notice(dev_idx, flags):
    return [hid_gesture.SHORT_ID, dev_idx, 0x41, 0x04, flags, 0xB0, 0x34]


class LinkNoticeTests(unittest.TestCase):
    def setUp(self):
        self.l = hid_gesture.HidGestureListener()
        self.l._dev_idx = 0x05
        self.l._feat_idx = 0x0D
        self.l.force_reconnect = Mock()
        self.clock = [1000.0]
        p = patch.object(hid_gesture.time, "monotonic", side_effect=lambda: self.clock[0])
        p.start(); self.addCleanup(p.stop)

    def test_link_established_reapplies(self):
        self.l._on_report(_notice(0x05, 0x00))
        self.l.force_reconnect.assert_called_once()

    def test_link_lost_does_not_reapply(self):
        self.l._on_report(_notice(0x05, 0x40))
        self.l.force_reconnect.assert_not_called()

    def test_other_slot_ignored(self):
        self.l._on_report(_notice(0x02, 0x00))
        self.l.force_reconnect.assert_not_called()

    def test_direct_device_ignored(self):
        self.l._dev_idx = 0xFF
        self.l._on_report(_notice(0xFF, 0x00))
        self.l.force_reconnect.assert_not_called()

    def test_debounced(self):
        self.l._on_report(_notice(0x05, 0x00))
        self.clock[0] += 1
        self.l._on_report(_notice(0x05, 0x00))
        self.clock[0] += 10
        self.l._on_report(_notice(0x05, 0x00))
        self.assertEqual(self.l.force_reconnect.call_count, 2)

    def test_enable_notifications_writes_register(self):
        self.l._dev = Mock()
        self.l._enable_receiver_notifications()
        self.l._dev.write.assert_called_once_with([0x10, 0xFF, 0x80, 0x00, 0x00, 0x09, 0x00])

    def test_enable_notifications_skipped_direct(self):
        self.l._dev_idx = 0xFF
        self.l._dev = Mock()
        self.l._enable_receiver_notifications()
        self.l._dev.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
