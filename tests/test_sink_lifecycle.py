"""The process-global Deskflow sink must survive a listener reconnect.

Regression for the 27 h / 100 % CPU spin: cleanup close()d the singleton,
the listener re-attached the same object, and read() returned None with no
wait. ``close()`` stays terminal; what changes is that a read-only session
never calls it.
"""

import io
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import hid_gesture
from core.hid_deskflow_backend import (
    DeskflowSinkDevice,
    get_deskflow_sink,
    reset_deskflow_sink_for_tests,
)
from core.hid_gesture import HidGestureListener
from core.hid_sink import encode_report_frame
from core.mouse_hook_types import DEVICE_SOURCE_DESKFLOW_SHIM

from tests.test_deskflow_listener_ingress import (
    DECODE,
    FRAME_GESTURE_DOWN,
    _IngressFixture,
    _wait_until,
)


class ClosedSinkReadTests(unittest.TestCase):
    def test_read_on_closed_sink_logs_exactly_once(self):
        sink = DeskflowSinkDevice(read_timeout_ms=50)
        sink.close()
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(5):
                self.assertIsNone(sink.read(64, timeout_ms=1000))
        self.assertEqual(out.getvalue().count("read() on a closed sink"), 1)
        self.assertTrue(sink.closed)

    def test_open_sink_read_waits_for_its_timeout(self):
        sink = DeskflowSinkDevice(read_timeout_ms=50)
        started = time.monotonic()
        self.assertIsNone(sink.read(64, timeout_ms=50))
        self.assertGreaterEqual(time.monotonic() - started, 0.04)


class SinkSurvivesReconnectTests(unittest.TestCase):
    def setUp(self):
        reset_deskflow_sink_for_tests()

    def tearDown(self):
        reset_deskflow_sink_for_tests()

    @patch.object(HidGestureListener, "_vendor_hid_infos", return_value=[])
    def test_attach_reconnect_attach_keeps_sink_readable(self, _infos):
        with _IngressFixture(_infos) as fx:
            hg = fx.hook._hid_gesture
            sink = get_deskflow_sink()
            self.assertIs(hg._dev, sink)

            reconnects = []
            previous_on_connect = hg._on_connect

            def on_connect():
                reconnects.append(time.monotonic())
                if previous_on_connect:
                    previous_on_connect()

            hg._on_connect = on_connect
            hg.force_reconnect()
            self.assertTrue(
                _wait_until(lambda: len(reconnects) >= 1, timeout=10.0),
                "listener did not re-attach the Deskflow sink",
            )
            self.assertTrue(hg._deskflow_readonly)
            self.assertIs(hg._dev, sink)
            self.assertFalse(sink.closed)

            # read() must wait for its timeout, not return an instant None.
            started = time.monotonic()
            self.assertIsNone(sink.read(64, timeout_ms=50))
            self.assertGreaterEqual(time.monotonic() - started, 0.04)

            fx.client.send_frame(encode_report_frame(1, FRAME_GESTURE_DOWN))
            self.assertTrue(
                _wait_until(
                    lambda: ("dispatch", "gesture_down") in fx.hook.calls,
                    timeout=5.0,
                ),
                "gesture frame was not delivered after the reconnect",
            )


class _InstantNoneDevice:
    """A dead backend: read() returns None without waiting."""

    def __init__(self):
        self.reads = 0

    def read(self, size, timeout_ms=0):
        self.reads += 1
        return None

    def write(self, data):
        return len(data)

    def close(self):
        pass


class InstantNoneGuardTests(unittest.TestCase):
    def _listener(self, dev, *, readonly):
        hg = HidGestureListener()
        hg._running = True

        def connect():
            hg._dev = dev
            hg._deskflow_readonly = readonly
            return True

        hg._try_connect = connect
        hg._undivert = lambda: None
        hg._wait_reconnect = lambda delay: setattr(hg, "_running", False)
        hg._update_reconnect_backoff = lambda *a, **k: 0.0
        return hg

    def test_instant_none_on_readonly_sink_backs_off_instead_of_spinning(self):
        dev = _InstantNoneDevice()
        hg = self._listener(dev, readonly=True)
        with patch.object(hid_gesture, "INSTANT_NONE_LIMIT", 20):
            started = time.monotonic()
            hg._run_main_loop()
            elapsed = time.monotonic() - started
        self.assertFalse(hg._running)
        # 20 instant Nones at 5 ms each, then IOError -> _wait_reconnect.
        self.assertLessEqual(dev.reads, 25)
        self.assertGreaterEqual(elapsed, 0.08)

    def test_instant_none_on_usb_only_sleeps(self):
        dev = _InstantNoneDevice()
        hg = self._listener(dev, readonly=False)
        stop_at = time.monotonic() + 0.15
        original_rx = hg._rx

        def rx_then_stop(timeout_ms=2000):
            if time.monotonic() > stop_at:
                hg._running = False
            return original_rx(timeout_ms)

        hg._rx = rx_then_stop
        with patch.object(hid_gesture, "INSTANT_NONE_LIMIT", 5):
            hg._run_main_loop()
        # ~150 ms at >=5 ms per read is ~30 reads; a spin would be thousands.
        self.assertLess(dev.reads, 100)


class RejectedAttachTests(unittest.TestCase):
    """A pending attach short-circuits _wait_reconnect. If the attach is
    rejected (bad decode, closed sink) and stays pending, the outer loop
    spins at >100k iterations/s with a log line each. Uses the REAL
    _wait_reconnect."""

    def _spin_test(self, attach_decode, *, close_sink=False):
        reset_deskflow_sink_for_tests()
        self.addCleanup(reset_deskflow_sink_for_tests)
        if close_sink:
            get_deskflow_sink().close()
        hg = HidGestureListener()
        hg._running = True
        hg._deskflow_attach = {
            "decode": attach_decode,
            "product_id": 0xB042,
            "product_name": "MX Master 4",
        }
        hg._vendor_hid_infos = lambda manager: []
        hg._mac_manager = lambda: None
        connect_attempts = []
        original = hg._try_connect

        def counting_try_connect():
            connect_attempts.append(time.monotonic())
            if len(connect_attempts) >= 3 or time.monotonic() > stop_at:
                hg._running = False
            return original()

        hg._try_connect = counting_try_connect
        stop_at = time.monotonic() + 0.5
        with patch.object(hid_gesture, "ABSENT_POLL_UNNOTIFIED_S", 0.05), \
                patch.object(hid_gesture, "ABSENT_POLL_NOTIFIED_S", 0.05), \
                redirect_stdout(io.StringIO()):
            hg._run_main_loop()
        return hg, connect_attempts

    def test_invalid_decode_attach_is_dropped_and_the_loop_backs_off(self):
        hg, attempts = self._spin_test({"gesture_cid": "0x01A0"})
        self.assertIsNone(hg._deskflow_attach)
        self.assertLessEqual(len(attempts), 3)
        # Second attempt only after the absent-poll wait, not instantly.
        self.assertGreaterEqual(attempts[1] - attempts[0], 0.04)

    def test_closed_sink_attach_is_dropped_and_the_loop_backs_off(self):
        hg, attempts = self._spin_test(dict(DECODE), close_sink=True)
        self.assertIsNone(hg._deskflow_attach)
        self.assertLessEqual(len(attempts), 3)
        self.assertGreaterEqual(attempts[1] - attempts[0], 0.04)

    def test_a_newer_attach_survives_the_rejection_of_an_older_one(self):
        reset_deskflow_sink_for_tests()
        self.addCleanup(reset_deskflow_sink_for_tests)
        hg = HidGestureListener()
        hg._vendor_hid_infos = lambda manager: []
        hg._mac_manager = lambda: None
        stale = {"decode": {"gesture_cid": "0x01A0"}, "product_id": 1, "product_name": "x"}
        fresh = {"decode": dict(DECODE), "product_id": 0xB042, "product_name": "MX Master 4"}
        hg._deskflow_attach = stale
        original = hg._try_connect_deskflow

        def reject_then_replace(attach):
            hg._deskflow_attach = fresh
            return original(attach)

        hg._try_connect_deskflow = reject_then_replace
        with redirect_stdout(io.StringIO()):
            self.assertFalse(hg._try_connect())
        self.assertIs(hg._deskflow_attach, fresh)


class ReadOnlyEarlyReturnTests(unittest.TestCase):
    """A read-only Deskflow session cannot write firmware: every device
    write/read must answer immediately instead of waiting out a 3 s poll."""

    def setUp(self):
        self.hg = HidGestureListener()
        self.hg._deskflow_readonly = True
        self.hg._connected = True

    def _assert_immediate(self, call, expected):
        started = time.monotonic()
        self.assertEqual(call(), expected)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_set_dpi_returns_false_immediately(self):
        self._assert_immediate(lambda: self.hg.set_dpi(1600), False)
        self.assertIsNone(self.hg._pending_dpi)

    def test_read_dpi_returns_none_immediately(self):
        self._assert_immediate(self.hg.read_dpi, None)

    def test_set_smart_shift_returns_false_immediately(self):
        self._assert_immediate(lambda: self.hg.set_smart_shift("ratchet"), False)
        self.assertIsNone(self.hg._pending_smart_shift)

    def test_read_smart_shift_returns_none_immediately(self):
        self._assert_immediate(self.hg.read_smart_shift, None)

    def test_read_battery_returns_none_immediately(self):
        self._assert_immediate(self.hg.read_battery, None)
        self.assertIsNone(self.hg._pending_battery)

    def test_wheel_native_invert_returns_false_pair_immediately(self):
        self._assert_immediate(
            lambda: self.hg.request_wheel_native_invert(True, True), (False, False)
        )
        self.assertIsNone(self.hg._pending_wheel_divert)


class ReadOnlyReplayTests(unittest.TestCase):
    def _engine(self, source):
        from tests.test_engine_threads import _make_engine

        engine = _make_engine()
        self.addCleanup(engine.stop)
        engine.cfg.setdefault("settings", {})["dpi"] = 1600
        hg = SimpleNamespace(
            connected_device=SimpleNamespace(source=source),
            smart_shift_supported=True,
            set_dpi=Mock(return_value=True),
            set_smart_shift=Mock(return_value=True),
        )
        engine.hook._hid_gesture = hg
        engine.hook.connected_device = hg.connected_device
        return engine, hg

    def test_replay_skips_device_writes_on_deskflow_shim(self):
        engine, hg = self._engine(DEVICE_SOURCE_DESKFLOW_SHIM)
        self.assertTrue(engine.device_readonly)
        with patch("core.engine.time.sleep") as sleep:
            self.assertTrue(engine._run_saved_settings_replay())
        sleep.assert_not_called()
        hg.set_dpi.assert_not_called()
        hg.set_smart_shift.assert_not_called()

    def test_replay_still_writes_on_a_physical_device(self):
        engine, hg = self._engine("hidapi")
        self.assertFalse(engine.device_readonly)
        with patch("core.engine.time.sleep"):
            engine._run_saved_settings_replay()
        hg.set_dpi.assert_called()
        hg.set_smart_shift.assert_called()


if __name__ == "__main__":
    unittest.main()
