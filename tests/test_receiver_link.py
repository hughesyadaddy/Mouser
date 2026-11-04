"""Receiver layer: re-link re-apply and companion wheel-invert sweep."""
import unittest
from unittest.mock import Mock, patch

from core import hid_gesture, receiver_link


def _notice(dev_idx, flags, with_report_id=True):
    body = [dev_idx, 0x41, 0x04, flags, 0xB0, 0x34]
    return ([hid_gesture.SHORT_ID] + body) if with_report_id else body


class LinkNoticeTests(unittest.TestCase):
    def setUp(self):
        self.l = hid_gesture.HidGestureListener()
        self.l._dev_idx = 0x05
        self.l._feat_idx = 0x0D
        self.l.force_reconnect = Mock()
        self.clock = [1000.0]
        p = patch.object(receiver_link.time, "monotonic", side_effect=lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def test_link_established_reapplies(self):
        self.l._on_report(_notice(0x05, 0x00))
        self.l.force_reconnect.assert_called_once()

    def test_windows_layout_without_report_id(self):
        self.l._on_report(_notice(0x05, 0x00, with_report_id=False))
        self.l.force_reconnect.assert_called_once()

    def test_link_lost_does_not_reapply(self):
        self.l._on_report(_notice(0x05, 0x40))
        self.l.force_reconnect.assert_not_called()

    def test_direct_device_ignored(self):
        self.l._dev_idx = 0xFF
        self.l._on_report(_notice(0xFF, 0x00))
        self.l.force_reconnect.assert_not_called()

    def test_long_report_ignored(self):
        raw = [hid_gesture.LONG_ID, 0x05, 0x41, 0x04, 0x00] + [0] * 15
        self.assertFalse(receiver_link.handle_report(self.l, raw, hid_gesture._parse(raw)))

    def test_debounced(self):
        self.l._on_report(_notice(0x05, 0x00))
        self.clock[0] += 1
        self.l._on_report(_notice(0x05, 0x00))
        self.clock[0] += 10
        self.l._on_report(_notice(0x05, 0x00))
        self.assertEqual(self.l.force_reconnect.call_count, 2)

    def test_companion_link_schedules_sweep(self):
        self.l._pending_companion_sweep = False
        self.l._on_report(_notice(0x04, 0x00))
        self.assertTrue(self.l._pending_companion_sweep)
        self.l.force_reconnect.assert_not_called()

    def test_companion_link_lost_no_sweep(self):
        self.l._pending_companion_sweep = False
        self.l._on_report(_notice(0x04, 0x40))
        self.assertFalse(self.l._pending_companion_sweep)

    def test_enable_notifications(self):
        self.l._dev = Mock()
        receiver_link.enable_notifications(self.l)
        self.l._dev.write.assert_called_once_with([0x10, 0xFF, 0x80, 0x00, 0x00, 0x09, 0x00])
        self.l._dev_idx = 0xFF
        self.l._dev = Mock()
        receiver_link.enable_notifications(self.l)
        self.l._dev.write.assert_not_called()


class CompanionSweepTests(unittest.TestCase):
    def setUp(self):
        l = self.l = hid_gesture.HidGestureListener()
        l._dev = Mock()
        l._dev_idx = 0x05
        l._hires_wheel_idx = 0x12
        l._thumbwheel_idx = 0x13
        l._companion_invert_target = (True, True)
        self.seen = []
        online = {0x04: (0x12, 0x13)}

        def find(fid, timeout_ms=2000):
            ent = online.get(l._dev_idx)
            if not ent:
                return None
            return ent[0] if fid == hid_gesture.FEAT_HIRES_WHEEL_ENHANCED else ent[1]
        l._find_feature = find
        l._set_native_wheel_invert_vertical = lambda inv: self.seen.append(("v", l._dev_idx, inv)) or True
        l._set_native_wheel_invert_horizontal = lambda inv: self.seen.append(("h", l._dev_idx, inv)) or True

    def test_sweeps_other_online_slot_only_and_restores(self):
        receiver_link.sweep_companions(self.l)
        self.assertEqual(self.seen, [("v", 4, True), ("h", 4, True)])
        self.assertEqual(self.l.companion_invert_state, {4: (True, True)})
        self.assertEqual((self.l._dev_idx, self.l._hires_wheel_idx, self.l._thumbwheel_idx),
                         (5, 0x12, 0x13))

    def test_direct_device_skips(self):
        self.l._dev_idx = 0xFF
        receiver_link.sweep_companions(self.l)
        self.assertEqual(self.seen, [])

    def test_schedule_only_on_change(self):
        l = self.l
        l._companion_invert_target = None
        for expect in (True, False):
            l._pending_companion_sweep = False
            receiver_link.on_native_invert_applied(l, True, True, True)
            self.assertEqual(l._pending_companion_sweep, expect)
        l._pending_companion_sweep = False
        receiver_link.on_native_invert_applied(l, False, True, True)
        self.assertTrue(l._pending_companion_sweep)
        l._pending_companion_sweep = False
        receiver_link.on_native_invert_applied(l, True, True, False)
        self.assertFalse(l._pending_companion_sweep)

    def test_apply_pending_invert_schedules_sweep(self):
        l = self.l
        l._companion_invert_target = None
        l._pending_wheel_divert = (True, True)
        l._apply_pending_native_wheel_invert()
        self.assertTrue(l._pending_companion_sweep)


if __name__ == "__main__":
    unittest.main()
