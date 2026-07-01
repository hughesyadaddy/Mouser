"""Tests for the pure capability-resolution layer.

These are table-driven and hardware-free: ``resolve_capabilities`` is a pure
function of (catalog spec, REPROG_V4 controls, discovered features), so every
device scenario is expressed as plain data. The point of the layer is that the
bug class -- behavior chosen from identity / ACK / feature-index instead of real
capability -- becomes structurally impossible, and these tests pin that.
"""

from __future__ import annotations

import unittest

from core.device_capabilities import (
    FEAT_ADJUSTABLE_DPI,
    FEAT_HIRES_WHEEL_ENHANCED,
    FEAT_SMART_SHIFT,
    FEAT_SMART_SHIFT_ENHANCED,
    FEAT_THUMB_WHEEL,
    SENSE_PANEL_CID,
    DeviceCapabilities,
    resolve_capabilities,
)
from core.logi_devices import DEFAULT_DPI_MAX, DEFAULT_DPI_MIN, resolve_device

# Control dicts mirror hid_gesture._discover_reprog_controls output.
GESTURE_BUTTON_NO_RAWXY = {"cid": 0x00C3, "flags": 0x0031, "mapping_flags": 0x0001}
SENSE_PANEL_RAWXY = {"cid": 0x01A0, "flags": 0x0531, "mapping_flags": 0x0011}


class GestureSourceTests(unittest.TestCase):
    def test_original_mx_master_button_resolves_to_event_tap(self):
        """0x00C3 (flags=0x0031, no raw_xy bit) must never claim rawXY -- this
        is the exact original-MX-Master case that caused the cursor drift."""
        spec = resolve_device(product_id=0xB012)
        caps = resolve_capabilities(
            spec,
            [GESTURE_BUTTON_NO_RAWXY],
            {},
            active_gesture_cid=0x00C3,
        )
        self.assertEqual(caps.gesture_source, "event_tap")
        self.assertEqual(caps.active_gesture_cid, 0x00C3)
        self.assertTrue(caps.gesture_click)
        self.assertTrue(caps.gesture_directions)

    def test_rawxy_capable_control_resolves_to_rawxy(self):
        caps = resolve_capabilities(
            resolve_device(product_id=0xB042),
            [SENSE_PANEL_RAWXY],
            {},
            active_gesture_cid=0x01A0,
        )
        self.assertEqual(caps.gesture_source, "rawxy")

    def test_unconfirmed_rawxy_divert_falls_back_to_event_tap(self):
        """Even a flag-capable control drops to event_tap when the runtime says
        the rawXY divert did not stick (gesture_rawxy_confirmed=False)."""
        caps = resolve_capabilities(
            resolve_device(product_id=0xB042),
            [SENSE_PANEL_RAWXY],
            {},
            active_gesture_cid=0x01A0,
            gesture_rawxy_confirmed=False,
        )
        self.assertEqual(caps.gesture_source, "event_tap")

    def test_no_gesture_cid_resolves_to_none(self):
        caps = resolve_capabilities(resolve_device(product_id=0xB012), [], {})
        self.assertEqual(caps.gesture_source, "none")
        self.assertFalse(caps.gesture_click)
        self.assertFalse(caps.gesture_directions)


class WheelAxisIndependenceTests(unittest.TestCase):
    def test_axes_are_resolved_independently(self):
        """Vertical present, horizontal probed-absent: the two are separate
        Capability values, so a horizontal limitation cannot touch vertical."""
        caps = resolve_capabilities(
            resolve_device(product_id=0xB012),
            [],
            {FEAT_HIRES_WHEEL_ENHANCED: 0x0C, FEAT_THUMB_WHEEL: None},
        )
        self.assertTrue(caps.wheel_invert_vertical.supported)
        self.assertEqual(caps.wheel_invert_vertical.feature_index, 0x0C)
        self.assertFalse(caps.wheel_invert_horizontal.supported)
        self.assertIsNone(caps.wheel_invert_horizontal.feature_index)

    def test_unprobed_axis_is_tristate_none(self):
        """Feature key absent entirely = 'not probed' -> None (defer), distinct
        from key->None = 'probed, absent' -> False."""
        caps = resolve_capabilities(None, [], {FEAT_HIRES_WHEEL_ENHANCED: 0x0C})
        self.assertTrue(caps.wheel_invert_vertical.supported)
        self.assertIsNone(caps.wheel_invert_horizontal.supported)

    def test_both_axes_present(self):
        caps = resolve_capabilities(
            resolve_device(product_id=0xB034),
            [],
            {FEAT_HIRES_WHEEL_ENHANCED: 0x0C, FEAT_THUMB_WHEEL: 0x13},
        )
        self.assertTrue(caps.wheel_invert_vertical.supported)
        self.assertTrue(caps.wheel_invert_horizontal.supported)


class ThumbAndSensePanelTests(unittest.TestCase):
    def test_sense_panel_fallback_routes_thumb_via_os(self):
        """MX Master 4 with the small button (0x00C3) as the active gesture CID
        (sense-panel divert failed) -> gesture_via_sense_panel and thumb via OS."""
        spec = resolve_device(product_id=0xB042)
        caps = resolve_capabilities(
            spec,
            [GESTURE_BUTTON_NO_RAWXY],
            {},
            active_gesture_cid=0x00C3,
        )
        self.assertTrue(caps.gesture_via_sense_panel)
        self.assertEqual(caps.thumb_button_routing, "os")

    def test_thumb_diverted_over_hid_when_distinct_and_present(self):
        spec = resolve_device(product_id=0xB042)
        caps = resolve_capabilities(
            spec,
            [SENSE_PANEL_RAWXY, GESTURE_BUTTON_NO_RAWXY],
            {},
            active_gesture_cid=SENSE_PANEL_CID,
        )
        self.assertFalse(caps.gesture_via_sense_panel)
        self.assertEqual(caps.thumb_button_routing, "hid")
        self.assertEqual(caps.thumb_button_cid, 0x00C3)

    def test_device_without_thumb_button(self):
        caps = resolve_capabilities(
            resolve_device(product_id=0xB012),
            [GESTURE_BUTTON_NO_RAWXY],
            {},
            active_gesture_cid=0x00C3,
        )
        self.assertEqual(caps.thumb_button_routing, "none")
        self.assertFalse(caps.gesture_via_sense_panel)


class SmartShiftAndDpiTests(unittest.TestCase):
    def test_smart_shift_prefers_enhanced(self):
        caps = resolve_capabilities(
            None, [], {FEAT_SMART_SHIFT_ENHANCED: 0x0A, FEAT_SMART_SHIFT: 0x09}
        )
        self.assertTrue(caps.smart_shift.supported)
        self.assertEqual(caps.smart_shift.feature_index, 0x0A)
        self.assertTrue(caps.smart_shift_enhanced)

    def test_smart_shift_basic_only(self):
        caps = resolve_capabilities(
            None, [], {FEAT_SMART_SHIFT_ENHANCED: None, FEAT_SMART_SHIFT: 0x09}
        )
        self.assertTrue(caps.smart_shift.supported)
        self.assertFalse(caps.smart_shift_enhanced)

    def test_dpi_range_from_catalog(self):
        spec = resolve_device(product_id=0xB012)
        caps = resolve_capabilities(spec, [], {FEAT_ADJUSTABLE_DPI: 0x08})
        self.assertTrue(caps.dpi.supported)
        self.assertEqual(caps.dpi.feature_index, 0x08)
        self.assertEqual(caps.dpi.dpi_min, spec.dpi_min)
        self.assertEqual(caps.dpi.dpi_max, spec.dpi_max)


class RobustnessTests(unittest.TestCase):
    def test_empty_inputs_are_conservative(self):
        caps = resolve_capabilities(None, None, None)
        self.assertIsInstance(caps, DeviceCapabilities)
        self.assertEqual(caps.gesture_source, "none")
        self.assertIsNone(caps.wheel_invert_vertical.supported)
        self.assertIsNone(caps.wheel_invert_horizontal.supported)
        self.assertEqual(caps.thumb_button_routing, "none")
        self.assertEqual(caps.dpi.dpi_min, DEFAULT_DPI_MIN)
        self.assertEqual(caps.dpi.dpi_max, DEFAULT_DPI_MAX)

    def test_malformed_controls_do_not_raise(self):
        caps = resolve_capabilities(
            None,
            [{"cid": "0x00C3", "flags": "bad"}, "not-a-dict", {}],
            {0x2150: "nope"},
            active_gesture_cid="0x00C3",
        )
        self.assertIsInstance(caps, DeviceCapabilities)
        # "0x00C3" coerces to int; flags unparseable -> treated as 0 (no rawXY).
        self.assertEqual(caps.active_gesture_cid, 0x00C3)
        self.assertEqual(caps.gesture_source, "event_tap")

    def test_notes_are_populated_for_audit(self):
        caps = resolve_capabilities(
            resolve_device(product_id=0xB012),
            [GESTURE_BUTTON_NO_RAWXY],
            {FEAT_HIRES_WHEEL_ENHANCED: 0x0C, FEAT_THUMB_WHEEL: 0x13},
            active_gesture_cid=0x00C3,
        )
        subsystems = {subsystem for subsystem, _reason in caps.notes}
        self.assertIn("gesture", subsystems)
        self.assertIn("wheel_v", subsystems)
        self.assertIn("wheel_h", subsystems)


if __name__ == "__main__":
    unittest.main()
