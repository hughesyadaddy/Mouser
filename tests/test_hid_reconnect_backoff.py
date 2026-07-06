"""Reconnect-storm protection: backoff, REPROG_V4 negative cache, and
device-arrival interrupts (see docs: deskflow cursor-lag debrief).

An asleep Logitech device used to drive a tight open/probe/timeout loop
that pegged a core and starved the WH_MOUSE_LL hook chain, lagging every
injected cursor move.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import hid_gesture


class _FakeHidDevice:
    def __init__(self):
        self.open_path = Mock()
        self.set_nonblocking = Mock()
        self.close = Mock()


def _candidate_info():
    return {
        "product_id": 0xC537,
        "usage_page": 0xFF00,
        "usage": 0x0002,
        "transport": "USB",
        "source": "hidapi-enumerate",
        "product_string": "Bolt Receiver",
        "path": b"/dev/hidraw-test",
    }


class ReconnectBackoffTests(unittest.TestCase):
    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()

    def test_backoff_doubles_to_cap_on_timeout_disconnects(self):
        delays = []
        with patch("builtins.print"):
            for _ in range(7):
                delays.append(self.listener._update_reconnect_backoff(
                    session_healthy=False, timed_out_disconnect=True))

        self.assertEqual(
            delays, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        self.assertEqual(self.listener._reconnect_backoff_s, 60.0)

    def test_healthy_session_resets_backoff(self):
        with patch("builtins.print"):
            for _ in range(5):
                self.listener._update_reconnect_backoff(
                    session_healthy=False, timed_out_disconnect=True)
            delay = self.listener._update_reconnect_backoff(
                session_healthy=True, timed_out_disconnect=False)

        self.assertEqual(delay, 2.0)
        self.assertEqual(self.listener._reconnect_backoff_s, 0.0)

    def test_non_timeout_disconnect_keeps_backoff_flat(self):
        delay = self.listener._update_reconnect_backoff(
            session_healthy=False, timed_out_disconnect=False)
        self.assertEqual(delay, 2.0)
        self.assertEqual(self.listener._reconnect_backoff_s, 0.0)


class WaitReconnectTests(unittest.TestCase):
    """Plan acceptance criterion B3: a device arrival must interrupt an
    in-progress backoff wait within ~0.1 s (not wait out the timer)."""

    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()
        # _wait_reconnect only loops while the listener runs; tests drive
        # it directly without start()'s thread.
        self.listener._running = True

    def test_device_arrival_interrupts_wait_and_resets_backoff(self):
        self.listener._reconnect_backoff_s = 60.0
        # Arrival signaled during session teardown (before the wait even
        # starts) must not be lost: the event is consumed by the wait.
        self.listener.notify_device_arrival()

        started = time.time()
        self.listener._wait_reconnect(60.0)
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(self.listener._reconnect_backoff_s, 0.0)
        self.assertFalse(self.listener._device_arrival.is_set())

    def test_wait_runs_full_delay_without_interrupts(self):
        started = time.time()
        self.listener._wait_reconnect(0.3)
        self.assertGreaterEqual(time.time() - started, 0.3)

    def test_stop_interrupts_wait(self):
        self.listener._running = False
        started = time.time()
        self.listener._wait_reconnect(60.0)
        self.assertLess(time.time() - started, 0.5)


class ReprogNegativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()
        self.info = _candidate_info()
        self.fake_dev = _FakeHidDevice()

    def _try_connect(self):
        with (
            patch.object(
                self.listener, "_vendor_hid_infos", return_value=[self.info]),
            patch.object(self.listener, "_find_feature", return_value=None),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: self.fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            return self.listener._try_connect()

    def test_reprog_failure_populates_cache_and_skips_reprobe(self):
        self.assertFalse(self._try_connect())
        self.assertEqual(len(self.listener._reprog_negative_cache), 1)
        open_calls_after_first = self.fake_dev.open_path.call_count
        self.assertGreater(open_calls_after_first, 0)

        # Second attempt inside the TTL must not reopen the device.
        self.assertFalse(self._try_connect())
        self.assertEqual(
            self.fake_dev.open_path.call_count, open_calls_after_first)

    def test_cache_expires_after_ttl(self):
        self.assertFalse(self._try_connect())
        key = next(iter(self.listener._reprog_negative_cache))
        self.listener._reprog_negative_cache[key] = (
            time.time() - hid_gesture.REPROG_NEGATIVE_CACHE_TTL_S - 1)

        open_calls_after_first = self.fake_dev.open_path.call_count
        self.assertFalse(self._try_connect())
        # Expired entry: the candidate is probed (and re-cached) again.
        self.assertGreater(
            self.fake_dev.open_path.call_count, open_calls_after_first)
        self.assertEqual(len(self.listener._reprog_negative_cache), 1)

    def test_device_arrival_clears_cache_and_signals_event(self):
        self.assertFalse(self._try_connect())
        self.assertEqual(len(self.listener._reprog_negative_cache), 1)

        self.listener.notify_device_arrival()

        self.assertEqual(len(self.listener._reprog_negative_cache), 0)
        self.assertTrue(self.listener._device_arrival.is_set())

    def test_repeated_arrivals_are_rate_limited(self):
        # Probing the receiver itself fires DBT_DEVNODES_CHANGED, so an
        # unthrottled clear would wipe the cache right after each probe
        # cycle populates it and restart the storm forever.
        self.listener.notify_device_arrival()
        self.listener._device_arrival.clear()

        self.assertFalse(self._try_connect())
        self.assertEqual(len(self.listener._reprog_negative_cache), 1)

        self.listener.notify_device_arrival()  # inside the 60 s window
        self.assertEqual(len(self.listener._reprog_negative_cache), 1)
        self.assertFalse(self.listener._device_arrival.is_set())

        # Once the window passes, the next arrival clears again.
        self.listener._last_arrival_clear = (
            time.time() - hid_gesture.ARRIVAL_CLEAR_MIN_INTERVAL_S - 1)
        self.listener.notify_device_arrival()
        self.assertEqual(len(self.listener._reprog_negative_cache), 0)
        self.assertTrue(self.listener._device_arrival.is_set())


if __name__ == "__main__":
    unittest.main()
