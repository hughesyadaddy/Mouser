"""
Shared mouse hook behavior used by platform implementations.
"""

import queue
import threading

try:
    from core.hid_gesture import HidGestureListener
except Exception:
    HidGestureListener = None

from core.mouse_hook_types import (
    HidRuntimeState,
    MouseEvent,
    format_debug_details,
    is_physical_device_source,
)



class BaseMouseHook:
    def __init__(self):
        self._callbacks = {}
        self._blocked_events = set()
        self._debug_callback = None
        self._gesture_callback = None
        self._status_callback = None
        self.debug_mode = False
        self.invert_vscroll = False
        self.invert_hscroll = False
        self._gesture_active = False
        self._hid_gesture = None
        self._device_connected = False
        self._connection_change_cb = None
        self.divert_mode_shift = False
        self.divert_dpi_switch = False
        self._gesture_direction_enabled = False
        self._gesture_threshold = 50.0
        self._gesture_tracking = False
        self._gesture_delta_x = 0.0
        self._gesture_delta_y = 0.0
        self._gesture_input_source = None
        # Latched once a swipe fires live mid-hold, so a single hold cannot
        # fire twice and the button release becomes a no-op.
        self._gesture_fired = False
        # Guards the capture state above: the HID++ listener thread, the
        # OS-level hook thread, and the remote bridge can all touch an
        # in-flight capture concurrently.
        self._gesture_lock = threading.Lock()
        self._connected_device = None
        self._dispatch_queue = None
        # Per-axis: True when the device is inverting THAT axis at the firmware
        # level (vertical = HID++ 0x2121, horizontal = 0x2150); the matching
        # OS-layer path must skip its own flip to avoid cancelling out. The
        # engine flips each independently after the per-axis request result, so
        # a firmware limit on one axis never disables the OS fallback on the
        # other (the original-MX-Master horizontal-scroll bug).
        self.wheel_native_invert_vertical = False
        self.wheel_native_invert_horizontal = False
        # Optional core.remote_forward.RemoteForwarder. While it reports
        # should_forward() (bridge up AND KVM focus on a remote machine),
        # HID++ events are relayed instead of handled locally and the
        # OS-level intercept gate stands down.
        self._remote_forwarder = None

    def _init_dispatch_queue(self, maxsize=0):
        """Initialize dispatch queue storage for subclasses with event threads."""
        self._dispatch_queue = queue.Queue(maxsize=max(0, int(maxsize)))

    def _enqueue_dispatch_event(self, event):
        """Best-effort enqueue that bounds memory when queue has a max size."""
        q = self._dispatch_queue
        if q is None:
            return
        if q.maxsize <= 0:
            q.put(event)
            return
        try:
            q.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(event)
        except queue.Full:
            self._emit_debug(f"Dropped event due to full dispatch queue: {event.event_type}")

    def register(self, event_type, callback):
        self._callbacks.setdefault(event_type, []).append(callback)

    def block(self, event_type):
        self._blocked_events.add(event_type)

    def unblock(self, event_type):
        self._blocked_events.discard(event_type)

    def reset_bindings(self):
        self._callbacks.clear()
        self._blocked_events.clear()

    def configure_gestures(self, enabled=False, threshold=50, **_deprecated):
        """Configure swipe detection. ``threshold`` is the net rawXY
        displacement (sensor counts) a stroke must reach by release to
        read as a swipe instead of a tap -- the single tunable Logi
        Options exposes.

        ``**_deprecated`` swallows the retired mid-hold-era knobs
        (deadzone / axis_ratio / timeout_ms / cooldown_ms) so stale
        callers and configs stay harmless.
        """
        self._gesture_direction_enabled = bool(enabled)
        self._gesture_threshold = float(max(5, threshold))
        if not self._gesture_direction_enabled:
            with self._gesture_lock:
                self._finish_gesture_tracking()

    def set_connection_change_callback(self, cb):
        self._connection_change_cb = cb

    @property
    def device_connected(self):
        return self._device_connected

    @property
    def connected_device(self):
        return self._connected_device

    @property
    def hid_runtime_state(self):
        hg = getattr(self, "_hid_gesture", None)
        hid_device = getattr(hg, "connected_device", None) if hg else None
        return HidRuntimeState(
            input_ready=bool(self._device_connected),
            hid_ready=hid_device is not None,
            connected_device=self._connected_device,
        )

    # MX Master 4's Sense Panel exposes itself as CID 0x01A0
    # (raw-XY divertable) AND as OS btn=6 / BTN_TASK.
    SENSE_PANEL_CID = 0x01A0

    def _logitech_device_bound(self) -> bool:
        """True while a Logitech device is bound to this host's pipeline."""
        return self._connected_device is not None

    def _physical_logitech_bound(self) -> bool:
        """True when a Logitech is physically attached on this machine.

        KVM virtual devices (``remote-virtual``, ``deskflow-shim``) do not
        count — scroll invert must not touch Deskflow-forwarded wheel events
        on a client that only has a remote-described device connected.
        """
        device = self._connected_device
        if device is None:
            return False
        return is_physical_device_source(getattr(device, "source", None))

    def _should_intercept_events(self) -> bool:
        """True only when the platform hook should block, remap, or dispatch
        OS-level mouse events to the engine.

        Mouser exists to remap a Logitech mouse's buttons. The global event
        taps on macOS (CGEventTap) and Windows (WH_MOUSE_LL) see events
        from every input device the OS knows about -- when no Logitech is
        currently bound to this host (KVM switched to another machine,
        the device is mid-reconnect after sleep, or the user simply has
        not plugged one in) those hooks must stay completely out of the
        way, otherwise xbutton clicks and scroll events from a trackpad
        or generic USB mouse get swallowed and routed through Mouser's
        remap pipeline.

        Linux's evdev hook only attaches once a Logitech source device
        has been resolved, so it is naturally gated -- but consult this
        helper defensively before dispatching there as well so the
        contract stays platform-uniform.

        When a remote forwarder reports active (KVM focus is on another
        machine), the hook also stands down for button/gesture remaps:
        Deskflow forwards pointer and scroll through untouched while the
        DMSR bridge relays only decoded HID++ gesture/button events to
        the focused client's Mouser. Scroll inversion is host-local and
        is gated separately via :meth:`_apply_vscroll_invert_fallback`.
        """
        if not self._logitech_device_bound():
            return False
        fwd = self._remote_forwarder
        if fwd is not None and fwd.should_forward():
            return False
        return True

    def set_remote_forwarder(self, forwarder):
        """Attach/detach a core.remote_forward.RemoteForwarder."""
        self._remote_forwarder = forwarder

    def _maybe_forward_raw_report(self, raw) -> bool:
        """Listener raw-report tap.

        Legacy DMSR relay ships decoded gesture/button events only
        (``_hid_event_entry``); raw HID++ frames are always decoded on
        the host. Returns False so local handling always runs.
        """
        return False

    def gesture_decode_context(self):
        """The live gesture decode map, for a host to advertise to slaves."""
        listener = getattr(self, "_hid_gesture", None)
        if listener is None:
            return None
        try:
            return listener.decode_context()
        except Exception:  # noqa: BLE001 - boundary
            return None

    def _scroll_event_targets_logitech(
        self,
        *,
        cg_event=None,
        wParam=None,
        lParam=None,
        linux_evdev=False,
    ) -> bool:
        """True when the scroll event originated from a Logitech mouse.

        Linux evdev only forwards the grabbed Logitech source, so callers
        there pass ``linux_evdev=True``. macOS and Windows override this to
        attribute individual CGEvent / WH_MOUSE_* wheel messages.
        """
        return bool(linux_evdev)

    def _apply_vscroll_invert_fallback(
        self,
        *,
        cg_event=None,
        wParam=None,
        lParam=None,
        linux_evdev=False,
    ) -> bool:
        """True only when the OS-layer vertical-scroll inversion fallback
        should fire on the current event.

        The user's wheel-invert toggle is meant to flip *Logitech* scroll --
        firmware-first on HID++-capable devices, OS-layer event-tap on the
        rest. When no Logitech is currently connected we have no source-of-
        truth that the event came from a device the toggle applies to, so the
        fallback must stand down rather than invert every trackpad / generic
        USB mouse scroll the OS forwards through us.

        Unlike button/gesture remaps, scroll inversion stays active on the
        host even while KVM focus is remote: Deskflow forwards scroll through
        untouched and must not need to know about this setting.
        """
        if not self.invert_vscroll:
            return False
        # Per-axis: only stand down when the VERTICAL axis is firmware-inverted.
        if self.wheel_native_invert_vertical:
            return False
        if not self._physical_logitech_bound():
            return False
        return self._scroll_event_targets_logitech(
            cg_event=cg_event,
            wParam=wParam,
            lParam=lParam,
            linux_evdev=linux_evdev,
        )

    def _apply_hscroll_invert_fallback(
        self,
        *,
        cg_event=None,
        wParam=None,
        lParam=None,
        linux_evdev=False,
    ) -> bool:
        """Horizontal twin of :meth:`_apply_vscroll_invert_fallback`.

        Gated on the HORIZONTAL firmware flag only -- this is the line that
        fixes the original MX Master: its thumbwheel rejects firmware invert,
        so ``wheel_native_invert_horizontal`` stays False and the OS-layer
        horizontal flip fires, while vertical keeps its firmware invert.
        """
        if not self.invert_hscroll:
            return False
        if self.wheel_native_invert_horizontal:
            return False
        if not self._physical_logitech_bound():
            return False
        return self._scroll_event_targets_logitech(
            cg_event=cg_event,
            wParam=wParam,
            lParam=lParam,
            linux_evdev=linux_evdev,
        )

    @property
    def _thumb_button_via_hid(self) -> bool:
        """True when thumb_button presses arrive over the HID++ vendor
        channel (via the thumb_button extra divert); platform hooks
        swallow any leaked btn=6 / BTN_TASK instead of double-dispatching."""
        device = self._connected_device
        return bool(device is not None and getattr(
            device, "thumb_button_via_hid", False
        ))

    @property
    def _gesture_via_sense_panel(self) -> bool:
        """True only on the OS-level fallback path: catalog declares
        ``gesture_via_sense_panel=True`` AND the listener diverted
        something other than 0x01A0 as the gesture CID.

        Reads the resolved single source of truth
        (``capabilities.gesture_via_sense_panel``) when present so this
        decision lives in one place; falls back to deriving it locally for
        device-info objects built without a resolver (older/virtual paths).
        """
        device = self._connected_device
        if device is None:
            return False
        caps = getattr(device, "capabilities", None)
        if caps is not None:
            return bool(caps.gesture_via_sense_panel)
        if not getattr(device, "gesture_via_sense_panel", False):
            return False
        active = getattr(device, "active_gesture_cid", None)
        return active != self.SENSE_PANEL_CID

    def dump_device_info(self):
        hg = getattr(self, "_hid_gesture", None)
        if hg and hasattr(hg, "dump_device_info"):
            return hg.dump_device_info()
        return None

    def _set_device_connected(self, connected):
        if connected == self._device_connected:
            return
        self._device_connected = connected
        state = "Connected" if connected else "Disconnected"
        print(f"[MouseHook] Device {state}")
        if self._connection_change_cb:
            try:
                self._connection_change_cb(connected)
            except Exception as exc:  # noqa: BLE001 - callback boundary
                print(
                    f"[MouseHook] connection_change_cb raised on "
                    f"{state.lower()}: {exc!r}"
                )

    def set_debug_callback(self, callback):
        self._debug_callback = callback

    def set_gesture_callback(self, callback):
        self._gesture_callback = callback

    def set_status_callback(self, callback):
        self._status_callback = callback

    def _emit_debug(self, message):
        if self.debug_mode and self._debug_callback:
            try:
                self._debug_callback(message)
            except Exception as exc:  # noqa: BLE001 - callback boundary
                # ``_emit_debug`` is itself the diagnostic channel, so the
                # failure goes straight to print() rather than recursing.
                print(f"[MouseHook] debug_callback raised: {exc!r}")

    def _emit_status(self, message):
        if self._status_callback:
            try:
                self._status_callback(message)
            except Exception as exc:  # noqa: BLE001 - callback boundary
                print(f"[MouseHook] status_callback raised: {exc!r}")

    def _emit_gesture_event(self, event):
        if self.debug_mode and self._gesture_callback:
            try:
                self._gesture_callback(event)
            except Exception as exc:  # noqa: BLE001 - callback boundary
                print(f"[MouseHook] gesture_callback raised: {exc!r}")

    def _dispatch(self, event):
        callbacks = self._callbacks.get(event.event_type, [])
        self._emit_debug(
            f"Dispatch {event.event_type}"
            f"{format_debug_details(event.raw_data)} callbacks={len(callbacks)}"
        )
        if event.event_type.startswith("gesture_"):
            self._emit_gesture_event(
                {
                    "type": "dispatch",
                    "event_name": event.event_type,
                    "callbacks": len(callbacks),
                }
            )
        if not callbacks:
            self._emit_debug(f"No mapped action for {event.event_type}")
            if event.event_type.startswith("gesture_"):
                self._emit_gesture_event(
                    {
                        "type": "unmapped",
                        "event_name": event.event_type,
                    }
                )
        for callback in callbacks:
            try:
                callback(event)
            except Exception as exc:
                print(f"[MouseHook] callback error: {exc}")

    def _hid_gesture_available(self):
        return self._hid_gesture is not None and self._device_connected

    def _start_gesture_tracking(self):
        self._gesture_tracking = self._gesture_direction_enabled
        self._gesture_delta_x = 0.0
        self._gesture_delta_y = 0.0
        self._gesture_input_source = None
        self._gesture_fired = False

    def _finish_gesture_tracking(self):
        self._gesture_tracking = False
        self._gesture_delta_x = 0.0
        self._gesture_delta_y = 0.0
        self._gesture_input_source = None
        self._gesture_fired = False

    def _classify_gesture(self, delta_x, delta_y):
        """Map the net displacement of a completed capture to a swipe
        direction, or None when the stroke reads as a tap.

        Called live from _accumulate_gesture_delta the moment the running
        displacement crosses the threshold (one decision per hold, latched).
        The dominant axis wins on a plain 45-degree split -- no diagonal dead
        zone, so the first stroke past the threshold always resolves to a
        direction.
        """
        abs_x = abs(delta_x)
        abs_y = abs(delta_y)

        # Tap gate: below the activation threshold the hold is a click.
        if max(abs_x, abs_y) < self._gesture_threshold:
            return None

        if abs_x >= abs_y:
            return (
                MouseEvent.GESTURE_SWIPE_RIGHT
                if delta_x > 0
                else MouseEvent.GESTURE_SWIPE_LEFT
            )
        return (
            MouseEvent.GESTURE_SWIPE_DOWN
            if delta_y > 0
            else MouseEvent.GESTURE_SWIPE_UP
        )

    def _accumulate_gesture_delta(self, delta_x, delta_y, source):
        """Fold one movement report into the running net displacement for the
        current hold. The swipe direction is resolved once, on release, from
        the net displacement (see _end_gesture_capture) -- Logi Options'
        OnRelease semantics. Nothing dispatches from here."""
        with self._gesture_lock:
            if not (self._gesture_direction_enabled and self._gesture_active):
                return
            if not self._gesture_tracking:
                self._emit_debug(f"Gesture tracking started source={source}")
                self._emit_gesture_event(
                    {
                        "type": "tracking_started",
                        "source": source,
                    }
                )
                self._start_gesture_tracking()

            # HID++ rawXY is the authoritative feed: when it shows up
            # mid-capture, drop whatever the OS-level fallback source
            # (event_tap / raw_mouse / evdev) accumulated and restart.
            if (
                source == "hid_rawxy"
                and self._gesture_input_source not in (None, "hid_rawxy")
            ):
                self._emit_debug(
                    "Gesture source promoted from "
                    f"{self._gesture_input_source} to hid_rawxy "
                    f"prev_accum_x={self._gesture_delta_x} "
                    f"prev_accum_y={self._gesture_delta_y}"
                )
                self._start_gesture_tracking()

            if self._gesture_input_source not in (None, source):
                self._emit_debug(
                    f"Gesture source locked to {self._gesture_input_source}; "
                    f"ignoring {source} dx={delta_x} dy={delta_y}"
                )
                return
            self._gesture_input_source = source

            self._gesture_delta_x += delta_x
            self._gesture_delta_y += delta_y
            self._emit_debug(
                f"Gesture segment source={source} "
                f"accum_x={self._gesture_delta_x} accum_y={self._gesture_delta_y}"
            )
            self._emit_gesture_event(
                {
                    "type": "segment",
                    "source": source,
                    "dx": self._gesture_delta_x,
                    "dy": self._gesture_delta_y,
                }
            )

            # Resolve the swipe on RELEASE from the NET displacement of the
            # whole stroke (see _end_gesture_capture) -- NOT live on the first
            # axis to cross the threshold. Firing live latched the direction
            # on the few units of sideways jitter every swipe opens with, so
            # the horizontal axis almost always won first: it ate up/down
            # swipes and repeated the prior direction on quick strokes. The
            # net displacement over the full hold reflects the user's intent.
            # This is the Logi Options behaviour (commit on release).

    def _begin_gesture_capture(self, source_label):
        """Open a capture on gesture-button press (HID++ divert or the
        OS-level Sense Panel fallback). Deltas accumulate until the
        matching _end_gesture_capture resolves the stroke."""
        with self._gesture_lock:
            if self._gesture_active:
                return
            self._gesture_active = True
            self._emit_debug(f"{source_label} button down")
            self._emit_gesture_event({"type": "button_down"})
            if self._gesture_direction_enabled:
                self._start_gesture_tracking()
            else:
                self._gesture_tracking = False

    def _end_gesture_capture(self, source_label):
        """Resolve the capture on release: exactly one outcome per hold.
        The net displacement classifies as a single swipe, or as
        GESTURE_CLICK (tap) when it stayed under the threshold.
        Dispatch runs outside the lock."""
        event = None
        with self._gesture_lock:
            if not self._gesture_active:
                return
            self._gesture_active = False
            if self._gesture_fired:
                # The swipe already fired live during the hold; the release
                # is a no-op so one motion can never trigger twice.
                self._emit_debug(f"{source_label} button up (swipe already fired live)")
                self._emit_gesture_event({"type": "button_up", "resolved": "already_fired"})
                self._finish_gesture_tracking()
                return
            delta_x = self._gesture_delta_x
            delta_y = self._gesture_delta_y
            source = self._gesture_input_source
            swipe = (
                self._classify_gesture(delta_x, delta_y)
                if self._gesture_tracking
                else None
            )
            self._finish_gesture_tracking()
            # Always-on decode log (independent of debug mode): one line per
            # hold so a wrong/empty resolution is never silent.
            print(
                f"[Gesture] decoded {swipe or 'tap'} "
                f"(dx={delta_x:.0f} dy={delta_y:.0f}, src={source})"
            )
            self._emit_debug(
                f"{source_label} button up resolved={swipe or 'click'} "
                f"dx={delta_x} dy={delta_y} source={source}"
            )
            self._emit_gesture_event(
                {
                    "type": "button_up",
                    "resolved": swipe or "click",
                    "click_candidate": swipe is None,
                    "dx": delta_x,
                    "dy": delta_y,
                    "source": source,
                }
            )
            if swipe is not None:
                self._emit_gesture_event(
                    {
                        "type": "detected",
                        "event_name": swipe,
                        "source": source,
                        "dx": delta_x,
                        "dy": delta_y,
                    }
                )
                event = MouseEvent(
                    swipe,
                    {
                        "delta_x": delta_x,
                        "delta_y": delta_y,
                        "source": source,
                    },
                )
            else:
                event = MouseEvent(MouseEvent.GESTURE_CLICK)
        self._dispatch(event)

    def _hid_event_entry(self, name, handler):
        """Wrap a HID-listener callback so it forwards to the remote
        bridge instead of running locally while KVM focus is remote.

        Gating happens here -- at the listener-callback boundary -- rather
        than inside the ``_on_hid_*`` methods because the platform hooks
        override several of those (Sense-Panel role routing); the sender
        must relay raw, un-rerouted events and let the receiving machine's
        pipeline interpret them.
        """
        def _entry(*args):
            fwd = self._remote_forwarder
            if fwd is not None and fwd.should_forward():
                try:
                    if name == "gesture_move":
                        sent = fwd.send_event(name, dx=args[0], dy=args[1])
                    else:
                        sent = fwd.send_event(name)
                except Exception as exc:  # noqa: BLE001 - relay boundary
                    print(f"[MouseHook] remote forward of {name} raised: {exc!r}")
                    sent = False
                if sent is not False:
                    return None
                # Forwarding failed mid-flight: fall through to local
                # handling so the press is not lost entirely.
            return handler(*args)
        return _entry

    def _build_extra_diverts(self):
        extra = {}
        if self.divert_mode_shift:
            extra[0x00C4] = {
                "on_down": self._hid_event_entry(
                    "mode_shift_down", self._on_hid_mode_shift_down),
                "on_up": self._hid_event_entry(
                    "mode_shift_up", self._on_hid_mode_shift_up),
            }
        if self.divert_dpi_switch:
            extra[0x00FD] = {
                "on_down": self._hid_event_entry(
                    "dpi_switch_down", self._on_hid_dpi_switch_down),
                "on_up": self._hid_event_entry(
                    "dpi_switch_up", self._on_hid_dpi_switch_up),
            }
        return extra

    def _start_hid_listener(self):
        platform_module = getattr(self.__class__, "_platform_module", None)
        listener_cls = getattr(platform_module, "HidGestureListener", HidGestureListener)
        if listener_cls is None:
            return None
        listener = listener_cls(
            on_down=self._hid_event_entry(
                "gesture_down", self._on_hid_gesture_down),
            on_up=self._hid_event_entry(
                "gesture_up", self._on_hid_gesture_up),
            on_move=self._hid_event_entry(
                "gesture_move", self._on_hid_gesture_move),
            on_connect=self._on_hid_connect,
            on_disconnect=self._on_hid_disconnect,
            extra_diverts=self._build_extra_diverts(),
            on_thumb_button_down=self._hid_event_entry(
                "thumb_button_down", self._on_hid_thumb_button_down),
            on_thumb_button_up=self._hid_event_entry(
                "thumb_button_up", self._on_hid_thumb_button_up),
        )
        self._hid_gesture = listener
        listener._on_raw_report = self._maybe_forward_raw_report
        if not listener.start():
            self._hid_gesture = None
        return self._hid_gesture

    def _on_hid_thumb_button_down(self):
        """Dispatch THUMB_BUTTON_DOWN from a HID++ extra divert."""
        self._emit_debug("HID thumb_button button down")
        try:
            from core.mouse_hook_types import MouseEvent
            self._dispatch(MouseEvent(MouseEvent.THUMB_BUTTON_DOWN))
        except Exception as exc:
            print(f"[MouseHook] thumb_button down dispatch error: {exc}")

    def _on_hid_thumb_button_up(self):
        self._emit_debug("HID thumb_button button up")
        try:
            from core.mouse_hook_types import MouseEvent
            self._dispatch(MouseEvent(MouseEvent.THUMB_BUTTON_UP))
        except Exception as exc:
            print(f"[MouseHook] thumb_button up dispatch error: {exc}")

    def configure_wheel_multipliers(self, vertical: int, horizontal: int) -> None:
        """No-op kept for divert+inject-era callers; native invert never
        injects scroll so multipliers are unused."""
        del vertical, horizontal

    def _stop_hid_listener(self):
        if self._hid_gesture:
            self._hid_gesture.stop()
            self._hid_gesture = None

    def _on_hid_connect(self):
        self._connected_device = (
            self._hid_gesture.connected_device if self._hid_gesture else None
        )
        self._set_device_connected(True)

    def _on_hid_disconnect(self):
        self._connected_device = None
        self._set_device_connected(False)

    def attach_deskflow_ingress(self, decode, product_id=None, product_name=None):
        """Route Deskflow passthrough through the main HID listener (Tier 1.5)."""
        listener = self._hid_gesture
        if listener is None:
            listener = self._start_hid_listener()
        if listener is None:
            return False
        return listener.request_deskflow_attach(
            decode, product_id=product_id, product_name=product_name
        )

    def detach_deskflow_ingress(self):
        """Release Deskflow ingress and return to local USB if available."""
        listener = self._hid_gesture
        if listener is not None:
            listener.clear_deskflow_attach()

    def _on_hid_gesture_down(self):
        self._dispatch(MouseEvent(MouseEvent.GESTURE_DOWN))

    def _on_hid_gesture_up(self):
        self._dispatch(MouseEvent(MouseEvent.GESTURE_UP))

    def _on_hid_gesture_move(self, dx, dy):
        self._accumulate_gesture_delta(dx, dy, "hid_rawxy")

    def _on_hid_mode_shift_down(self):
        self._dispatch(MouseEvent(MouseEvent.MODE_SHIFT_DOWN))

    def _on_hid_mode_shift_up(self):
        self._dispatch(MouseEvent(MouseEvent.MODE_SHIFT_UP))

    def _on_hid_dpi_switch_down(self):
        self._dispatch(MouseEvent(MouseEvent.DPI_SWITCH_DOWN))

    def _on_hid_dpi_switch_up(self):
        self._dispatch(MouseEvent(MouseEvent.DPI_SWITCH_UP))
