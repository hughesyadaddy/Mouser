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
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import call, MagicMock, Mock, patch

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
    # A disabled-by-system notification finds the tap disabled.
    q.CGEventTapIsEnabled.return_value = False
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

    def test_deskflow_injected_button_passes_through_untouched(self):
        """Deskflow marks the events it posts for a remote seat with 'DSKF';
        the receiving Mac's tap must not route them through the remap
        pipeline (or re-invert its wheel)."""
        hook = self._hook()
        self.fields[_F_USER_DATA] = self.module._DESKFLOW_INJECTED_EVENT_MARKER
        self.fields[_F_BUTTON] = 3

        for event_type in (_OTHER_DOWN, _OTHER_UP, _SCROLL):
            with self.subTest(event_type=event_type):
                self.assertIs(self._fire(hook, event_type), self.cg_event)
        self.assertEqual(self.int_reads, 3)
        self.assertTrue(hook._dispatch_queue.empty())
        self.quartz.CGEventSetIntegerValueField.assert_not_called()

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


class MotionTapGatingTests(_MacOSHookCase):
    """Pointer motion has its own tap that is off unless a directional
    gesture is armed, so idle motion never enters Python at all."""

    def _started_hook(self):
        hook = self.module.MouseHook()
        self.quartz.CGEventTapCreate.side_effect = ["main-tap", "motion-tap"]
        self.quartz.CFRunLoopRunInMode.side_effect = (
            lambda _mode, _secs, _once: time.sleep(0.005)
        )
        for patcher in (
            patch.object(self.module.NativeTap, "load", return_value=None),
            patch.object(hook, "_start_hid_listener"),
            patch.object(hook, "_register_wake_observer"),
            patch.object(hook, "_unregister_wake_observer"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch("builtins.print"):
            self.assertTrue(hook.start())
        # Timeouts are only answered on a tap Mouser wants enabled.
        hook._tap_wanted = True
        self.addCleanup(hook.stop)
        return hook

    def _mask_of(self, create_call):
        return create_call.args[3]

    def test_start_splits_motion_into_a_second_tap_that_starts_disabled(self):
        hook = self._started_hook()

        creates = self.quartz.CGEventTapCreate.call_args_list
        self.assertEqual(len(creates), 2)
        self.assertEqual(hook._tap, "main-tap")
        self.assertEqual(hook._motion_tap, "motion-tap")
        self.assertEqual(
            self.quartz.CGEventTapEnable.call_args_list[-1].args, ("motion-tap", False)
        )

    def test_main_tap_mask_excludes_motion(self):
        self.quartz.CGEventMaskBit.side_effect = lambda t: 1 << t
        hook = self._started_hook()
        main_mask = self._mask_of(self.quartz.CGEventTapCreate.call_args_list[0])
        motion_mask = self._mask_of(self.quartz.CGEventTapCreate.call_args_list[1])
        self.assertEqual(main_mask, (1 << _OTHER_DOWN) | (1 << _OTHER_UP) | (1 << _SCROLL))
        self.assertEqual(motion_mask, (1 << _MOVED) | (1 << _OTHER_DRAGGED))
        self.assertIsNotNone(hook._motion_tap)

    def test_arm_enables_motion_tap_and_release_disables_it(self):
        hook = self._started_hook()
        hook._gesture_direction_enabled = True
        self.quartz.CGEventTapEnable.reset_mock()

        hook._arm_gesture_anchor()
        self.assertEqual(
            self.quartz.CGEventTapEnable.call_args_list, [call("motion-tap", True)]
        )
        self.assertEqual(hook._gesture_anchor, (10.0, 20.0))

        hook._release_gesture_anchor()
        self.assertEqual(
            self.quartz.CGEventTapEnable.call_args_list[-1], call("motion-tap", False)
        )
        self.assertIsNone(hook._gesture_anchor)

    def test_arm_reads_the_anchor_from_a_fresh_event_not_a_tracked_move(self):
        hook = self._started_hook()
        hook._gesture_direction_enabled = True
        hook._last_cursor_pos = (1.0, 1.0)
        self.quartz.CGEventGetLocation.return_value = (33.0, 44.0)

        hook._arm_gesture_anchor()

        self.quartz.CGEventCreate.assert_called_once_with(None)
        self.assertEqual(hook._gesture_anchor, (33.0, 44.0))
        self.quartz.CGWarpMouseCursorPosition.assert_called_once_with((33.0, 44.0))

    def test_arm_with_direction_disabled_leaves_motion_tap_off(self):
        hook = self._started_hook()
        hook._gesture_direction_enabled = False
        self.quartz.CGEventTapEnable.reset_mock()

        hook._arm_gesture_anchor()

        self.quartz.CGEventTapEnable.assert_not_called()
        self.assertIsNone(hook._gesture_anchor)

    def test_tap_timeout_re_enables_motion_tap_only_while_armed(self):
        hook = self._started_hook()
        hook._gesture_direction_enabled = True
        self.quartz.CGEventTapEnable.reset_mock()
        with patch("builtins.print"):
            self._fire(hook, self.module._kCGEventTapDisabledByTimeout)
        self.assertEqual(
            self.quartz.CGEventTapEnable.call_args_list, [call("main-tap", True)]
        )
        self.assertEqual(hook.tap_reenable_total, 1)

        hook._arm_gesture_anchor()
        self.quartz.CGEventTapEnable.reset_mock()
        with patch("builtins.print"):
            self._fire(hook, self.module._kCGEventTapDisabledByTimeout)
        self.assertEqual(
            self.quartz.CGEventTapEnable.call_args_list,
            [call("main-tap", True), call("motion-tap", True)],
        )
        self.assertEqual(hook.tap_reenable_total, 2)

    def test_release_disarms_before_disabling_the_motion_tap(self):
        """The disable's own DisabledByUserInput notification races the
        release; it must find no armed anchor, or it re-enables the tap."""
        hook = self._started_hook()
        hook._gesture_direction_enabled = True
        hook._arm_gesture_anchor()
        seen = []
        self.quartz.CGEventTapEnable.side_effect = (
            lambda tap, enabled: seen.append((tap, enabled, hook._gesture_anchor))
        )

        hook._release_gesture_anchor()

        self.assertEqual(seen, [("motion-tap", False, None)])
        self.quartz.CGWarpMouseCursorPosition.assert_called_with((10.0, 20.0))

    def test_stop_tears_down_both_taps(self):
        hook = self._started_hook()
        with patch("builtins.print"):
            hook.stop()
        self.assertIsNone(hook._tap)
        self.assertIsNone(hook._motion_tap)
        self.assertEqual(self.quartz.CFRunLoopRemoveSource.call_count, 2)


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


class _TapLifecycleCase(_MacOSHookCase):
    """start()/stop() against a fake Quartz whose run loop is per-thread."""

    def setUp(self):
        super().setUp()
        self.main_tap = MagicMock(name="tap")
        self.motion_tap = MagicMock(name="motion-tap")
        self.quartz.CGEventTapCreate.side_effect = [self.main_tap, self.motion_tap]
        self.quartz.CFRunLoopGetCurrent.side_effect = (
            lambda: ("loop", threading.get_ident())
        )
        self.quartz.CFRunLoopRunInMode.side_effect = (
            lambda _mode, _secs, _once: time.sleep(0.005)
        )
        self.native_load = patch.object(self.module.NativeTap, "load", return_value=None)
        self.native_load.start()
        self.addCleanup(self.native_load.stop)
        self.listener = patch.object(self.module, "HidGestureListener", None)
        self.listener.start()
        self.addCleanup(self.listener.stop)
        self.hook = self.module.MouseHook()
        self.hook._logitech_scroll_monitor = Mock(name="monitor")
        self.hook._register_wake_observer = lambda: None
        self.hook._unregister_wake_observer = lambda: None
        self.addCleanup(self.hook.stop)

    def _bind(self):
        self.hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(source="hidapi", thumb_button_via_hid=False,
                                             gesture_via_sense_panel=False),
            stop=lambda: None,
        )
        self.hook._on_hid_connect()

    def _main_tap_enables(self):
        tap = self.main_tap
        return [c.args[1] for c in self.quartz.CGEventTapEnable.call_args_list if c.args[0] is tap]


class PythonTapThreadTests(_TapLifecycleCase):
    def test_tap_is_created_on_its_own_thread_and_the_loop_is_captured(self):
        self.assertTrue(self.hook.start())
        self.assertTrue(self.hook._running)
        tap_ident = self.hook._tap_loop[1]
        self.assertNotEqual(tap_ident, threading.get_ident())
        self.assertEqual(self.hook._tap_thread.ident, tap_ident)
        add = self.quartz.CFRunLoopAddSource.call_args
        self.assertEqual(add.args[0], ("loop", tap_ident))
        self.assertEqual(add.args[1], self.quartz.CFMachPortCreateRunLoopSource.return_value)

    def test_tap_starts_idle_until_a_device_binds(self):
        self.hook.start()
        self.assertEqual(self._main_tap_enables(), [False])

    def test_tap_starts_enabled_when_a_device_is_already_bound(self):
        self._bind()
        self.hook.start()
        self.assertEqual(self._main_tap_enables(), [True])

    def test_stop_targets_the_captured_loop_not_the_callers(self):
        self.hook.start()
        loop = self.hook._tap_loop
        thread = self.hook._tap_thread

        self.hook.stop()

        self.quartz.CFRunLoopStop.assert_called_once_with(loop)
        self.assertFalse(thread.is_alive())
        remove = self.quartz.CFRunLoopRemoveSource.call_args
        self.assertEqual(remove.args[0], loop)
        self.assertNotEqual(remove.args[0][1], threading.get_ident())
        self.assertIsNone(self.hook._tap)
        self.assertIsNone(self.hook._tap_loop)
        self.assertIsNone(self.hook._tap_thread)
        self.assertIn(call(self.main_tap, False), self.quartz.CGEventTapEnable.call_args_list)
        self.assertIn(call(self.motion_tap, False), self.quartz.CGEventTapEnable.call_args_list)
        self.hook._logitech_scroll_monitor.stop.assert_called()

    def test_failed_tap_creation_returns_false_and_leaves_nothing_running(self):
        self.quartz.CGEventTapCreate.side_effect = None
        self.quartz.CGEventTapCreate.return_value = None
        with patch("builtins.print"):
            self.assertFalse(self.hook.start())
        self.assertFalse(self.hook._running)
        self.assertIsNone(self.hook._tap_thread)
        self.assertIsNone(self.hook._tap_loop)
        self.quartz.CFRunLoopAddSource.assert_not_called()

    def test_start_twice_is_idempotent(self):
        self.hook.start()
        self.assertTrue(self.hook.start())
        # One main tap plus one motion tap.
        self.assertEqual(self.quartz.CGEventTapCreate.call_count, 2)

    def test_each_loop_pass_runs_inside_an_autorelease_pool(self):
        pool = MagicMock(name="autorelease_pool")
        with patch.object(self.module, "objc", SimpleNamespace(autorelease_pool=pool)):
            self.hook.start()
            time.sleep(0.03)
            self.hook.stop()
        self.assertGreater(pool.call_count, 1)
        self.assertEqual(pool.call_count, pool.return_value.__enter__.call_count)


class PythonTapSyncTests(_TapLifecycleCase):
    def _run_posted_blocks(self):
        for call in self.quartz.CFRunLoopPerformBlock.call_args_list:
            call.args[2]()
        self.quartz.CFRunLoopPerformBlock.reset_mock()

    def test_device_bind_enables_on_the_tap_loop(self):
        self.hook.start()
        self.quartz.CGEventTapEnable.reset_mock()
        self._bind()
        post = self.quartz.CFRunLoopPerformBlock.call_args
        self.assertEqual(post.args[0], self.hook._tap_loop)
        self.quartz.CFRunLoopWakeUp.assert_called_with(self.hook._tap_loop)
        self.quartz.CGEventTapEnable.assert_not_called()
        self._run_posted_blocks()
        self.quartz.CGEventTapEnable.assert_called_once_with(self.hook._tap, True)

    def test_remote_focus_with_nothing_to_do_disables_and_stops_the_monitor(self):
        self._bind()
        self.hook.start()
        self.hook._logitech_scroll_monitor.stop.reset_mock()
        self.hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))
        self._run_posted_blocks()
        self.quartz.CGEventTapEnable.assert_called_with(self.hook._tap, False)
        self.hook._logitech_scroll_monitor.stop.assert_called_once()
        self.assertIsNone(self.hook._scroll_monitor_applied)

    def test_remote_focus_keeps_the_tap_for_the_invert_fallback(self):
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.start()
        self.hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))
        self._run_posted_blocks()
        self.quartz.CGEventTapEnable.assert_called_with(self.hook._tap, True)

    def test_last_pushed_state_wins_when_blocks_coalesce(self):
        self.hook.start()
        self._bind()
        self.hook._on_hid_disconnect()
        self._bind()
        self.quartz.CGEventTapEnable.reset_mock()
        self._run_posted_blocks()
        for call in self.quartz.CGEventTapEnable.call_args_list:
            self.assertEqual(call.args[1], True)

    def test_focus_return_re_enables(self):
        self._bind()
        self.hook.start()
        focus = {"remote": True}
        self.hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: focus["remote"]))
        self._run_posted_blocks()
        self.quartz.CGEventTapEnable.assert_called_with(self.hook._tap, False)
        focus["remote"] = False
        self.hook.sync_hook_state()
        self._run_posted_blocks()
        self.quartz.CGEventTapEnable.assert_called_with(self.hook._tap, True)

    def test_sync_before_start_is_harmless(self):
        self._bind()
        self.quartz.CFRunLoopPerformBlock.assert_not_called()
        self.quartz.CGEventTapEnable.assert_not_called()

    def test_wake_converges_to_the_predicate_instead_of_forcing_on(self):
        self.hook.start()
        with patch.object(self.hook, "sync_hook_state") as sync:
            hg = Mock()
            self.hook._hid_gesture = hg
            center = MagicMock()
            center.addObserverForName_object_queue_usingBlock_.side_effect = (
                lambda name, _o, _q, block: block
            )
            workspace = SimpleNamespace(notificationCenter=lambda: center)
            fake_appkit = SimpleNamespace(
                NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)
            )
            self.hook._register_wake_observer = self.module.MouseHook._register_wake_observer.__get__(self.hook)
            with patch.dict(sys.modules, {"AppKit": fake_appkit}), patch("builtins.print"):
                self.hook._register_wake_observer()
                self.hook._session_activate_observer(None)
        sync.assert_called_once()
        hg.force_reconnect.assert_called_once()
        self.assertEqual(self._main_tap_enables(), [False])

    def test_focus_loss_mid_capture_aborts_the_stroke(self):
        self._bind()
        self.hook._gesture_direction_enabled = True
        self.hook.start()
        self.hook._begin_gesture_capture("HID gesture")
        self.assertTrue(self.hook._gesture_active)
        with patch("builtins.print"):
            self.hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))
        self.assertFalse(self.hook._gesture_active)


class TapDisabledNotificationTests(_MacOSHookCase):
    """macOS delivers kCGEventTapDisabledByUserInput for a programmatic
    CGEventTapEnable(False) as well; re-enabling on it would undo every
    stand-down."""

    def test_unwanted_tap_is_not_put_back(self):
        hook = self._hook()
        hook._tap_wanted = False
        for kind in (self.module._kCGEventTapDisabledByUserInput,
                     self.module._kCGEventTapDisabledByTimeout):
            self.assertIs(self._fire(hook, kind), self.cg_event)
        self.quartz.CGEventTapEnable.assert_not_called()

    def test_wanted_tap_is_re_enabled(self):
        hook = self._hook()
        hook._tap_wanted = True
        with patch("builtins.print"):
            self._fire(hook, self.module._kCGEventTapDisabledByTimeout)
        self.quartz.CGEventTapEnable.assert_called_once_with(hook._tap, True)
        self.assertEqual(hook.tap_reenable_total, 1)

    def test_motion_tap_release_is_not_counted_as_a_system_disable(self):
        """Every gesture release disables the motion tap; counting that
        would trip the watchdog after ten gestures in an hour."""
        hook = self._hook()
        hook._tap_wanted = True
        hook._motion_tap = MagicMock(name="motion-tap")
        hook._gesture_anchor = None
        self.quartz.CGEventTapIsEnabled.return_value = True
        self.assertIs(self._fire(hook, self.module._kCGEventTapDisabledByUserInput), self.cg_event)
        self.quartz.CGEventTapEnable.assert_not_called()
        self.assertEqual(hook.tap_reenable_total, 0)

    def test_motion_tap_timeout_mid_gesture_is_put_back(self):
        hook = self._hook()
        hook._tap_wanted = True
        hook._motion_tap = MagicMock(name="motion-tap")
        hook._gesture_anchor = (1.0, 1.0)
        self.quartz.CGEventTapIsEnabled.return_value = True
        with patch("builtins.print"):
            self._fire(hook, self.module._kCGEventTapDisabledByTimeout)
        self.quartz.CGEventTapEnable.assert_called_once_with(hook._motion_tap, True)
        self.assertEqual(hook.tap_reenable_total, 1)


class ButtonPairingTests(_MacOSHookCase):
    def test_up_is_not_swallowed_when_its_down_reached_the_os(self):
        hook = self._hook()
        hook.block(MouseEvent.MIDDLE_UP)
        self.fields[_F_BUTTON] = 2
        self.assertIs(self._fire(hook, _OTHER_UP), self.cg_event)

    def test_up_is_swallowed_when_its_down_was(self):
        hook = self._hook()
        hook.block(MouseEvent.MIDDLE_DOWN)
        hook.block(MouseEvent.MIDDLE_UP)
        self.fields[_F_BUTTON] = 2
        self.assertIsNone(self._fire(hook, _OTHER_DOWN))
        self.assertIsNone(self._fire(hook, _OTHER_UP))
        self.assertIs(self._fire(hook, _OTHER_UP), self.cg_event)

    def test_thumb_button_pairs_too(self):
        hook = self._hook()
        hook.block(MouseEvent.THUMB_BUTTON_UP)
        self.fields[_F_BUTTON] = 6
        self.assertIs(self._fire(hook, _OTHER_UP), self.cg_event)


class _FakeNative:
    def __init__(self, *, start_ok=True):
        self.path = "/fake/libmouser_tap.dylib"
        self.start_ok = start_ok
        self.filters = []
        self.enabled_calls = []
        self.started = False
        self.stopped = False
        self.capture_delta = (0, 0)
        self.dropped = 0
        self.reenabled = 0
        self.hid_monitor_open = True
        self.events = []

    def start(self):
        self.started = True
        return self.start_ok

    def stop(self):
        self.stopped = True
        return True

    def set_enabled(self, enabled):
        self.enabled_calls.append(bool(enabled))

    @property
    def enabled(self):
        return self.enabled_calls[-1] if self.enabled_calls else False

    def set_filter(self, flags, interest, block):
        self.filters.append((flags, interest, block))

    def next_event(self, event, timeout_ms):
        if not self.events:
            time.sleep(timeout_ms / 1000.0)
            return False
        src = self.events.pop(0)
        for name, _ in event._fields_:
            setattr(event, name, getattr(src, name, 0))
        return True

    def take_capture_delta(self):
        delta, self.capture_delta = self.capture_delta, (0, 0)
        return delta


class NativeTapTests(_TapLifecycleCase):
    def setUp(self):
        super().setUp()
        self.native = _FakeNative()
        self.native_load.stop()
        self.native_load = patch.object(
            self.module.NativeTap, "load", return_value=self.native
        )
        self.native_load.start()
        self.addCleanup(self.native_load.stop)

    def test_native_tap_is_preferred_and_python_tap_never_created(self):
        self.assertTrue(self.hook.start())
        self.assertTrue(self.native.started)
        self.assertEqual(self.native.enabled_calls, [False])
        self.assertEqual(len(self.native.filters), 1)
        self.quartz.CGEventTapCreate.assert_not_called()
        self.assertIsNone(self.hook._tap_thread)
        self.assertTrue(self.hook._native_drain_thread.is_alive())

    def test_failed_native_start_falls_back_to_the_python_tap(self):
        self.native.start_ok = False
        with patch("builtins.print"):
            self.assertTrue(self.hook.start())
        self.assertIsNone(self.hook._native)
        self.assertEqual(self.quartz.CGEventTapCreate.call_count, 2)
        self.assertIsNotNone(self.hook._tap_thread)

    def test_device_bind_pushes_filter_and_enables(self):
        self.hook.start()
        self._bind()
        self.assertEqual(self.native.enabled_calls[-1], True)
        flags, _interest, _block = self.native.filters[-1]
        self.assertTrue(flags & self.module.compute_tap_filter.__globals__["FILTER_INTERCEPT"])

    def test_filter_is_computed_under_its_lock(self):
        """A drain tick computing capture=0 must not land after the HID
        thread pushed capture=1 for a stroke that just began."""
        self._bind()
        self.hook.start()
        held = []
        real = self.module.compute_tap_filter
        with patch.object(
            self.module, "compute_tap_filter",
            side_effect=lambda hook: (held.append(hook._native_filter_lock.locked()), real(hook))[1],
        ):
            self.hook._gesture_active = True
            self.hook._gesture_direction_enabled = True
            self.hook._push_native_filter()
        self.assertTrue(held and all(held))

    def test_unchanged_filter_is_not_repushed(self):
        self.hook.start()
        self._bind()
        pushes = len(self.native.filters)
        self.hook.sync_hook_state()
        self.hook.sync_hook_state()
        self.assertEqual(len(self.native.filters), pushes)

    def test_drain_thread_converges_attribute_writes(self):
        self._bind()
        self.hook.start()
        pushes = len(self.native.filters)
        self.hook.invert_vscroll = True
        time.sleep(0.15)
        self.assertGreater(len(self.native.filters), pushes)
        self.assertTrue(self.native.filters[-1][0] & 0b10)

    def test_native_reenables_feed_the_watchdog_counter(self):
        self.hook.start()
        self.native.reenabled = 3
        deadline = time.monotonic() + 2
        while self.hook.tap_reenable_total < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.hook.tap_reenable_total, 3)

    def test_stop_stops_native_and_joins_the_drain(self):
        self.hook.start()
        drain = self.hook._native_drain_thread
        self.hook.stop()
        self.assertTrue(self.native.stopped)
        self.assertIsNone(self.hook._native)
        self.assertFalse(drain.is_alive())
        self.quartz.CFRunLoopStop.assert_not_called()

    def test_queued_events_reach_the_dispatch_queue(self):
        from core.native_hook_mac import EVT_HSCROLL_LEFT, EVT_MIDDLE_DOWN, NativeTapEvent

        self._bind()
        self.hook.start()
        seen = []
        self.hook.register(MouseEvent.MIDDLE_DOWN, seen.append)
        self.hook.register(MouseEvent.HSCROLL_LEFT, seen.append)
        self.native.events.append(NativeTapEvent(event_type=_OTHER_DOWN, event_code=EVT_MIDDLE_DOWN, button=2))
        self.native.events.append(NativeTapEvent(event_type=_SCROLL, event_code=EVT_HSCROLL_LEFT, h_fixed=-98304))
        deadline = time.monotonic() + 2
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([e.event_type for e in seen], [MouseEvent.MIDDLE_DOWN, MouseEvent.HSCROLL_LEFT])
        self.assertEqual(seen[1].raw_data, 1.5)

    def test_sense_panel_events_drive_capture_and_drain_motion(self):
        from core.native_hook_mac import EVT_SENSE_PANEL_DOWN, EVT_SENSE_PANEL_UP, NativeTapEvent

        self._bind()
        self.hook._gesture_direction_enabled = True
        self.hook._gesture_threshold = 10.0
        self.hook.start()
        swipes = []
        self.hook.register(MouseEvent.GESTURE_SWIPE_RIGHT, swipes.append)
        self.hook._handle_native_event(NativeTapEvent(event_type=_OTHER_DOWN, event_code=EVT_SENSE_PANEL_DOWN, button=6))
        self.assertTrue(self.hook._gesture_active)
        self.assertTrue(self.native.filters[-1][0] & 0b10000)
        self.native.capture_delta = (400, 3)
        with patch("builtins.print"):
            self.hook._handle_native_event(NativeTapEvent(event_type=_OTHER_UP, event_code=EVT_SENSE_PANEL_UP, button=6))
        self.assertFalse(self.hook._gesture_active)
        self.assertFalse(self.native.filters[-1][0] & 0b10000)
        self.assertEqual([e.event_type for e in swipes], [MouseEvent.GESTURE_SWIPE_RIGHT])
        self.assertEqual(swipes[0].raw_data["source"], "event_tap")

    def test_debug_mirror_logs_and_dispatches_nothing(self):
        from core.native_hook_mac import NativeTapEvent

        self._bind()
        self.hook.start()
        logged = []
        self.hook.debug_mode = True
        self.hook.set_debug_callback(logged.append)
        self.hook._handle_native_event(NativeTapEvent(event_type=_OTHER_DOWN, event_code=0, button=5))
        self.hook._handle_native_event(NativeTapEvent(event_type=_SCROLL, event_code=0, v_fixed=65536, h_fixed=0))
        self.assertEqual(logged, ["OtherMouseDown btn=5", "ScrollWheel v=1.0 h=0.0"])
        self.assertTrue(self.hook._dispatch_queue.empty())
