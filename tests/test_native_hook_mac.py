"""The contract between Mouser and its native macOS CGEventTap callback.

Three layers, each runnable anywhere except the last:

* the constants both sides agree on, parsed straight out of the C source;
* :func:`core.native_hook_mac.decide`, the Python statement of the
  early-return set in ``MouseHook._event_tap_callback``;
* on a Mac with clang, the dylib is compiled into a temp dir and its
  ``tap_decide`` is driven through the same table as the Python one.
"""

import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import LOGITECH_SCROLL_RECENT_S, MouseEvent
from core import native_hook_mac as nm
from core.native_hook_filter import (
    FILTER_CAPTURE,
    FILTER_DEBUG,
    FILTER_HSCROLL_INVERT,
    FILTER_INTERCEPT,
    FILTER_VSCROLL_INVERT,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C_SOURCE = os.path.join(REPO_ROOT, "native", "mac", "mouser_tap.m")
BUILD_PY = os.path.join(REPO_ROOT, "native", "mac", "build.py")

_MOVED = nm.CG_EVENT_MOUSE_MOVED
_DRAGGED = nm.CG_EVENT_OTHER_MOUSE_DRAGGED
_DOWN = nm.CG_EVENT_OTHER_MOUSE_DOWN
_UP = nm.CG_EVENT_OTHER_MOUSE_UP
_SCROLL = nm.CG_EVENT_SCROLL_WHEEL


def _hook(*, source="hidapi", device=True):
    hook = BaseMouseHook()
    hook.ignore_trackpad = True
    if device:
        hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(
                name="MX Master 3S",
                source=source,
                thumb_button_via_hid=False,
                gesture_via_sense_panel=False,
            )
        )
        hook._on_hid_connect()
    return hook


class ConstantParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(C_SOURCE, encoding="utf-8") as handle:
            cls.source = handle.read()

    def _defined(self, name):
        match = re.search(
            rf"^#define\s+{re.escape(name)}\s+"
            rf"\(?(0x[0-9A-Fa-f]+|[0-9]+)[uUlL]*\s*(?:<<\s*([0-9]+)[uUlL]*)?\)?\s*$",
            self.source,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, f"{name} is not defined in mouser_tap.m")
        value, shift = int(match.group(1), 0), match.group(2)
        return value << int(shift) if shift else value

    def test_abi_version(self):
        self.assertEqual(self._defined("MOUSER_TAP_ABI"), nm.ABI_VERSION)

    def test_filter_flags(self):
        for name, value in (
            ("FILTER_INTERCEPT", FILTER_INTERCEPT),
            ("FILTER_VSCROLL_INVERT", FILTER_VSCROLL_INVERT),
            ("FILTER_HSCROLL_INVERT", FILTER_HSCROLL_INVERT),
            ("FILTER_DEBUG", FILTER_DEBUG),
            ("FILTER_CAPTURE", FILTER_CAPTURE),
            ("FILTER_IGNORE_TRACKPAD", nm.FILTER_IGNORE_TRACKPAD),
            ("FILTER_THUMB_VIA_HID", nm.FILTER_THUMB_VIA_HID),
            ("FILTER_SENSE_PANEL", nm.FILTER_SENSE_PANEL),
        ):
            with self.subTest(name):
                self.assertEqual(self._defined(name), value)

    def test_event_codes(self):
        for code, name in nm.TAP_EVENT_NAMES.items():
            c_name = "EVT_" + name.upper().replace("THUMB_BUTTON", "THUMB")
            with self.subTest(name):
                self.assertEqual(self._defined(c_name), code)
        self.assertEqual(self._defined("EVT_SENSE_PANEL_DOWN"), nm.EVT_SENSE_PANEL_DOWN)
        self.assertEqual(self._defined("EVT_SENSE_PANEL_UP"), nm.EVT_SENSE_PANEL_UP)

    def test_injected_markers_cover_mouser_and_deskflow(self):
        from core.mouse_hook_macos import _INJECTED_EVENT_MARKER

        self.assertEqual(self._defined("MARKER_MOUSER"), _INJECTED_EVENT_MARKER)
        self.assertEqual(self._defined("MARKER_MOUSER"), nm.MARKER_MOUSER)
        self.assertEqual(self._defined("MARKER_DESKFLOW"), nm.MARKER_DESKFLOW)

    def test_button_numbers_and_wheel_window(self):
        from core import mouse_hook_macos as mac

        self.assertEqual(self._defined("BTN_MIDDLE"), mac._BTN_MIDDLE)
        self.assertEqual(self._defined("BTN_BACK"), mac._BTN_BACK)
        self.assertEqual(self._defined("BTN_FORWARD"), mac._BTN_FORWARD)
        self.assertEqual(self._defined("BTN_OS_EXTRA"), mac._BTN_OS_EXTRA)
        self.assertEqual(
            self._defined("LOGITECH_WHEEL_RECENT_MS"),
            round(LOGITECH_SCROLL_RECENT_S * 1000),
        )

    def test_stop_wait_is_shorter_than_the_python_join(self):
        from core.mouse_hook_macos import TAP_THREAD_JOIN_S

        self.assertLess(self._defined("TAP_STOP_WAIT_MS") / 1000.0, TAP_THREAD_JOIN_S)


class ComputeTapFilterTests(unittest.TestCase):
    def test_nothing_bound_arms_nothing_but_trackpad_policy(self):
        flags, interest, block = nm.compute_tap_filter(_hook(device=False))
        self.assertEqual(flags, nm.FILTER_IGNORE_TRACKPAD)
        self.assertEqual((interest, block), (0, 0))

    def test_bound_device_intercepts(self):
        self.assertTrue(nm.compute_tap_filter(_hook())[0] & FILTER_INTERCEPT)

    def test_remote_focus_drops_intercept_but_keeps_invert(self):
        hook = _hook()
        hook.invert_vscroll = True
        hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))
        flags = nm.compute_tap_filter(hook)[0]
        self.assertFalse(flags & FILTER_INTERCEPT)
        self.assertTrue(flags & FILTER_VSCROLL_INVERT)

    def test_firmware_invert_disarms_its_own_axis_only(self):
        hook = _hook()
        hook.invert_vscroll = hook.invert_hscroll = True
        hook.wheel_native_invert_vertical = True
        flags = nm.compute_tap_filter(hook)[0]
        self.assertFalse(flags & FILTER_VSCROLL_INVERT)
        self.assertTrue(flags & FILTER_HSCROLL_INVERT)

    def test_virtual_device_never_inverts(self):
        hook = _hook(source="deskflow-shim")
        hook.invert_vscroll = hook.invert_hscroll = True
        flags = nm.compute_tap_filter(hook)[0]
        self.assertFalse(flags & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT))

    def test_capture_follows_the_python_tap_not_the_windows_rule(self):
        """On macOS the tap is the motion source for devices without rawXY,
        so a held gesture captures even with a physical mouse."""
        hook = _hook()
        hook._gesture_direction_enabled = True
        hook._gesture_active = True
        self.assertTrue(nm.compute_tap_filter(hook)[0] & FILTER_CAPTURE)
        hook._gesture_direction_enabled = False
        self.assertFalse(nm.compute_tap_filter(hook)[0] & FILTER_CAPTURE)

    def test_capture_needs_intercept(self):
        hook = _hook()
        hook._gesture_direction_enabled = True
        hook._gesture_active = True
        hook.set_remote_forwarder(SimpleNamespace(should_forward=lambda: True))
        self.assertFalse(nm.compute_tap_filter(hook)[0] & FILTER_CAPTURE)

    def test_thumb_and_sense_panel_routing_bits(self):
        hook = _hook()
        hook._connected_device = SimpleNamespace(
            source="hidapi", thumb_button_via_hid=True, gesture_via_sense_panel=True,
            active_gesture_cid=0x00C3,
        )
        flags = nm.compute_tap_filter(hook)[0]
        self.assertTrue(flags & nm.FILTER_THUMB_VIA_HID)
        self.assertTrue(flags & nm.FILTER_SENSE_PANEL)

    def test_thumb_events_have_mask_bits(self):
        hook = _hook()
        hook.block(MouseEvent.THUMB_BUTTON_DOWN)
        hook.register(MouseEvent.THUMB_BUTTON_UP, lambda e: None)
        _flags, interest, block = nm.compute_tap_filter(hook)
        self.assertEqual(block, 1 << nm.EVT_THUMB_DOWN)
        self.assertEqual(interest, (1 << nm.EVT_THUMB_DOWN) | (1 << nm.EVT_THUMB_UP))

    def test_debug_needs_mode_and_callback(self):
        hook = _hook()
        hook.debug_mode = True
        self.assertFalse(nm.compute_tap_filter(hook)[0] & FILTER_DEBUG)
        hook.set_debug_callback(lambda m: None)
        self.assertTrue(nm.compute_tap_filter(hook)[0] & FILTER_DEBUG)

    def test_describe_names_flags(self):
        text = nm.describe_tap_filter(FILTER_INTERCEPT | nm.FILTER_SENSE_PANEL, 0x20, 0)
        self.assertIn("intercept", text)
        self.assertIn("sense-panel", text)
        self.assertIn("0x0020", text)


# ── the decision table ───────────────────────────────────────────────


def _cases():
    """Every combination the callback can see, as (flags, interest, block,
    fields). Both decision functions are run over all of them."""
    flag_bits = (
        FILTER_INTERCEPT, FILTER_VSCROLL_INVERT, FILTER_HSCROLL_INVERT,
        FILTER_DEBUG, FILTER_CAPTURE, nm.FILTER_IGNORE_TRACKPAD,
        nm.FILTER_THUMB_VIA_HID, nm.FILTER_SENSE_PANEL,
    )
    interests = (0, 1 << nm.EVT_HSCROLL_LEFT, 1 << nm.EVT_MIDDLE_DOWN, 0xFFFF)
    blocks = (0, 1 << nm.EVT_HSCROLL_RIGHT, 1 << nm.EVT_XBUTTON1_DOWN, 1 << nm.EVT_THUMB_UP, 0xFFFF)
    moves = [nm.TapFields(t) for t in (_MOVED, _DRAGGED)]
    buttons = [
        nm.TapFields(t, user_data=u, button=b)
        for t in (_DOWN, _UP)
        for u in (0, nm.MARKER_MOUSER, nm.MARKER_DESKFLOW)
        for b in (0, 2, 3, 4, 5, 6)
    ]
    scrolls = [
        nm.TapFields(_SCROLL, user_data=u, is_continuous=c, momentum_phase=m,
                     scroll_phase=p, h_fixed=h, v_fixed=-65536, recent_logitech_wheel=r)
        for u in (0, nm.MARKER_DESKFLOW)
        for c in (0, 1)
        for m in (0, 1)
        for p in (0, 1, 4)
        for h in (0, 65536, -32768)
        for r in (0, 1)
    ]
    for flags in range(1 << len(flag_bits)):
        packed = sum(bit for i, bit in enumerate(flag_bits) if flags & (1 << i))
        for interest, block in itertools.product(interests, blocks):
            for f in moves + buttons + scrolls:
                yield packed, interest, block, f


class PythonDecisionTests(unittest.TestCase):
    def test_moves_pass_unless_capturing(self):
        for t in (_MOVED, _DRAGGED):
            self.assertEqual(nm.decide(FILTER_INTERCEPT, 0, 0, nm.TapFields(t)).action, nm.ACT_PASS)
            self.assertEqual(
                nm.decide(FILTER_INTERCEPT | FILTER_CAPTURE, 0, 0, nm.TapFields(t)).action,
                nm.ACT_DROP,
            )

    def test_both_injected_markers_pass_before_anything_else(self):
        for marker in (nm.MARKER_MOUSER, nm.MARKER_DESKFLOW):
            for t in (_DOWN, _UP, _SCROLL):
                d = nm.decide(
                    0xFF, 0xFFFF, 0xFFFF,
                    nm.TapFields(t, user_data=marker, button=3, h_fixed=65536,
                                 recent_logitech_wheel=1),
                )
                self.assertEqual(d, nm.TapDecision(), (marker, t))

    def test_no_intercept_only_inverts_logitech_wheel(self):
        flags = FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT | nm.FILTER_IGNORE_TRACKPAD
        f = nm.TapFields(_SCROLL, recent_logitech_wheel=1)
        self.assertEqual(nm.decide(flags, 0xFFFF, 0xFFFF, f).action, nm.ACT_INVERT_V | nm.ACT_INVERT_H)
        self.assertEqual(nm.decide(flags, 0, 0, nm.TapFields(_SCROLL)).action, nm.ACT_PASS)
        self.assertEqual(nm.decide(flags, 0, 0, nm.TapFields(_DOWN, button=3)).action, nm.ACT_PASS)

    def test_attribution_gates(self):
        flags = FILTER_VSCROLL_INVERT | nm.FILTER_IGNORE_TRACKPAD
        for kw in (
            {"is_continuous": 1},
            {"momentum_phase": 2},
            {"scroll_phase": 1},
            {"recent_logitech_wheel": 0},
        ):
            f = nm.TapFields(_SCROLL, recent_logitech_wheel=1)
            f = nm.TapFields(**{**f.__dict__, **kw})
            self.assertEqual(nm.decide(flags, 0, 0, f).action, nm.ACT_PASS, kw)
        ended = nm.TapFields(_SCROLL, scroll_phase=4, recent_logitech_wheel=1)
        self.assertEqual(nm.decide(flags, 0, 0, ended).action, nm.ACT_INVERT_V)

    def test_trackpad_is_still_inverted_when_the_policy_is_off(self):
        flags = FILTER_VSCROLL_INVERT
        f = nm.TapFields(_SCROLL, is_continuous=1, recent_logitech_wheel=1)
        self.assertEqual(nm.decide(flags, 0, 0, f).action, nm.ACT_INVERT_V)

    def test_hscroll_remap_wins_over_inversion(self):
        flags = FILTER_INTERCEPT | FILTER_HSCROLL_INVERT
        f = nm.TapFields(_SCROLL, h_fixed=65536, recent_logitech_wheel=1)
        block = 1 << nm.EVT_HSCROLL_RIGHT
        d = nm.decide(flags, block, block, f)
        self.assertEqual(d, nm.TapDecision(nm.ACT_QUEUE | nm.ACT_DROP, nm.EVT_HSCROLL_RIGHT, 1))
        unblocked = nm.decide(flags, block, 0, f)
        self.assertEqual(unblocked.action, nm.ACT_QUEUE | nm.ACT_INVERT_H)
        self.assertEqual(unblocked.blocked, 0)

    def test_hscroll_sign_maps_to_direction(self):
        self.assertEqual(
            nm.decide(FILTER_INTERCEPT, 0xFFFF, 0, nm.TapFields(_SCROLL, h_fixed=-1)).event_code,
            nm.EVT_HSCROLL_LEFT,
        )

    def test_continuous_wheel_is_ignored_entirely_while_intercepting(self):
        flags = FILTER_INTERCEPT | FILTER_VSCROLL_INVERT | nm.FILTER_IGNORE_TRACKPAD | FILTER_DEBUG
        f = nm.TapFields(_SCROLL, is_continuous=1, h_fixed=65536, recent_logitech_wheel=1)
        self.assertEqual(nm.decide(flags, 0xFFFF, 0xFFFF, f), nm.TapDecision())

    def test_buttons_classify_and_block(self):
        for button, code in ((2, nm.EVT_MIDDLE_DOWN), (3, nm.EVT_XBUTTON1_DOWN),
                             (4, nm.EVT_XBUTTON2_DOWN), (6, nm.EVT_THUMB_DOWN)):
            f = nm.TapFields(_DOWN, button=button)
            d = nm.decide(FILTER_INTERCEPT, 1 << code, 1 << code, f)
            self.assertEqual(d, nm.TapDecision(nm.ACT_QUEUE | nm.ACT_DROP, code, 1), button)
            d = nm.decide(FILTER_INTERCEPT, 0, 0, f)
            self.assertEqual(d, nm.TapDecision(nm.ACT_PASS, code, 0), button)

    def test_unknown_button_passes(self):
        self.assertEqual(nm.decide(FILTER_INTERCEPT, 0xFFFF, 0xFFFF, nm.TapFields(_DOWN, button=5)),
                         nm.TapDecision())

    def test_btn6_routing(self):
        f = nm.TapFields(_UP, button=6)
        sense = nm.decide(FILTER_INTERCEPT | nm.FILTER_SENSE_PANEL, 0, 0, f)
        self.assertEqual(sense, nm.TapDecision(nm.ACT_QUEUE | nm.ACT_DROP, nm.EVT_SENSE_PANEL_UP, 1))
        hid = nm.decide(FILTER_INTERCEPT | nm.FILTER_THUMB_VIA_HID, 0, 0, f)
        self.assertEqual(hid, nm.TapDecision(nm.ACT_DROP, nm.EVT_NONE, 1))
        both = nm.decide(FILTER_INTERCEPT | nm.FILTER_SENSE_PANEL | nm.FILTER_THUMB_VIA_HID, 0, 0, f)
        self.assertEqual(both.event_code, nm.EVT_SENSE_PANEL_UP)

    def test_debug_queues_everything_it_would_have_logged(self):
        flags = FILTER_INTERCEPT | FILTER_DEBUG
        self.assertTrue(nm.decide(flags, 0, 0, nm.TapFields(_DOWN, button=5)).action & nm.ACT_QUEUE)
        self.assertTrue(nm.decide(flags, 0, 0, nm.TapFields(_SCROLL)).action & nm.ACT_QUEUE)
        self.assertFalse(nm.decide(FILTER_DEBUG, 0, 0, nm.TapFields(_DOWN, button=3)).action & nm.ACT_QUEUE)


class NativeDecisionParityTests(unittest.TestCase):
    """Compile the dylib and run the C table against the Python one."""

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest("libmouser_tap.dylib targets macOS")
        compiler = shutil.which("clang")
        if not compiler:
            raise unittest.SkipTest("clang not on PATH")
        import importlib.util

        spec = importlib.util.spec_from_file_location("native_mac_build", BUILD_PY)
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        cls.tmp = tempfile.TemporaryDirectory()
        output = os.path.join(cls.tmp.name, nm.DYLIB_NAME)
        result = subprocess.run(
            build.compile_command(output, compiler=compiler),
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            cls.tmp.cleanup()
            raise AssertionError(f"native tap did not compile:\n{result.stderr}")
        assert result.stderr.strip() == "", result.stderr
        cls.native = nm.NativeTap.load(output)
        assert cls.native is not None, "freshly built dylib refused to load"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_abi_and_layouts_agree(self):
        self.assertEqual(self.native.path, os.path.join(self.tmp.name, nm.DYLIB_NAME))
        self.assertFalse(self.native.enabled)
        self.assertEqual(self.native.dropped, 0)
        self.assertEqual(self.native.reenabled, 0)

    def test_c_table_matches_python_table_everywhere(self):
        count = 0
        for flags, interest, block, f in _cases():
            expected = nm.decide(flags, interest, block, f)
            got = self.native.decide(flags, interest, block, f)
            if got != expected:
                self.fail(
                    f"flags=0x{flags:02X} interest=0x{interest:04X} "
                    f"block=0x{block:04X} {f}: C {got} != Python {expected}"
                )
            count += 1
        self.assertGreater(count, 100_000)

    def test_capture_delta_reads_as_zero_when_idle(self):
        self.assertEqual(self.native.take_capture_delta(), (0, 0))

    def test_next_event_times_out_cleanly_without_a_tap(self):
        event = nm.NativeTapEvent()
        self.assertFalse(self.native.next_event(event, 1))


class LoadTests(unittest.TestCase):
    def test_override_env_comes_first(self):
        with patch.dict(os.environ, {"MOUSER_TAP_DYLIB": "/x/y.dylib"}):
            self.assertEqual(nm.candidate_paths()[0], "/x/y.dylib")

    def test_bundle_dir_precedes_repo(self):
        with patch.object(sys, "_MEIPASS", "/bundle", create=True):
            paths = nm.candidate_paths()
        self.assertEqual(paths[0], os.path.join("/bundle", nm.DYLIB_NAME))
        self.assertTrue(paths[1].endswith(os.path.join("native", "mac", nm.DYLIB_NAME)))

    def test_missing_file_loads_nothing(self):
        with patch.object(nm, "_resolve_dylib_path", return_value=None):
            self.assertIsNone(nm.NativeTap.load())

    def test_other_platforms_load_nothing(self):
        with patch.object(sys, "platform", "linux"):
            self.assertIsNone(nm.NativeTap.load("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
