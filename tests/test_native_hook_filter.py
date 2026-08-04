"""The contract between Mouser and its native WH_MOUSE_LL procedure.

The procedure decides natively from the masks built here, so a wrong bit is
not a cosmetic bug: it is a button that stops being swallowed, or a wheel
event that stops reaching Python. The C source is parsed alongside the Python
constants so a one-sided edit fails here instead of on the machine.
"""

import os
import re
import unittest

from types import SimpleNamespace

from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import LOGITECH_SCROLL_RECENT_S, MouseEvent
from core.native_hook_filter import (
    ABI_VERSION,
    EVENT_CODES,
    FILTER_FIELD_BITS,
    HOOK_THREAD_JOIN_S,
    EVENT_NAMES,
    EVT_HSCROLL_LEFT,
    EVT_HSCROLL_RIGHT,
    EVT_MIDDLE_DOWN,
    EVT_NONE,
    EVT_XBUTTON1_DOWN,
    EVT_XBUTTON2_UP,
    FILTER_DEBUG,
    FILTER_HSCROLL_INVERT,
    FILTER_INTERCEPT,
    FILTER_VSCROLL_INVERT,
    build_filter_flags,
    build_mask,
    compute_filter,
    describe_filter,
    event_bit,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C_SOURCE = os.path.join(REPO_ROOT, "native", "win", "mouser_hook.c")


class EventBitTests(unittest.TestCase):
    def test_every_event_code_has_a_distinct_bit(self):
        bits = [event_bit(name) for name in EVENT_CODES]
        self.assertEqual(len(bits), len(set(bits)))
        self.assertNotIn(0, bits)

    def test_unrecognised_events_contribute_nothing(self):
        """Gestures and HID++ buttons never reach the hook, so they must not
        claim a bit -- otherwise they would collide with one that matters."""
        self.assertEqual(event_bit(MouseEvent.GESTURE_SWIPE_LEFT), 0)
        self.assertEqual(event_bit(MouseEvent.THUMB_BUTTON_DOWN), 0)
        self.assertEqual(event_bit("not_an_event"), 0)

    def test_event_names_round_trip(self):
        for name, code in EVENT_CODES.items():
            self.assertEqual(EVENT_NAMES[code], name)
        self.assertNotIn(EVT_NONE, EVENT_NAMES)


class BuildMaskTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(build_mask([]), 0)

    def test_mask_ors_recognised_events_only(self):
        mask = build_mask(
            [
                MouseEvent.XBUTTON1_DOWN,
                MouseEvent.HSCROLL_LEFT,
                MouseEvent.GESTURE_SWIPE_UP,  # not a hook event
            ]
        )
        expected = (1 << EVT_XBUTTON1_DOWN) | (1 << EVT_HSCROLL_LEFT)
        self.assertEqual(mask, expected)

    def test_mask_accepts_a_dict_of_callbacks(self):
        """The hook passes ``self._callbacks`` straight in; iterating a dict
        must read its keys, not blow up."""
        callbacks = {MouseEvent.MIDDLE_DOWN: [lambda e: None]}
        self.assertEqual(build_mask(callbacks), 1 << EVT_MIDDLE_DOWN)

    def test_duplicates_do_not_double_count(self):
        events = [MouseEvent.XBUTTON2_UP, MouseEvent.XBUTTON2_UP]
        self.assertEqual(build_mask(events), 1 << EVT_XBUTTON2_UP)


class FilterFlagTests(unittest.TestCase):
    def test_all_off(self):
        flags = build_filter_flags(
            intercept=False, vscroll_invert=False, hscroll_invert=False, debug=False
        )
        self.assertEqual(flags, 0)

    def test_each_flag_is_independent(self):
        cases = (
            ("intercept", FILTER_INTERCEPT),
            ("vscroll_invert", FILTER_VSCROLL_INVERT),
            ("hscroll_invert", FILTER_HSCROLL_INVERT),
            ("debug", FILTER_DEBUG),
        )
        for name, bit in cases:
            with self.subTest(flag=name):
                kwargs = dict(
                    intercept=False,
                    vscroll_invert=False,
                    hscroll_invert=False,
                    debug=False,
                )
                kwargs[name] = True
                self.assertEqual(build_filter_flags(**kwargs), bit)

    def test_all_on(self):
        flags = build_filter_flags(
            intercept=True, vscroll_invert=True, hscroll_invert=True, debug=True
        )
        self.assertEqual(
            flags,
            FILTER_INTERCEPT
            | FILTER_VSCROLL_INVERT
            | FILTER_HSCROLL_INVERT
            | FILTER_DEBUG,
        )

    def test_describe_names_the_set_flags(self):
        text = describe_filter(FILTER_INTERCEPT | FILTER_DEBUG, 0x0A, 0x02)
        self.assertIn("intercept", text)
        self.assertIn("debug", text)
        self.assertNotIn("vinvert", text)
        self.assertIn("interest=0x0A", text)
        self.assertIn("block=0x02", text)

    def test_describe_says_none_when_nothing_is_set(self):
        self.assertIn("flags=none", describe_filter(0, 0, 0))


class ComputeFilterTests(unittest.TestCase):
    """What the hook's live state means to the native procedure.

    Get this wrong and the procedure swallows the wrong thing, or passes an
    event Mouser was supposed to remap -- neither is visible until someone
    presses the button.
    """

    def setUp(self):
        self.hook = BaseMouseHook()

    def _bind(self, source="usb"):
        self.hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(name="MX Master 3S", source=source)
        )
        self.hook._on_hid_connect()

    def test_nothing_bound_arms_nothing(self):
        flags, interest, block = compute_filter(self.hook)
        self.assertEqual((flags, interest, block), (0, 0, 0))

    def test_bound_device_with_local_focus_intercepts(self):
        self._bind()
        flags, _interest, _block = compute_filter(self.hook)
        self.assertTrue(flags & FILTER_INTERCEPT)

    def test_remote_focus_drops_intercept_but_keeps_scroll_invert(self):
        """Scroll inversion is host-local: Deskflow forwards scroll through
        untouched, so it stays armed even while Mouser is not driving."""
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.set_remote_forwarder(
            SimpleNamespace(should_forward=lambda: True)
        )

        flags, _interest, _block = compute_filter(self.hook)

        self.assertFalse(flags & FILTER_INTERCEPT)
        self.assertTrue(flags & FILTER_VSCROLL_INVERT)

    def test_firmware_invert_disarms_only_its_own_axis(self):
        """The original MX Master: the thumbwheel refuses firmware invert, so
        horizontal must keep the OS fallback while vertical drops it."""
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.invert_hscroll = True
        self.hook.wheel_native_invert_vertical = True
        self.hook.wheel_native_invert_horizontal = False

        flags, _interest, _block = compute_filter(self.hook)

        self.assertFalse(flags & FILTER_VSCROLL_INVERT)
        self.assertTrue(flags & FILTER_HSCROLL_INVERT)

    def test_virtual_device_never_arms_scroll_invert(self):
        """A KVM client with only a remote-described device must not invert
        the Deskflow-forwarded wheel of whatever mouse is really upstream."""
        self._bind(source="remote-virtual")
        self.hook.invert_vscroll = True
        self.hook.invert_hscroll = True

        flags, _interest, _block = compute_filter(self.hook)

        self.assertFalse(flags & FILTER_VSCROLL_INVERT)
        self.assertFalse(flags & FILTER_HSCROLL_INVERT)

    def test_debug_flag_needs_both_the_mode_and_a_callback(self):
        self._bind()
        self.hook.debug_mode = True
        self.assertFalse(compute_filter(self.hook)[0] & FILTER_DEBUG)

        self.hook.set_debug_callback(lambda message: None)
        self.assertTrue(compute_filter(self.hook)[0] & FILTER_DEBUG)

    def test_blocked_events_are_also_interesting(self):
        """A swallowed button still has to reach Python -- swallowing it is
        the first half of remapping it."""
        self._bind()
        self.hook.block(MouseEvent.XBUTTON1_DOWN)

        _flags, interest, block = compute_filter(self.hook)

        self.assertEqual(block, 1 << EVT_XBUTTON1_DOWN)
        self.assertEqual(interest & block, block)

    def test_registered_callbacks_are_interesting_without_being_blocked(self):
        self._bind()
        self.hook.register(MouseEvent.HSCROLL_LEFT, lambda event: None)

        _flags, interest, block = compute_filter(self.hook)

        self.assertTrue(interest & (1 << EVT_HSCROLL_LEFT))
        self.assertEqual(block, 0)

    def test_reset_bindings_clears_both_masks(self):
        self._bind()
        self.hook.register(MouseEvent.MIDDLE_DOWN, lambda event: None)
        self.hook.block(MouseEvent.MIDDLE_DOWN)
        self.hook.reset_bindings()

        _flags, interest, block = compute_filter(self.hook)

        self.assertEqual((interest, block), (0, 0))


class CSourceAgreementTests(unittest.TestCase):
    """The C procedure hard-codes the same numbering. Drift here means the
    native side swallows the wrong button, so assert they match."""

    @classmethod
    def setUpClass(cls):
        with open(C_SOURCE, encoding="utf-8") as handle:
            cls.source = handle.read()

    def _defined(self, name):
        match = re.search(
            rf"^#define\s+{re.escape(name)}\s+"
            rf"\(?([0-9]+)[uUlL]*\s*(?:<<\s*([0-9]+)[uUlL]*)?\)?\s*$",
            self.source,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, f"{name} is not defined in mouser_hook.c")
        value, shift = match.group(1), match.group(2)
        return int(value) << int(shift) if shift else int(value)

    def test_abi_version_matches(self):
        self.assertEqual(self._defined("MOUSER_HOOK_ABI"), ABI_VERSION)

    def test_wheel_recency_window_matches_the_python_constant(self):
        """The native procedure attributes a wheel message to the Logitech
        purely from this window, so a drift here silently changes which
        scrolls get inverted."""
        self.assertEqual(
            self._defined("LOGITECH_WHEEL_RECENT_MS"),
            round(LOGITECH_SCROLL_RECENT_S * 1000),
        )

    def test_uninstall_wait_stays_under_the_python_join(self):
        """If the DLL outwaited Python, stop() would drop its handle on a
        thread still running and a later install() would race it."""
        self.assertLess(
            self._defined("NATIVE_UNINSTALL_WAIT_MS") / 1000.0,
            HOOK_THREAD_JOIN_S,
        )

    def test_every_mask_fits_the_packed_filter_field(self):
        """The three words share one 64-bit register, 16 bits each."""
        widest = max(event_bit(name) for name in EVENT_CODES)
        self.assertLess(widest, 1 << FILTER_FIELD_BITS)
        self.assertLess(
            build_filter_flags(
                intercept=True,
                vscroll_invert=True,
                hscroll_invert=True,
                debug=True,
            ),
            1 << FILTER_FIELD_BITS,
        )
        for name in ("FILTER_FLAGS_SHIFT", "FILTER_INTEREST_SHIFT",
                     "FILTER_BLOCK_SHIFT"):
            with self.subTest(shift=name):
                self.assertEqual(self._defined(name) % FILTER_FIELD_BITS, 0)

    def test_filter_flags_match(self):
        for name, expected in (
            ("FILTER_INTERCEPT", FILTER_INTERCEPT),
            ("FILTER_VSCROLL_INVERT", FILTER_VSCROLL_INVERT),
            ("FILTER_HSCROLL_INVERT", FILTER_HSCROLL_INVERT),
            ("FILTER_DEBUG", FILTER_DEBUG),
        ):
            with self.subTest(flag=name):
                self.assertEqual(self._defined(name), expected)

    def test_event_codes_match(self):
        for event_name, code in EVENT_CODES.items():
            c_name = f"EVT_{event_name.upper()}"
            with self.subTest(event=event_name):
                self.assertEqual(self._defined(c_name), code)
        self.assertEqual(self._defined("EVT_NONE"), EVT_NONE)

    def test_hscroll_codes_are_the_only_delta_carrying_events(self):
        """The drain thread reads a delta off exactly these two; if a third
        joined them it would silently dispatch with no magnitude."""
        self.assertEqual(
            {EVT_HSCROLL_LEFT, EVT_HSCROLL_RIGHT},
            {EVENT_CODES[MouseEvent.HSCROLL_LEFT],
             EVENT_CODES[MouseEvent.HSCROLL_RIGHT]},
        )


if __name__ == "__main__":
    unittest.main()
