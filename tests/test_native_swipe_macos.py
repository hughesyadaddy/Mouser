import struct
import threading
import unittest
import unittest.mock

from core import native_swipe_macos as ns


class FakeQuartz:
    kCGHIDEventTap = 0

    def __init__(self, fail_after=None):
        self.posted = []
        self.fail_after = fail_after

    def CGEventCreate(self, _src):
        return {}

    def CGEventSetIntegerValueField(self, e, f, v):
        e[f] = v

    def CGEventSetDoubleValueField(self, e, f, v):
        e[f] = v

    def CGEventPost(self, tap, e):
        if self.fail_after is not None and len(self.posted) >= self.fail_after:
            self.fail_after = None
            raise RuntimeError("post failed")
        self.posted.append((tap, dict(e)))


class InstantTimer:
    def __init__(self, delay, fn):
        self.fn = fn
        self.daemon = False

    def start(self):
        self.fn()


class DockSwipeTests(unittest.TestCase):
    def _run(self, q, **kw):
        return ns.dock_swipe(ns.AXIS_HORIZONTAL, +1, quartz=q,
                             sleep=lambda s: None, timer=InstantTimer, **kw)

    def test_sequence_tap_marker_and_end_resend(self):
        q = FakeQuartz()
        self.assertTrue(self._run(q))
        self.assertTrue(all(tap == q.kCGHIDEventTap for tap, _ in q.posted))
        markers = {e[ns.F_SOURCE_USER_DATA] for _, e in q.posted}
        self.assertEqual(markers, {ns.NATIVE_SWIPE_MARKER})
        self.assertNotIn(0x4D53, {e[ns.F_SOURCE_USER_DATA] for _, e in q.posted})
        docks = [e for _, e in q.posted if e[ns.F_TYPE] == ns.NS_EVENT_TYPE_DOCK_CONTROL]
        phases = [e[ns.F_PHASE_A] for e in docks]
        self.assertEqual(phases[0], ns.PHASE_BEGAN)
        self.assertEqual(phases[-2:], [ns.PHASE_ENDED, ns.PHASE_ENDED])  # end + resend
        self.assertEqual(phases.count(ns.PHASE_CHANGED), ns.FRAMES)
        end = docks[-1]
        self.assertEqual(end[ns.F_HID_SUBTYPE], ns.HID_DOCK_SWIPE)
        self.assertEqual(end[ns.F_MOTION_A], ns.AXIS_HORIZONTAL)
        self.assertAlmostEqual(end[ns.F_PROGRESS], 1.0)
        self.assertEqual(end[ns.F_PROGRESS_F32], struct.unpack("<I", struct.pack("<f", 1.0))[0])
        self.assertGreater(end[ns.F_EXIT_SPEED_A], 0)
        # every dock event is followed by its type-29 companion
        types = [e[ns.F_TYPE] for _, e in q.posted]
        self.assertEqual(types[1::2], [ns.NS_EVENT_TYPE_GESTURE] * (len(types) // 2))

    def test_end_always_sent_when_a_frame_fails(self):
        q = FakeQuartz(fail_after=6)  # dies mid-stream
        with self.assertRaises(RuntimeError):
            self._run(q)
        docks = [e for _, e in q.posted if e[ns.F_TYPE] == ns.NS_EVENT_TYPE_DOCK_CONTROL]
        self.assertEqual(docks[-1][ns.F_PHASE_A], ns.PHASE_ENDED)
        self.assertFalse(ns._lock.locked())

    def test_overlapping_swipe_is_dropped(self):
        ns._lock.acquire()
        try:
            q = FakeQuartz()
            self.assertFalse(self._run(q))
            self.assertEqual(q.posted, [])
        finally:
            ns._lock.release()

    def test_negative_direction(self):
        plan = ns.frame_plan(-1, frames=4)
        self.assertEqual(plan[-1], (ns.PHASE_ENDED, -1.0))

    def test_bad_axis(self):
        with self.assertRaises(ValueError):
            ns.dock_swipe(9, 1, quartz=FakeQuartz())


class NavSwipeTests(unittest.TestCase):
    def test_began_then_ended(self):
        q = FakeQuartz()
        self.assertTrue(ns.navigation_swipe(-1, quartz=q))
        self.assertEqual([e[ns.F_PHASE_A] for _, e in q.posted], [ns.PHASE_BEGAN, ns.PHASE_ENDED])
        self.assertEqual(q.posted[0][1][ns.F_SWIPE_DIRECTION], ns.SWIPE_LEFT)
        self.assertEqual(q.posted[1][1][ns.F_SWIPE_DIRECTION], ns.SWIPE_NONE)
        self.assertEqual(q.posted[0][1][ns.F_HID_SUBTYPE], ns.HID_NAVIGATION_SWIPE)

    def test_ended_sent_even_if_began_fails(self):
        q = FakeQuartz(fail_after=0)
        with self.assertRaises(RuntimeError):
            ns.navigation_swipe(1, quartz=q)
        self.assertEqual(q.posted[-1][1][ns.F_PHASE_A], ns.PHASE_ENDED)


class SupportTests(unittest.TestCase):
    def test_supported_gates_on_version(self):
        with unittest.mock.patch.object(ns.sys, "platform", "darwin"):
            for ver, ok in (("26.2", True), ("15.7", True), ("27.0", False), ("", False)):
                with unittest.mock.patch.object(ns.platform, "mac_ver", return_value=(ver, "", "")):
                    self.assertEqual(ns.supported(), ok, ver)
        with unittest.mock.patch.object(ns.sys, "platform", "win32"):
            self.assertFalse(ns.supported())


if __name__ == "__main__":
    unittest.main()
