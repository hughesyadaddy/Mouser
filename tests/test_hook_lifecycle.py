"""Dynamic WH_MOUSE_LL lifecycle: the hook (and generic-mouse Raw Input
registration) exist only while `_hook_should_be_installed()` is True.

Regression coverage for the tiny11 cursor freeze: a Python LL hook sits in
the delivery path of every system mouse event; on a KVM client with no
bound local Logitech it must not be installed at all.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import MouseEvent


def _physical_device(name="MX Master 3S"):
    return SimpleNamespace(name=name, source="usb")


class HookPredicateTests(unittest.TestCase):
    """Truth table for _hook_should_be_installed on the base hook."""

    def setUp(self):
        self.hook = BaseMouseHook()

    def _bind(self, device=None):
        self.hook._hid_gesture = SimpleNamespace(
            connected_device=device or _physical_device()
        )
        self.hook._on_hid_connect()

    def test_no_device_bound_means_no_hook(self):
        self.assertFalse(self.hook._hook_should_be_installed())

    def test_bound_device_with_local_focus_wants_hook(self):
        self._bind()
        self.assertTrue(self.hook._hook_should_be_installed())

    def test_remote_focus_stands_down_without_invert(self):
        self._bind()
        forwarder = SimpleNamespace(should_forward=lambda: True)
        self.hook.set_remote_forwarder(forwarder)
        self.assertFalse(self.hook._hook_should_be_installed())

    def test_remote_focus_stands_down_even_with_invert_enabled(self):
        """Off-host, Mouser touches nothing but relayed gestures.

        The OS hook is a Python callback in the delivery path of EVERY
        mouse event, so keeping it installed for scroll inversion taxed
        pointer and wheel latency on a machine Mouser was not driving --
        the reported scroll lag. Scroll inversion is host-local only.
        """
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.invert_hscroll = True
        forwarder = SimpleNamespace(should_forward=lambda: True)
        self.hook.set_remote_forwarder(forwarder)
        self.assertFalse(self.hook._hook_should_be_installed())

    def test_local_focus_still_gets_the_invert_fallback(self):
        """...but on the host the fallback must still work when the
        firmware cannot invert natively."""
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.wheel_native_invert_vertical = False
        self.assertTrue(self.hook._hook_should_be_installed())

    def test_firmware_invert_stands_the_fallback_down(self):
        self._bind()
        self.hook.invert_vscroll = True
        self.hook.wheel_native_invert_vertical = True
        forwarder = SimpleNamespace(should_forward=lambda: True)
        self.hook.set_remote_forwarder(forwarder)
        self.assertFalse(self.hook._hook_should_be_installed())

    def test_invert_without_physical_device_means_no_hook(self):
        # tiny11: invert toggle on, but only a remote-virtual device bound.
        self.hook.invert_vscroll = True
        self.hook._hid_gesture = SimpleNamespace(
            connected_device=SimpleNamespace(name="ingress", source="remote-virtual")
        )
        self.hook._on_hid_connect()
        forwarder = SimpleNamespace(should_forward=lambda: True)
        self.hook.set_remote_forwarder(forwarder)
        self.assertFalse(self.hook._hook_should_be_installed())

    def test_set_remote_forwarder_attaches_focus_callback_and_syncs(self):
        synced = []
        self.hook.sync_hook_state = lambda: synced.append(True)
        forwarder = SimpleNamespace(should_forward=lambda: False)

        self.hook.set_remote_forwarder(forwarder)

        self.assertIs(forwarder.on_focus_change, self.hook.sync_hook_state)
        self.assertEqual(synced, [True])

    def test_hid_connect_and_disconnect_trigger_sync(self):
        synced = []
        self.hook.sync_hook_state = lambda: synced.append(True)
        self.hook._hid_gesture = SimpleNamespace(connected_device=_physical_device())

        self.hook._on_hid_connect()
        self.hook._on_hid_disconnect()

        self.assertEqual(len(synced), 2)


class BlockedUpDownPairingTests(unittest.TestCase):
    """An UP is only swallowed when its DOWN was: a hook (re)installed
    mid-hold must never leave the OS with an unmatched button-down."""

    def test_pairing_logic(self):
        hook = BaseMouseHook()

        # Down blocked -> recorded; matching up blocked.
        self.assertTrue(hook._pair_blocked_updown(MouseEvent.XBUTTON1_DOWN, True))
        self.assertTrue(hook._pair_blocked_updown(MouseEvent.XBUTTON1_UP, True))
        # Up without a recorded down (hook installed mid-hold): pass through.
        self.assertFalse(hook._pair_blocked_updown(MouseEvent.XBUTTON2_UP, True))
        # Unblocked down never records; its up passes.
        self.assertFalse(hook._pair_blocked_updown(MouseEvent.MIDDLE_DOWN, False))
        self.assertFalse(hook._pair_blocked_updown(MouseEvent.MIDDLE_UP, True))
        # Wheel-style events are unaffected by pairing.
        self.assertTrue(hook._pair_blocked_updown(MouseEvent.HSCROLL_LEFT, True))


class RemoteForwarderFocusCallbackTests(unittest.TestCase):
    def test_focus_flip_invokes_callback(self):
        from core.remote_forward import RemoteForwarder

        forwarder = RemoteForwarder(token="t")
        flips = []
        forwarder.on_focus_change = lambda: flips.append(True)

        forwarder._handle_message({"type": "focus", "local": False, "screen": "x"})
        forwarder._handle_message({"type": "focus", "local": False, "screen": "x"})
        forwarder._handle_message({"type": "focus", "local": True, "screen": None})

        # Only actual flips notify (remote, then local) -- not repeats.
        self.assertEqual(len(flips), 2)

    def test_callback_exception_is_contained(self):
        from core.remote_forward import RemoteForwarder

        forwarder = RemoteForwarder(token="t")

        def boom():
            raise RuntimeError("boom")

        forwarder.on_focus_change = boom
        forwarder._handle_message({"type": "focus", "local": False, "screen": "x"})


if __name__ == "__main__":
    unittest.main()
