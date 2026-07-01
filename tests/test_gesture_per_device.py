"""Per-device gesture validation: prove the gesture path is chosen ADAPTIVELY
from each device's runtime HID++ profile, never hard-coded per model.

The HID++ gesture story differs by generation along three axes:

  1. Is there a divertable gesture control at all? (none -> no gestures)
  2. Does that control advertise the rawXY capability flag (key flag 0x0100)?
       * yes -> firmware streams swipe motion over HID++ and pins the cursor
                itself  (gesture_source = "rawxy")
       * no  -> Mouser reads OS cursor motion via the event tap and
                warp-restores the pointer  (gesture_source = "event_tap")
  3. Is gestures driven by a separate Sense Panel (0x01A0) with the small
     button (0x00C3) repurposed as a Thumb button? (MX Master 4 family)

Confirmed ground truth used below:
  * original MX Master (0xB012): 0x00C3 flags=0x0031 -> NO rawXY  (live logs)
  * MX Master 3S: gesture button 0xC3 "raw_XY: yes"  (libratbag device data)
  * MX Master 4: gestures via Sense Panel 0x01A0 (rawXY), 0x00C3 = thumb button

These tests feed representative REPROG_V4 control dumps for each family through
the SAME code the live connect path uses -- _choose_gesture_candidates + _divert
(which sets _rawxy_enabled) and resolve_capabilities -- and assert the resolved
gesture path. No hardware required; adding a new device is a new row, not new
branching.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core import hid_gesture
from core.device_capabilities import resolve_capabilities
from core.logi_devices import resolve_device

# REPROG_V4 key-flag bits (mirror core.hid_gesture / device_capabilities).
_MSE = 0x0001
_REPROG = 0x0010
_DIVERTABLE = 0x0020
_RAW_XY = 0x0100

# Common controls.
SMART_SHIFT = {"cid": 0x00C4, "flags": _MSE | _REPROG | _DIVERTABLE, "mapping_flags": 0x0000}
# Gesture button WITHOUT rawXY (original MX Master era): flags 0x0031.
GESTURE_BTN_NO_RAWXY = {"cid": 0x00C3, "flags": _MSE | _REPROG | _DIVERTABLE, "mapping_flags": 0x0000}
# Gesture button WITH rawXY (MX Master 3/3S): flags 0x0131.
GESTURE_BTN_RAWXY = {"cid": 0x00C3, "flags": _MSE | _REPROG | _DIVERTABLE | _RAW_XY, "mapping_flags": 0x0000}
# MX Master 4 Sense Panel: rawXY-capable virtual gesture control.
SENSE_PANEL = {"cid": 0x01A0, "flags": _MSE | _REPROG | _DIVERTABLE | _RAW_XY | 0x0080, "mapping_flags": 0x0000}
# Plain gaming-mouse button (no gesture CID present).
PLAIN_BTN = {"cid": 0x0050, "flags": _MSE | _REPROG, "mapping_flags": 0x0000}


def _run_divert(spec, controls, *, reject_cids=()):
    """Drive the real candidate-selection + divert path against a control dump.

    ``reject_cids`` are CIDs whose setCidReporting fails (simulates a firmware
    that rejects a divert -- e.g. MX Master 4 refusing the Sense Panel divert,
    forcing the 0x00C3 fallback). A CID absent from ``controls`` is also
    rejected, mirroring how the firmware NAKs an unknown control.

    Returns ``(active_gesture_cid, rawxy_enabled)`` after divert.
    """
    listener = hid_gesture.HidGestureListener()
    listener._feat_idx = 0x08
    present = {c["cid"] for c in controls}
    listener._gesture_candidates = listener._choose_gesture_candidates(controls, device_spec=spec)
    # Install the thumb_button hint so button-only routing matches the live path.
    thumb_cid = getattr(spec, "thumb_button_cid", None)
    if thumb_cid is not None:
        listener._button_only_cids = {int(thumb_cid)}

    def fake_set_cid_reporting(cid, flags):
        if cid not in present or cid in reject_cids:
            return None  # firmware NAK
        return (0xFF, 0x08, 0x00, 0x00, [])

    with (
        patch.object(listener, "_set_cid_reporting", side_effect=fake_set_cid_reporting),
        patch("builtins.print"),
    ):
        listener._divert()
    return listener._gesture_cid, listener._rawxy_enabled


def _caps(spec, controls, active_cid, rawxy_confirmed):
    return resolve_capabilities(
        spec, controls, {},
        active_gesture_cid=active_cid,
        gesture_rawxy_confirmed=rawxy_confirmed,
    )


class OriginalMxMasterTests(unittest.TestCase):
    """0xB012 -- the device this whole effort started from. No rawXY on 0x00C3."""

    def test_resolves_to_event_tap(self):
        spec = resolve_device(product_id=0xB012)
        controls = [GESTURE_BTN_NO_RAWXY, SMART_SHIFT]
        active, rawxy = _run_divert(spec, controls)
        self.assertEqual(active, 0x00C3)
        self.assertFalse(rawxy, "0x00C3 lacks rawXY -> button-only divert")
        caps = _caps(spec, controls, active, rawxy)
        self.assertEqual(caps.gesture_source, "event_tap")
        self.assertFalse(caps.gesture_via_sense_panel)


class MxMaster3STests(unittest.TestCase):
    """0xB034/0xB043 -- gesture button 0xC3 advertises raw_XY (libratbag)."""

    def test_resolves_to_rawxy(self):
        spec = resolve_device(product_id=0xB034)
        controls = [GESTURE_BTN_RAWXY, SMART_SHIFT]
        active, rawxy = _run_divert(spec, controls)
        self.assertEqual(active, 0x00C3)
        self.assertTrue(rawxy, "rawXY-capable 0x00C3 -> rawXY divert")
        caps = _caps(spec, controls, active, rawxy)
        self.assertEqual(caps.gesture_source, "rawxy")


class MxMaster4Tests(unittest.TestCase):
    """0xB042 -- Sense Panel (0x01A0) drives gestures, 0x00C3 is the Thumb button."""

    def test_sense_panel_is_primary_rawxy_gesture(self):
        spec = resolve_device(product_id=0xB042)
        controls = [SENSE_PANEL, GESTURE_BTN_NO_RAWXY, SMART_SHIFT]
        active, rawxy = _run_divert(spec, controls)
        self.assertEqual(active, 0x01A0, "Sense Panel preferred as gesture CID")
        self.assertTrue(rawxy)
        caps = _caps(spec, controls, active, rawxy)
        self.assertEqual(caps.gesture_source, "rawxy")
        self.assertFalse(caps.gesture_via_sense_panel, "panel IS the gesture -> not fallback")
        self.assertEqual(caps.thumb_button_routing, "hid", "0x00C3 distinct -> thumb over HID")

    def test_falls_back_to_button_and_thumb_via_os_when_panel_rejected(self):
        # Firmware refuses the Sense Panel divert -> 0x00C3 becomes the gesture
        # CID (button-only), gestures run on the OS event-tap path, and the
        # Thumb button is then routed via the OS button.
        spec = resolve_device(product_id=0xB042)
        controls = [SENSE_PANEL, GESTURE_BTN_NO_RAWXY, SMART_SHIFT]
        active, rawxy = _run_divert(spec, controls, reject_cids={0x01A0})
        self.assertEqual(active, 0x00C3)
        self.assertFalse(rawxy)
        caps = _caps(spec, controls, active, rawxy)
        self.assertEqual(caps.gesture_source, "event_tap")
        self.assertTrue(caps.gesture_via_sense_panel)
        self.assertEqual(caps.thumb_button_routing, "os")


class GamingMouseTests(unittest.TestCase):
    """G502 family -- no thumb gesture control: gestures resolve to none."""

    def test_no_gesture_control_resolves_to_none(self):
        spec = resolve_device(product_id=0xC08B)  # g502_hero
        controls = [PLAIN_BTN]
        active, rawxy = _run_divert(spec, controls)
        # No gesture CID is present, so the divert finds nothing to divert.
        self.assertFalse(rawxy)
        caps = _caps(spec, controls, None, None)
        self.assertEqual(caps.gesture_source, "none")
        self.assertFalse(caps.gesture_directions)


class AdaptivityInvariantTests(unittest.TestCase):
    """The same control profile must resolve the same way regardless of which
    catalog model it is matched to -- behavior follows the HID, not the name."""

    def test_rawxy_flag_decides_source_independent_of_model(self):
        controls_rawxy = [GESTURE_BTN_RAWXY]
        controls_plain = [GESTURE_BTN_NO_RAWXY]
        for pid in (0xB012, 0xB034, 0xB023, 0xB019, 0xB037, 0xB020):
            spec = resolve_device(product_id=pid)
            # rawXY-capable control -> rawxy on every model.
            a1, r1 = _run_divert(spec, controls_rawxy)
            self.assertTrue(r1, f"{spec.key}: rawXY-capable control should divert rawXY")
            self.assertEqual(_caps(spec, controls_rawxy, a1, r1).gesture_source, "rawxy")
            # non-rawXY control -> event_tap on every model.
            a2, r2 = _run_divert(spec, controls_plain)
            self.assertFalse(r2, f"{spec.key}: non-rawXY control should divert button-only")
            self.assertEqual(_caps(spec, controls_plain, a2, r2).gesture_source, "event_tap")


if __name__ == "__main__":
    unittest.main()
