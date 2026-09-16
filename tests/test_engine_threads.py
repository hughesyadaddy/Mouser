"""Thread-churn contract for core/engine.py (hotspot M6).

Covers the three churn sources:
  * safety auto-release -- one reusable thread, one deadline per action;
  * SmartShift/DPI device writes -- one FIFO worker, order preserved;
  * BatteryPoll -- exactly one live poller per connection, retired by Event
    (never joined from the HID thread), and a prompt ``stop()``.

HID and hook layers are faked; no OS hooks are installed.
"""

import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.config import DEFAULT_CONFIG
from tests.support.fake_mouse_hook import FakeMouseHook


class _FakeAppDetector:
    def __init__(self, callback):
        self.callback = callback

    def start(self):
        pass

    def stop(self):
        pass


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _threads_named(name):
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _make_engine():
    from core.engine import Engine

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    with (
        patch("core.engine.MouseHook", FakeMouseHook),
        patch("core.engine.AppDetector", _FakeAppDetector),
        patch("core.engine.load_config", return_value=cfg),
        patch("core.deskflow_integration.resolve_integration", return_value=None),
    ):
        return Engine()


class SafetyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.clock = _FakeClock()
        self.engine._clock = self.clock
        self.addCleanup(self.engine.stop)

    def _press(self, action_id, n):
        down = self.engine._make_mouse_down_handler(action_id)
        evt = SimpleNamespace(event_type="xbutton1_down")
        with patch("core.engine.inject_mouse_down"):
            for _ in range(n):
                down(evt)

    def test_thousand_presses_create_at_most_one_timer_thread(self):
        before = len(_threads_named("SafetyRelease"))
        self.assertEqual(before, 0)
        with patch("core.engine.threading.Timer") as timer_cls:
            self._press("mouse_left", 1000)
        timer_cls.assert_not_called()
        self.assertEqual(len(_threads_named("SafetyRelease")), 1)
        self.assertEqual(len(self.engine._mouse_release_deadlines), 1)
        self.assertAlmostEqual(
            self.engine._mouse_release_deadlines["mouse_left"],
            self.clock.now + self.engine.SAFETY_RELEASE_S,
        )

    def test_up_disarms_and_deadline_fires_only_when_due(self):
        released = []
        with patch("core.engine.inject_mouse_up", side_effect=released.append):
            self._press("mouse_left", 1)
            self._press("mouse_right", 1)
            up = self.engine._make_mouse_up_handler("mouse_right")
            up(SimpleNamespace(event_type="xbutton2_up"))
            self.assertEqual(released, ["mouse_right"])
            self.assertNotIn("mouse_right", self.engine._mouse_release_deadlines)

            # Not yet due: the worker must stay quiet.
            self.clock.now += self.engine.SAFETY_RELEASE_S - 1
            with self.engine._release_cv:
                self.engine._release_cv.notify_all()
            time.sleep(0.05)
            self.assertEqual(released, ["mouse_right"])

            # Now overdue: exactly one auto-release for the held button.
            self.clock.now += 2
            with self.engine._release_cv:
                self.engine._release_cv.notify_all()
            self.assertTrue(_wait_until(lambda: released == ["mouse_right", "mouse_left"]))
            self.assertEqual(self.engine._mouse_release_deadlines, {})
        # Worker is still the single reusable thread, idle.
        self.assertEqual(len(_threads_named("SafetyRelease")), 1)

    def test_stop_clears_deadlines_and_retires_worker_promptly(self):
        with patch("core.engine.inject_mouse_up") as up:
            self._press("mouse_left", 3)
            t0 = time.monotonic()
            self.engine.stop()
            self.assertLess(time.monotonic() - t0, 1.0)
            up.assert_not_called()
        self.assertTrue(_wait_until(lambda: not _threads_named("SafetyRelease")))
        self.assertEqual(self.engine._mouse_release_deadlines, {})


class DeviceWriteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.addCleanup(self.engine.stop)

    def test_three_actions_run_in_order_on_one_worker(self):
        order = []
        gate = threading.Event()

        def _set_smart_shift(mode, enabled, threshold):
            gate.wait(2)
            order.append(("smart_shift", mode, enabled, threading.current_thread().name))
            return True

        def _set_dpi(dpi):
            order.append(("dpi", dpi, threading.current_thread().name))
            return True

        hg = SimpleNamespace(
            smart_shift_supported=True,
            connected_device=SimpleNamespace(name="MX Master 3S", dpi_max=8000),
            set_smart_shift=_set_smart_shift,
            set_dpi=_set_dpi,
        )
        self.engine.hook._hid_gesture = hg
        self.engine.cfg["settings"].update({
            "smart_shift_enabled": False,
            "smart_shift_mode": "ratchet",
            "smart_shift_threshold": 25,
            "dpi": 800,
            "dpi_presets": [800, 1600],
        })
        with patch("core.engine.save_config"), patch("core.engine.clamp_dpi", side_effect=lambda d, _dev: d):
            self.engine._toggle_smart_shift()    # -> ratchet, enabled=True
            self.engine._switch_scroll_mode()    # -> freespin, enabled=False
            self.engine._cycle_dpi()             # -> 1600
        # All three are queued behind the gate; only one worker exists.
        self.assertEqual(len(_threads_named("DeviceWrite")), 1)
        self.assertEqual(order, [])
        gate.set()
        self.assertTrue(_wait_until(lambda: len(order) == 3))
        self.assertEqual(
            [o[:-1] for o in order],
            [
                ("smart_shift", "ratchet", True),
                ("smart_shift", "freespin", False),
                ("dpi", 1600),
            ],
        )
        self.assertEqual({o[-1] for o in order}, {"DeviceWrite"})
        self.assertEqual(len(_threads_named("DeviceWrite")), 1)

    def test_worker_survives_a_failing_write(self):
        calls = []
        hg = SimpleNamespace(
            smart_shift_supported=True,
            connected_device=SimpleNamespace(name="MX Master 3S"),
            set_smart_shift=Mock(side_effect=[RuntimeError("boom"), True]),
            set_dpi=lambda dpi: calls.append(dpi),
        )
        self.engine.hook._hid_gesture = hg
        with patch("core.engine.save_config"), patch("core.engine.clamp_dpi", side_effect=lambda d, _dev: d):
            self.engine._toggle_smart_shift()
            self.engine._toggle_smart_shift()
            self.engine._cycle_dpi()
        self.assertTrue(_wait_until(lambda: len(calls) == 1))
        self.assertEqual(hg.set_smart_shift.call_count, 2)
        self.assertEqual(len(_threads_named("DeviceWrite")), 1)


class BatteryPollerTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.addCleanup(self.engine.stop)
        self.engine.hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(name="MX Master 3S"),
            smart_shift_supported=False,
            read_battery=Mock(return_value=80),
        )

    def test_reconnect_100_times_leaves_exactly_one_poller(self):
        self.assertEqual(_threads_named("BatteryPoll"), [])
        for _ in range(100):
            self.engine._on_connection_change(True)
            self.engine._on_connection_change(False)
        self.engine._on_connection_change(True)
        live = self.engine._battery_poll_thread
        self.assertIsNotNone(live)
        self.assertTrue(
            _wait_until(lambda: _threads_named("BatteryPoll") == [live]),
            f"lingering pollers: {_threads_named('BatteryPoll')}",
        )

    def test_retire_never_joins_from_the_hid_thread(self):
        self.engine._on_connection_change(True)
        poller = self.engine._battery_poll_thread
        with patch.object(poller, "join", wraps=poller.join) as join:
            self.engine._on_connection_change(False)
        join.assert_not_called()
        self.assertIsNone(self.engine._battery_poll_thread)
        self.assertTrue(_wait_until(lambda: not poller.is_alive()))

    def test_retire_returns_promptly_while_poller_is_blocked_in_hid_read(self):
        """A poller stuck in read_battery must not stall the HID thread."""
        release = threading.Event()

        def _slow_read():
            release.wait(5)
            return 50

        self.engine.hook._hid_gesture.read_battery = _slow_read
        self.engine._on_connection_change(True)
        poller = self.engine._battery_poll_thread
        t0 = time.monotonic()
        self.engine._on_connection_change(False)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(poller.is_alive())
        release.set()
        self.assertTrue(_wait_until(lambda: not poller.is_alive()))

    def test_stop_is_prompt(self):
        self.engine._on_connection_change(True)
        poller = self.engine._battery_poll_thread
        t0 = time.monotonic()
        self.engine.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(poller.is_alive())
        self.assertEqual(_threads_named("BatteryPoll"), [])


if __name__ == "__main__":
    unittest.main()
