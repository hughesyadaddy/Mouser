"""The Windows hook's use of the native WH_MOUSE_LL procedure.

Everything here is about the seam: the native side owns the procedure, but
Python still owns *what* it should swallow, when it exists, and what happens
to the events it queues. A mistake at this seam is a dead remap or a dead
gesture, which is the one outcome this work is not allowed to produce -- so
the fallback to the Python procedure is tested as hard as the native path.

``core.mouse_hook_windows`` binds ``ctypes.windll`` at import, so it is
imported through the shared helper. Only module-level Win32 declarations touch
it; none of the behaviour under test calls into the OS.
"""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mouse_hook_types import MouseEvent
from core.native_hook_filter import (
    EVT_HSCROLL_LEFT,
    EVT_MIDDLE_DOWN,
    EVT_NONE,
    EVT_XBUTTON1_DOWN,
    FILTER_CAPTURE,
    FILTER_INTERCEPT,
)
from tests.support.windows_hook_import import import_windows_hook

mouse_hook_windows = import_windows_hook()
MouseHook = mouse_hook_windows.MouseHook


class _FakeNative:
    """Stands in for a loaded mouser_hook.dll."""

    def __init__(self, *, install_result=True, uninstall_result=True):
        self.path = "fake/mouser_hook_x64.dll"
        self.install_result = install_result
        self.uninstall_result = uninstall_result
        self.installed = False
        self.filters = []
        self.inject_targets = []
        self.wheel_marks = 0
        self.pending_vscroll = 0
        self.pending_hscroll = 0
        self.dropped = 0
        self.capture_delta = (0, 0)
        self._queued = []

    def install(self):
        if self.install_result:
            self.installed = True
        return self.install_result

    def uninstall(self):
        if self.uninstall_result:
            self.installed = False
        return self.uninstall_result

    def set_filter(self, flags, interest_mask, block_mask):
        self.filters.append((flags, interest_mask, block_mask))

    def set_inject_target(self, hwnd, vscroll_msg, hscroll_msg):
        self.inject_targets.append((hwnd, vscroll_msg, hscroll_msg))

    def mark_logitech_wheel(self):
        self.wheel_marks += 1

    def take_pending_vscroll(self):
        delta, self.pending_vscroll = self.pending_vscroll, 0
        return delta

    def take_capture_delta(self):
        delta = self.capture_delta
        self.capture_delta = (0, 0)
        return delta

    def take_pending_hscroll(self):
        delta, self.pending_hscroll = self.pending_hscroll, 0
        return delta

    def next_event(self, event, timeout_ms):
        del timeout_ms
        if not self._queued:
            return False
        fields = self._queued.pop(0)
        for name, value in fields.items():
            setattr(event, name, value)
        return True

    def queue(self, **fields):
        self._queued.append(fields)


def _hook(*, native=None, bound=True):
    hook = MouseHook()
    if bound:
        hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(name="MX Master 3S", source="usb")
        )
        hook._on_hid_connect()
    if native is not None:
        hook._native = native
    return hook


def _mouse_data(hiword_value):
    """Pack a signed HIWORD the way MSLLHOOKSTRUCT.mouseData carries it."""
    return (hiword_value & 0xFFFF) << 16


class FilterPushTests(unittest.TestCase):
    def test_push_is_a_no_op_without_the_dll(self):
        hook = _hook()
        hook._push_native_filter()  # must not raise
        self.assertIsNone(hook._native_filter_state)

    def test_first_push_sends_the_current_state(self):
        native = _FakeNative()
        hook = _hook(native=native)

        hook._push_native_filter()

        self.assertEqual(len(native.filters), 1)
        flags, _interest, _block = native.filters[0]
        self.assertTrue(flags & FILTER_INTERCEPT)

    def test_unchanged_state_is_not_resent(self):
        """The drain thread pushes on every tick; without this guard that is
        twenty pointless DLL calls a second."""
        native = _FakeNative()
        hook = _hook(native=native)

        hook._push_native_filter()
        hook._push_native_filter()
        hook._push_native_filter()

        self.assertEqual(len(native.filters), 1)

    def test_a_new_binding_is_pushed(self):
        native = _FakeNative()
        hook = _hook(native=native)
        hook._push_native_filter()

        hook.block(MouseEvent.XBUTTON1_DOWN)
        hook._push_native_filter()

        self.assertEqual(len(native.filters), 2)
        _flags, interest, block = native.filters[-1]
        self.assertEqual(block, 1 << EVT_XBUTTON1_DOWN)
        self.assertTrue(interest & (1 << EVT_XBUTTON1_DOWN))

    def test_invert_toggle_reaches_the_dll_without_a_sync(self):
        """``invert_vscroll`` is a plain attribute write with no hook to hang
        a push on -- the drain tick is what makes it land."""
        native = _FakeNative()
        hook = _hook(native=native)
        hook._push_native_filter()
        pushes = len(native.filters)

        hook.invert_vscroll = True
        hook._push_native_filter()

        self.assertEqual(len(native.filters), pushes + 1)


class FilterPushConcurrencyTests(unittest.TestCase):
    """The hook thread and the drain thread both push. If the cache and the
    DLL write can be reordered against each other, the DLL ends up stranded
    on a stale filter and every later remap decision is made from it."""

    def test_the_dll_never_ends_on_a_state_older_than_the_cache(self):
        native = _FakeNative()
        hook = _hook(native=native)
        barrier = threading.Barrier(2)
        errors = []

        def flip(blocked_event):
            try:
                barrier.wait(timeout=5)
                for _ in range(200):
                    hook.block(blocked_event)
                    hook._push_native_filter()
                    hook.unblock(blocked_event)
                    hook._push_native_filter()
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=flip, args=(MouseEvent.XBUTTON1_DOWN,)),
            threading.Thread(target=flip, args=(MouseEvent.XBUTTON2_DOWN,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(native.filters[-1], hook._native_filter_state)

    def test_iterating_bindings_survives_concurrent_rebinding(self):
        """`compute_filter` used to iterate the live collections; the engine
        rewrites them from another thread whenever an app profile activates,
        and CPython raises on a set that changes size mid-iteration."""
        hook = _hook(native=_FakeNative())
        stop = threading.Event()
        errors = []

        def churn():
            while not stop.is_set():
                hook.register(MouseEvent.MIDDLE_DOWN, lambda event: None)
                hook.block(MouseEvent.MIDDLE_DOWN)
                hook.reset_bindings()

        def push():
            try:
                for _ in range(500):
                    hook._push_native_filter()
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)
            finally:
                stop.set()

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        pusher = threading.Thread(target=push)
        pusher.start()
        pusher.join(timeout=10)
        stop.set()
        churner.join(timeout=5)

        self.assertEqual(errors, [])


class InstallRoutingTests(unittest.TestCase):
    def test_install_uses_the_native_procedure(self):
        native = _FakeNative()
        hook = _hook(native=native)

        hook._install_ll_hook()

        self.assertTrue(native.installed)
        self.assertTrue(hook._native_installed)
        self.assertTrue(hook._hook_is_installed())
        self.assertIsNone(hook._hook)

    def test_installing_twice_is_idempotent(self):
        native = _FakeNative()
        hook = _hook(native=native)

        hook._install_ll_hook()
        hook._install_ll_hook()

        self.assertTrue(hook._native_installed)

    def test_a_failed_native_install_falls_back_to_python(self):
        """Losing the fast path is acceptable. Running with no hook at all is
        not -- that is remaps and gestures gone."""
        native = _FakeNative(install_result=False)
        hook = _hook(native=native)

        with patch.object(
            mouse_hook_windows, "SetWindowsHookExW", return_value=4242
        ):
            hook._install_ll_hook()

        self.assertIsNone(hook._native)
        self.assertFalse(hook._native_installed)
        self.assertEqual(hook._hook, 4242)
        self.assertTrue(hook._hook_is_installed())

    def test_uninstall_uses_the_native_procedure(self):
        native = _FakeNative()
        hook = _hook(native=native)
        hook._install_ll_hook()

        hook._uninstall_ll_hook()

        self.assertFalse(native.installed)
        self.assertFalse(hook._native_installed)
        self.assertFalse(hook._hook_is_installed())

    def test_a_wedged_uninstall_keeps_reporting_installed(self):
        """If the DLL's thread will not come down, the hook is still in the
        input path; claiming otherwise would let a second one be installed."""
        native = _FakeNative(uninstall_result=False)
        hook = _hook(native=native)
        hook._install_ll_hook()

        hook._uninstall_ll_hook()

        self.assertTrue(hook._native_installed)
        self.assertTrue(hook._hook_is_installed())

    def test_python_path_is_untouched_when_no_dll_loaded(self):
        hook = _hook()

        with patch.object(
            mouse_hook_windows, "SetWindowsHookExW", return_value=7
        ):
            hook._install_ll_hook()
        self.assertEqual(hook._hook, 7)

        with patch.object(mouse_hook_windows, "UnhookWindowsHookEx") as unhook:
            hook._uninstall_ll_hook()
        unhook.assert_called_once_with(7)
        self.assertIsNone(hook._hook)


class ApplyHookStateTests(unittest.TestCase):
    def test_filter_is_armed_before_the_hook_exists(self):
        """The procedure must know what to swallow before the first event
        reaches it, so the push has to precede the install."""
        native = _FakeNative()
        hook = _hook(native=native)
        order = []
        native.set_filter = lambda *a: order.append("filter")
        original_install = native.install

        def install():
            order.append("install")
            return original_install()

        native.install = install

        hook._apply_hook_state()

        self.assertEqual(order, ["filter", "install"])

    def test_state_converges_to_uninstalled(self):
        native = _FakeNative()
        hook = _hook(native=native)
        hook._apply_hook_state()
        self.assertTrue(hook._native_installed)

        hook._on_hid_disconnect()  # nothing bound -> nothing to swallow
        hook._apply_hook_state()

        self.assertFalse(hook._native_installed)

    def test_uninstall_is_deferred_mid_gesture(self):
        """The gesture-up must pass through the same hook that swallowed the
        down; the retry timer re-arms until the capture ends."""
        native = _FakeNative()
        hook = _hook(native=native)
        hook._apply_hook_state()
        hook._ri_hwnd = 99
        hook._gesture_active = True
        hook._on_hid_disconnect()

        with patch.object(mouse_hook_windows, "SetTimer") as set_timer:
            hook._apply_hook_state()

        set_timer.assert_called_once()
        self.assertTrue(hook._native_installed)


class FallbackApplyHookStateTests(unittest.TestCase):
    """The same state machine with no DLL loaded.

    The Python procedure is what keeps remaps and gestures alive when the
    native filter is unavailable, so its lifecycle has to be held to the same
    guarantees -- not merely assumed to still work because it used to.
    """

    def setUp(self):
        self.hook = _hook()
        self.install = patch.object(
            mouse_hook_windows, "SetWindowsHookExW", return_value=4321
        )
        self.install.start()
        self.addCleanup(self.install.stop)

    def test_state_converges_to_installed_then_uninstalled(self):
        self.hook._apply_hook_state()
        self.assertEqual(self.hook._hook, 4321)
        self.assertTrue(self.hook._hook_is_installed())

        self.hook._on_hid_disconnect()
        with patch.object(mouse_hook_windows, "UnhookWindowsHookEx"):
            self.hook._apply_hook_state()

        self.assertIsNone(self.hook._hook)
        self.assertFalse(self.hook._hook_is_installed())

    def test_uninstall_is_deferred_mid_gesture(self):
        self.hook._apply_hook_state()
        self.hook._ri_hwnd = 99
        self.hook._gesture_active = True
        self.hook._on_hid_disconnect()

        with patch.object(mouse_hook_windows, "SetTimer") as set_timer, \
                patch.object(mouse_hook_windows, "UnhookWindowsHookEx") as unhook:
            self.hook._apply_hook_state()

        set_timer.assert_called_once()
        unhook.assert_not_called()
        self.assertEqual(self.hook._hook, 4321)

    def test_pushing_a_filter_is_harmless_with_no_dll(self):
        self.hook._apply_hook_state()
        self.assertIsNone(self.hook._native_filter_state)


class GestureCaptureTests(unittest.TestCase):
    """Arming, draining and -- above all -- always disarming the capture.

    The swallow happens in C, off the GIL, so a capture nothing closes freezes
    the pointer. Every exit path is tested because there is no recovering from
    a missed one at runtime.
    """

    def _client_hook(self, native=None):
        native = native or _FakeNative()
        hook = MouseHook()
        hook._native = native
        hook._gesture_direction_enabled = True
        hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(
                name="MX Master 4", source="deskflow-shim"
            )
        )
        hook._on_hid_connect()
        return hook, native

    def _capture_armed(self, native):
        if not native.filters:
            return False
        return bool(native.filters[-1][0] & FILTER_CAPTURE)

    def test_capture_arms_as_the_stroke_opens(self):
        """Waiting for the drain tick would drop the first 50ms of the swipe --
        the part where the direction is already decided."""
        hook, native = self._client_hook()

        hook._begin_gesture_capture("HID gesture")

        self.assertTrue(self._capture_armed(native))

    def test_accumulated_motion_classifies_the_swipe(self):
        hook, native = self._client_hook()
        dispatched = []
        hook._dispatch = dispatched.append
        hook._begin_gesture_capture("HID gesture")
        native.capture_delta = (-400, 10)

        hook._end_gesture_capture("HID gesture")

        self.assertEqual(
            [event.event_type for event in dispatched],
            [MouseEvent.GESTURE_SWIPE_LEFT],
        )
        self.assertFalse(self._capture_armed(native))

    def test_a_tap_stays_a_tap(self):
        hook, native = self._client_hook()
        dispatched = []
        hook._dispatch = dispatched.append
        hook._begin_gesture_capture("HID gesture")
        native.capture_delta = (1, 1)

        hook._end_gesture_capture("HID gesture")

        self.assertEqual(
            [event.event_type for event in dispatched], [MouseEvent.GESTURE_CLICK]
        )

    def test_capture_disarms_even_when_the_stroke_raises(self):
        hook, native = self._client_hook()
        hook._begin_gesture_capture("HID gesture")

        def boom(event):
            raise RuntimeError("dispatch blew up")

        hook._dispatch = boom
        native.capture_delta = (-400, 0)

        with self.assertRaises(RuntimeError):
            hook._end_gesture_capture("HID gesture")

        self.assertFalse(
            self._capture_armed(native),
            "a raising dispatch must not leave the pointer frozen",
        )

    def test_focus_leaving_the_machine_aborts_an_open_capture(self):
        """The button-up is never coming once focus moves; without this the
        retry timer spins forever and the pointer stays frozen."""
        hook, native = self._client_hook()
        hook._begin_gesture_capture("HID gesture")
        self.assertTrue(self._capture_armed(native))

        hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))

        self.assertFalse(hook._gesture_active)
        self.assertFalse(self._capture_armed(native))

    def test_device_loss_aborts_an_open_capture(self):
        hook, native = self._client_hook()
        hook._begin_gesture_capture("HID gesture")

        hook._on_hid_disconnect()

        self.assertFalse(hook._gesture_active)
        self.assertFalse(self._capture_armed(native))

    def test_aborting_without_a_capture_is_a_no_op(self):
        hook, native = self._client_hook()
        hook._abort_gesture_capture("nothing open")
        self.assertFalse(hook._gesture_active)

    def test_a_failing_drain_does_not_strand_the_capture(self):
        hook, native = self._client_hook()
        hook._begin_gesture_capture("HID gesture")

        def boom():
            raise OSError("dll went away")

        native.take_capture_delta = boom
        hook._end_gesture_capture("HID gesture")

        self.assertFalse(self._capture_armed(native))

    def test_no_capture_arming_without_the_dll(self):
        hook = MouseHook()
        hook._gesture_direction_enabled = True
        hook._begin_gesture_capture("HID gesture")
        hook._end_gesture_capture("HID gesture")  # must not raise


class NativeEventTests(unittest.TestCase):
    def _drain(self, hook):
        events = []
        while True:
            try:
                events.append(hook._dispatch_queue.get_nowait())
            except queue.Empty:
                return events

    def test_a_button_event_is_dispatched(self):
        hook = _hook(native=_FakeNative())
        event = mouse_hook_windows.NativeHookEvent()
        event.event_code = EVT_XBUTTON1_DOWN
        event.message = mouse_hook_windows.WM_XBUTTONDOWN

        hook._handle_native_event(event)

        dispatched = self._drain(hook)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].event_type, MouseEvent.XBUTTON1_DOWN)

    def test_horizontal_scroll_carries_its_magnitude(self):
        hook = _hook(native=_FakeNative())
        event = mouse_hook_windows.NativeHookEvent()
        event.event_code = EVT_HSCROLL_LEFT
        event.message = mouse_hook_windows.WM_MOUSEHWHEEL
        event.mouse_data = _mouse_data(-120)

        hook._handle_native_event(event)

        dispatched = self._drain(hook)
        self.assertEqual(dispatched[0].event_type, MouseEvent.HSCROLL_LEFT)
        self.assertEqual(dispatched[0].raw_data, 120)

    def test_a_debug_mirror_dispatches_nothing(self):
        hook = _hook(native=_FakeNative())
        messages = []
        hook.debug_mode = True
        hook.set_debug_callback(messages.append)
        event = mouse_hook_windows.NativeHookEvent()
        event.event_code = EVT_NONE
        event.message = mouse_hook_windows.WM_MOUSEWHEEL
        event.mouse_data = _mouse_data(120)

        hook._handle_native_event(event)

        self.assertEqual(self._drain(hook), [])
        self.assertEqual(len(messages), 1)
        self.assertIn("WM_MOUSEWHEEL", messages[0])

    def test_a_raising_debug_callback_does_not_lose_the_event(self):
        hook = _hook(native=_FakeNative())
        hook.debug_mode = True
        hook.set_debug_callback(lambda message: (_ for _ in ()).throw(RuntimeError))
        event = mouse_hook_windows.NativeHookEvent()
        event.event_code = EVT_MIDDLE_DOWN
        event.message = mouse_hook_windows.WM_MBUTTONDOWN

        hook._handle_native_event(event)

        self.assertEqual(len(self._drain(hook)), 1)


class DrainWorkerTests(unittest.TestCase):
    def test_worker_dispatches_and_converges_the_filter(self):
        native = _FakeNative()
        hook = _hook(native=native)
        native.queue(
            event_code=EVT_XBUTTON1_DOWN,
            message=mouse_hook_windows.WM_XBUTTONDOWN,
        )
        hook._running = True

        original = native.next_event

        def next_event(event, timeout_ms):
            got = original(event, timeout_ms)
            if not got:
                hook._running = False
            return got

        native.next_event = next_event

        hook._native_drain_worker()

        self.assertEqual(
            hook._dispatch_queue.get_nowait().event_type,
            MouseEvent.XBUTTON1_DOWN,
        )
        self.assertTrue(native.filters, "drain tick never pushed the filter")

    def test_a_failing_filter_push_does_not_kill_the_worker(self):
        """A dead drain thread is every remapped button silently gone, so a
        push that raises has to degrade rather than end the loop."""
        native = _FakeNative()
        hook = _hook(native=native)
        native.queue(
            event_code=EVT_XBUTTON1_DOWN,
            message=mouse_hook_windows.WM_XBUTTONDOWN,
        )
        hook._running = True
        pushes = []

        def boom(*_args):
            pushes.append(1)
            if len(pushes) >= 3:
                hook._running = False
            raise OSError("set_filter blew up")

        native.set_filter = boom

        hook._native_drain_worker()

        self.assertGreaterEqual(len(pushes), 3)
        self.assertEqual(
            hook._dispatch_queue.get_nowait().event_type,
            MouseEvent.XBUTTON1_DOWN,
        )

    def test_a_ring_overflow_is_reported(self):
        native = _FakeNative()
        hook = _hook(native=native)
        native.dropped = 4
        hook._running = True
        original = native.next_event

        def next_event(event, timeout_ms):
            hook._running = False
            return original(event, timeout_ms)

        native.next_event = next_event

        with patch("builtins.print") as printed:
            hook._native_drain_worker()

        self.assertTrue(
            any("dropped 4" in str(call) for call in printed.call_args_list),
            f"overflow never reported: {printed.call_args_list}",
        )

    def test_worker_exits_on_a_native_error(self):
        native = _FakeNative()
        hook = _hook(native=native)
        hook._running = True

        def boom(event, timeout_ms):
            raise OSError("dll went away")

        native.next_event = boom

        hook._native_drain_worker()  # returns instead of spinning


class ScrollInjectionTests(unittest.TestCase):
    def test_vertical_injection_takes_the_native_delta(self):
        native = _FakeNative()
        native.pending_vscroll = -120
        hook = _hook(native=native)

        with patch.object(mouse_hook_windows, "_inject_scroll_impl") as inject:
            hook._ri_wndproc(0, mouse_hook_windows.WM_APP_INJECT_VSCROLL, 0, 0)

        inject.assert_called_once_with(mouse_hook_windows.MOUSEEVENTF_WHEEL, -120)
        self.assertEqual(native.pending_vscroll, 0)

    def test_horizontal_injection_takes_the_native_delta(self):
        native = _FakeNative()
        native.pending_hscroll = 240
        hook = _hook(native=native)

        with patch.object(mouse_hook_windows, "_inject_scroll_impl") as inject:
            hook._ri_wndproc(0, mouse_hook_windows.WM_APP_INJECT_HSCROLL, 0, 0)

        inject.assert_called_once_with(mouse_hook_windows.MOUSEEVENTF_HWHEEL, 240)

    def test_a_zero_delta_injects_nothing(self):
        hook = _hook(native=_FakeNative())

        with patch.object(mouse_hook_windows, "_inject_scroll_impl") as inject:
            hook._ri_wndproc(0, mouse_hook_windows.WM_APP_INJECT_VSCROLL, 0, 0)

        inject.assert_not_called()

    def test_python_path_still_uses_its_own_pending_counters(self):
        hook = _hook()
        hook._pending_vscroll = -360
        hook._vscroll_posted = True

        with patch.object(mouse_hook_windows, "_inject_scroll_impl") as inject:
            hook._ri_wndproc(0, mouse_hook_windows.WM_APP_INJECT_VSCROLL, 0, 0)

        inject.assert_called_once_with(mouse_hook_windows.MOUSEEVENTF_WHEEL, -360)
        self.assertEqual(hook._pending_vscroll, 0)
        self.assertFalse(hook._vscroll_posted)


class LoadNativeFilterTests(unittest.TestCase):
    def test_a_missing_dll_leaves_the_python_procedure_in_charge(self):
        hook = _hook()
        with patch.object(
            mouse_hook_windows.NativeHookFilter, "load", return_value=None
        ):
            hook._load_native_filter()
        self.assertIsNone(hook._native)

    def test_loading_hands_over_the_injection_window(self):
        native = _FakeNative()
        hook = _hook()
        hook._ri_hwnd = 0xBEEF

        with patch.object(
            mouse_hook_windows.NativeHookFilter, "load", return_value=native
        ):
            hook._load_native_filter()

        self.assertIs(hook._native, native)
        self.assertEqual(
            native.inject_targets,
            [
                (
                    0xBEEF,
                    mouse_hook_windows.WM_APP_INJECT_VSCROLL,
                    mouse_hook_windows.WM_APP_INJECT_HSCROLL,
                )
            ],
        )


class LogitechWheelMarkTests(unittest.TestCase):
    def test_a_logitech_wheel_report_marks_the_native_side(self):
        """The procedure runs on its own thread and cannot read this one's
        raw-input queue, so this mark is its whole attribution."""
        native = _FakeNative()
        hook = _hook(native=native)
        hook._device_name_cache[7] = "\\\\?\\HID#VID_046D&PID_C548"

        header = SimpleNamespace(
            dwType=mouse_hook_windows.RIM_TYPEMOUSE, hDevice=7
        )
        mouse = SimpleNamespace(
            usButtonFlags=mouse_hook_windows.RI_MOUSE_WHEEL,
            lLastX=0,
            lLastY=0,
            ulRawButtons=0,
        )

        with patch.object(
            mouse_hook_windows, "GetRawInputData", side_effect=self._sized
        ), patch.object(
            mouse_hook_windows.RAWINPUTHEADER, "from_buffer_copy", return_value=header
        ), patch.object(
            mouse_hook_windows.RAWMOUSE, "from_buffer_copy", return_value=mouse
        ), patch.object(hook, "_check_raw_mouse_gesture"):
            hook._process_raw_input(0)

        self.assertEqual(native.wheel_marks, 1)

    @staticmethod
    def _sized(_lparam, _cmd, _buffer, size, _header_size):
        # Both the sizing call and the fetch report the same packet size.
        size._obj.value = 64
        return 64


if __name__ == "__main__":
    unittest.main()
