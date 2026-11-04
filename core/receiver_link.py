"""Receiver-level wheel invert and re-link handling (ported from the fork).

Two receiver behaviours upstream's listener does not cover, as a layer
called from four small hooks in ``core/hid_gesture.py``:

1. **Re-link re-apply** (fork 90cf718). When the bound mouse re-links to
   its Unifying/Bolt receiver (wake, power cycle, Easy-Switch back) the
   receiver HID device never goes away, so no removal/arrival fires -- but
   the mouse firmware has reset its diverts, wheel invert and SmartShift.
   The receiver's HID++ 1.0 device-connection notice (sub_id 0x41) for our
   slot triggers a full reconnect, debounced to one per burst.
2. **Companion sweep** (fork 4c9ca3b / aae2ed5). All mice on one receiver
   share a single OS HID device, so the OS layer cannot invert them
   separately; firmware is the only per-mouse place. After the bound mouse
   gets its firmware invert, every *other* online mouse on the receiver
   gets the same intent -- again whenever a companion links, and only when
   the intent or the session changed (not on every re-apply).

Direct (USB cable / Bluetooth, devIdx 0xFF) devices never see either path.
Runs on the listener thread only.
"""

from __future__ import annotations

import time

DJ_DEVICE_CONNECTION = 0x41
LINK_LOST_FLAG = 0x40
LINK_REAPPLY_DEBOUNCE_S = 5.0
COMPANION_SLOTS = range(1, 7)
COMPANION_TIMEOUT_MS = 300
DIRECT_DEV_IDX = 0xFF


def _hg():
    from core import hid_gesture
    return hid_gesture


def handle_report(listener, raw, msg) -> bool:
    """Consume a receiver connection notice; True when it was one."""
    dev, feat, _func, _sw, params = msg
    if feat != DJ_DEVICE_CONNECTION or dev == DIRECT_DEV_IDX:
        return False
    hg = _hg()
    # Short (HID++ 1.0) notice only. On Windows hidapi strips the report ID,
    # so raw[0] is the devIdx there; only an explicit LONG_ID is rejected.
    if not raw or raw[0] == hg.LONG_ID:
        return False
    bound = getattr(listener, "_dev_idx", DIRECT_DEV_IDX)
    if bound == DIRECT_DEV_IDX:
        return False
    flags = params[0] if params else 0
    link_lost = bool(flags & LINK_LOST_FLAG)
    if dev == bound:
        if link_lost:
            print("[HidGesture] receiver: device link lost (mouse asleep/off)")
            return True
        now = time.monotonic()
        if now - getattr(listener, "_last_link_reapply", -1e9) < LINK_REAPPLY_DEBOUNCE_S:
            return True
        listener._last_link_reapply = now
        print("[HidGesture] receiver: device link established -- re-applying settings")
        listener.force_reconnect()
        return True
    if not link_lost:
        print(f"[HidGesture] receiver: companion devIdx=0x{dev:02X} linked -- re-applying wheel invert")
        listener._pending_companion_sweep = True
    return True


def enable_notifications(listener) -> None:
    """Ask the receiver to report wireless (dis)connections (HID++ 1.0
    register 0x00, flags wireless|software-present). Best effort."""
    if getattr(listener, "_dev_idx", DIRECT_DEV_IDX) == DIRECT_DEV_IDX:
        return
    dev = getattr(listener, "_dev", None)
    if dev is None:
        return
    try:
        dev.write([_hg().SHORT_ID, 0xFF, 0x80, 0x00, 0x00, 0x09, 0x00])
    except Exception as exc:
        print(f"[HidGesture] receiver notification enable failed: {exc}")


def on_native_invert_applied(listener, invert_v, invert_h, success) -> None:
    """Schedule a companion sweep when the intent or the session changed."""
    if not success:
        return
    target = (bool(invert_v), bool(invert_h))
    session = id(getattr(listener, "_dev", None))
    if (getattr(listener, "_companion_invert_target", None) != target
            or getattr(listener, "_companion_swept_session", None) != session):
        listener._companion_invert_target = target
        listener._companion_swept_session = session
        listener._pending_companion_sweep = True


def drain(listener) -> None:
    if getattr(listener, "_pending_companion_sweep", False):
        sweep_companions(listener)


def sweep_companions(listener) -> None:
    """Apply the invert intent to every other online mouse on the receiver."""
    hg = _hg()
    listener._pending_companion_sweep = False
    target = getattr(listener, "_companion_invert_target", None)
    bound = getattr(listener, "_dev_idx", DIRECT_DEV_IDX)
    if target is None or bound == DIRECT_DEV_IDX or getattr(listener, "_dev", None) is None:
        return
    invert_v, invert_h = target
    saved = (listener._dev_idx, listener._hires_wheel_idx, listener._thumbwheel_idx,
             listener._hires_wheel_mode_initial, listener._consecutive_request_timeouts)
    results = {}
    try:
        for slot in COMPANION_SLOTS:
            if slot == bound:
                continue
            listener._dev_idx = slot
            listener._hires_wheel_mode_initial = None
            hw = listener._find_feature(hg.FEAT_HIRES_WHEEL_ENHANCED, timeout_ms=COMPANION_TIMEOUT_MS)
            if hw is None:
                continue
            listener._hires_wheel_idx = hw
            listener._thumbwheel_idx = listener._find_feature(
                hg.FEAT_THUMB_WHEEL, timeout_ms=COMPANION_TIMEOUT_MS)
            ok_v = listener._set_native_wheel_invert_vertical(invert_v)
            ok_h = (listener._set_native_wheel_invert_horizontal(invert_h)
                    if listener._thumbwheel_idx else (not invert_h))
            results[slot] = (ok_v, ok_h)
    except Exception as exc:
        print(f"[HidGesture] companion invert sweep error: {exc}")
    finally:
        (listener._dev_idx, listener._hires_wheel_idx, listener._thumbwheel_idx,
         listener._hires_wheel_mode_initial, listener._consecutive_request_timeouts) = saved
    listener.companion_invert_state = results
    if results:
        print("[HidGesture] companion wheel invert: " + ", ".join(
            f"devIdx=0x{k:02X} v={'OK' if v[0] else 'FAIL'} h={'OK' if v[1] else 'FAIL'}"
            for k, v in sorted(results.items())))
