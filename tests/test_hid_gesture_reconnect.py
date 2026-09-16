"""M2-reconnect-loop: reconnect backoff, one long-lived IOHIDManager,
device-arrival wake-up, enumeration skip, bounded report queue, and
log rate limiting (deskflow harness registry entry M2).

All IOKit / hidapi access is mocked; time is a fake clock.
"""

import ctypes
import sys
import unittest
from ctypes import POINTER, c_int
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import hid_gesture


# ── helpers ────────────────────────────────────────────────────────


class _FakeClock:
    """Replaces ``hid_gesture.time``: sleep() advances time()."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeHidDevice:
    def __init__(self):
        self.open_path = Mock()
        self.set_nonblocking = Mock()
        self.close = Mock()


def _candidate_info(pid=0xC537, path=b"/dev/hidraw-test"):
    return {
        "product_id": pid,
        "usage_page": 0xFF00,
        "usage": 0x0002,
        "transport": "USB",
        "source": "hidapi-enumerate",
        "product_string": "Bolt Receiver",
        "path": path,
    }


def _printed(print_mock):
    return [
        " ".join(str(a) for a in call.args) for call in print_mock.call_args_list
    ]


class _HidapiFailingConnect:
    """Context: one hidapi candidate that opens but has no REPROG_V4."""

    def __init__(self, listener, infos):
        self.listener = listener
        self.infos = infos
        self.fake_dev = _FakeHidDevice()
        self.enumerate = Mock(return_value=list(infos))

    def __enter__(self):
        self._patches = [
            patch.object(self.listener, "_vendor_hid_infos", self.enumerate),
            patch.object(self.listener, "_find_feature", return_value=None),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture, "_hid",
                SimpleNamespace(device=lambda: self.fake_dev), create=True,
            ),
            patch.object(hid_gesture, "_load_last_device_cache", return_value=None),
            patch("builtins.print"),
        ]
        self.print_mock = None
        for p in self._patches:
            obj = p.start()
            if p is self._patches[-1]:
                self.print_mock = obj
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# ── (1) present-but-not-connectable backoff ────────────────────────


class PresentBackoffTests(unittest.TestCase):
    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()
        self.clock = _FakeClock()

    def test_present_backoff_doubles_from_1s_to_30s_cap(self):
        delays = [self.listener._next_retry_delay(present=True) for _ in range(8)]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0])
        self.assertGreaterEqual(min(delays), 1.0)

    def test_absent_uses_slow_poll_when_notifications_exist(self):
        with patch.object(self.listener, "_has_arrival_notifications", return_value=True):
            self.assertEqual(self.listener._next_retry_delay(present=False), 30.0)
        with patch.object(self.listener, "_has_arrival_notifications", return_value=False):
            self.assertEqual(self.listener._next_retry_delay(present=False), 5.0)

    def test_main_loop_waits_backoff_and_resets_on_connect(self):
        listener = self.listener
        listener._running = True
        listener._last_scan_had_candidates = True
        listener._iokit_manager = None
        attempts = []

        def try_connect():
            attempts.append(self.clock.now)
            if len(attempts) < 7:
                return False
            listener._running = False   # end the session immediately
            return True

        with (
            patch.object(hid_gesture, "time", self.clock),
            patch.object(listener, "_try_connect", side_effect=try_connect),
            patch.object(listener, "_undivert"),
            patch("builtins.print"),
        ):
            listener._run_main_loop()

        gaps = [round(b - a, 3) for a, b in zip(attempts, attempts[1:])]
        self.assertEqual(gaps, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertEqual(listener._present_backoff_s, 0.0)
        self.assertIsNone(listener._iokit_manager)

    def test_arrival_during_wait_resets_present_backoff(self):
        listener = self.listener
        listener._running = True
        listener._present_backoff_s = 16.0
        listener.notify_device_arrival()
        with patch.object(hid_gesture, "time", self.clock):
            listener._wait_reconnect(60.0)
        self.assertEqual(listener._present_backoff_s, 0.0)
        self.assertLess(self.clock.now - 1_000_000.0, 1.0)
        # Next failed attempt starts the ladder over at the floor.
        self.assertEqual(listener._next_retry_delay(present=True), 1.0)


# ── (4) log rate limit ─────────────────────────────────────────────


class RetryLogRateLimitTests(unittest.TestCase):
    def test_retry_message_once_per_backoff_step(self):
        listener = hid_gesture.HidGestureListener()
        listener._running = True
        listener._last_scan_had_candidates = True
        listener._iokit_manager = None
        clock = _FakeClock()
        calls = {"n": 0}

        def try_connect():
            calls["n"] += 1
            if calls["n"] >= 12:
                listener._running = False
            return False

        with (
            patch.object(hid_gesture, "time", clock),
            patch.object(listener, "_try_connect", side_effect=try_connect),
            patch("builtins.print") as print_mock,
        ):
            listener._run_main_loop()

        retry_lines = [m for m in _printed(print_mock) if "retrying in" in m]
        # 1, 2, 4, 8, 16, 30 -> six distinct steps for twelve attempts.
        self.assertEqual(len(retry_lines), 6)
        self.assertIn("retrying in 1 s", retry_lines[0])
        self.assertIn("retrying in 30 s", retry_lines[-1])

    def test_candidate_block_logged_once_per_candidate_set(self):
        listener = hid_gesture.HidGestureListener()
        info = _candidate_info()
        with _HidapiFailingConnect(listener, [info]) as ctx:
            self.assertFalse(listener._try_connect())
            # Force a real re-enumeration (as after TTL expiry).
            listener._reprog_negative_cache.clear()
            listener._last_failed_scan = None
            self.assertFalse(listener._try_connect())
            lines = _printed(ctx.print_mock)
        self.assertEqual(
            sum(1 for m in lines if "Candidate HID interfaces" in m), 1)
        self.assertEqual(
            sum(1 for m in lines if m.startswith("[HidGesture] Candidate PID=")), 1)
        self.assertEqual(ctx.enumerate.call_count, 2)


# ── (3) negative cache skips enumeration ───────────────────────────


class EnumerationSkipTests(unittest.TestCase):
    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()
        self.info = _candidate_info()

    def test_negative_cached_set_skips_enumeration_until_ttl(self):
        listener = self.listener
        with _HidapiFailingConnect(listener, [self.info]) as ctx:
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 1)
            self.assertIsNotNone(listener._last_failed_scan)
            self.assertEqual(
                listener._last_failed_scan["digest"],
                hid_gesture._scan_digest([self.info]))

            for _ in range(50):
                self.assertFalse(listener._try_connect())
            # No enumeration, no open, no probe while the set is unchanged.
            self.assertEqual(ctx.enumerate.call_count, 1)
            self.assertEqual(ctx.fake_dev.open_path.call_count, 1)
            self.assertEqual(listener._enumeration_skips, 50)
            self.assertTrue(listener._last_scan_had_candidates)

            # TTL expiry re-enumerates and re-probes.
            key = next(iter(listener._reprog_negative_cache))
            listener._reprog_negative_cache[key] -= (
                hid_gesture.REPROG_NEGATIVE_CACHE_TTL_S + 1)
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 2)
            self.assertEqual(ctx.fake_dev.open_path.call_count, 2)

    def test_device_arrival_invalidates_skip(self):
        listener = self.listener
        with _HidapiFailingConnect(listener, [self.info]) as ctx:
            self.assertFalse(listener._try_connect())
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 1)
            listener.notify_device_arrival()
            self.assertIsNone(listener._last_failed_scan)
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 2)

    def test_removal_callback_invalidates_skip(self):
        listener = self.listener
        with _HidapiFailingConnect(listener, [self.info]) as ctx:
            self.assertFalse(listener._try_connect())
            listener._on_candidate_set_changed()
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 2)

    def test_open_failure_is_not_skipped(self):
        # A candidate that could not be opened is not negative-cached, so
        # the next attempt must enumerate again (it may open next time).
        listener = self.listener
        with _HidapiFailingConnect(listener, [self.info]) as ctx:
            ctx.fake_dev.open_path.side_effect = OSError("busy")
            self.assertFalse(listener._try_connect())
            self.assertIsNone(listener._last_failed_scan)
            self.assertFalse(listener._try_connect())
            self.assertEqual(ctx.enumerate.call_count, 2)


# ── (5) bounded report queue ───────────────────────────────────────


class BoundedReportQueueTests(unittest.TestCase):
    def test_drop_oldest_and_counter(self):
        q = hid_gesture._BoundedReportQueue(maxsize=4, label="T")
        with patch("builtins.print") as print_mock:
            for i in range(6):
                q.put(bytes([i]))
        self.assertEqual(q.qsize(), 4)
        self.assertEqual(q.dropped, 2)
        self.assertEqual([q.get_nowait() for _ in range(4)],
                         [b"\x02", b"\x03", b"\x04", b"\x05"])
        lines = _printed(print_mock)
        # First drop logs immediately; the second lands inside the
        # rate-limit window and is folded into the next line's total.
        self.assertEqual(len(lines), 1)
        self.assertIn("[T] mitigation: dropped 1 reports (total=1", lines[0])
        q._last_drop_log = 0.0
        with patch("builtins.print") as print_mock:
            for i in range(5):   # refill (4) + one overflow
                q.put(bytes([0x10 + i]))
        self.assertIn(
            "mitigation: dropped 2 reports (total=3", _printed(print_mock)[0])

    def test_no_drops_in_normal_drain(self):
        q = hid_gesture._BoundedReportQueue(maxsize=4, label="T")
        with patch("builtins.print") as print_mock:
            for i in range(100):
                q.put(bytes([i % 256]))
                q.get_nowait()
        self.assertEqual(q.dropped, 0)
        self.assertEqual(print_mock.call_count, 0)

    def test_default_bound(self):
        q = hid_gesture._BoundedReportQueue()
        self.assertEqual(q.maxsize, hid_gesture.REPORT_QUEUE_MAXSIZE)
        self.assertEqual(q.maxsize, 4096)


# ── (2) one long-lived IOHIDManager + arrival callback (macOS) ─────


class _FakeIOKit:
    """Minimal ctypes-shaped IOKit/CF double for one Logitech interface."""

    MANAGER = 0x1000
    DEVICE = 0x2000

    def __init__(self, clock, pid=0xC548, up=0xFF00, usage=0x0002,
                 transport="USB", product="Bolt Receiver"):
        self.clock = clock
        self.props = {
            "ProductID": pid, "PrimaryUsagePage": up, "PrimaryUsage": usage,
            "Transport": transport, "Product": product,
        }
        self.manager_creates = 0
        self.manager_closes = 0
        self.copy_devices = 0
        self.device_opens = 0
        self.matching_cb = None
        self.removal_cb = None
        self.fire_arrival_on_pump = False
        self.pumps = 0
        self._strings = {}
        self._numbers = {}
        self._next = 0x100
        self.cf = SimpleNamespace(
            CFStringCreateWithCString=self._cfstring,
            CFNumberCreate=self._cfnumber,
            CFNumberGetValue=self._cfnumber_get,
            CFStringGetCString=self._cfstring_get,
            CFDictionaryCreate=lambda *a: 0x3000,
            CFSetGetCount=lambda s: 1,
            CFSetGetValues=self._set_values,
            CFRelease=lambda ref: None,
            CFRetain=lambda ref: ref,
            CFRunLoopGetCurrent=lambda: 0x4000,
            CFRunLoopRunInMode=self._run_loop,
        )
        self.iokit = SimpleNamespace(
            IOHIDManagerCreate=self._manager_create,
            IOHIDManagerSetDeviceMatching=lambda m, d: None,
            IOHIDManagerOpen=lambda m, o: 0,
            IOHIDManagerClose=self._manager_close,
            IOHIDManagerCopyDevices=self._copy,
            IOHIDManagerScheduleWithRunLoop=lambda m, l, mode: None,
            IOHIDManagerUnscheduleFromRunLoop=lambda m, l, mode: None,
            IOHIDManagerRegisterDeviceMatchingCallback=self._reg_matching,
            IOHIDManagerRegisterDeviceRemovalCallback=self._reg_removal,
            IOHIDDeviceGetProperty=self._get_property,
            IOHIDDeviceOpen=self._device_open,
            IOHIDDeviceClose=lambda d, o: 0,
            IOHIDDeviceScheduleWithRunLoop=lambda d, l, m: None,
            IOHIDDeviceUnscheduleFromRunLoop=lambda d, l, m: None,
            IOHIDDeviceRegisterInputReportCallback=lambda *a: None,
            IOHIDDeviceSetReport=lambda *a: 0,
        )

    def _alloc(self):
        self._next += 1
        return self._next

    def _cfstring(self, _alloc, text, _enc):
        ref = self._alloc()
        self._strings[ref] = text.decode("utf-8")
        return ref

    def _cfnumber(self, _alloc, _type, ptr):
        ref = self._alloc()
        self._numbers[ref] = ctypes.cast(ptr, POINTER(c_int))[0]
        return ref

    def _cfnumber_get(self, ref, _type, out):
        ctypes.cast(out, POINTER(c_int))[0] = int(self._numbers.get(ref, 0))
        return 1

    def _cfstring_get(self, ref, buf, _len, _enc):
        buf.value = self._strings.get(ref, "").encode("utf-8")
        return 1

    def _get_property(self, _device, key):
        name = self._strings.get(key)
        value = self.props.get(name)
        if value is None:
            return None
        ref = self._alloc()
        if isinstance(value, str):
            self._strings[ref] = value
        else:
            self._numbers[ref] = int(value)
        return ref

    def _set_values(self, _set, buf):
        buf[0] = self.DEVICE

    def _manager_create(self, _alloc, _opts):
        self.manager_creates += 1
        return self.MANAGER

    def _manager_close(self, _m, _o):
        self.manager_closes += 1
        return 0

    def _copy(self, _m):
        self.copy_devices += 1
        return 0x5000

    def _device_open(self, _d, _o):
        self.device_opens += 1
        return 0

    def _reg_matching(self, _m, cb, _ctx):
        self.matching_cb = cb

    def _reg_removal(self, _m, cb, _ctx):
        self.removal_cb = cb

    def _run_loop(self, _mode, seconds, _once):
        self.pumps += 1
        if self.fire_arrival_on_pump and self.matching_cb is not None:
            self.fire_arrival_on_pump = False
            self.matching_cb(None, 0, None, self.DEVICE)
            return 2   # kCFRunLoopRunHandledSource
        self.clock.sleep(float(seconds))
        return 0


@unittest.skipUnless(
    sys.platform == "darwin" and hid_gesture._MAC_NATIVE_OK,
    "native IOKit backend only",
)
class SharedIOHIDManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.fake = _FakeIOKit(self.clock)
        self.listener = hid_gesture.HidGestureListener()
        self._patches = [
            patch.object(hid_gesture, "_iokit", self.fake.iokit),
            patch.object(hid_gesture, "_cf", self.fake.cf),
            patch.object(hid_gesture, "HIDAPI_OK", False),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "iokit"),
            patch.object(hid_gesture, "_load_last_device_cache", return_value=None),
            patch.object(self.listener, "_find_feature", return_value=None),
            patch("builtins.print"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _reset_negative_cache(self):
        self.listener._reprog_negative_cache.clear()
        self.listener._last_failed_scan = None

    def test_single_manager_across_100_attempts(self):
        for _ in range(100):
            self.assertFalse(self.listener._try_connect())
            # Defeat the enumeration skip so every attempt really
            # enumerates and opens the device.
            self._reset_negative_cache()
        self.assertEqual(self.fake.manager_creates, 1)
        self.assertEqual(self.fake.copy_devices, 200)   # enumerate + find
        self.assertEqual(self.fake.device_opens, 100)
        self.assertEqual(self.fake.manager_closes, 0)
        manager = self.listener._iokit_manager
        self.assertIsNotNone(manager)
        self.assertEqual(manager.create_count, 1)
        self.assertTrue(manager.active)

        self.listener.stop()
        self.assertEqual(self.fake.manager_closes, 1)
        self.assertIsNone(self.listener._iokit_manager)

    def test_arrival_callback_wakes_wait_and_clears_skip(self):
        self.assertFalse(self.listener._try_connect())
        self.assertIsNotNone(self.listener._last_failed_scan)
        self.assertIsNotNone(self.fake.matching_cb)

        self.listener._running = True
        self.listener._present_backoff_s = 30.0
        self.fake.fire_arrival_on_pump = True
        started = self.clock.now
        with patch.object(hid_gesture, "time", self.clock):
            self.listener._wait_reconnect(30.0)
        self.assertLess(self.clock.now - started, 0.5)
        self.assertEqual(self.listener._present_backoff_s, 0.0)
        self.assertIsNone(self.listener._last_failed_scan)
        self.assertEqual(self.listener._iokit_manager.arrival_count, 1)
        self.assertEqual(len(self.listener._reprog_negative_cache), 0)

    def test_wait_pumps_run_loop_instead_of_sleeping(self):
        self.assertFalse(self.listener._try_connect())
        self.listener._running = True
        pumps_before = self.fake.pumps
        with patch.object(hid_gesture, "time", self.clock):
            self.listener._wait_reconnect(1.0)
        self.assertGreaterEqual(self.fake.pumps - pumps_before, 10)

    def test_initial_matching_burst_is_not_an_arrival(self):
        # Scheduling replays a callback per present device; the manager
        # must swallow that instead of clearing the negative cache.
        self.fake.fire_arrival_on_pump = True
        self.assertFalse(self.listener._try_connect())
        self.assertEqual(self.listener._iokit_manager.arrival_count, 0)
        self.assertFalse(self.listener._device_arrival.is_set())

    def test_native_device_uses_shared_manager(self):
        self.assertFalse(self.listener._try_connect())
        self.assertEqual(self.fake.manager_creates, 1)
        self.assertEqual(self.fake.device_opens, 1)


if __name__ == "__main__":
    unittest.main()
