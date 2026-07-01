"""Per-device wheel-invert validation: prove each scroll axis is resolved and
applied INDEPENDENTLY from the device's runtime wheel-feature profile.

The wheel story differs by device along two independent axes:

  * vertical invert  -> HID++ Hi-Res Wheel Enhanced (0x2121), key fn invert bit
  * horizontal invert -> HID++ Thumbwheel (0x2150), setThumbwheelReporting invert

A device may firmware-invert one axis, the other, both, or neither -- and even
when a feature is *present* the firmware may not *honor* the invert write (the
original MX Master advertises 0x2150 but rejects invertDirection=1). Mouser
resolves each axis on its own and verifies firmware writes by read-back, so a
limit on one axis never disturbs the other and an unsupported axis cleanly falls
back to OS-level inversion.

This file asserts:
  1. resolve_capabilities maps wheel-feature presence to per-axis capability.
  2. The original MX Master profile (both features present, thumbwheel invert
     REJECTED) keeps the vertical firmware lease while horizontal falls back --
     driven through the real _apply_pending_native_wheel_invert path.

No hardware required. See also docs/wheel-invert-per-device.md.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core import hid_gesture
from core.device_capabilities import (
    FEAT_HIRES_WHEEL_ENHANCED,
    FEAT_THUMB_WHEEL,
    resolve_capabilities,
)
from core.logi_devices import resolve_device


def _resp(params):
    return (0xFF, 0x12, 0x0, 0x0, list(params))


def _stateful_wheel_router(*, vertical_honors=True, thumb_honors=True):
    """Simulate a device that HONORS (or not) each axis's invert write, with a
    read-back that reflects the last honored write. 0x07 = hi-res wheel
    (mode byte0, invert bit 0x04); 0x08 = thumbwheel (status[1] = invert)."""
    state = {"vmode": 0x00, "tinvert": 0x00}

    def _route(feat, func, params, timeout_ms=2000):
        if feat == 0x07 and func == 1:
            return _resp([state["vmode"]])
        if feat == 0x07 and func == 2:
            if vertical_honors:
                state["vmode"] = int(params[0]) & 0xFF
            return _resp([0])
        if feat == 0x08 and func == 1:
            return _resp([0x00, state["tinvert"]])
        if feat == 0x08 and func == 2:
            if thumb_honors:
                state["tinvert"] = int(params[1]) & 0x01
            return _resp([0])
        return _resp([0])

    return _route


class WheelCapabilityResolutionTests(unittest.TestCase):
    """resolve_capabilities maps wheel-feature presence to per-axis capability,
    each axis independent."""

    def test_mx_master_family_has_both_axes(self):
        caps = resolve_capabilities(
            resolve_device(product_id=0xB012),
            [],
            {FEAT_HIRES_WHEEL_ENHANCED: 0x0C, FEAT_THUMB_WHEEL: 0x13},
        )
        self.assertTrue(caps.wheel_invert_vertical.supported)
        self.assertTrue(caps.wheel_invert_horizontal.supported)

    def test_hires_only_device_has_no_horizontal_firmware_axis(self):
        # MX Anywhere at runtime: hi-res wheel present, no thumbwheel. Horizontal
        # invert has no firmware path -> resolves to the OS-level fallback.
        caps = resolve_capabilities(
            resolve_device(product_id=0xB037),  # mx_anywhere_3s
            [],
            {FEAT_HIRES_WHEEL_ENHANCED: 0x0C},  # 0x2150 absent
        )
        self.assertTrue(caps.wheel_invert_vertical.supported)
        self.assertNotEqual(
            caps.wheel_invert_horizontal.supported, True,
            "no thumbwheel feature -> horizontal is not firmware-capable",
        )

    def test_gaming_mouse_has_no_firmware_wheel_invert(self):
        caps = resolve_capabilities(resolve_device(product_id=0xC08B), [], {})
        self.assertNotEqual(caps.wheel_invert_vertical.supported, True)
        self.assertNotEqual(caps.wheel_invert_horizontal.supported, True)


class OriginalMxMasterWheelTests(unittest.TestCase):
    """The real device: 0x2121 and 0x2150 both PRESENT, but the thumbwheel
    firmware rejects invert (read-back stays non-inverted). Verify-after-write
    catches it; the axes stay independent."""

    def _listener(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev = object()  # non-None: _write_verify gates on a live device
        listener._hires_wheel_idx = 0x07
        listener._thumbwheel_idx = 0x08
        return listener

    def test_vertical_firmware_horizontal_rejected_stays_per_axis(self):
        listener = self._listener()
        with patch.object(
            listener, "_request",
            side_effect=_stateful_wheel_router(vertical_honors=True, thumb_honors=False),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
        # Per-axis result: vertical honored, horizontal NOT.
        self.assertEqual(listener._wheel_divert_result, (True, False))
        # Vertical firmware invert was written once and NEVER reverted.
        vertical_writes = [
            c.args[2] for c in req.call_args_list if c.args[:2] == (0x07, 2)
        ]
        self.assertEqual(vertical_writes, [[0x04]])

    def test_both_honored_when_firmware_cooperates(self):
        listener = self._listener()
        with patch.object(
            listener, "_request",
            side_effect=_stateful_wheel_router(vertical_honors=True, thumb_honors=True),
        ):
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
        self.assertEqual(listener._wheel_divert_result, (True, True))


if __name__ == "__main__":
    unittest.main()
