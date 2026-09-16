"""Bridge-crossing budget for the macOS CGEventTap callback (hotspot M5).

The tap callback runs on the main run loop at up to 1 kHz. These tests pin
how many Quartz calls each event type may make on the idle fast path versus
the active-gesture path, that the IOHID scroll monitor is only started or
stopped on a device-bound transition (never per wheel tick), and that a wake
burst never stacks a second ``MouseHook-resume`` worker behind a live one.

Quartz is mocked; nothing here needs macOS.
"""

import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import core
from core.mouse_hook_types import MouseEvent

_MOVED = 5
_OTHER_DOWN = 25
_OTHER_UP = 26
_OTHER_DRAGGED = 27
_SCROLL = 22
_F_BUTTON = 0x21
_F_USER_DATA = 0x2A
_F_DX = 4
_F_DY = 5
_F_H_FIXED = 97
_F_V_FIXED = 96


def _load_module():
    module = sys.modules.get("core.mouse_hook_macos")
    if module is not None:
        return module, False
    fake_objc = SimpleNamespace(autorelease_pool=MagicMock())
    with patch.dict(
        sys.modules, {"objc": fake_objc, "Quartz": MagicMock(name="Quartz")}
    ):
        module = importlib.import_module("core.mouse_hook_macos")
    return module, True


def _fake_quartz():
    q = MagicMock(name="Quartz")
    q.kCGEventMouseMoved = _MOVED
    q.kCGEventOtherMouseDown = _OTHER_DOWN
    q.kCGEventOtherMouseUp = _OTHER_UP
    q.kCGEventOtherMouseDragged = _OTHER_DRAGGED
    q.kCGEventScrollWheel = _SCROLL
    q.kCGMouseEventButtonNumber = _F_BUTTON
    q.kCGEventSourceUserData = _F_USER_DATA
    q.kCGMouseEventDeltaX = _F_DX
    q.kCGMouseEventDeltaY = _F_DY
    q.kCGScrollWheelEventFixedPtDeltaAxis2 = _F_H_FIXED
    q.kCGScrollWheelEventFixedPtDeltaAxis1 = _F_V_FIXED
    q.CGEventGetLocation.return_value = (10.0, 20.0)
    return q


class _MacOSHookCase(unittest.TestCase):
    def setUp(self):
        self.module, loaded_here = _load_module()
        if loaded_here:
            self.addCleanup(self._unload_module)
        self._prev_quartz = self.module.__dict__.get("Quartz")
        self.quartz = _fake_quartz()
        self.module.Quartz = self.quartz
        self.addCleanup(self._restore_quartz)
        # The module resolves these at import time from whichever Quartz was
        # present then (a MagicMock on Linux CI). Pin the real ids so the
        # attribution checks are deterministic regardless of import order.
        for name, value in (
            ("_CG_SCROLL_FIELD_IS_CONTINUOUS", 88),
            ("_CG_SCROLL_FIELD_MOMENTUM_PHASE", 123),
            ("_CG_SCROLL_FIELD_SCROLL_PHASE", 99),
            ("_CG_SCROLL_PHASE_NONE", 0),
            ("_CG_SCROLL_PHASE_ENDED", 4),
        ):
            patcher = patch.object(self.module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fields = {}
        self.quartz.CGEventGetIntegerValueField.side_effect = (
            lambda _event, field: self.fields.get(field, 0)
        )
        self.cg_event = MagicMock(name="cg_event")

    def _unload_module(self):
        if sys.modules.get("core.mouse_hook_macos") is self.module:
            sys.modules.pop("core.mouse_hook_macos", None)
        if getattr(core, "mouse_hook_macos", None) is self.module:
            delattr(core, "mouse_hook_macos")

    def _restore_quartz(self):
        if self._prev_quartz is None:
            self.module.__dict__.pop("Quartz", None)
        else:
            self.module.Quartz = self._prev_quartz

    def _hook(self, *, device=True):
        hook = self.module.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        if device:
            hook._connected_device = SimpleNamespace(
                key="mx_master_3s",
                source="hidapi",
                thumb_button_via_hid=False,
                gesture_via_sense_panel=False,
            )
        return hook

    def _fire(self, hook, event_type):
        return hook._event_tap_callback(None, event_type, self.cg_event, None)

    @property
    def int_reads(self):
        return self.quartz.CGEventGetIntegerValueField.call_count

    def _int_reads_of(self, field):
        return sum(
            1
            for c in self.quartz.CGEventGetIntegerValueField.call_args_list
            if c.args[1] == field
        )

    @property
    def location_reads(self):
        return self.quartz.CGEventGetLocation.call_count


class MotionFastPathTests(_MacOSHookCase):
    """Pointer motion is the 1 kHz path; it must cost zero crossings unless
    the gesture engine can actually use the data."""

    def test_move_with_no_device_bound_makes_zero_quartz_calls(self):
        hook = self._hook(device=False)
        hook._gesture_direction_enabled = True

        for event_type in (_MOVED, _OTHER_DRAGGED):
            with self.subTest(event_type=event_type):
                result = self._fire(hook, event_type)
                self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 0)
        self.assertEqual(self.location_reads, 0)

    def test_move_with_gesture_direction_disabled_makes_zero_quartz_calls(self):
        hook = self._hook()
        hook._gesture_direction_enabled = False

        for _ in range(50):
            self.assertIs(self._fire(hook, _MOVED), self.cg_event)
        self.assertEqual(self.int_reads, 0)
        self.assertEqual(self.location_reads, 0)

    def test_disabling_direction_clears_stale_anchor_source(self):
        hook = self._hook()
        hook._gesture_direction_enabled = True
        self._fire(hook, _MOVED)
        self.assertEqual(hook._last_cursor_pos, (10.0, 20.0))

        hook._gesture_direction_enabled = False
        self._fire(hook, _MOVED)
        self.assertIsNone(hook._last_cursor_pos)

        hook._arm_gesture_anchor()
        self.quartz.CGWarpMouseCursorPosition.assert_not_called()

    def test_idle_move_with_direction_enabled_reads_location_exactly_once(self):
        hook = self._hook()
        hook._gesture_direction_enabled = True

        result = self._fire(hook, _MOVED)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.location_reads, 1)
        # No injected-marker or delta reads on an idle move.
        self.assertEqual(self.int_reads, 0)
        self.assertEqual(hook._last_cursor_pos, (10.0, 20.0))

    def test_idle_move_anchor_is_used_on_gesture_press(self):
        hook = self._hook()
        hook._gesture_direction_enabled = True
        self._fire(hook, _MOVED)

        hook._arm_gesture_anchor()

        self.assertEqual(hook._gesture_anchor, (10.0, 20.0))
        self.quartz.CGWarpMouseCursorPosition.assert_called_once_with((10.0, 20.0))


class ActiveGesturePathTests(_MacOSHookCase):
    def _capturing_hook(self):
        hook = self._hook()
        hook._gesture_direction_enabled = True
        hook._accumulate_gesture_delta = Mock(name="accumulate")
        hook._begin_gesture_capture("HID gesture")
        self.assertTrue(hook._gesture_active)
        self.fields[_F_DX] = 7
        self.fields[_F_DY] = -3
        return hook

    def test_active_gesture_reads_each_delta_once_and_swallows_event(self):
        hook = self._capturing_hook()

        result = self._fire(hook, _OTHER_DRAGGED)

        self.assertIsNone(result)
        self.assertEqual(self._int_reads_of(_F_DX), 1)
        self.assertEqual(self._int_reads_of(_F_DY), 1)
        self.assertEqual(self.int_reads, 2)
        self.assertEqual(self.location_reads, 0)
        hook._accumulate_gesture_delta.assert_called_once_with(7, -3, "event_tap")

    def test_active_gesture_does_not_move_anchor_source(self):
        hook = self._capturing_hook()
        hook._last_cursor_pos = (1.0, 1.0)

        self._fire(hook, _MOVED)

        self.assertEqual(hook._last_cursor_pos, (1.0, 1.0))

    def test_active_gesture_repins_to_anchor(self):
        hook = self._capturing_hook()
        hook._gesture_anchor = (40.0, 50.0)

        self._fire(hook, _MOVED)

        self.quartz.CGWarpMouseCursorPosition.assert_called_once_with((40.0, 50.0))
        self.quartz.CGAssociateMouseAndMouseCursorPosition.assert_called_once_with(True)

    def test_hid_rawxy_locked_capture_drops_event_without_accumulating(self):
        hook = self._capturing_hook()
        hook._gesture_input_source = "hid_rawxy"

        result = self._fire(hook, _MOVED)

        self.assertIsNone(result)
        hook._accumulate_gesture_delta.assert_not_called()
        self.assertEqual(self.int_reads, 2)

    def test_debug_off_emits_nothing_and_builds_no_event_dict(self):
        hook = self._capturing_hook()
        gesture_cb = Mock(name="gesture_cb")
        debug_cb = Mock(name="debug_cb")
        hook.set_gesture_callback(gesture_cb)
        hook.set_debug_callback(debug_cb)
        hook.debug_mode = False

        self._fire(hook, _MOVED)

        gesture_cb.assert_not_called()
        debug_cb.assert_not_called()

    def test_debug_on_reports_the_single_read_deltas(self):
        hook = self._capturing_hook()
        gesture_cb = Mock(name="gesture_cb")
        hook.set_gesture_callback(gesture_cb)
        hook.debug_mode = True

        self._fire(hook, _MOVED)

        gesture_cb.assert_called_once_with(
            {"type": "move", "source": "event_tap", "dx": 7, "dy": -3}
        )
        # Debug reporting must not re-read the fields.
        self.assertEqual(self.int_reads, 2)

    def test_active_gesture_with_direction_disabled_passes_through(self):
        hook = self._hook()
        hook._gesture_direction_enabled = False
        hook._begin_gesture_capture("HID gesture")

        result = self._fire(hook, _MOVED)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 0)


class ButtonPathTests(_MacOSHookCase):
    def test_button_down_reads_marker_and_button_once_each(self):
        hook = self._hook()
        hook.block(MouseEvent.XBUTTON1_DOWN)
        self.fields[_F_BUTTON] = 3

        result = self._fire(hook, _OTHER_DOWN)

        self.assertIsNone(result)
        self.assertEqual(self._int_reads_of(_F_USER_DATA), 1)
        self.assertEqual(self._int_reads_of(_F_BUTTON), 1)
        self.assertEqual(self.int_reads, 2)
        self.assertEqual(self.location_reads, 0)
        self.assertEqual(
            hook._dispatch_queue.get_nowait().event_type, MouseEvent.XBUTTON1_DOWN
        )

    def test_button_up_reads_marker_and_button_once_each(self):
        hook = self._hook()
        self.fields[_F_BUTTON] = 2

        result = self._fire(hook, _OTHER_UP)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 2)
        self.assertEqual(
            hook._dispatch_queue.get_nowait().event_type, MouseEvent.MIDDLE_UP
        )

    def test_injected_button_returns_after_the_marker_read(self):
        hook = self._hook()
        self.fields[_F_USER_DATA] = self.module._INJECTED_EVENT_MARKER
        self.fields[_F_BUTTON] = 3

        result = self._fire(hook, _OTHER_DOWN)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 1)
        self.assertTrue(hook._dispatch_queue.empty())

    def test_button_with_no_device_costs_only_the_marker_read(self):
        hook = self._hook(device=False)
        hook.block(MouseEvent.XBUTTON1_DOWN)
        self.fields[_F_BUTTON] = 3

        result = self._fire(hook, _OTHER_DOWN)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 1)
        self.assertTrue(hook._dispatch_queue.empty())

    def test_debug_button_logging_does_not_add_reads(self):
        hook = self._hook()
        hook.debug_mode = True
        hook.set_debug_callback(Mock())
        self.fields[_F_BUTTON] = 4

        self._fire(hook, _OTHER_DOWN)

        self.assertEqual(self.int_reads, 2)


class ScrollPathTests(_MacOSHookCase):
    _F_CONT = None
    _F_MOMENTUM = None
    _F_PHASE = None

    def setUp(self):
        super().setUp()
        self._F_CONT = self.module._CG_SCROLL_FIELD_IS_CONTINUOUS
        self._F_MOMENTUM = self.module._CG_SCROLL_FIELD_MOMENTUM_PHASE
        self._F_PHASE = self.module._CG_SCROLL_FIELD_SCROLL_PHASE
        self._avail = patch.object(self.module, "SCROLL_MONITOR_AVAILABLE", True)
        self._avail.start()
        self.addCleanup(self._avail.stop)

    def _scroll_hook(self, *, invert=True):
        hook = self._hook()
        hook.invert_vscroll = invert
        hook.invert_hscroll = invert
        hook._logitech_scroll_monitor = Mock(name="monitor")
        hook._logitech_scroll_monitor.recent_wheel.return_value = True
        return hook

    def test_plain_wheel_without_invert_reads_marker_continuous_and_hdelta(self):
        hook = self._scroll_hook(invert=False)

        result = self._fire(hook, _SCROLL)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self._int_reads_of(_F_USER_DATA), 1)
        self.assertEqual(self._int_reads_of(self._F_CONT), 1)
        self.assertEqual(self._int_reads_of(_F_H_FIXED), 1)
        self.assertEqual(self.int_reads, 3)
        self.assertEqual(self._int_reads_of(self._F_MOMENTUM), 0)
        self.assertEqual(self._int_reads_of(self._F_PHASE), 0)

    def test_marker_is_read_once_per_wheel_event(self):
        hook = self._scroll_hook()

        self._fire(hook, _SCROLL)

        self.assertEqual(self._int_reads_of(_F_USER_DATA), 1)

    def test_invert_both_axes_shares_attribution_reads(self):
        hook = self._scroll_hook()
        self.fields[self.module.Quartz.kCGScrollWheelEventDeltaAxis1] = 1
        self.fields[self.module.Quartz.kCGScrollWheelEventDeltaAxis2] = 1

        result = self._fire(hook, _SCROLL)

        self.assertIs(result, self.cg_event)
        # Continuous / momentum / phase are read once, not once per axis.
        self.assertEqual(self._int_reads_of(self._F_CONT), 1)
        self.assertEqual(self._int_reads_of(self._F_MOMENTUM), 1)
        self.assertEqual(self._int_reads_of(self._F_PHASE), 1)
        negated = {
            c.args[1] for c in self.quartz.CGEventSetIntegerValueField.call_args_list
        }
        self.assertIn(self.module.Quartz.kCGScrollWheelEventDeltaAxis1, negated)
        self.assertIn(self.module.Quartz.kCGScrollWheelEventDeltaAxis2, negated)
        self.assertIsNone(hook._scroll_prefetch)

    def test_prefetch_cache_never_outlives_the_event(self):
        hook = self._scroll_hook()
        hook._apply_vscroll_invert_fallback = Mock(side_effect=RuntimeError("boom"))

        with patch("builtins.print"):
            result = self._fire(hook, _SCROLL)

        self.assertIs(result, self.cg_event)
        self.assertIsNone(hook._scroll_prefetch)

    def test_continuous_trackpad_wheel_stops_after_marker_and_continuous(self):
        hook = self._scroll_hook()
        self.fields[self._F_CONT] = 1

        result = self._fire(hook, _SCROLL)

        self.assertIs(result, self.cg_event)
        self.assertEqual(self.int_reads, 2)

    def test_attribution_outside_callback_still_reads_fields_itself(self):
        hook = self._scroll_hook()

        self.assertTrue(hook._scroll_event_targets_logitech(cg_event=self.cg_event))

        self.assertEqual(self._int_reads_of(self._F_CONT), 1)
        self.assertEqual(self._int_reads_of(self._F_MOMENTUM), 1)
        self.assertEqual(self._int_reads_of(self._F_PHASE), 1)

    def test_stale_prefetch_for_another_event_is_ignored(self):
        hook = self._scroll_hook()
        hook._scroll_prefetch = [MagicMock(name="other"), 1, 1, 99]

        self.assertTrue(hook._scroll_event_targets_logitech(cg_event=self.cg_event))
        self.assertEqual(self.int_reads, 3)


class ScrollMonitorTransitionTests(_MacOSHookCase):
    def setUp(self):
        super().setUp()
        self._avail = patch.object(self.module, "SCROLL_MONITOR_AVAILABLE", True)
        self._avail.start()
        self.addCleanup(self._avail.stop)

    def _monitor_hook(self, *, device=True):
        hook = self._hook(device=device)
        hook._logitech_scroll_monitor = Mock(name="monitor")
        hook._logitech_scroll_monitor.recent_wheel.return_value = False
        return hook

    def test_start_called_once_across_many_wheel_events(self):
        hook = self._monitor_hook()

        for _ in range(200):
            self._fire(hook, _SCROLL)

        hook._logitech_scroll_monitor.start.assert_called_once()
        hook._logitech_scroll_monitor.stop.assert_not_called()

    def test_stop_called_once_when_device_unbinds(self):
        hook = self._monitor_hook()
        self._fire(hook, _SCROLL)
        hook._connected_device = None

        for _ in range(50):
            self._fire(hook, _SCROLL)

        hook._logitech_scroll_monitor.start.assert_called_once()
        hook._logitech_scroll_monitor.stop.assert_called_once()

    def test_no_device_from_cold_start_stops_once(self):
        hook = self._monitor_hook(device=False)

        for _ in range(20):
            self._fire(hook, _SCROLL)

        hook._logitech_scroll_monitor.stop.assert_called_once()
        hook._logitech_scroll_monitor.start.assert_not_called()

    def test_hid_connect_rearms_one_retry_after_failed_start(self):
        hook = self._monitor_hook()
        for _ in range(5):
            self._fire(hook, _SCROLL)
        self.assertEqual(hook._logitech_scroll_monitor.start.call_count, 1)

        hook._hid_gesture = SimpleNamespace(connected_device=hook._connected_device)
        hook._on_hid_connect()
        for _ in range(5):
            self._fire(hook, _SCROLL)

        self.assertEqual(hook._logitech_scroll_monitor.start.call_count, 2)

    def test_remote_virtual_device_transitions_to_stop(self):
        hook = self._monitor_hook()
        self._fire(hook, _SCROLL)
        hook._connected_device = SimpleNamespace(
            key="mx_master_3s",
            source="remote-virtual",
            thumb_button_via_hid=False,
            gesture_via_sense_panel=False,
        )

        self._fire(hook, _SCROLL)
        self._fire(hook, _SCROLL)

        hook._logitech_scroll_monitor.start.assert_called_once()
        hook._logitech_scroll_monitor.stop.assert_called_once()

    def test_stop_resets_transition_state(self):
        hook = self._monitor_hook()
        self._fire(hook, _SCROLL)
        hook._tap = None
        hook._dispatch_thread = None

        hook.stop()

        self.assertIsNone(hook._scroll_monitor_applied)
        hook._logitech_scroll_monitor.stop.assert_called_once()

    def test_sync_is_a_noop_without_monitor_support(self):
        hook = self._monitor_hook()
        with patch.object(self.module, "SCROLL_MONITOR_AVAILABLE", False):
            hook._sync_logitech_scroll_monitor()
        hook._logitech_scroll_monitor.start.assert_not_called()
        hook._logitech_scroll_monitor.stop.assert_not_called()


class QuartzBindingTests(_MacOSHookCase):
    def test_bindings_follow_a_swapped_quartz_module(self):
        hook = self._hook()
        first = hook._quartz()
        self.assertIs(first.module, self.quartz)
        self.assertIs(hook._quartz(), first)

        replacement = _fake_quartz()
        self.module.Quartz = replacement
        second = hook._quartz()

        self.assertIsNot(second, first)
        self.assertIs(second.module, replacement)
        self.assertIs(second.get_int, replacement.CGEventGetIntegerValueField)

    def test_callback_never_touches_the_module_after_binding(self):
        hook = self._hook()
        hook._gesture_direction_enabled = True
        self._fire(hook, _MOVED)
        self.fields[_F_BUTTON] = 3
        self._fire(hook, _OTHER_DOWN)

        # Every Quartz call went through the bound callables.
        used = {name for name, _args, _kw in self.quartz.mock_calls}
        self.assertLessEqual(
            used, {"CGEventGetLocation", "CGEventGetIntegerValueField"}
        )

    def test_negate_uses_prebound_fields(self):
        hook = self._hook()
        self.fields[self.quartz.kCGScrollWheelEventPointDeltaAxis1] = 4

        hook._negate_scroll_axis(self.cg_event, 1)

        self.quartz.CGEventSetIntegerValueField.assert_called_once_with(
            self.cg_event, self.quartz.kCGScrollWheelEventPointDeltaAxis1, -4
        )
        self.assertEqual(self.int_reads, 3)


class ResumeRecoveryDedupeTests(_MacOSHookCase):
    class _FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.alive = False
            self.started = 0

        def start(self):
            self.started += 1
            self.alive = True

        def is_alive(self):
            return self.alive

    def _wake_hook(self):
        hook = self._hook()
        hook._device_connected = True
        hook._hid_gesture = Mock(name="listener")
        return hook

    def test_live_worker_blocks_a_new_one_even_outside_dedupe_window(self):
        hook = self._wake_hook()
        created = []

        def factory(**kw):
            t = self._FakeThread(**kw)
            created.append(t)
            return t

        with (
            patch.object(self.module.threading, "Thread", factory),
            patch.object(self.module.time, "monotonic", side_effect=(100.0, 200.0, 300.0)),
            patch("builtins.print"),
        ):
            self.assertTrue(hook._start_resume_recovery("system wake"))
            self.assertFalse(hook._start_resume_recovery("screens wake"))
            created[0].alive = False
            self.assertTrue(hook._start_resume_recovery("later wake"))

        self.assertEqual(len(created), 2)
        self.assertEqual([t.name for t in created], ["MouseHook-resume"] * 2)
        self.assertTrue(all(t.daemon for t in created))

    def test_worker_clears_its_own_handle_when_done(self):
        hook = self._wake_hook()
        hook._resume_thread = self.module.threading.current_thread()

        with patch.object(self.module.time, "sleep"):
            hook._resume_recovery_worker()

        self.assertIsNone(hook._resume_thread)
        hook._hid_gesture.force_reconnect.assert_called_once()

    def test_worker_leaves_a_different_handle_alone(self):
        hook = self._wake_hook()
        other = object()
        hook._resume_thread = other

        with patch.object(self.module.time, "sleep"):
            hook._resume_recovery_worker()

        self.assertIs(hook._resume_thread, other)

    def test_real_thread_runs_once_and_releases_handle(self):
        hook = self._wake_hook()
        hook._RESUME_RECONNECT_DELAY_S = 0.0

        real_thread = self.module.threading.Thread
        created = []

        def recording_thread(**kw):
            t = real_thread(**kw)
            created.append(t)
            return t

        with (
            patch.object(self.module.threading, "Thread", recording_thread),
            patch("builtins.print"),
        ):
            self.assertTrue(hook._start_resume_recovery("system wake"))
        (thread,) = created
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertIsNone(hook._resume_thread)
        hook._hid_gesture.force_reconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
