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

    def __init__(self):
        super().__init__()
        self._running = False
        self._tap = None
        self._tap_source = None
        self.ignore_trackpad = True
        self._wake_observer = None
        self._screens_wake_observer = None
        self._session_resign_observer = None
        self._session_activate_observer = None
        self._last_resume_at = 0.0
        self._init_dispatch_queue(maxsize=512)
        self._dispatch_thread = None
        self._first_event_logged = False
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
                # start() failed (Input Monitoring denied): retry on the next
                # wheel event; the monitor negative-caches denials itself.
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
                print(
                    f"[MouseHook] CGEventTap disabled by system "
                    f"(type=0x{event_type:X}), re-enabling",
                    flush=True,
                )
                Quartz.CGEventTapEnable(self._tap, True)
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
                if get_int(cg_event, qb.f_user_data) == _INJECTED_EVENT_MARKER:
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
        self._gesture_anchor = self._last_cursor_pos
        if self._gesture_anchor is not None:
            self._warp_cursor(self._gesture_anchor)

    def _release_gesture_anchor(self):
        if self._gesture_anchor is not None:
            self._warp_cursor(self._gesture_anchor)
            self._gesture_anchor = None

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
            if self._tap and self._running:
                Quartz.CGEventTapEnable(self._tap, True)
                ok = Quartz.CGEventTapIsEnabled(self._tap)
                print(
                    f"[MouseHook] Event tap re-enabled ({reason}): "
                    f"{'OK' if ok else 'FAILED -- may need restart'}",
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
            Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDragged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventScrollWheel)
        )

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            event_mask,
            self._event_tap_callback,
            None,
        )

        if self._tap is None:
            print("[MouseHook] ERROR: Failed to create CGEventTap!")
            print("[MouseHook] Grant Accessibility permission in:")
            print(
                "[MouseHook]   System Settings -> Privacy & Security -> Accessibility"
            )
            return False

        print("[MouseHook] CGEventTap created successfully", flush=True)

        self._tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(),
            self._tap_source,
            Quartz.kCFRunLoopCommonModes,
        )
        Quartz.CGEventTapEnable(self._tap, True)
        print("[MouseHook] CGEventTap enabled and integrated with run loop", flush=True)
        self._running = True

        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker,
            daemon=True,
            name="MouseHook-dispatch",
        )
        self._dispatch_thread.start()

        self._start_hid_listener()
        self._register_wake_observer()
        return True

    def stop(self):
        self._unregister_wake_observer()
        self._running = False
        self._stop_hid_listener()
        self._connected_device = None
        self._logitech_scroll_monitor.stop()
        self._scroll_monitor_applied = None
        self._scroll_prefetch = None

        if self._tap:
            Quartz.CGEventTapEnable(self._tap, False)
            if self._tap_source:
                Quartz.CFRunLoopRemoveSource(
                    Quartz.CFRunLoopGetCurrent(),
                    self._tap_source,
                    Quartz.kCFRunLoopCommonModes,
                )
                self._tap_source = None
            self._tap = None
            print("[MouseHook] CGEventTap disabled and removed", flush=True)

        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=1)
            self._dispatch_thread = None


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
    "_kCGEventTapDisabledByTimeout",
    "_kCGEventTapDisabledByUserInput",
]
