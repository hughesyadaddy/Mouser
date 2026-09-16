"""
Engine -- wires the mouse hook to the key simulator using the
current configuration.  Sits between the hook layer and the UI.
Supports per-application auto-switching of profiles.
"""

import threading
import time
from core.mouse_hook import MouseHook, MouseEvent
from core.key_simulator import (
    ACTIONS, execute_action, is_mouse_button_action,
    inject_mouse_down, inject_mouse_up,
)
from core.config import (
    load_config, get_active_mappings, get_profile_for_app,
    BUTTON_TO_EVENTS, GESTURE_DIRECTION_BUTTONS, save_config,
    WHEEL_DIVERT_OFF, coerce_wheel_divert_setting,
)
from core.app_detector import AppDetector
from core.mouse_hook_types import HidRuntimeState
from core.linux_permissions import (
    linux_permission_log_message,
    linux_permission_report,
    linux_permission_status_message,
)
from core.logi_devices import clamp_dpi

HSCROLL_ACTION_COOLDOWN_S = 0.35
HSCROLL_VOLUME_COOLDOWN_S = 0.06
_VOLUME_ACTIONS = {"volume_up", "volume_down"}


class Engine:
    """
    Core logic: reads config, installs the mouse hook,
    dispatches actions when mapped buttons are pressed,
    and auto-switches profiles when the foreground app changes.
    """

    def __init__(self):
        self.hook = MouseHook()
        self.cfg = load_config()
        self._enabled = True
        self._hscroll_state = {
            MouseEvent.HSCROLL_LEFT: {"accum": 0.0, "last_fire_at": 0.0},
            MouseEvent.HSCROLL_RIGHT: {"accum": 0.0, "last_fire_at": 0.0},
        }
        self._current_profile: str = self.cfg.get("active_profile", "default")
        self._app_detector = AppDetector(self._on_app_change)
        self._profile_change_cb = None       # UI callback
        self._connection_change_cb = None   # UI callback for device status
        self._status_cb = None             # UI callback for status messages
        self._battery_read_cb = None        # UI callback for battery level
        self._dpi_read_cb = None            # UI callback for current DPI
        self._smart_shift_read_cb = None   # UI callback for Smart Shift mode
        self._debug_cb = None               # UI callback for debug messages
        self._gesture_event_cb = None       # UI callback for structured gesture events
        self._gesture_outcome_cb = None     # always-on cb(arrow, label, status, detail)
        self._debug_events_enabled = bool(
            self.cfg.get("settings", {}).get("debug_mode", False)
        )
        self._battery_poll_stop = threading.Event()
        self._battery_poll_thread = None          # track the poller thread
        self._last_connection_state = bool(self._hid_runtime_state().input_ready)
        self._last_hid_features_ready = bool(self.hid_features_ready)
        self._hid_replay_requested_this_launch = False
        self._replay_inflight = False
        self._replay_pending_rerun = False
        self._replay_lock = threading.Lock()
        # Safety auto-release: one monotonic deadline per held action, serviced
        # by a single reusable "SafetyRelease" thread (created on first press)
        # instead of a threading.Timer -- a real OS thread -- per button press.
        self._clock = time.monotonic
        self._release_cv = threading.Condition()
        self._mouse_release_deadlines = {}   # action_id -> monotonic deadline
        self._release_thread = None
        # Device writes triggered from HID callbacks (SmartShift/DPI) run on
        # one FIFO worker so ordering is preserved and no thread is spawned
        # per press. `_workers_stop` retires both helper threads in stop().
        self._workers_stop = threading.Event()
        self._device_write_cv = threading.Condition()
        self._device_write_queue = []   # (name, callable) in submit order
        self._device_write_thread = None
        self._remote_device_server = None  # core/remote_device.py listener
        self._remote_forwarder = None      # core/remote_forward.py bridge client
        self._lock = threading.Lock()
        # HID++ native-invert tracking. `_last_native_invert_target` caches the
        # most recently applied intent (target_active, invert_v, invert_h) so
        # the fast-path skips redundant device round-trips on profile changes;
        # None means "never applied". `_wheel_divert_active_local` is True while
        # firmware is inverting at least one axis.
        self._wheel_divert_change_cb = None
        self._wheel_divert_active_local = False
        self._last_native_invert_target = None
        self.hook.set_debug_callback(self._emit_debug)
        self.hook.set_gesture_callback(self._emit_gesture_event)
        self.hook.set_status_callback(self._emit_status)
        self._setup_hooks()
        self.hook.set_connection_change_callback(self._on_connection_change)
        # Apply persisted DPI setting
        dpi = self.cfg.get("settings", {}).get("dpi", 1000)
        try:
            if hasattr(self.hook, "set_dpi"):
                self.hook.set_dpi(dpi)
        except Exception as e:
            print(f"[Engine] Failed to set DPI: {e}")

    def _hid_runtime_state(self):
        state = getattr(self.hook, "hid_runtime_state", None)
        if state is not None:
            return state
        hg = getattr(self.hook, "_hid_gesture", None)
        hid_device = getattr(hg, "connected_device", None) if hg else None
        return HidRuntimeState(
            input_ready=bool(getattr(self.hook, "device_connected", False)),
            hid_ready=hid_device is not None,
            connected_device=getattr(self.hook, "connected_device", None),
        )

    # ------------------------------------------------------------------
    # Hook wiring
    # ------------------------------------------------------------------
    def _setup_hooks(self):
        """Register callbacks and block events for all mapped buttons."""
        mappings = get_active_mappings(self.cfg)

        # Apply scroll inversion settings to the hook
        settings = self.cfg.get("settings", {})
        self.hook.invert_vscroll = settings.get("invert_vscroll", False)
        self.hook.invert_hscroll = settings.get("invert_hscroll", False)
        if hasattr(self.hook, "ignore_trackpad"):
            self.hook.ignore_trackpad = settings.get("ignore_trackpad", True)
        self.hook.debug_mode = self._debug_events_enabled
        self.hook.configure_gestures(
            enabled=any(mappings.get(key, "none") != "none"
                        for key in GESTURE_DIRECTION_BUTTONS),
            threshold=settings.get("gesture_threshold", 50),
        )
        # Divert mode shift CID only when the device has the button and
        # at least one profile maps it to an action.  When no device is
        # connected yet, assume the button exists (safe: if the device
        # turns out not to have it, the divert simply has no effect).
        device = getattr(self, "connected_device", None)
        device_buttons = getattr(device, "supported_buttons", None)
        has_mode_shift = device_buttons is None or "mode_shift" in device_buttons
        self.hook.divert_mode_shift = (
            has_mode_shift
            and any(
                pdata.get("mappings", {}).get("mode_shift", "none") != "none"
                for pdata in self.cfg.get("profiles", {}).values()
            )
        )

        # Divert DPI switch CID (0x00FD) on MX Vertical when mapped.
        has_dpi_switch = device_buttons is None or "dpi_switch" in device_buttons
        self.hook.divert_dpi_switch = (
            has_dpi_switch
            and any(
                pdata.get("mappings", {}).get("dpi_switch", "none") != "none"
                for pdata in self.cfg.get("profiles", {}).values()
            )
        )

        self._emit_mapping_snapshot("Hook mappings refreshed", mappings)
        # Drive HID++ firmware wheel-invert from settings + device capability.
        self._apply_wheel_invert_setting()

        for btn_key, action_id in mappings.items():
            events = list(BUTTON_TO_EVENTS.get(btn_key, ()))
            has_paired_down = any(e.endswith("_down") for e in events)
            has_up = any(e.endswith("_up") for e in events)

            for evt_type in events:
                if has_paired_down and evt_type.endswith("_up"):
                    if action_id != "none":
                        self.hook.block(evt_type)
                        if is_mouse_button_action(action_id):
                            self.hook.register(evt_type, self._make_mouse_up_handler(action_id))
                    continue

                if action_id != "none":
                    self.hook.block(evt_type)

                    if "hscroll" in evt_type:
                        self.hook.register(evt_type, self._make_hscroll_handler(action_id))
                    elif is_mouse_button_action(action_id):
                        if has_up:
                            # Button has a matching _up event → split press/release
                            self.hook.register(evt_type, self._make_mouse_down_handler(action_id))
                        else:
                            # Single-fire event (gesture, swipe) → full click
                            self.hook.register(evt_type, self._make_handler(action_id))
                    else:
                        self.hook.register(evt_type, self._make_handler(action_id))

        # Settings applied above (invert toggles, blocked events) feed the
        # hook-existence predicate; converge the OS hook to the new state.
        # getattr: engine test fakes predate the dynamic-hook contract.
        getattr(self.hook, "sync_hook_state", lambda: None)()

    def _make_handler(self, action_id):
        def handler(event):
            if not self._enabled:
                return
            is_gesture = event.event_type.startswith("gesture_")
            label = self._action_label(action_id)
            try:
                self._emit_debug(
                    f"Mapped {event.event_type} -> {action_id} ({label})"
                )
                if is_gesture:
                    self._emit_gesture_event({
                        "type": "mapped",
                        "event_name": event.event_type,
                        "action_id": action_id,
                        "action_label": label,
                    })
                if action_id == "toggle_smart_shift":
                    self._toggle_smart_shift()
                elif action_id == "switch_scroll_mode":
                    self._switch_scroll_mode()
                elif action_id == "cycle_dpi":
                    self._cycle_dpi()
                else:
                    execute_action(action_id)
                if is_gesture:
                    self._report_gesture_outcome(event.event_type, label, "fired")
            except Exception as exc:
                import traceback
                print(f"[Engine] ACTION FAILED {event.event_type} -> {action_id}: {exc}")
                traceback.print_exc()
                # Surface the failure loudly so a blocked keystroke injection
                # (e.g. missing Accessibility permission) is never silent.
                if is_gesture:
                    self._report_gesture_outcome(event.event_type, label, "failed", str(exc))
        return handler

    # ------------------------------------------------------------------
    # Safety auto-release (one thread, many deadlines)
    # ------------------------------------------------------------------
    SAFETY_RELEASE_S = 20.0

    def _arm_safety_release(self, action_id):
        """(Re)arm the auto-release deadline for ``action_id``.

        Replaces the per-press ``threading.Timer``: the deadline lives in a
        dict and a single long-lived thread sleeps until the earliest one.
        Re-pressing the same action simply moves its deadline, which is what
        cancelling the old timer and starting a new one used to do.
        """
        with self._release_cv:
            self._mouse_release_deadlines[action_id] = (
                self._clock() + self.SAFETY_RELEASE_S
            )
            thread = self._release_thread
            if thread is None or not getattr(thread, "is_alive", lambda: False)():
                self._release_thread = threading.Thread(
                    target=self._safety_release_loop,
                    daemon=True,
                    name="SafetyRelease",
                )
                self._release_thread.start()
            self._release_cv.notify()

    def _disarm_safety_release(self, action_id):
        with self._release_cv:
            self._mouse_release_deadlines.pop(action_id, None)

    def _safety_release_loop(self):
        while True:
            with self._release_cv:
                # Exit when retired by stop(); also refuse to linger when
                # this loop is not the engine's live release thread (a
                # replaced worker, or a test running the target inline).
                while not self._workers_stop.is_set():
                    owner = self._release_thread is threading.current_thread()
                    if not self._mouse_release_deadlines:
                        if not owner:
                            return
                        self._release_cv.wait()
                        continue
                    action_id, deadline = min(
                        self._mouse_release_deadlines.items(), key=lambda kv: kv[1]
                    )
                    now = self._clock()
                    if deadline <= now:
                        self._mouse_release_deadlines.pop(action_id, None)
                        break
                    if not owner:
                        return
                    self._release_cv.wait(deadline - now)
                else:
                    return
            # Fire outside the lock: inject_mouse_up may take a while.
            try:
                print(f"[Engine] SAFETY RELEASE fired for {action_id} (UP never received)")
                inject_mouse_up(action_id)
            except Exception as exc:
                print(f"[Engine] _safety_release EXCEPTION for {action_id}: {exc}")
                import traceback; traceback.print_exc()

    def _make_mouse_down_handler(self, action_id):
        def handler(event):
            try:
                if self._enabled:
                    self._emit_debug(
                        f"Mapped {event.event_type} -> {action_id} (mouse down)"
                    )
                    inject_mouse_down(action_id)
                    # Safety: auto-release after 20s if UP event is never received
                    self._arm_safety_release(action_id)
            except Exception as exc:
                print(f"[Engine] mouse_down_handler EXCEPTION for {action_id}: {exc}")
                import traceback; traceback.print_exc()
        return handler

    def _make_mouse_up_handler(self, action_id):
        def handler(event):
            try:
                if self._enabled:
                    self._emit_debug(
                        f"Mapped {event.event_type} -> {action_id} (mouse up)"
                    )
                    # Cancel safety deadline
                    self._disarm_safety_release(action_id)
                    inject_mouse_up(action_id)
            except Exception as exc:
                print(f"[Engine] mouse_up_handler EXCEPTION for {action_id}: {exc}")
                import traceback; traceback.print_exc()
        return handler

    # ------------------------------------------------------------------
    # Device-write worker (one FIFO thread for HID-callback-triggered writes)
    # ------------------------------------------------------------------
    def _submit_device_write(self, name, fn):
        """Queue ``fn`` for the single ``DeviceWrite`` thread.

        Called from HID callbacks, where a synchronous ``hg.set_*`` would
        deadlock on the HID loop. One FIFO worker replaces the
        thread-per-press ``ToggleSmartShift``/``SwitchScrollMode``/``CycleDPI``
        threads while keeping submit order, so nothing is dropped or
        reordered.
        """
        with self._device_write_cv:
            self._device_write_queue.append((name, fn))
            thread = self._device_write_thread
            if thread is None or not getattr(thread, "is_alive", lambda: False)():
                self._device_write_thread = threading.Thread(
                    target=self._device_write_loop,
                    daemon=True,
                    name="DeviceWrite",
                )
                self._device_write_thread.start()
            self._device_write_cv.notify()

    def _device_write_loop(self):
        while True:
            with self._device_write_cv:
                while not self._device_write_queue:
                    if self._workers_stop.is_set():
                        return
                    if self._device_write_thread is not threading.current_thread():
                        # Replaced worker or inline (test) runner: drain, then leave.
                        return
                    self._device_write_cv.wait()
                if self._workers_stop.is_set():
                    return
                name, fn = self._device_write_queue.pop(0)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - worker must survive
                print(f"[Engine] {name} device write EXCEPTION: {exc}")
                import traceback; traceback.print_exc()

    def _toggle_smart_shift(self):
        """Toggle SmartShift auto-switching on/off.

        IMPORTANT: this is called from a HID event callback which runs on the HID
        loop thread.  Calling hg.set_smart_shift() directly would block waiting for
        the same loop to process the pending request -- a deadlock that causes the
        3-second timeout seen in the logs.  Config and UI are updated synchronously;
        the device write is dispatched to a separate thread.
        """
        settings = self.cfg.get("settings", {})
        new_enabled = not settings.get("smart_shift_enabled", False)
        mode = settings.get("smart_shift_mode", "ratchet")
        threshold = settings.get("smart_shift_threshold", 25)
        print(f"[Engine] toggle_smart_shift -> enabled={new_enabled}")
        settings["smart_shift_enabled"] = new_enabled
        save_config(self.cfg)
        if self._smart_shift_read_cb:
            try:
                self._smart_shift_read_cb({"mode": mode, "enabled": new_enabled, "threshold": threshold})
            except Exception:
                pass
        hg = self.hook._hid_gesture
        if hg:
            def _write():
                ok = hg.set_smart_shift(mode, new_enabled, threshold)
                print(f"[Engine] toggle_smart_shift device write -> {'OK' if ok else 'FAILED'}")
            self._submit_device_write("toggle_smart_shift", _write)

    def _switch_scroll_mode(self):
        """Switch between ratchet and free-spin (Logi Options+ physical button behaviour).

        SmartShift auto-switching is disabled so the chosen fixed mode takes effect.
        Same deadlock caveat as _toggle_smart_shift -- device write runs off-thread.
        """
        settings = self.cfg.get("settings", {})
        current_mode = settings.get("smart_shift_mode", "ratchet")
        new_mode = "freespin" if current_mode == "ratchet" else "ratchet"
        threshold = settings.get("smart_shift_threshold", 25)
        print(f"[Engine] switch_scroll_mode -> {new_mode}")
        settings["smart_shift_mode"] = new_mode
        settings["smart_shift_enabled"] = False
        save_config(self.cfg)
        if self._smart_shift_read_cb:
            try:
                self._smart_shift_read_cb({"mode": new_mode, "enabled": False, "threshold": threshold})
            except Exception:
                pass
        hg = self.hook._hid_gesture
        if hg:
            def _write():
                ok = hg.set_smart_shift(new_mode, False, threshold)
                print(f"[Engine] switch_scroll_mode device write -> {'OK' if ok else 'FAILED'}")
            self._submit_device_write("switch_scroll_mode", _write)

    _DEFAULT_DPI_PRESETS = [800, 1200, 1600, 2400]

    def _cycle_dpi(self):
        """Cycle through user-configured DPI presets.

        Advances to the next preset in the list.  If the current DPI doesn't
        match any preset, jumps to the first one.  Updates config, notifies
        the UI, and writes to the device off-thread.
        """
        settings = self.cfg.setdefault("settings", {})
        presets = settings.get("dpi_presets") or list(self._DEFAULT_DPI_PRESETS)
        if not presets:
            return
        current_dpi = settings.get("dpi", 1000)
        try:
            idx = presets.index(current_dpi)
            next_idx = (idx + 1) % len(presets)
        except ValueError:
            next_idx = 0
        new_dpi = clamp_dpi(presets[next_idx], self.connected_device)
        print(f"[Engine] cycle_dpi {current_dpi} -> {new_dpi} (preset {next_idx + 1}/{len(presets)})")
        settings["dpi"] = new_dpi
        save_config(self.cfg)
        if self._dpi_read_cb:
            try:
                self._dpi_read_cb(new_dpi)
            except Exception:
                pass
        hg = self.hook._hid_gesture
        if hg:
            def _write():
                hg.set_dpi(new_dpi)
            self._submit_device_write("cycle_dpi", _write)

    def _apply_wheel_invert_setting(self, *, force: bool = False) -> None:
        """Drive HID++ firmware wheel-invert from settings + device
        capability. ``force=True`` re-issues writes even when cached
        state matches the target -- used by ``_run_saved_settings_replay``
        to realign firmware that forgot state after sleep. On success the
        engine + platform hook flip ``wheel_native_invert_active`` so the
        OS-layer inversion path is suppressed; on failure the OS-layer
        path handles inversion."""
        settings = self.cfg.get("settings", {})
        kill_switch_off = (
            coerce_wheel_divert_setting(settings.get("wheel_divert")) == WHEEL_DIVERT_OFF
        )
        invert_v = bool(settings.get("invert_vscroll", False))
        invert_h = bool(settings.get("invert_hscroll", False))
        device = self.connected_device
        physical = self.hook._physical_logitech_bound()
        capable = bool(physical and device and (
            getattr(device, "has_hires_wheel", False)
            or getattr(device, "has_thumbwheel", False)
        ))
        # Stay True even when both invert flags are False so we own the
        # wheel-mode write and a stale invert lease from a crashed Mouser
        # session is reset to native on reconnect.
        target_active = bool(capable and not kill_switch_off)
        hg = self.hook._hid_gesture
        # Early-return cache is the full intent, so a firmware axis that can't
        # honor invert (handled by OS fallback) doesn't get retried on every
        # apply. force=True (sleep/reconnect realignment) always re-issues.
        desired = (target_active, invert_v, invert_h)
        if not force and self._last_native_invert_target == desired:
            return
        # Per-axis firmware-invert outcome. request_wheel_native_invert returns
        # (vertical_ok, horizontal_ok); the axes are independent so one can hold
        # the firmware lease while the other falls back to OS-layer inversion.
        ok_v = ok_h = False
        if hg is not None and hasattr(hg, "request_wheel_native_invert"):
            try:
                if target_active:
                    ok_v, ok_h = hg.request_wheel_native_invert(invert_v, invert_h)
                else:
                    hg.request_wheel_native_invert(False, False)
            except Exception as exc:
                print(f"[Engine] wheel native-invert request failed: {exc}")
                ok_v = ok_h = False
        # An axis holds the firmware lease only when the user wants it inverted
        # AND the firmware confirmed (read-back) it. Otherwise the OS-layer
        # fallback owns that axis.
        active_v = bool(target_active and invert_v and ok_v)
        active_h = bool(target_active and invert_h and ok_h)
        prev_active = self._wheel_divert_active_local
        new_active = bool(active_v or active_h)
        self._wheel_divert_active_local = new_active
        self.hook.wheel_native_invert_vertical = active_v
        self.hook.wheel_native_invert_horizontal = active_h
        # Firmware-invert leases change whether the OS-layer scroll fallback
        # (and therefore the LL hook) is needed at all.
        getattr(self.hook, "sync_hook_state", lambda: None)()
        self._last_native_invert_target = desired
        if hg is not None and hasattr(hg, "set_wheel_divert_active_flags"):
            try:
                hg.set_wheel_divert_active_flags(active_v, active_h)
            except Exception as exc:
                print(f"[Engine] set_wheel_divert_active_flags failed: {exc}")
        # A requested invert the capable firmware could not honor falls back to
        # OS-layer inversion on that axis -- surfaced per-axis, not all-or-nothing.
        fw_failed_v = bool(target_active and invert_v and not ok_v)
        fw_failed_h = bool(target_active and invert_h and not ok_h)
        print(
            "[Engine] wheel native-invert "
            f"vertical={'firmware' if active_v else 'os' if fw_failed_v else 'off'} "
            f"horizontal={'firmware' if active_h else 'os' if fw_failed_h else 'off'} "
            f"capable={capable} kill_switch_off={kill_switch_off} "
            f"invert_v={invert_v} invert_h={invert_h}"
        )
        if fw_failed_v or fw_failed_h:
            axes = ", ".join(
                ax for ax, failed in (("vertical", fw_failed_v), ("horizontal", fw_failed_h))
                if failed
            )
            self._emit_status(
                f"Firmware wheel invert unavailable ({axes}) -- "
                "using OS-level inversion."
            )
        if new_active != prev_active:
            self._notify_wheel_divert_change(new_active)

    def _notify_wheel_divert_change(self, active: bool) -> None:
        if self._wheel_divert_change_cb is None:
            return
        try:
            self._wheel_divert_change_cb(bool(active))
        except Exception as exc:
            print(f"[Engine] wheel divert change callback raised: {exc}")

    def set_wheel_divert_change_callback(self, cb) -> None:
        """Register ``cb(active: bool)`` invoked whenever the HID++ wheel
        divert lease toggles. Fires once immediately with the current
        state. Pass ``None`` to detach the currently registered callback."""
        self._wheel_divert_change_cb = cb
        if cb is None:
            return
        try:
            cb(bool(self._wheel_divert_active_local))
        except Exception as exc:
            print(f"[Engine] wheel divert change callback (initial) raised: {exc}")

    @property
    def wheel_native_invert_active(self) -> bool:
        """True iff the connected device is performing scroll inversion at
        the firmware level (so the OS-layer inversion path is suppressed)."""
        return bool(self._wheel_divert_active_local)

    def _make_hscroll_handler(self, action_id):
        def handler(event):
            if not self._enabled:
                return
            state = self._hscroll_state.setdefault(
                event.event_type,
                {"accum": 0.0, "last_fire_at": 0.0},
            )
            step = self._hscroll_step(event.raw_data)
            threshold = self._hscroll_threshold()
            now = getattr(event, "timestamp", None) or time.time()

            cooldown = HSCROLL_VOLUME_COOLDOWN_S if action_id in _VOLUME_ACTIONS else HSCROLL_ACTION_COOLDOWN_S
            if now - state["last_fire_at"] < cooldown:
                state["accum"] = 0.0
                return

            state["accum"] += step
            if state["accum"] < threshold:
                return

            state["accum"] = 0.0
            state["last_fire_at"] = now
            self._emit_debug(
                f"Mapped {event.event_type} -> {action_id} "
                f"({self._action_label(action_id)})"
            )
            execute_action(action_id)
        return handler

    def _hscroll_step(self, raw_value):
        if not isinstance(raw_value, (int, float)):
            return 1.0

        # Treat large wheel deltas as a single logical step while preserving
        # sub-step deltas from macOS event tap scrolling.
        return min(abs(float(raw_value)), 1.0)

    def _hscroll_threshold(self):
        return max(
            0.1,
            float(self.cfg.get("settings", {}).get("hscroll_threshold", 1)),
        )

    # ------------------------------------------------------------------
    # Per-app auto-switching
    # ------------------------------------------------------------------
    def _on_app_change(self, exe_name: str):
        """Called by AppDetector when foreground window changes."""
        target = get_profile_for_app(self.cfg, exe_name)
        if target == self._current_profile:
            return
        print(f"[Engine] App changed to {exe_name} -> profile '{target}'")
        self._switch_profile(target)

    def _switch_profile(self, profile_name: str):
        with self._lock:
            self.cfg["active_profile"] = profile_name
            self._current_profile = profile_name
            # Lightweight: just re-wire callbacks, keep hook + HID++ alive
            self.hook.reset_bindings()
            self._setup_hooks()
            self._emit_debug(f"Active profile -> {profile_name}")
        # Notify UI (if connected)
        if self._profile_change_cb:
            try:
                self._profile_change_cb(profile_name)
            except Exception:
                pass

    def set_profile_change_callback(self, cb):
        """Register a callback ``cb(profile_name)`` invoked on auto-switch."""
        self._profile_change_cb = cb

    def set_debug_callback(self, cb):
        """Register ``cb(message: str)`` invoked for debug events."""
        self._debug_cb = cb

    def set_status_callback(self, cb):
        """Register ``cb(message: str)`` invoked for status messages."""
        self._status_cb = cb

    def set_gesture_event_callback(self, cb):
        """Register ``cb(event: dict)`` invoked for structured gesture debug events."""
        self._gesture_event_cb = cb

    def set_gesture_outcome_callback(self, cb):
        """Register ``cb(arrow, label, status, detail)`` for EVERY gesture
        outcome. Unlike the debug event channel this always fires, so the UI
        can show on-screen feedback and we always KNOW what a swipe did."""
        self._gesture_outcome_cb = cb

    # Arrow glyphs for on-screen feedback, keyed by gesture event type.
    _GESTURE_ARROWS = {
        "gesture_swipe_up": "↑",
        "gesture_swipe_down": "↓",
        "gesture_swipe_left": "←",
        "gesture_swipe_right": "→",
    }

    def _report_gesture_outcome(self, event_type, label, status, detail=""):
        """Always-on: log one line per gesture outcome and notify the UI.
        ``status`` is "fired" | "failed" | "unmapped"."""
        arrow = self._GESTURE_ARROWS.get(event_type, "•")
        suffix = f": {detail}" if detail else ""
        level = "ERROR" if status == "failed" else "Gesture"
        print(f"[{level}] {arrow} {event_type} -> {label or '(none)'} [{status}]{suffix}")
        cb = self._gesture_outcome_cb
        if cb:
            try:
                cb(arrow, label, status, detail)
            except Exception as exc:  # noqa: BLE001 - UI callback boundary
                print(f"[Engine] gesture outcome callback error: {exc}")

    def set_debug_enabled(self, enabled):
        enabled = bool(enabled)
        self.cfg.setdefault("settings", {})["debug_mode"] = enabled
        self._debug_events_enabled = enabled
        self.hook.debug_mode = enabled
        if enabled:
            self._emit_debug(f"Debug enabled on profile {self._current_profile}")
            self._emit_mapping_snapshot(
                "Current mappings", get_active_mappings(self.cfg)
            )

    def set_debug_events_enabled(self, enabled):
        self._debug_events_enabled = bool(enabled)
        self.hook.debug_mode = self._debug_events_enabled

    def _action_label(self, action_id):
        return ACTIONS.get(action_id, {}).get("label", action_id)

    def _emit_debug(self, message):
        if not self._debug_events_enabled:
            return
        if self._debug_cb:
            try:
                self._debug_cb(message)
            except Exception:
                pass

    def _emit_status(self, message):
        if self._status_cb:
            try:
                self._status_cb(message)
            except Exception:
                pass

    def _emit_gesture_event(self, event):
        if not self._debug_events_enabled:
            return
        if self._gesture_event_cb:
            try:
                self._gesture_event_cb(event)
            except Exception:
                pass

    def _emit_mapping_snapshot(self, prefix, mappings):
        if not self._debug_events_enabled:
            return
        interesting = [
            "gesture",
            "gesture_left",
            "gesture_right",
            "gesture_up",
            "gesture_down",
            "xbutton1",
            "xbutton2",
        ]
        summary = ", ".join(f"{key}={mappings.get(key, 'none')}" for key in interesting)
        self._emit_debug(f"{prefix}: {summary}")

    def _saved_smart_shift_state(self):
        settings = self.cfg.get("settings", {})
        return {
            "mode": settings.get("smart_shift_mode", "ratchet"),
            "enabled": settings.get("smart_shift_enabled", False),
            "threshold": settings.get("smart_shift_threshold", 25),
        }

    def _run_saved_settings_replay(self):
        hg = self.hook._hid_gesture
        if hg is None:
            return False
        if hasattr(hg, "connected_device") and hg.connected_device is None:
            return False

        replay_ok = True
        retry_dpi = False
        retry_smart_shift = False
        saved_dpi = self.cfg.get("settings", {}).get("dpi")

        saved_ss_state = self._saved_smart_shift_state()
        saved_ss = saved_ss_state["mode"]
        ss_enabled = saved_ss_state["enabled"]
        ss_threshold = saved_ss_state["threshold"]

        # Phase A: apply Smart Shift immediately so the physical wheel mode
        # converges before the settled replay.
        if saved_ss and getattr(hg, "smart_shift_supported", False):
            if not hasattr(hg, "set_smart_shift"):
                replay_ok = False
            else:
                if not hg.set_smart_shift(saved_ss, ss_enabled, ss_threshold):
                    replay_ok = False
                if self._smart_shift_read_cb:
                    try:
                        self._smart_shift_read_cb(saved_ss_state)
                    except Exception:
                        pass

        # Phase A.5: re-apply HID++ native wheel invert with force=True so
        # firmware that forgot invert state after sleep is realigned.
        self._apply_wheel_invert_setting(force=True)
        native_invert_target = (
            coerce_wheel_divert_setting(
                self.cfg.get("settings", {}).get("wheel_divert")
            ) != WHEEL_DIVERT_OFF
            and bool(getattr(self.connected_device, "has_hires_wheel", False)
                     or getattr(self.connected_device, "has_thumbwheel", False))
        )
        if native_invert_target and not self._wheel_divert_active_local:
            replay_ok = False

        time.sleep(3)
        hg = self.hook._hid_gesture
        if hg is None or getattr(hg, "connected_device", None) is None:
            return False

        if saved_dpi is not None:
            if not hasattr(hg, "set_dpi"):
                replay_ok = False
            elif hg.set_dpi(saved_dpi):
                if self._dpi_read_cb:
                    try:
                        self._dpi_read_cb(saved_dpi)
                    except Exception:
                        pass
            else:
                replay_ok = False
                retry_dpi = True

        if saved_ss and getattr(hg, "smart_shift_supported", False):
            if not hasattr(hg, "set_smart_shift"):
                replay_ok = False
            elif hg.set_smart_shift(saved_ss, ss_enabled, ss_threshold):
                if self._smart_shift_read_cb:
                    try:
                        self._smart_shift_read_cb(saved_ss_state)
                    except Exception:
                        pass
            else:
                replay_ok = False
                retry_smart_shift = True

        if retry_dpi or retry_smart_shift:
            time.sleep(5)
            hg = self.hook._hid_gesture
            if hg is None or getattr(hg, "connected_device", None) is None:
                return False
            if retry_dpi:
                if not hasattr(hg, "set_dpi") or not hg.set_dpi(saved_dpi):
                    replay_ok = False
                elif self._dpi_read_cb:
                    try:
                        self._dpi_read_cb(saved_dpi)
                    except Exception:
                        pass
            if retry_smart_shift and getattr(hg, "smart_shift_supported", False):
                if not hasattr(hg, "set_smart_shift") or not hg.set_smart_shift(
                    saved_ss, ss_enabled, ss_threshold
                ):
                    replay_ok = False
                elif self._smart_shift_read_cb:
                    try:
                        self._smart_shift_read_cb(saved_ss_state)
                    except Exception:
                        pass

        return replay_ok

    def _replay_saved_settings_worker(self):
        while True:
            with self._replay_lock:
                self._replay_pending_rerun = False
            replay_ok = self._run_saved_settings_replay()
            should_emit_failure = False
            with self._replay_lock:
                if self._replay_pending_rerun:
                    continue
                self._replay_inflight = False
                should_emit_failure = not replay_ok
            if should_emit_failure:
                self._emit_status(
                    "Mouse reconnected, but saved device settings could not be restored yet."
                )
            return

    def _request_saved_settings_replay(self, *, startup_fallback=False):
        with self._replay_lock:
            if startup_fallback and self._hid_replay_requested_this_launch:
                return
            if self._replay_inflight:
                self._replay_pending_rerun = True
                return
            self._hid_replay_requested_this_launch = True
            self._replay_inflight = True
        if startup_fallback:
            self._emit_status("Using startup fallback to replay saved device settings")
        threading.Thread(
            target=self._replay_saved_settings_worker,
            daemon=True,
            name="SavedSettingsReplay",
        ).start()

    def _on_connection_change(self, connected):
        connection_changed = connected != self._last_connection_state
        hid_features_ready = self.hid_features_ready
        hid_features_changed = hid_features_ready != self._last_hid_features_ready
        if connection_changed:
            # Device lifecycle goes to the proto-2 bridge (Deskflow dials
            # us; ``role: server`` peers cache the connect line) and, while
            # it is still running, the legacy dial-out forwarder.
            for label, relay in (
                ("bridge", self._remote_device_server),
                ("remote forwarder", self._remote_forwarder),
            ):
                if relay is None or not hasattr(relay, "notify_device_connected"):
                    continue
                try:
                    if connected:
                        relay.notify_device_connected(self.hook.connected_device)
                    else:
                        relay.notify_device_disconnected()
                except Exception as exc:  # noqa: BLE001 - relay boundary
                    print(f"[Engine] {label} notify failed: {exc!r}")
        if connection_changed:
            self._last_connection_state = connected
            self._retire_battery_poller()
        self._last_hid_features_ready = hid_features_ready
        if self._connection_change_cb:
            try:
                self._connection_change_cb(connected)
            except Exception:
                pass
        if connected and connection_changed:
            self._start_battery_poller()
        if hid_features_ready and hid_features_changed:
            self._request_saved_settings_replay()
            if self._remote_forwarder is not None:
                try:
                    self._remote_forwarder.notify_decode_changed()
                except Exception as exc:  # noqa: BLE001 - relay boundary
                    print(f"[Engine] remote forwarder decode notify failed: {exc!r}")
            bridge = self._remote_device_server
            if bridge is not None and hasattr(bridge, "notify_decode_changed"):
                try:
                    bridge.notify_decode_changed()
                except Exception as exc:  # noqa: BLE001 - relay boundary
                    print(f"[Engine] bridge decode notify failed: {exc!r}")

    def _retire_battery_poller(self):
        """Signal the current poller to exit; never block on it.

        This runs on the HID loop thread. The poller may be inside
        ``hg.read_battery()`` waiting on that very thread, so a ``join`` here
        (even with a timeout) either deadlocks for the timeout or returns
        with the old poller still running. Every poller owns its stop Event
        and all its waits are Event-based, so setting the flag is enough for
        it to exit as soon as its current HID round-trip returns.
        """
        self._battery_poll_stop.set()
        self._battery_poll_thread = None

    def _start_battery_poller(self):
        """Start exactly one BatteryPoll thread for the connected device."""
        previous = self._battery_poll_thread
        if previous is not None:
            # Should not happen: connect without a disconnect in between.
            # Retire it so there is never more than one live poller.
            print("[Engine] battery poller already running; replacing it")
            self._retire_battery_poller()
        self._battery_poll_stop = threading.Event()
        self._battery_poll_thread = threading.Thread(
            target=self._battery_poll_loop,
            args=(self._battery_poll_stop,),
            daemon=True,
            name="BatteryPoll",
        )
        self._battery_poll_thread.start()

    def _battery_poll_loop(self, stop_event):
        """Read battery and smart shift mode periodically until disconnected.

        Every wait is ``stop_event``-based so a retire is honored promptly.
        """
        _battery_poll_interval = 300   # seconds between battery reads
        _ss_poll_interval = 15         # seconds between scroll-mode reads
        _last_battery = time.time() - _battery_poll_interval  # fire immediately
        _last_ss = time.time() - _ss_poll_interval            # fire immediately
        _last_ss_mode = None

        while not stop_event.is_set():
            now = time.time()
            hg = self.hook._hid_gesture
            if hg and hg.connected_device is not None:
                if now - _last_battery >= _battery_poll_interval:
                    _last_battery = now
                    level = hg.read_battery()
                    if stop_event.is_set():
                        return
                    if level is not None and self._battery_read_cb:
                        try:
                            self._battery_read_cb(level)
                        except Exception:
                            pass

                # Read ``_replay_inflight`` under the same lock that the
                # replay thread uses to flip it, otherwise the battery
                # loop can issue a Smart Shift poll partway through a
                # replay round-trip and the firmware queues conflicting
                # HID++ writes.
                with self._replay_lock:
                    replay_inflight = self._replay_inflight
                if (
                    not replay_inflight
                    and now - _last_ss >= _ss_poll_interval
                    and hg.smart_shift_supported
                ):
                    _last_ss = now
                    ss_mode = hg.read_smart_shift()
                    if stop_event.is_set():
                        return
                    if ss_mode is not None:
                        if ss_mode != _last_ss_mode:
                            print(f"[Engine] Scroll mode: {ss_mode}"
                                  + (" (changed)" if _last_ss_mode is not None else ""))
                            _last_ss_mode = ss_mode
                        if self._smart_shift_read_cb:
                            try:
                                self._smart_shift_read_cb(ss_mode)
                            except Exception:
                                pass

            if stop_event.wait(5):
                return

    def set_battery_callback(self, cb):
        """Register ``cb(level: int)`` invoked when battery level is read (0-100)."""
        self._battery_read_cb = cb

    def set_connection_change_callback(self, cb):
        """Register ``cb(connected: bool)`` invoked on device connect/disconnect."""
        self._connection_change_cb = cb
        if cb:
            try:
                cb(bool(self._hid_runtime_state().input_ready))
            except Exception:
                pass

    @property
    def device_connected(self):
        return self._hid_runtime_state().input_ready

    @property
    def connected_device(self):
        return self._hid_runtime_state().connected_device

    def dump_device_info(self):
        return getattr(self.hook, "dump_device_info", lambda: None)()

    @property
    def hid_features_ready(self):
        return self._hid_runtime_state().hid_ready

    @property
    def enabled(self):
        return self._enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_dpi(self, dpi_value):
        """Send DPI change to the mouse via HID++."""
        dpi = clamp_dpi(dpi_value, self.connected_device)
        self.cfg.setdefault("settings", {})["dpi"] = dpi
        save_config(self.cfg)
        # Try via the hook's HidGestureListener
        hg = self.hook._hid_gesture
        if hg:
            return hg.set_dpi(dpi)
        print("[Engine] No HID++ connection -- DPI not applied")
        return False

    def set_smart_shift(self, mode, smart_shift_enabled=False, threshold=25):
        """Send Smart Shift settings to device.
        mode: 'ratchet' or 'freespin' (fixed mode when smart_shift_enabled=False)
        smart_shift_enabled: True to enable auto SmartShift
        threshold: 1-50 sensitivity when SmartShift is enabled"""
        print(f"[Engine] set_smart_shift({mode}, enabled={smart_shift_enabled}, threshold={threshold}) called")
        settings = self.cfg.setdefault("settings", {})
        settings["smart_shift_mode"] = mode
        settings["smart_shift_enabled"] = smart_shift_enabled
        settings["smart_shift_threshold"] = threshold
        save_config(self.cfg)
        hg = self.hook._hid_gesture
        if hg:
            result = hg.set_smart_shift(mode, smart_shift_enabled, threshold)
            print(f"[Engine] set_smart_shift -> {'OK' if result else 'FAILED'}")
            return result
        print("[Engine] set_smart_shift: No HID++ connection -- not applied")
        return False

    @property
    def smart_shift_supported(self):
        hg = self.hook._hid_gesture
        return hg.smart_shift_supported if hg else False

    def reload_mappings(self):
        """
        Called by the UI when the user changes a mapping.
        Re-wire callbacks without tearing down the hook or HID++.
        """
        with self._lock:
            self.cfg = load_config()
            self._current_profile = self.cfg.get("active_profile", "default")
            self.hook.reset_bindings()
            self._setup_hooks()
            self._emit_debug(f"reload_mappings profile={self._current_profile}")

    def reload_kvm_integration(self):
        """Restart Deskflow / KVM bridge clients after a settings change."""
        with self._lock:
            self.cfg = load_config()
            self._stop_remote_forwarder()
            # "restart": Deskflow reconnects immediately instead of backing off.
            self._stop_remote_device_server(reason="restart")
            self._start_remote_device_server()
            self._start_remote_forwarder()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)

    def set_ui_passthrough(self, enabled):
        if hasattr(self.hook, "set_ui_passthrough"):
            self.hook.set_ui_passthrough(enabled)

    def _emit_linux_permission_warning(self):
        report = linux_permission_report()
        log_message = linux_permission_log_message(report)
        if log_message:
            print(log_message)
        status_message = linux_permission_status_message(report)
        if status_message:
            self._emit_status(status_message)

    def _resolve_deskflow_integration(self):
        from core.deskflow_integration import resolve_integration

        return resolve_integration(self.cfg) or {}

    def _start_remote_device_server(self):
        """Start the loopback bridge (``core/bridge_server.py``).

        Mouser owns ``127.0.0.1:19795`` for both directions of the Deskflow
        integration; Deskflow dials it (proto 2, ``bridge.token``). Runs
        whenever Deskflow integration is on (the default) or the legacy
        ``settings.remote_device.enabled`` flag is set. Protocol-1 peers are
        still accepted on the same port when a legacy token is known
        (``settings.remote_device.token`` or Deskflow.conf/manifest).
        """
        settings = self.cfg.get("settings", {}) or {}
        remote_cfg = settings.get("remote_device", {}) or {}
        deskflow_cfg = settings.get("deskflow", {}) or {}
        from core.bridge_server import BridgeServer
        from core.deskflow_integration import use_transparent_transport
        from core.remote_device import DEFAULT_PORT

        deskflow = self._resolve_deskflow_integration()
        auto = deskflow_cfg.get("auto") is not False
        enabled = auto or bool(remote_cfg.get("enabled", False))
        if not enabled:
            return

        legacy_token = str(remote_cfg.get("token") or "")
        if not legacy_token and deskflow and deskflow.get("client_sink"):
            legacy_token = str(deskflow.get("token") or "")
        port = DEFAULT_PORT
        for candidate in (remote_cfg.get("port"), (deskflow or {}).get("port")):
            try:
                if candidate not in (None, ""):
                    port = int(candidate)
                    break
            except (TypeError, ValueError):
                continue

        transparent = use_transparent_transport(self.cfg) or (
            auto and deskflow_cfg.get("transparent_transport") is not False
        )
        server = BridgeServer(
            self.hook,
            port=port,
            legacy_token=legacy_token,
            status_cb=self._emit_status,
            decode_override=remote_cfg.get("decode"),
            transparent_transport=transparent,
            decode_supplier=lambda: self.hook.gesture_decode_context(),
            device_supplier=lambda: self.hook.connected_device,
            on_proto2_seen=self._on_bridge_proto2_seen,
        )
        if server.start():
            self._remote_device_server = server
            # The bridge is the hook's focus gate (``should_forward``): local
            # remaps stand down while a server-role Deskflow says the cursor
            # is on another screen. A still-running legacy forwarder takes
            # the slot over in _start_remote_forwarder and hands it back in
            # _stop_remote_forwarder.
            if self._remote_forwarder is None:
                self.hook.set_remote_forwarder(server)

    def _on_bridge_proto2_seen(self):
        """First proto-2 hello: persist the marker and retire the legacy
        dial-out (Deskflow now dials Mouser). Runs on a bridge thread."""
        with self._lock:
            settings = self.cfg.setdefault("settings", {})
            if settings.get("bridge_proto") != 2:
                settings["bridge_proto"] = 2
                try:
                    save_config(self.cfg)
                except Exception as exc:  # noqa: BLE001 - persist boundary
                    print(f"[Engine] could not persist bridge_proto: {exc!r}")
            if self._remote_forwarder is not None:
                print("[Engine] proto-2 bridge peer seen; stopping legacy dial-out")
                self._stop_remote_forwarder()

    def _legacy_dial_enabled(self) -> bool:
        """Legacy ``RemoteForwarder`` dial-out to Deskflow:19796 stays on
        until the bridge token file exists AND a proto-2 hello has been seen
        once (``settings.bridge_proto == 2``). ``settings.deskflow.legacy_dial``
        forces it either way."""
        settings = self.cfg.get("settings", {}) or {}
        explicit = (settings.get("deskflow", {}) or {}).get("legacy_dial")
        if isinstance(explicit, bool):
            return explicit
        import os
        from core.bridge_server import token_path

        try:
            proto = int(settings.get("bridge_proto") or 0)
        except (TypeError, ValueError):
            proto = 0
        return not (proto >= 2 and os.path.isfile(token_path()))

    def _stop_remote_device_server(self, reason="shutdown"):
        server = self._remote_device_server
        if server is None:
            return
        if getattr(self.hook, "_remote_forwarder", None) is server:
            self.hook.set_remote_forwarder(None)
        try:
            server.stop(reason)
        except Exception as exc:  # noqa: BLE001 - shutdown must complete
            print(f"[Engine] stop: remote device server raised: {exc!r}")
        self._remote_device_server = None

    def _start_remote_forwarder(self):
        """Start the legacy KVM-bridge dial-out when still needed.

        Deskflow (proto 2) dials Mouser's bridge instead; this loop is only
        kept for un-upgraded Deskflow builds (see ``_legacy_dial_enabled``).
        """
        if not self._legacy_dial_enabled():
            return
        fwd_cfg = self.cfg.get("settings", {}).get("remote_forward", {}) or {}
        from core.remote_forward import DEFAULT_BRIDGE_PORT, RemoteForwarder

        deskflow = self._resolve_deskflow_integration()
        enabled = bool(fwd_cfg.get("enabled", False))
        decode_only = bool(fwd_cfg.get("passthrough_decode_only", False))
        token = str(fwd_cfg.get("token") or "")
        host = str(fwd_cfg.get("host", "127.0.0.1") or "127.0.0.1")
        port = DEFAULT_BRIDGE_PORT

        if deskflow and deskflow.get("host_bridge"):
            enabled = True
            if not token:
                token = str(deskflow.get("bridge_token") or "")
            try:
                port = int(fwd_cfg.get("port", deskflow.get("bridge_port", DEFAULT_BRIDGE_PORT)))
            except (TypeError, ValueError):
                port = int(deskflow.get("bridge_port", DEFAULT_BRIDGE_PORT))
        elif not enabled:
            return
        else:
            try:
                port = int(fwd_cfg.get("port", DEFAULT_BRIDGE_PORT))
            except (TypeError, ValueError):
                port = DEFAULT_BRIDGE_PORT

        if not token:
            return

        # Re-read the host on every connection attempt. Deskflow re-elects a
        # server whenever it restarts, so a value captured here goes stale the
        # first time the fleet flips and the bridge never relinks.
        def _live_host(default_host=host):
            try:
                cfg = self.config.get("settings", {}) or {}
                live = str((cfg.get("remote_forward", {}) or {}).get("host", "") or "")
                return live or default_host
            except Exception:
                return default_host

        forwarder = RemoteForwarder(
            token=token,
            host=_live_host,
            port=port,
            device_supplier=lambda: self.hook.connected_device,
            decode_supplier=lambda: self.hook.gesture_decode_context(),
            status_cb=self._emit_status,
            decode_only=decode_only,
        )
        if forwarder.start():
            self._remote_forwarder = forwarder
            self.hook.set_remote_forwarder(forwarder)
            if decode_only:
                self._schedule_decode_publish()
            elif deskflow and deskflow.get("host_bridge"):
                self._emit_status("Deskflow device sharing auto-enabled")

    def _schedule_decode_publish(self):
        """Poll until feat_idx is ready and published to the bridge.

        Until this lands the far machine cannot decode gestures, so the
        interval is the delay between plugging in / regaining the mouse and
        gestures actually working. A flat 200ms poll spent most of that time
        asleep; ramp instead -- near-instant when the device is ready (the
        normal case) and still patient when the feature index takes a while.
        """
        def poll():
            delay = 0.02
            waited = 0.0
            while waited < 10.0:
                fwd = self._remote_forwarder
                if fwd is None:
                    return
                try:
                    fwd.notify_decode_changed()
                except Exception as exc:  # noqa: BLE001 - relay boundary
                    print(f"[Engine] decode publish poll failed: {exc!r}")
                    return
                if fwd.decode_published:
                    return
                time.sleep(delay)
                waited += delay
                delay = min(delay * 1.6, 0.2)

        threading.Thread(
            target=poll, daemon=True, name="DecodePublishPoll"
        ).start()

    def _stop_remote_forwarder(self):
        if self._remote_forwarder is None:
            return
        # Hand the focus gate back to the bridge (or clear it).
        self.hook.set_remote_forwarder(self._remote_device_server)
        try:
            self._remote_forwarder.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must complete
            print(f"[Engine] stop: remote forwarder raised: {exc!r}")
        self._remote_forwarder = None

    def start(self):
        self._emit_linux_permission_warning()
        self.hook.start()
        self._app_detector.start()
        self._start_remote_device_server()
        self._start_remote_forwarder()
        # Temporary safety-net: keep the old delayed replay path until the
        # hid-ready transition path has proven out in the field.
        def _startup_replay_fallback():
            time.sleep(3)
            if not self.hid_features_ready:
                return
            self._request_saved_settings_replay(startup_fallback=True)
        threading.Thread(target=_startup_replay_fallback, daemon=True).start()

    def set_dpi_read_callback(self, cb):
        """Register a callback ``cb(dpi_value)`` invoked when DPI is read from device."""
        self._dpi_read_cb = cb

    def set_smart_shift_read_callback(self, cb):
        """Register a callback ``cb(state)`` invoked when Smart Shift is read."""
        self._smart_shift_read_cb = cb

    def stop(self, reason="shutdown"):
        """``reason`` is relayed to bridge peers as ``{"t":"bye"}`` --
        ``"shutdown"`` (default) or ``"restart"`` when the app relaunches."""
        self._stop_remote_forwarder()
        self._stop_remote_device_server(reason=reason)
        poller = self._battery_poll_thread
        self._retire_battery_poller()
        # Retire the helper threads before the hook goes away. Clearing the
        # deadlines first means a pending safety auto-release can no longer
        # fire ``inject_mouse_up`` against a torn-down hook (a phantom
        # release on every quit during a long press).
        self._workers_stop.set()
        with self._release_cv:
            self._mouse_release_deadlines.clear()
            self._release_cv.notify_all()
        with self._device_write_cv:
            self._device_write_cv.notify_all()
        # stop() runs on the main thread, not the HID thread, so a bounded
        # join is safe here; the pollers' waits are Event-based, so this
        # returns as soon as any in-flight HID round-trip completes.
        for thread in (poller, self._release_thread, self._device_write_thread):
            if thread is not None and getattr(thread, "is_alive", lambda: False)():
                thread.join(timeout=5)
        self._release_thread = None
        self._device_write_thread = None
        self._app_detector.stop()
        self.hook.stop()
