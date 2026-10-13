"""Every mouse paired to the bound receiver gets the wheel-invert intent."""
import unittest
from unittest.mock import Mock

from core import hid_gesture


class CompanionSweepTests(unittest.TestCase):
    def setUp(self):
        l = self.l = hid_gesture.HidGestureListener()
        l._dev = Mock(); l._dev_idx = 0x05; l._hires_wheel_idx = 0x12; l._thumbwheel_idx = 0x13
        l._companion_invert_target = (True, True)
        self.seen = []
        online = {0x04: (0x12, 0x13)}
        def find(fid, timeout_ms=2000):
            ent = online.get(l._dev_idx)
            if not ent: return None
            return ent[0] if fid == hid_gesture.FEAT_HIRES_WHEEL_ENHANCED else ent[1]
        l._find_feature = find
        l._set_native_wheel_invert_vertical = lambda inv: self.seen.append(("v", l._dev_idx, inv)) or True
        l._set_native_wheel_invert_horizontal = lambda inv: self.seen.append(("h", l._dev_idx, inv)) or True

    def test_sweeps_other_online_slot_only(self):
        self.l._sweep_companion_wheel_invert()
        self.assertEqual(self.seen, [("v", 4, True), ("h", 4, True)])
        self.assertEqual(self.l.companion_invert_state, {4: (True, True)})

    def test_restores_bound_state(self):
        self.l._sweep_companion_wheel_invert()
        self.assertEqual((self.l._dev_idx, self.l._hires_wheel_idx, self.l._thumbwheel_idx), (5, 0x12, 0x13))

    def test_direct_device_skips(self):
        self.l._dev_idx = 0xFF
        self.l._sweep_companion_wheel_invert()
        self.assertEqual(self.seen, [])

    def test_companion_link_notice_schedules_sweep(self):
        self.l._feat_idx = 0x0D
        self.l._pending_companion_sweep = False
        self.l._on_report([hid_gesture.SHORT_ID, 0x04, 0x41, 0x04, 0x00, 0x00, 0x00])
        self.assertTrue(self.l._pending_companion_sweep)

    def test_companion_link_lost_no_sweep(self):
        self.l._feat_idx = 0x0D
        self.l._pending_companion_sweep = False
        self.l._on_report([hid_gesture.SHORT_ID, 0x04, 0x41, 0x04, 0x40, 0x00, 0x00])
        self.assertFalse(self.l._pending_companion_sweep)


if __name__ == "__main__":
    unittest.main()


class SweepSchedulingTests(unittest.TestCase):
    def test_realign_same_intent_does_not_resweep(self):
        l = hid_gesture.HidGestureListener()
        l._dev = Mock(); l._hires_wheel_idx = 0x12; l._thumbwheel_idx = 0x13
        l._set_native_wheel_invert_vertical = lambda inv: True
        l._set_native_wheel_invert_horizontal = lambda inv: True
        for expect in (True, False):
            l._pending_companion_sweep = False
            l._pending_wheel_divert = (True, True)
            l._apply_pending_native_wheel_invert()
            self.assertEqual(l._pending_companion_sweep, expect)
