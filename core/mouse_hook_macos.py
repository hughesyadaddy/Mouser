"""
macOS mouse hook implementation.
"""

import functools
import queue
import sys
import threading
import time

from core.mouse_hook_base import BaseMouseHook, HidGestureListener
from core.mouse_hook_types import MouseEvent

try:
    import objc
except ImportError as exc:
    raise ImportError(
        "PyObjC is required on macOS. Run "
        "`python -m pip install -r requirements.txt`."
    ) from exc

try:
    import Quartz

    _QUARTZ_OK = True
except ImportError:
    _QUARTZ_OK = False
    print(
        "[MouseHook] pyobjc-framework-Quartz not installed -- "
        "pip install pyobjc-framework-Quartz"
    )


def _autoreleased(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with objc.autorelease_pool():
            return fn(*args, **kwargs)
    return wrapper


_BTN_MIDDLE = 2
_BTN_BACK = 3
_BTN_FORWARD = 4
# MX Master 4 Sense Panel ("Action Ring" in Logi Options+) arrives as
# btn=6 at the OS level (kCGEventOtherMouseDown). With HID++ divert
# disabled, the gesture path falls back to the event-tap source, so
# btn=6 drives _begin_gesture_capture / _end_gesture_capture below.
_BTN_OS_EXTRA = 6
_INJECTED_EVENT_MARKER = 0x4D4F5554
# 'DSKF': set by the Deskflow client on the events it posts for a remote
# seat, so a receiving Mac's tap passes them through untouched.
_DESKFLOW_INJECTED_EVENT_MARKER = 0x44534B46
_INJECTED_EVENT_MARKERS = frozenset(
    {_INJECTED_EVENT_MARKER, _DESKFLOW_INJECTED_EVENT_MARKER}
)
# CGEvent integer-value-field id for kCGScrollWheelEventIsContinuous. Some
# Quartz versions surface the symbolic constant (``Quartz.kCGScrollWheelEventIsContinuous``),
# others do not -- we cache the integer here so the event-tap path does not
# carry a naked magic number, and we still fall back to the symbol when the
# binding is available so future SDK renumbering picks up automatically.
_CG_SCROLL_FIELD_IS_CONTINUOUS = getattr(
    Quartz if _QUARTZ_OK else object(),
    "kCGScrollWheelEventIsContinuous",
    88,
)
_CG_SCROLL_FIELD_MOMENTUM_PHASE = getattr(
    Quartz if _QUARTZ_OK else object(),
    "kCGScrollWheelEventMomentumPhase",
    123,
)
_CG_SCROLL_FIELD_SCROLL_PHASE = getattr(
    Quartz if _QUARTZ_OK else object(),
    "kCGScrollWheelEventScrollPhase",
    99,
)
_CG_SCROLL_PHASE_NONE = getattr(
    Quartz if _QUARTZ_OK else object(),
    "kCGScrollPhaseNone",
    0,
)
_CG_SCROLL_PHASE_ENDED = getattr(
    Quartz if _QUARTZ_OK else object(),
    "kCGScrollPhaseEnded",
    4,
)
_kCGEventTapDisabledByTimeout = 0xFFFFFFFE
_kCGEventTapDisabledByUserInput = 0xFFFFFFFF

from core.macos_iokit_scroll import LogitechScrollMonitor, SCROLL_MONITOR_AVAILABLE
from core.native_hook_mac import (
    EVT_NONE,
    EVT_SENSE_PANEL_DOWN,
    EVT_SENSE_PANEL_UP,
    TAP_EVENT_NAMES,
    NativeTap,
    NativeTapEvent,
    compute_tap_filter,
    describe_tap_filter,
)

#: How long stop() waits for the tap thread. Must stay above the dylib's
#: TAP_STOP_WAIT_MS so Python never abandons a thread still tearing down.
TAP_THREAD_JOIN_S = 3.0
NATIVE_DRAIN_TIMEOUT_MS = 50


class _QuartzBindings:
    """Quartz functions and field ids resolved once, not per event.

    The CGEventTap callback runs on the main run loop at up to 1 kHz. Every
    ``Quartz.<name>`` inside it is a module-dict lookup (through PyObjC's
    lazy loader on first touch), and every ``CGEventGet*`` is an ObjC bridge
    crossing. Resolving the names once per Quartz module object keeps the
    per-event cost to the crossings the event type actually needs.

    Bound to the module object so a test that swaps ``mouse_hook_macos.Quartz``
    for a mock after import still gets a matching binding (see
    :meth:`MouseHook._quartz`).
    """

    __slots__ = (
        "module",
        "get_int",
        "set_int",
        "get_location",
        "warp",
        "associate",
        "f_user_data",
        "f_button",
        "f_dx",
        "f_dy",
        "f_h_fixed",
        "f_v_fixed",
        "t_moved",
        "t_dragged",
        "t_other_down",
        "t_other_up",
        "t_scroll",
        "negate_fields",
    )

    def __init__(self, module):
        self.module = module
        self.get_int = module.CGEventGetIntegerValueField
        self.set_int = module.CGEventSetIntegerValueField
        self.get_location = module.CGEventGetLocation
        self.warp = module.CGWarpMouseCursorPosition
        self.associate = module.CGAssociateMouseAndMouseCursorPosition
        self.f_user_data = module.kCGEventSourceUserData
        self.f_button = module.kCGMouseEventButtonNumber
        self.f_dx = module.kCGMouseEventDeltaX
        self.f_dy = module.kCGMouseEventDeltaY
        self.f_h_fixed = module.kCGScrollWheelEventFixedPtDeltaAxis2
        self.f_v_fixed = module.kCGScrollWheelEventFixedPtDeltaAxis1
        self.t_moved = module.kCGEventMouseMoved
        self.t_dragged = module.kCGEventOtherMouseDragged
        self.t_other_down = module.kCGEventOtherMouseDown
        self.t_other_up = module.kCGEventOtherMouseUp
        self.t_scroll = module.kCGEventScrollWheel
        self.negate_fields = {
            axis: tuple(
                field
                for field in (
                    getattr(module, f"kCGScrollWheelEventDeltaAxis{axis}", None),
                    getattr(module, f"kCGScrollWheelEventFixedPtDeltaAxis{axis}", None),
                    getattr(module, f"kCGScrollWheelEventPointDeltaAxis{axis}", None),
                )
                if field is not None
            )
            for axis in (1, 2)
        }


class MouseHook(BaseMouseHook):
    """
    Uses CGEventTap on macOS to intercept mouse button presses and scroll
    events. Requires Accessibility permission.
    """

    _UP_TO_DOWN_EVENT = {
        **BaseMouseHook._UP_TO_DOWN_EVENT,
        MouseEvent.THUMB_BUTTON_UP: MouseEvent.THUMB_BUTTON_DOWN,
    }

    def __init__(self):
        super().__init__()
        self._running = False
        self._tap = None
        self._tap_source = None
        # Pointer motion gets its own tap, enabled only while a directional
        # gesture is armed: at up to 1 kHz it was the bulk of the Python
        # entries on the main run loop and did nothing outside a capture.
        self._motion_tap = None
        self._motion_tap_source = None
        # The tap runs on its own thread's CFRunLoop (Python callback and
        # native dylib alike); stop()/sync target the captured loop.
        self._tap_loop = None
        self._tap_thread = None
        self._tap_wanted = False
        self._tap_lock = threading.Lock()
        # Native CGEventTap callback (native/mac/mouser_tap.m); None means
        # the Python callback is in the input path.
        self._native = None
        self._native_drain_thread = None
        self._native_filter_state = None
        self._native_filter_lock = threading.Lock()
        self.ignore_trackpad = True
        self._wake_observer = None
        self._screens_wake_observer = None
        self._session_resign_observer = None
        self._session_activate_observer = None
        self._last_resume_at = 0.0
        self._init_dispatch_queue(maxsize=512)
        self._dispatch_thread = None
        self._first_event_logged = False
        # Lifetime count of kCGEventTapDisabledBy* re-enables; sampled by
        # the self-check watchdog (macOS kills the tap when the callback
        # stalls, so a climbing count means the main thread is starved).
        self.tap_reenable_total = 0
        # Cursor anchoring for the event-tap gesture path (devices that can't
        # stream HID++ rawXY, e.g. the original MX Master). _last_cursor_pos
        # tracks the pre-gesture pointer location; on a gesture press we pin
        # the cursor there so detection motion never reaches the screen and
        # the BLE-notification latency can't leak a visible drift.
        self._last_cursor_pos = None
        self._gesture_anchor = None
        self._logitech_scroll_monitor = LogitechScrollMonitor()
        # Last monitor state this hook applied (True = start() was called,
        # False = stop() was called, None = unknown / must re-apply). The
        # scroll path syncs on every wheel event, so start()/stop() must
        # only run when the *wanted* state differs from this.
        self._scroll_monitor_applied = None
        # Per-event cache of the scroll attribution fields, so the vertical
        # and horizontal invert fallbacks (which both consult
        # ``_scroll_event_targets_logitech``) share one set of bridge reads.
        # Set and cleared inside the tap callback only.
        self._scroll_prefetch = None
        self._qb = None
        # Single in-flight resume-recovery worker; see _start_resume_recovery.
        self._resume_thread = None

    def _quartz(self):
        """Return the Quartz bindings for the *current* module object.

        One global lookup and one identity compare per event; rebinding
        only happens when ``mouse_hook_macos.Quartz`` is replaced (tests
        install a MagicMock per test case).
        """
        module = globals().get("Quartz")
        bindings = self._qb
        if bindings is None or bindings.module is not module:
            bindings = self._qb = _QuartzBindings(module)
        return bindings

    def _on_hid_connect(self):
        super()._on_hid_connect()
        # IOHID monitor start/stop runs on the event-tap run loop; the next
        # ScrollWheel event calls ``_sync_logitech_scroll_monitor``. Forget
        # the applied state so a monitor whose start() failed last time gets
        # exactly one retry per device arrival, not one per wheel tick.
        self._scroll_monitor_applied = None

    def _on_hid_disconnect(self):
        super()._on_hid_disconnect()
        # Same run-loop constraint as connect: sync on the next scroll event.

    def _warp_cursor(self, pos) -> None:
        """Pin the hardware cursor to ``pos`` without synthesizing an event.

        CGWarpMouseCursorPosition moves the pointer silently (unlike
        CGEventPost), so this never feeds back into the event tap. The
        paired CGAssociateMouseAndMouseCursorPosition(True) defeats the
        ~250 ms post-warp delta-suppression window macOS would otherwise
        impose, keeping hardware tracking responsive the instant the
        gesture releases."""
        if not _QUARTZ_OK or pos is None:
            return
        try:
            qb = self._quartz()
            qb.warp(pos)
            qb.associate(True)
        except Exception as exc:  # noqa: BLE001 - Quartz boundary
            self._emit_debug(f"warp cursor failed: {exc!r}")

    def _negate_scroll_axis(self, cg_event, axis: int) -> None:
        """In-place flip of Delta/FixedPtDelta/PointDelta on ``axis``
        (1 = vertical, 2 = horizontal). Modifying the original event
        preserves unit type, phase, and source identity for downstream
        consumers (VMs, remote desktops, games)."""
        if axis not in (1, 2):
            raise ValueError(f"axis must be 1 (vertical) or 2 (horizontal), got {axis!r}")
        qb = self._quartz()
        get_int = qb.get_int
        set_int = qb.set_int
        for field in qb.negate_fields[axis]:
            value = get_int(cg_event, field)
            if value:
                set_int(cg_event, field, -value)

    def _scroll_event_targets_logitech(
        self,
        *,
        cg_event=None,
        wParam=None,
        lParam=None,
        linux_evdev=False,
    ) -> bool:
        if linux_evdev:
            return True
        if cg_event is None:
            return False
        if not SCROLL_MONITOR_AVAILABLE:
            # Fail closed: without IOHID wheel attribution we cannot prove the
            # scroll came from a physical Logitech (firmware invert still works).
            return False
        get_int = self._quartz().get_int
        # The tap callback prefetches the fields for the event it is
        # handling; the vertical and horizontal fallbacks then share them
        # instead of each crossing the bridge three times.
        pre = self._scroll_prefetch
        if pre is None or pre[0] is not cg_event:
            pre = None
        try:
            if self.ignore_trackpad:
                is_continuous = (
                    pre[1] if pre is not None and pre[1] is not None
                    else get_int(cg_event, _CG_SCROLL_FIELD_IS_CONTINUOUS)
                )
                if pre is not None:
                    pre[1] = is_continuous
                if is_continuous:
                    return False
            momentum = pre[2] if pre is not None else None
            if momentum is None:
                momentum = get_int(cg_event, _CG_SCROLL_FIELD_MOMENTUM_PHASE)
                if pre is not None:
                    pre[2] = momentum
            if momentum:
                return False
            phase = pre[3] if pre is not None else None
            if phase is None:
                phase = get_int(cg_event, _CG_SCROLL_FIELD_SCROLL_PHASE)
                if pre is not None:
                    pre[3] = phase
            if phase not in (_CG_SCROLL_PHASE_NONE, _CG_SCROLL_PHASE_ENDED):
                return False
        except Exception as exc:  # noqa: BLE001 - Quartz boundary
            self._emit_debug(f"scroll attribution check failed: {exc!r}")
            return False
        return self._logitech_scroll_monitor.recent_wheel()

    def _sync_logitech_scroll_monitor(self) -> None:
        """Start/stop the IOHID wheel tap on the event-tap run loop.

        Called on every ScrollWheel event, so start()/stop() only run when
        the wanted state changes (device bound / unbound). ``None`` in
        ``_scroll_monitor_applied`` forces one re-apply, which is how a
        device arrival -- or a failed start() -- retries the monitor."""
        if not SCROLL_MONITOR_AVAILABLE:
            return
        wanted = bool(self._physical_logitech_bound())
        if wanted is self._scroll_monitor_applied:
            return
        self._scroll_monitor_applied = wanted
        if wanted:
            self._logitech_scroll_monitor.start()
            if not getattr(self._logitech_scroll_monitor, "running", True):
                # start() failed: keep re-applying on subsequent wheel events
                # until it comes up. This is cheap by contract: the monitor
                # negative-caches failures itself (permission denials for
                # PERMISSION_RETRY_S, anything else for FAILURE_RETRY_S), so
                # a start() inside that window is a no-op rather than a
                # fresh IOKit attempt per wheel tick.
                self._scroll_monitor_applied = None
        else:
            self._logitech_scroll_monitor.stop()

    def _dispatch_worker(self):
        while self._running:
            try:
                event = self._dispatch_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            # Action execution downstream of _dispatch creates Quartz
            # CGEvent / NSEvent objects (key_simulator). This worker runs on
            # its own thread, which has no NSAutoreleasePool, so without an
            # explicit pool every autoreleased Foundation temporary produced
            # while injecting a keystroke or mouse click leaks for the process
            # lifetime -- the memory-growth-per-click reported in #233. The
            # CGEventTap callback is already wrapped (@_autoreleased); this
            # covers the second thread that touches Foundation objects.
            with objc.autorelease_pool():
                self._dispatch(event)

    @_autoreleased
    def _event_tap_callback(self, proxy, event_type, cg_event, refcon):
        # The CGEventTap continues to fire briefly after ``stop()`` sets
        # ``_running = False`` -- macOS does not synchronously drain
        # in-flight callbacks before disabling the tap. Drop the event
        # untouched so we never enqueue into a torn-down dispatch worker,
        # mutate shared state, or apply scroll inversion after the device
        # connection has already been released.
        if not self._running:
            return cg_event
        try:
            if event_type in (
                _kCGEventTapDisabledByTimeout,
                _kCGEventTapDisabledByUserInput,
            ):
                # Also delivered for our own CGEventTapEnable(False).
                if not self._tap_wanted:
                    return cg_event
                self.tap_reenable_total += 1
                print(
                    f"[MouseHook] CGEventTap disabled by system "
                    f"(type=0x{event_type:X}), re-enabling "
                    f"(total={self.tap_reenable_total})",
                    flush=True,
                )
                Quartz.CGEventTapEnable(self._tap, True)
                if self._motion_tap is not None and self._gesture_anchor is not None:
                    Quartz.CGEventTapEnable(self._motion_tap, True)
                return cg_event

            if not self._first_event_logged:
                self._first_event_logged = True
                print("[MouseHook] CGEventTap: first event received", flush=True)

            qb = self._quartz()
            get_int = qb.get_int

            # ---- Pointer motion: the 1 kHz path. --------------------------
            # Mouser never injects MouseMoved / OtherMouseDragged (only
            # button and wheel events carry _INJECTED_EVENT_MARKER, see
            # key_simulator._inject_mac_mouse / _inject_mac_scroll), so the
            # marker read is skipped here. The only consumer of motion is the
            # gesture engine: _arm_gesture_anchor reads _last_cursor_pos, and
            # only while _gesture_direction_enabled; the move branch below
            # needs the deltas only while a directional capture is live.
            # Anything else is a zero-crossing pass-through.
            if event_type == qb.t_moved or event_type == qb.t_dragged:
                if not self._gesture_direction_enabled:
                    # Drop any anchor captured before the feature was
                    # switched off so a later re-enable cannot warp the
                    # pointer to a stale location.
                    self._last_cursor_pos = None
                    return cg_event
                if not self._should_intercept_events():
                    return cg_event
                if not self._gesture_active:
                    # Remember where the pointer is whenever a gesture is
                    # *not* in progress, so a gesture press has a clean
                    # anchor to pin back to.
                    try:
                        self._last_cursor_pos = qb.get_location(cg_event)
                    except Exception as exc:  # noqa: BLE001 - Quartz boundary
                        self._emit_debug(f"cursor location read failed: {exc!r}")
                    return cg_event

                # Directional capture live: fetch each delta exactly once.
                dx = get_int(cg_event, qb.f_dx)
                dy = get_int(cg_event, qb.f_dy)
                if self.debug_mode:
                    self._emit_debug(
                        f"Gesture move event type={int(event_type)} dx={dx} dy={dy}"
                    )
                    self._emit_gesture_event(
                        {
                            "type": "move",
                            "source": "event_tap",
                            "dx": dx,
                            "dy": dy,
                        }
                    )
                if self._gesture_input_source == "hid_rawxy":
                    # Dropping the event stops the pointer; re-pin as well so
                    # drift from before the capture engaged cannot persist.
                    if self._gesture_anchor is not None:
                        self._warp_cursor(self._gesture_anchor)
                    return None
                self._accumulate_gesture_delta(dx, dy, "event_tap")
                # Dropping the event (return None) already stops the pointer,
                # but pin it to the anchor too so any event racing past the
                # active-flag flip can't nudge the cursor mid-stroke.
                if self._gesture_anchor is not None:
                    self._warp_cursor(self._gesture_anchor)
                return None

            # ---- Buttons and wheel: one marker read, then per-type work. --
            try:
                if get_int(cg_event, qb.f_user_data) in _INJECTED_EVENT_MARKERS:
                    return cg_event
            except Exception as exc:  # noqa: BLE001 - Quartz boundary
                # Surface failures so a borked Quartz binding cannot make
                # the injected-event marker silently misfire on every
                # event for the rest of the session.
                self._emit_debug(
                    f"CGEventGetIntegerValueField(kCGEventSourceUserData) failed: {exc!r}"
                )

            # KVM / cold-start guard: when no Logitech is currently bound to
            # this host, the CGEventTap must be a complete pass-through. The
            # tap sees events from every mouse the OS knows about, so without
            # this guard a trackpad swipe or a generic USB mouse's xbutton
            # click would get routed through Mouser's remap pipeline -- the
            # exact failure mode users hit when their KVM switches the
            # Logitech to another machine while Mouser keeps running on
            # this one.
            if not self._should_intercept_events():
                if event_type == qb.t_scroll:
                    self._sync_logitech_scroll_monitor()
                    self._apply_scroll_invert_fallbacks(cg_event)
                return cg_event

            mouse_event = None
            should_block = False

            if event_type == qb.t_other_down:
                btn = get_int(cg_event, qb.f_button)
                if self.debug_mode and self._debug_callback:
                    try:
                        self._debug_callback(f"OtherMouseDown btn={btn}")
                    except Exception:
                        pass
                if btn == _BTN_MIDDLE:
                    mouse_event = MouseEvent(MouseEvent.MIDDLE_DOWN)
                    should_block = MouseEvent.MIDDLE_DOWN in self._blocked_events
                elif btn == _BTN_BACK:
                    mouse_event = MouseEvent(MouseEvent.XBUTTON1_DOWN)
                    should_block = MouseEvent.XBUTTON1_DOWN in self._blocked_events
                elif btn == _BTN_FORWARD:
                    mouse_event = MouseEvent(MouseEvent.XBUTTON2_DOWN)
                    should_block = MouseEvent.XBUTTON2_DOWN in self._blocked_events
                elif btn == _BTN_OS_EXTRA:
                    if self._gesture_via_sense_panel:
                        # Fallback path: 0x01a0 divert was rejected, so
                        # btn=6 (Sense Panel) drives swipe detection via
                        # the event_tap source. Swallow the click.
                        self._arm_gesture_anchor()
                        self._begin_gesture_capture("Sense panel gesture")
                        return None
                    if self._thumb_button_via_hid:
                        # The small Thumb button (CID 0x00c3) is being
                        # diverted over HID++ on this device, so any btn=6
                        # leaking through is the Sense Panel; suppress it.
                        return None
                    mouse_event = MouseEvent(MouseEvent.THUMB_BUTTON_DOWN)
                    should_block = MouseEvent.THUMB_BUTTON_DOWN in self._blocked_events

            elif event_type == qb.t_other_up:
                btn = get_int(cg_event, qb.f_button)
                if self.debug_mode and self._debug_callback:
                    try:
                        self._debug_callback(f"OtherMouseUp btn={btn}")
                    except Exception:
                        pass
                if btn == _BTN_MIDDLE:
                    mouse_event = MouseEvent(MouseEvent.MIDDLE_UP)
                    should_block = MouseEvent.MIDDLE_UP in self._blocked_events
                elif btn == _BTN_BACK:
                    mouse_event = MouseEvent(MouseEvent.XBUTTON1_UP)
                    should_block = MouseEvent.XBUTTON1_UP in self._blocked_events
                elif btn == _BTN_FORWARD:
                    mouse_event = MouseEvent(MouseEvent.XBUTTON2_UP)
                    should_block = MouseEvent.XBUTTON2_UP in self._blocked_events
                elif btn == _BTN_OS_EXTRA:
                    if self._gesture_via_sense_panel:
                        self._end_gesture_capture("Sense panel gesture")
                        self._release_gesture_anchor()
                        return None
                    if self._thumb_button_via_hid:
                        return None
                    mouse_event = MouseEvent(MouseEvent.THUMB_BUTTON_UP)
                    should_block = MouseEvent.THUMB_BUTTON_UP in self._blocked_events

            elif event_type == qb.t_scroll:
                self._sync_logitech_scroll_monitor()
                # Injected (Mouser-posted) wheel events already returned at
                # the marker check above.
                is_continuous = get_int(cg_event, _CG_SCROLL_FIELD_IS_CONTINUOUS)
                if self.ignore_trackpad and is_continuous:
                    return cg_event
                h_delta = get_int(cg_event, qb.f_h_fixed) / 65536.0
                if self.debug_mode and self._debug_callback:
                    try:
                        v_delta = get_int(cg_event, qb.f_v_fixed) / 65536.0
                        self._debug_callback(f"ScrollWheel v={v_delta} h={h_delta}")
                    except Exception:
                        pass
                if h_delta != 0:
                    if h_delta > 0:
                        mouse_event = MouseEvent(MouseEvent.HSCROLL_RIGHT, abs(h_delta))
                        should_block = MouseEvent.HSCROLL_RIGHT in self._blocked_events
                    else:
                        mouse_event = MouseEvent(MouseEvent.HSCROLL_LEFT, abs(h_delta))
                        should_block = MouseEvent.HSCROLL_LEFT in self._blocked_events
                if mouse_event:
                    self._enqueue_dispatch_event(mouse_event)
                    mouse_event = None
                if should_block:
                    return None
                # In-place sign flip on the original event so downstream
                # consumers see unit type / phase preserved. Gated on a
                # Logitech device being connected: the toggle is meant for
                # Logitech scroll, not for inverting every trackpad and
                # generic USB mouse the OS hands us. Also skipped when the
                # firmware already inverted at the source.
                self._apply_scroll_invert_fallbacks(
                    cg_event, is_continuous=is_continuous
                )

            if mouse_event:
                # The tap can be disabled between a press and its release
                # (focus flip, device unbind); never swallow an UP whose
                # DOWN reached the OS.
                should_block = self._pair_blocked_updown(
                    mouse_event.event_type, should_block
                )
                self._enqueue_dispatch_event(mouse_event)

            if should_block:
                return None
            return cg_event

        except Exception as exc:
            print(f"[MouseHook] event tap callback error: {exc}")
            return cg_event

    def _apply_scroll_invert_fallbacks(self, cg_event, *, is_continuous=None):
        """Run the vertical then horizontal OS-layer invert fallbacks for one
        wheel event, sharing the attribution field reads between them.

        ``is_continuous`` is the already-fetched kCGScrollWheelEventIsContinuous
        value when the caller read it; ``None`` means not read yet. The cache
        lives only for the duration of this call: CGEvent proxies can be
        re-allocated at the same address, so it must never outlive the event.
        """
        self._scroll_prefetch = [cg_event, is_continuous, None, None]
        try:
            if self._apply_vscroll_invert_fallback(cg_event=cg_event):
                self._negate_scroll_axis(cg_event, 1)
            if self._apply_hscroll_invert_fallback(cg_event=cg_event):
                self._negate_scroll_axis(cg_event, 2)
        finally:
            self._scroll_prefetch = None

    def _on_hid_gesture_down(self):
        # MX4 routing: when the Sense Panel is the gesture source for this
        # device, the small HID++ "gesture" button (CID 0x00c3) is the
        # Thumb button, not the gesture trigger.
        if self._gesture_via_sense_panel:
            self._emit_debug("HID thumb button down")
            self._dispatch(MouseEvent(MouseEvent.THUMB_BUTTON_DOWN))
            return
        self._arm_gesture_anchor()
        self._begin_gesture_capture("HID gesture")

    def _on_hid_gesture_up(self):
        if self._gesture_via_sense_panel:
            self._emit_debug("HID thumb button up")
            self._dispatch(MouseEvent(MouseEvent.THUMB_BUTTON_UP))
            return
        self._end_gesture_capture("HID gesture")
        self._release_gesture_anchor()

    def _arm_gesture_anchor(self):
        """Pin the cursor at the moment the gesture button goes down.

        This used to no-op whenever the device streamed rawXY, on the premise
        that the firmware already held the pointer still. That premise only
        covers the Sense Panel's own touch coordinates. Moving the mouse BODY
        still drives the pointer, and nothing pins it during the window between
        the physical press and _gesture_active flipping -- so grabbing the pad
        mid-movement let the cursor coast before the gesture took hold. Pinning
        here is safe on the rawXY path too: panel motion never reaches the tap
        (it returns early) and body motion is dropped once the capture is live,
        so there is no feed to fight -- only the pre-capture drift to undo.
        """
        if not self._gesture_direction_enabled:
            self._gesture_anchor = None
            return
        self._gesture_anchor = self._current_cursor_pos()
        if self._gesture_anchor is not None:
            self._warp_cursor(self._gesture_anchor)
        self._set_motion_tap_enabled(True)

    def _release_gesture_anchor(self):
        self._set_motion_tap_enabled(False)
        if self._gesture_anchor is not None:
            self._warp_cursor(self._gesture_anchor)
            self._gesture_anchor = None

    def _current_cursor_pos(self):
        """Pointer location at the moment of the press. The motion tap is
        off between gestures, so this cannot come from a tracked move."""
        if not _QUARTZ_OK:
            return self._last_cursor_pos
        try:
            return self._quartz().get_location(Quartz.CGEventCreate(None))
        except Exception as exc:  # noqa: BLE001 - Quartz boundary
            self._emit_debug(f"cursor location read failed: {exc!r}")
            return self._last_cursor_pos

    def _set_motion_tap_enabled(self, enabled: bool) -> None:
        tap = self._motion_tap
        if tap is None or not self._running:
            return
        try:
            Quartz.CGEventTapEnable(tap, bool(enabled))
        except Exception as exc:  # noqa: BLE001 - Quartz boundary
            self._emit_debug(f"motion tap enable({enabled}) failed: {exc!r}")

    def _on_hid_mode_shift_down(self):
        self._emit_debug("HID mode shift button down")
        self._dispatch(MouseEvent(MouseEvent.MODE_SHIFT_DOWN))

    def _on_hid_mode_shift_up(self):
        self._emit_debug("HID mode shift button up")
        self._dispatch(MouseEvent(MouseEvent.MODE_SHIFT_UP))

    def _on_hid_dpi_switch_down(self):
        self._emit_debug("HID DPI switch button down")
        self._dispatch(MouseEvent(MouseEvent.DPI_SWITCH_DOWN))

    def _on_hid_dpi_switch_up(self):
        self._emit_debug("HID DPI switch button up")
        self._dispatch(MouseEvent(MouseEvent.DPI_SWITCH_UP))

    def _on_hid_gesture_move(self, delta_x, delta_y):
        # MX4 fallback: drop rawXY from the small HID++ button so it
        # cannot pollute an in-flight haptic-panel gesture.
        if self._gesture_via_sense_panel:
            return
        self._emit_debug(f"HID rawxy move dx={delta_x} dy={delta_y}")
        self._emit_gesture_event(
            {
                "type": "move",
                "source": "hid_rawxy",
                "dx": delta_x,
                "dy": delta_y,
            }
        )
        self._accumulate_gesture_delta(delta_x, delta_y, "hid_rawxy")
    # Give the macOS HID stack a moment to return, then replace the stale
    # pre-sleep handle once. HidGestureListener owns all subsequent retries;
    # queueing more force requests while it reconnects would tear down each
    # fresh connection as soon as it opens.
    _RESUME_RECONNECT_DELAY_S = 0.5
    _RESUME_DEDUPE_S = 5.0

    @staticmethod
    def _thread_alive(thread) -> bool:
        is_alive = getattr(thread, "is_alive", None)
        return bool(is_alive is not None and is_alive())

    def _start_resume_recovery(self, reason):
        # A full wake commonly raises both system-wake and screens-wake.
        # Collapse that burst into one recovery pass: one worker at a time
        # (never stack a second thread behind one still sleeping or still
        # inside force_reconnect), and at most one per dedupe window.
        if self._thread_alive(self._resume_thread):
            return False
        now = time.monotonic()
        if now - self._last_resume_at < self._RESUME_DEDUPE_S:
            return False
        self._last_resume_at = now
        print(f"[MouseHook] Resume detected ({reason}) — recovering")
        thread = threading.Thread(
            target=self._resume_recovery_worker,
            daemon=True,
            name="MouseHook-resume",
        )
        self._resume_thread = thread
        thread.start()
        return True

    def _resume_recovery_worker(self):
        try:
            time.sleep(self._RESUME_RECONNECT_DELAY_S)
            if not self._running or not self._device_connected:
                return
            hg = self._hid_gesture
            if hg is None:
                return
            try:
                hg.force_reconnect()
            except Exception as exc:
                print(f"[MouseHook] resume reconnect request failed: {exc}")
        finally:
            if self._resume_thread is threading.current_thread():
                self._resume_thread = None

    def _register_wake_observer(self):
        try:
            from AppKit import NSWorkspace
        except ImportError:
            return
        notification_center = NSWorkspace.sharedWorkspace().notificationCenter()
        hg = self._hid_gesture

        def _re_enable_tap_and_reconnect(reason, reconnect=True):
            # macOS may have disabled the tap across sleep; converge it to
            # the predicate rather than forcing it on, so a wake with no
            # device bound does not put an idle callback back in the path.
            if self._running:
                self.sync_hook_state()
                print(
                    f"[MouseHook] Event tap re-synced ({reason}): "
                    f"{'enabled' if self._tap_wanted else 'idle'}",
                    flush=True,
                )
            if hg and reconnect:
                hg.force_reconnect()

        def _on_wake(notification):
            _re_enable_tap_and_reconnect("wake", reconnect=False)
            self._start_resume_recovery("system wake")

        def _on_screens_wake(notification):
            _re_enable_tap_and_reconnect("screens wake", reconnect=False)
            self._start_resume_recovery("screens wake")

        def _on_session_resign(notification):
            print("[MouseHook] Session deactivated", flush=True)

        def _on_session_activate(notification):
            _re_enable_tap_and_reconnect("user-switch")

        self._wake_observer = notification_center.addObserverForName_object_queue_usingBlock_(
            "NSWorkspaceDidWakeNotification",
            None,
            None,
            _on_wake,
        )
        self._screens_wake_observer = notification_center.addObserverForName_object_queue_usingBlock_(
            "NSWorkspaceScreensDidWakeNotification",
            None,
            None,
            _on_screens_wake,
        )
        self._session_resign_observer = (
            notification_center.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceSessionDidResignActiveNotification",
                None,
                None,
                _on_session_resign,
            )
        )
        self._session_activate_observer = (
            notification_center.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceSessionDidBecomeActiveNotification",
                None,
                None,
                _on_session_activate,
            )
        )

    def _unregister_wake_observer(self):
        try:
            from AppKit import NSWorkspace

            notification_center = NSWorkspace.sharedWorkspace().notificationCenter()
            for attr in (
                "_wake_observer",
                "_screens_wake_observer",
                "_session_resign_observer",
                "_session_activate_observer",
            ):
                observer = getattr(self, attr, None)
                if observer is not None:
                    notification_center.removeObserver_(observer)
                    setattr(self, attr, None)
        except Exception:
            pass

    def start(self):
        if not _QUARTZ_OK:
            print("[MouseHook] Quartz not available -- hook not installed")
            return False
        if self._running:
            return True

        event_mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventScrollWheel)
        )
        motion_mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDragged)
        )

        self._running = True
        self._tap_wanted = self._hook_should_be_installed()
        if not self._start_tap(event_mask, motion_mask):
            self._running = False
            return False

        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker,
            daemon=True,
            name="MouseHook-dispatch",
        )
        self._dispatch_thread.start()

        self._start_hid_listener()
        self._register_wake_observer()
        return True

    def _start_tap(self, event_mask, motion_mask):
        native = NativeTap.load()
        if native is not None:
            self._native = native
            self._push_native_filter()
            native.set_enabled(self._tap_wanted)
            if native.start():
                print(f"[MouseHook] CGEventTap created (native tap: {native.path})", flush=True)
                self._native_drain_thread = threading.Thread(
                    target=self._native_drain_worker,
                    daemon=True,
                    name="MouseHook-tap-drain",
                )
                self._native_drain_thread.start()
                return True
            print("[MouseHook] Native tap start failed -- using Python tap")
            self._native = None
            self._native_filter_state = None
        return self._start_python_tap(event_mask, motion_mask)

    def _start_python_tap(self, event_mask, motion_mask):
        ready = threading.Event()
        self._tap_thread = threading.Thread(
            target=self._python_tap_loop,
            args=(event_mask, motion_mask, ready),
            daemon=True,
            name="MouseHook-tap",
        )
        self._tap_thread.start()
        ready.wait(5.0)
        if self._tap is None:
            print("[MouseHook] ERROR: Failed to create CGEventTap!")
            print("[MouseHook] Grant Accessibility permission in:")
            print(
                "[MouseHook]   System Settings -> Privacy & Security -> Accessibility"
            )
            self._tap_thread.join(timeout=1)
            self._tap_thread = None
            return False
        print("[MouseHook] CGEventTap enabled on its own run loop", flush=True)
        return True

    def _python_tap_loop(self, event_mask, motion_mask, ready):
        """Tap thread: owns the CGEventTap for its whole life.

        Creating, enabling and tearing the tap down all happen here, so no
        other thread ever touches a tap a callback may be running on;
        stop() just stops the loop and waits.
        """
        tap = None
        try:
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                event_mask,
                self._event_tap_callback,
                None,
            )
            if tap is None:
                return
            with self._tap_lock:
                self._tap = tap
                self._tap_source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                self._tap_loop = Quartz.CFRunLoopGetCurrent()
                Quartz.CFRunLoopAddSource(
                    self._tap_loop, self._tap_source, Quartz.kCFRunLoopCommonModes
                )
                Quartz.CGEventTapEnable(tap, self._tap_wanted)
                self._create_motion_tap(motion_mask)
        finally:
            ready.set()
        if tap is None:
            return
        while self._running:
            with objc.autorelease_pool():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 1.0, False)
        self._teardown_python_tap()

    def _create_motion_tap(self, motion_mask):
        """Tap thread only, under _tap_lock: the motion tap shares the
        callback and loop, and starts disabled until a gesture arms it."""
        self._motion_tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            motion_mask,
            self._event_tap_callback,
            None,
        )
        if self._motion_tap is None:
            print("[MouseHook] WARNING: motion tap unavailable; directional gestures off")
            return
        self._motion_tap_source = Quartz.CFMachPortCreateRunLoopSource(
            None, self._motion_tap, 0
        )
        Quartz.CFRunLoopAddSource(
            self._tap_loop, self._motion_tap_source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(self._motion_tap, False)

    def _teardown_python_tap(self):
        with self._tap_lock:
            tap, source, loop = self._tap, self._tap_source, self._tap_loop
            motion_tap, motion_source = self._motion_tap, self._motion_tap_source
            self._tap = None
            self._tap_source = None
            self._tap_loop = None
            self._motion_tap = None
            self._motion_tap_source = None
        if motion_tap is not None:
            Quartz.CGEventTapEnable(motion_tap, False)
        if motion_source is not None and loop is not None:
            Quartz.CFRunLoopRemoveSource(loop, motion_source, Quartz.kCFRunLoopCommonModes)
        if tap is not None:
            Quartz.CGEventTapEnable(tap, False)
        if source is not None and loop is not None:
            Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopCommonModes)
        self._logitech_scroll_monitor.stop()
        if tap is not None:
            print("[MouseHook] CGEventTap disabled and removed", flush=True)

    def stop(self):
        self._unregister_wake_observer()
        self._running = False
        self._stop_hid_listener()
        self._connected_device = None
        self._scroll_monitor_applied = None
        self._scroll_prefetch = None
        self._stop_tap()

        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=1)
            self._dispatch_thread = None

    def _stop_tap(self):
        native = self._native
        if native is not None:
            # Drain first: it pushes filters on every tick, and nothing may
            # touch the dylib while its thread is tearing down.
            drain = self._native_drain_thread
            if drain is not None:
                drain.join(timeout=1)
                self._native_drain_thread = None
            if not native.stop():
                print("[MouseHook] Native tap stop timed out -- thread left running")
            self._native = None
            self._native_filter_state = None
            print("[MouseHook] CGEventTap disabled and removed (native tap)", flush=True)
            return
        loop = self._tap_loop
        if loop is not None:
            Quartz.CFRunLoopStop(loop)
        thread = self._tap_thread
        if thread is not None:
            thread.join(timeout=TAP_THREAD_JOIN_S)
            self._tap_thread = None
            if thread.is_alive():
                print("[MouseHook] Tap thread did not exit; disabling tap from caller")
        # No-op when the tap thread already tore down; the fallback for a
        # wedged thread or a tap that was never given one.
        self._teardown_python_tap()

    # ── hook state (enable / disable without recreating the tap) ────

    def sync_hook_state(self):
        """Thread-safe: converge the tap to ``_hook_should_be_installed``.

        A disabled tap costs nothing on the input path, so it stands down
        whenever nothing here would act (no Logitech bound, or KVM focus
        remote with no scroll-invert fallback armed) and comes back on the
        next device / focus change. A pointer capture that focus loss will
        never release is aborted first, mirroring the Windows hook.
        """
        if self._gesture_active and not self._should_intercept_events():
            self._abort_gesture_capture("hook standing down")
        if not self._running:
            return
        want = self._hook_should_be_installed()
        with self._tap_lock:
            self._tap_wanted = want
            native = self._native
            if native is not None:
                self._push_native_filter()
                native.set_enabled(want)
                return
            loop = self._tap_loop
            if loop is None:
                return
            Quartz.CFRunLoopPerformBlock(
                loop, Quartz.kCFRunLoopCommonModes, self._apply_python_tap_state
            )
            Quartz.CFRunLoopWakeUp(loop)

    def _apply_python_tap_state(self):
        """Tap thread only: the last pushed ``_tap_wanted`` wins."""
        with self._tap_lock:
            tap = self._tap
            want = self._tap_wanted
        if tap is None:
            return
        Quartz.CGEventTapEnable(tap, want)
        if not want:
            self._logitech_scroll_monitor.stop()
            self._scroll_monitor_applied = None

    def _abort_gesture_capture(self, reason):
        with self._gesture_lock:
            if not self._gesture_active:
                return
        self._emit_debug(f"Gesture capture aborted: {reason}")
        self._end_gesture_capture(f"aborted ({reason})")
        self._release_gesture_anchor()

    # ── native tap ──────────────────────────────────────────────────

    def _push_native_filter(self):
        native = self._native
        if native is None:
            return
        state = compute_tap_filter(self)
        with self._native_filter_lock:
            if state == self._native_filter_state:
                return
            try:
                native.set_filter(*state)
            except Exception as exc:  # noqa: BLE001 - native boundary
                print(f"[MouseHook] native set_filter failed: {exc}")
                return
            self._native_filter_state = state
        self._emit_debug(f"Native tap filter {describe_tap_filter(*state)}")

    def _native_drain_worker(self):
        """Collect events the native tap queued. ctypes releases the GIL for
        the wait, so this thread holds nothing the tap thread needs."""
        event = NativeTapEvent()
        native = self._native
        dropped_seen = 0
        reenabled_seen = 0
        while self._running:
            try:
                got = native.next_event(event, NATIVE_DRAIN_TIMEOUT_MS)
                # Invert toggles, debug_mode and friends are plain attribute
                # writes with nothing to hang a push on; the tick lands them.
                self._push_native_filter()
                if got:
                    with objc.autorelease_pool():
                        self._handle_native_event(event)
                dropped = native.dropped
                reenabled = native.reenabled
            except Exception as exc:  # noqa: BLE001 - native boundary
                print(f"[MouseHook] native tap drain error: {exc}")
                return
            if dropped > dropped_seen:
                print(
                    f"[MouseHook] native tap ring dropped {dropped - dropped_seen} "
                    "event(s) -- drain thread fell behind"
                )
                dropped_seen = dropped
            if reenabled > reenabled_seen:
                print(
                    "[MouseHook] CGEventTap disabled by system, re-enabled "
                    f"(native tap, {reenabled - reenabled_seen}x)",
                    flush=True,
                )
                reenabled_seen = reenabled

    def _handle_native_event(self, event):
        if self.debug_mode and self._debug_callback:
            qb = self._quartz()
            try:
                if event.event_type == qb.t_other_down:
                    self._debug_callback(f"OtherMouseDown btn={event.button}")
                elif event.event_type == qb.t_other_up:
                    self._debug_callback(f"OtherMouseUp btn={event.button}")
                elif event.event_type == qb.t_scroll:
                    self._debug_callback(
                        f"ScrollWheel v={event.v_fixed / 65536.0} "
                        f"h={event.h_fixed / 65536.0}"
                    )
            except Exception:
                pass
        code = event.event_code
        if code == EVT_NONE:
            return
        if code == EVT_SENSE_PANEL_DOWN:
            self._arm_gesture_anchor()
            self._begin_gesture_capture("Sense panel gesture")
            return
        if code == EVT_SENSE_PANEL_UP:
            self._end_gesture_capture("Sense panel gesture")
            self._release_gesture_anchor()
            return
        name = TAP_EVENT_NAMES.get(code)
        if name is None:
            return
        if name in (MouseEvent.HSCROLL_LEFT, MouseEvent.HSCROLL_RIGHT):
            self._enqueue_dispatch_event(
                MouseEvent(name, abs(event.h_fixed / 65536.0))
            )
            return
        self._enqueue_dispatch_event(MouseEvent(name))

    def _begin_gesture_capture(self, source_label):
        super()._begin_gesture_capture(source_label)
        # Pushed now rather than on the drain tick: the first 50 ms of a
        # swipe is where the direction is decided.
        self._push_native_filter()

    def _end_gesture_capture(self, source_label):
        self._drain_capture_motion()
        try:
            super()._end_gesture_capture(source_label)
        finally:
            self._push_native_filter()

    def _drain_capture_motion(self):
        native = self._native
        if native is None:
            return
        try:
            delta_x, delta_y = native.take_capture_delta()
        except Exception as exc:  # noqa: BLE001 - native boundary
            print(f"[MouseHook] native capture drain failed: {exc}")
            return
        if delta_x or delta_y:
            self._accumulate_gesture_delta(delta_x, delta_y, "event_tap")


MouseHook._platform_module = sys.modules[__name__]


__all__ = [
    "MouseHook",
    "HidGestureListener",
    "Quartz",
    "_QUARTZ_OK",
    "_BTN_MIDDLE",
    "_BTN_BACK",
    "_BTN_FORWARD",
    "_BTN_OS_EXTRA",
    "_INJECTED_EVENT_MARKER",
    "_DESKFLOW_INJECTED_EVENT_MARKER",
    "_INJECTED_EVENT_MARKERS",
    "_kCGEventTapDisabledByTimeout",
    "_kCGEventTapDisabledByUserInput",
]
