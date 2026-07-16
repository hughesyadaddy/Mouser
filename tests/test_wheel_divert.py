"""Tests for the HID++ native wheel-invert path.

Native invert = Mouser writes the firmware invert bit on `0x2121` /
`0x2150` *without* diverting the wheel through HID++ notifications. The OS
receives native HID scroll with the direction already flipped at the
device, so KVM forwarders see inverted scroll and the native scroll
cadence / momentum is preserved end-to-end.
"""

from __future__ import annotations

import copy
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from core import hid_gesture as hg_mod
from core.config import DEFAULT_CONFIG, _migrate
from core.hid_gesture import (
    FEAT_HIRES_WHEEL_ENHANCED,
    FEAT_THUMB_WHEEL,
    HidGestureListener,
)
from core.logi_devices import resolve_device
from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_contract import MouseHookLike
from core.mouse_hook_types import (
    DEVICE_SOURCE_DESKFLOW_SHIM,
    DEVICE_SOURCE_REMOTE_VIRTUAL,
    is_physical_device_source,
)
from core.macos_iokit_scroll import LogitechScrollMonitor


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_listener() -> HidGestureListener:
    return HidGestureListener()


def _resp(params):
    return (0xFF, 0x12, 0x0, 0x0, list(params))


# ──────────────────────────────────────────────────────────────────────────────
# Signed-int helper
# ──────────────────────────────────────────────────────────────────────────────


class DecodeS16BETests(unittest.TestCase):
    def test_decode_s16_be(self):
        decode = HidGestureListener._decode_s16_be
        self.assertEqual(decode(0x80, 0x00), -32768)
        self.assertEqual(decode(0x7F, 0xFF), 32767)
        self.assertEqual(decode(0x00, 0x01), 1)
        self.assertEqual(decode(0xFF, 0xFF), -1)
        self.assertEqual(decode(0x00, 0x00), 0)

    def test_decode_s16_be_full_range(self):
        decode = HidGestureListener._decode_s16_be
        for hi in range(256):
            for lo in range(256):
                v = decode(hi, lo)
                self.assertGreaterEqual(v, -32768)
                self.assertLessEqual(v, 32767)


# ──────────────────────────────────────────────────────────────────────────────
# Capability discovery
# ──────────────────────────────────────────────────────────────────────────────


class CapabilityDiscoveryTests(unittest.TestCase):
    def test_capability_discovery(self):
        listener = _make_listener()
        feature_map = {FEAT_HIRES_WHEEL_ENHANCED: 0x07, FEAT_THUMB_WHEEL: 0x08}
        request_responses = {
            (0x07, 0): _resp([8, 0x00, 0x10, 0x00]),     # multiplier=8
            (0x08, 0): _resp([0x00, 0x10, 0x00, 0x78]),  # divertedRes=120
        }

        def fake_find(feat_id):
            return feature_map.get(feat_id)

        def fake_request(feat, func, params, timeout_ms=2000):
            return request_responses.get((feat, func))

        with (
            patch.object(listener, "_find_feature", side_effect=fake_find),
            patch.object(listener, "_request", side_effect=fake_request),
        ):
            hw_fi = listener._find_feature(FEAT_HIRES_WHEEL_ENHANCED)
            if hw_fi:
                listener._hires_wheel_idx = hw_fi
                cap = listener._request(hw_fi, 0, [])
                if cap:
                    _, _, _, _, p = cap
                    listener._hires_wheel_multiplier = p[0] or None
            tw_fi = listener._find_feature(FEAT_THUMB_WHEEL)
            if tw_fi:
                listener._thumbwheel_idx = tw_fi
                info = listener._request(tw_fi, 0, [])
                if info:
                    _, _, _, _, p = info
                    listener._thumbwheel_multiplier = ((p[2] << 8) | p[3]) or None

        self.assertEqual(listener._hires_wheel_idx, 0x07)
        self.assertEqual(listener._hires_wheel_multiplier, 8)
        self.assertEqual(listener._thumbwheel_idx, 0x08)
        self.assertEqual(listener._thumbwheel_multiplier, 120)
        self.assertTrue(listener.hires_wheel_supported)
        self.assertTrue(listener.thumbwheel_supported)

    def test_capability_discovery_negative(self):
        listener = _make_listener()
        with (
            patch.object(listener, "_find_feature", return_value=None),
            patch.object(listener, "_request", return_value=None),
        ):
            self.assertIsNone(listener._find_feature(FEAT_HIRES_WHEEL_ENHANCED))
        self.assertFalse(listener.hires_wheel_supported)
        self.assertFalse(listener.thumbwheel_supported)


# ──────────────────────────────────────────────────────────────────────────────
# Native-invert apply
# ──────────────────────────────────────────────────────────────────────────────


class _FakeDevice:
    def write(self, *args, **kwargs):
        return len(args[0]) if args else 0

    def read(self, *args, **kwargs):
        return None

    def close(self):
        pass


class NativeInvertApplyTests(unittest.TestCase):
    def _setup_capable_listener(self):
        listener = _make_listener()
        listener._dev = _FakeDevice()
        listener._hires_wheel_idx = 0x07
        listener._thumbwheel_idx = 0x08
        listener._hires_wheel_multiplier = 8
        listener._thumbwheel_multiplier = 120
        return listener

    @staticmethod
    def _request_router(get_mode_response, write_response=None, *, thumb_honors=True):
        """Stateful side_effect for ``_request`` simulating a device that
        HONORS writes -- the read-back (fn=1) reflects the last write (fn=2),
        which the verify-after-write helper requires. 0x07 = hi-res wheel
        (mode byte0), 0x08 = thumbwheel (status [reportingMode, invert]).

        ``thumb_honors=False`` simulates firmware that ACKs the thumbwheel
        invert write but never applies it (the original MX Master): the write
        returns an ack, yet the status read-back stays non-inverted, so the
        verify-after-write helper correctly reports the axis as failed.
        """
        if write_response is None:
            write_response = _resp([0])
        seed = get_mode_response[4][0] if get_mode_response[4] else 0
        state = {"vmode": int(seed) & 0xFF, "tinvert": 0}

        def _route(feat, func, params, timeout_ms=2000):
            if feat == 0x07 and func == 1:
                return _resp([state["vmode"]])
            if feat == 0x07 and func == 2:
                state["vmode"] = int(params[0]) & 0xFF
                return write_response
            if feat == 0x08 and func == 1:
                return _resp([0x00, state["tinvert"]])
            if feat == 0x08 and func == 2:
                if thumb_honors:
                    state["tinvert"] = int(params[1]) & 0x01
                return write_response
            return write_response

        return _route

    def test_apply_invert_on_writes_invert_keeping_low_res(self):
        # Device is already low-res, so preserving bit 1 leaves it low-res.
        # Mouser only adds the invert bit.
        listener = self._setup_capable_listener()
        get_mode = _resp([0x00])
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
            self.assertTrue(listener._wheel_divert_state)
            req.assert_any_call(0x07, 1, [])               # read current mode
            req.assert_any_call(0x07, 2, [0x04])           # low-res kept + invert
            req.assert_any_call(0x08, 2, [0x00, 0x01])

    def test_apply_invert_on_preserves_existing_hires_bit(self):
        # The resolution bit is not ours. On Linux the kernel's
        # hid-logitech-hidpp enables hi-res at probe and then divides wheel
        # deltas by a latched multiplier; clearing it here made the device
        # emit 1 unit/detent while the kernel still divided, so scrolling
        # crawled and survived process exit (issue #244).
        listener = self._setup_capable_listener()
        get_mode = _resp([0x02])  # hi-res, native, no invert
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, False)
            listener._apply_pending_native_wheel_invert()
            req.assert_any_call(0x07, 2, [0x06])           # hi-res KEPT, invert set
            req.assert_any_call(0x08, 2, [0x00, 0x00])

    def test_apply_invert_off_preserves_hires_bit(self):
        listener = self._setup_capable_listener()
        listener._wheel_divert_state = True
        get_mode = _resp([0x06])  # hi-res + invert active
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (False, False)
            listener._apply_pending_native_wheel_invert()
            self.assertTrue(listener._wheel_divert_state)
            req.assert_any_call(0x07, 2, [0x02])           # invert dropped, hi-res kept
            req.assert_any_call(0x08, 2, [0x00, 0x00])

    def test_apply_invert_clears_divert_bit_but_keeps_hires(self):
        # Pathological case: device left in divert state from a crashed
        # Mouser session. We must still clear bit 0 (target) to recover it,
        # but bit 1 (hi-res) is not ours to clear -- see #244.
        listener = self._setup_capable_listener()
        get_mode = _resp([0x07])  # target + hi-res + invert
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, False)
            listener._apply_pending_native_wheel_invert()
            req.assert_any_call(0x07, 2, [0x06])           # divert cleared, hi-res kept

    def test_kernel_hires_survives_connect_with_invert_off(self):
        # Regression for #244. The reported case: a Linux user with hi-res
        # enabled by the kernel who never turned on scroll inversion. The
        # engine still calls request_wheel_native_invert(False, False) for
        # any hi-res-capable device, which used to blind-write 0x00 and
        # clobber the kernel's hi-res, leaving scroll ~8-15x too slow until
        # the mouse was physically power-cycled. Nothing needs changing
        # here, so the correct behaviour is to issue no write at all.
        listener = self._setup_capable_listener()
        get_mode = _resp([0x02])  # kernel enabled hi-res at probe
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (False, False)
            listener._apply_pending_native_wheel_invert()
            # Upstream asserts no write is issued at all. We keep the
            # unconditional write (firmware drifts across sleep, and the
            # read above can report a stale mode), so assert the actual #244
            # requirement instead: whatever is written must preserve the
            # kernel's resolution bit. A blind 0x00 -- the original bug --
            # still fails this.
            writes = [
                c.args[2][0]
                for c in req.call_args_list
                if c.args[:2] == (0x07, 2)
            ]
            for mode in writes:
                self.assertTrue(
                    mode & 0x02,
                    f"write of 0x{mode:02X} cleared the kernel's hi-res bit (#244)",
                )

    def test_stop_restores_wheel_mode_captured_at_connect(self):
        # stop() used to "revert" by writing 0x00, the same value that broke
        # #244, so even a graceful exit left the device degraded. It must
        # restore the byte we actually found at connect.
        listener = self._setup_capable_listener()
        listener._dev = MagicMock()
        listener._wheel_divert_state = True
        listener._hires_wheel_mode_initial = 0x02  # hi-res as found
        with patch.object(
            listener, "_request", side_effect=self._request_router(_resp([0x06])),
        ) as req:
            listener.stop()
        self.assertIn(
            ((0x07, 2, [0x02]), {}),
            [(c.args, c.kwargs) for c in req.call_args_list],
            "stop() must restore the wheel mode captured at connect (#244)",
        )

    def test_apply_invert_always_writes_to_drive_target(self):
        # Mouser drives the wheel mode to target regardless of the device's
        # current state, then verifies via read-back -- there is no "skip the
        # write" short-circuit (firmware can silently drift, e.g. after sleep).
        listener = self._setup_capable_listener()
        get_mode = _resp([0x04])  # already native low-res + invert
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
            self.assertEqual(listener._wheel_divert_result, (True, True))
            self.assertIn(
                [0x04],
                [c.args[2] for c in req.call_args_list if c.args[:2] == (0x07, 2)],
                "vertical setWheelMode write must always be issued",
            )

    def test_apply_invert_hscroll_without_thumbwheel_keeps_vertical(self):
        # Device exposes 0x2121 but not 0x2150 (e.g. MX Anywhere). The absent
        # thumbwheel reports horizontal as a no-firmware axis (handled by the OS
        # fallback) while vertical still holds the firmware lease -- per-axis,
        # never all-or-nothing.
        listener = self._setup_capable_listener()
        listener._thumbwheel_idx = None
        get_mode = _resp([0x00])
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ):
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
            # vertical honored (True); horizontal has no firmware feature ->
            # _set_native_wheel_invert_horizontal(True) returns False so the OS
            # layer owns it. The vertical lease is NOT collapsed.
            self.assertEqual(listener._wheel_divert_result, (True, False))
            self.assertTrue(listener._wheel_divert_state)

    def test_apply_invert_succeeds_vertical_only_without_thumbwheel(self):
        # Same device, but no horizontal inversion requested: the absent
        # thumbwheel feature must still count as a no-op success.
        listener = self._setup_capable_listener()
        listener._thumbwheel_idx = None
        get_mode = _resp([0x00])
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(get_mode),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, False)
            listener._apply_pending_native_wheel_invert()
            self.assertTrue(listener._wheel_divert_state)
            req.assert_any_call(0x07, 2, [0x04])

    def test_horizontal_failure_keeps_vertical(self):
        # THE regression test for the original-MX-Master double-invert bug.
        # Vertical (0x2121) honors invert; thumbwheel (0x2150) ACKs the write
        # but never applies it (read-back stays non-inverted). The vertical
        # firmware invert must STAY -- the old code reverted it (ok_v AND ok_h)
        # which double-inverted vertical scroll. Axes are independent.
        listener = self._setup_capable_listener()
        with patch.object(
            listener, "_request",
            side_effect=self._request_router(_resp([0x00]), thumb_honors=False),
        ) as req:
            with listener._wheel_divert_lock:
                listener._pending_wheel_divert = (True, True)
            listener._apply_pending_native_wheel_invert()
        self.assertEqual(listener._wheel_divert_result, (True, False))
        self.assertTrue(listener._wheel_divert_state)
        vertical_writes = [
            c.args[2] for c in req.call_args_list if c.args[:2] == (0x07, 2)
        ]
        self.assertEqual(
            vertical_writes, [[0x04]],
            "vertical written once and NEVER reverted after horizontal failure",
        )

    def test_request_native_invert_idempotent(self):
        """Two consecutive request_wheel_native_invert calls each issue
        fresh device reads/writes (firmware can forget after sleep)."""
        listener = self._setup_capable_listener()

        def drain_apply():
            listener._apply_pending_native_wheel_invert()

        with patch.object(
            listener, "_request",
            side_effect=self._request_router(_resp([0x00])),
        ) as req:
            def fake_wait(timeout=None):
                drain_apply()
                listener._wheel_divert_event.set()
                return True

            with patch.object(listener._wheel_divert_event, "wait", side_effect=fake_wait):
                ok1 = listener.request_wheel_native_invert(True, False)
                ok2 = listener.request_wheel_native_invert(True, False)

            # Per-axis result tuple: vertical honored, horizontal write (off)
            # also succeeds on this capable device.
            self.assertEqual(ok1, (True, True))
            self.assertEqual(ok2, (True, True))
            # Each call writes+reads both axes, so well above a handful of calls.
            self.assertGreaterEqual(req.call_count, 6)

    def test_undivert_on_stop(self):
        """stop() restores the device to native non-inverted state when the
        listener was holding firmware invert active. The read-modify-write
        helper inspects the current mode first, so we simulate a device
        currently inverted (bit 2 set) to force the revert write to fire."""
        listener = self._setup_capable_listener()
        listener._wheel_divert_state = True
        listener._connected_device_info = SimpleNamespace(key="mx_master_3s")
        listener._thread = None

        with patch.object(
            listener, "_request",
            side_effect=self._request_router(_resp([0x04])),
        ) as req:
            listener.stop()

        targets = {(c.args[0], c.args[1]) for c in req.call_args_list}
        self.assertIn((0x07, 1), targets)   # read current mode (RMW)
        self.assertIn((0x07, 2), targets)   # write reverted mode
        self.assertIn((0x08, 2), targets)   # thumbwheel revert
        self.assertFalse(listener._wheel_divert_state)


# ──────────────────────────────────────────────────────────────────────────────
# Catalog flags
# ──────────────────────────────────────────────────────────────────────────────


class CatalogFlagsTests(unittest.TestCase):
    def test_catalog_flags(self):
        for name in ("MX Master 3S", "MX Master 3", "MX Master 4", "MX Master 2S", "MX Master"):
            spec = resolve_device(product_name=name)
            self.assertIsNotNone(spec, name)
            self.assertTrue(spec.has_hires_wheel, name)
            self.assertTrue(spec.has_thumbwheel, name)

        spec = resolve_device(product_name="MX Vertical")
        self.assertIsNotNone(spec)
        self.assertFalse(spec.has_hires_wheel)
        self.assertFalse(spec.has_thumbwheel)


# ──────────────────────────────────────────────────────────────────────────────
# Base hook native-invert flag
# ──────────────────────────────────────────────────────────────────────────────


class BaseHookFlagTests(unittest.TestCase):
    def test_default_state(self):
        hook = BaseMouseHook()
        self.assertFalse(hook.wheel_native_invert_vertical)
        self.assertFalse(hook.wheel_native_invert_horizontal)

    def test_configure_wheel_multipliers_is_noop(self):
        # Native-invert mode does no scroll injection, so multipliers are
        # unused. The method is retained only for shape compatibility.
        hook = BaseMouseHook()
        hook.configure_wheel_multipliers(8, 120)
        # No exception, no state change beyond not having the old fields.
        self.assertFalse(hasattr(hook, "_wheel_residual_v"))


# ──────────────────────────────────────────────────────────────────────────────
# macOS event-tap suppression of OS-layer inversion
# ──────────────────────────────────────────────────────────────────────────────


class MacOSSuppressionTests(unittest.TestCase):
    """When the per-axis ``wheel_native_invert_{vertical,horizontal}`` flag is
    True, the macOS event-tap callback must skip the OS-layer inversion path
    (`_negate_scroll_axis`) for THAT axis so the firmware-level flip doesn't get
    double-applied. When inactive, in-place negation runs against the original
    event (no block-and-reinject). The axes are independent."""

    _kCGScrollWheelEventIsContinuous = 88
    _kCGEventScrollWheel = 22

    def _mark_recent_logitech_scroll(self, hook):
        hook._logitech_scroll_monitor.mark_wheel()

    def _mock_get_field(
        self,
        *,
        is_continuous=0,
        source_user_data=0,
        momentum_phase=0,
        scroll_phase=0,
    ):
        def _get(_event, field):
            if field == self._kCGScrollWheelEventIsContinuous:
                return is_continuous
            if field == self._mouse_hook_macos._CG_SCROLL_FIELD_MOMENTUM_PHASE:
                return momentum_phase
            if field == self._mouse_hook_macos._CG_SCROLL_FIELD_SCROLL_PHASE:
                return scroll_phase
            if field == self.mock_quartz.kCGEventSourceUserData:
                return source_user_data
            return 0
        return _get

    def setUp(self):
        try:
            from core import mouse_hook_macos
        except Exception:
            self.skipTest("macOS hook unavailable in this environment")
        self._mouse_hook_macos = mouse_hook_macos
        self._prev_quartz = getattr(mouse_hook_macos, "Quartz", None)
        self.mock_quartz = MagicMock(name="Quartz")
        self.mock_quartz.kCGEventScrollWheel = self._kCGEventScrollWheel
        mouse_hook_macos.Quartz = self.mock_quartz
        self._sync_patch = patch.object(
            mouse_hook_macos.MouseHook,
            "_sync_logitech_scroll_monitor",
            lambda self: None,
        )
        self._sync_patch.start()

    def tearDown(self):
        self._sync_patch.stop()
        if self._prev_quartz is None:
            if hasattr(self._mouse_hook_macos, "Quartz"):
                delattr(self._mouse_hook_macos, "Quartz")
        else:
            self._mouse_hook_macos.Quartz = self._prev_quartz

    def test_scroll_attribution_rejects_continuous_trackpad(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook.ignore_trackpad = True
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=1)
        )
        self.assertFalse(
            hook._scroll_event_targets_logitech(cg_event=cg_event)
        )

    def test_scroll_attribution_rejects_momentum_phase(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(momentum_phase=1)
        )
        self.assertFalse(
            hook._scroll_event_targets_logitech(cg_event=cg_event)
        )

    def test_scroll_attribution_rejects_in_progress_scroll_phase(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(scroll_phase=1)
        )
        self.assertFalse(
            hook._scroll_event_targets_logitech(cg_event=cg_event)
        )

    def test_os_inversion_skipped_when_native_active(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.wheel_native_invert_vertical = True
        hook.wheel_native_invert_horizontal = False
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        negate.assert_not_called()
        # Original event flows through untouched -- no block, no reinject.
        self.assertIs(result, cg_event)

    def _logitech_stub(self):
        """Minimal stand-in for a connected Logitech ``ConnectedDeviceInfo``.

        The OS-fallback inversion path requires ``_connected_device is not
        None`` as proof that scroll events are coming from a Logitech the
        user's invert toggle is meant to apply to. Tests that exercise the
        fallback path must pin this state explicitly.
        """
        return SimpleNamespace(
            key="mx_master_3s",
            display_name="MX Master 3S",
            source="hidapi",
            thumb_button_via_hid=False,
            gesture_via_sense_panel=False,
        )

    def test_os_inversion_runs_when_native_inactive(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.wheel_native_invert_vertical = False
        hook.wheel_native_invert_horizontal = False
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        # Vertical inversion negates axis 1 in place; the SAME event is
        # returned (not None), so the caller passes it through untouched
        # apart from the sign flip.
        negate.assert_called_once_with(cg_event, 1)
        self.assertIs(result, cg_event)

    def test_horizontal_inversion_negates_axis_2_in_place(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_hscroll = True
        hook.wheel_native_invert_vertical = False
        hook.wheel_native_invert_horizontal = False
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        negate.assert_called_once_with(cg_event, 2)
        self.assertIs(result, cg_event)

    def test_vertical_firmware_does_not_suppress_horizontal_os_fallback(self):
        """Original MX Master: vertical is firmware-inverted but the thumbwheel
        rejects firmware invert, so horizontal must still flip via the OS layer.
        The per-axis flags must keep the horizontal fallback alive even while
        the vertical firmware lease is held (the bug was a single global flag
        that suppressed BOTH)."""
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.invert_hscroll = True
        hook.wheel_native_invert_vertical = True    # firmware holds vertical
        hook.wheel_native_invert_horizontal = False  # OS owns horizontal
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        # Vertical is firmware-inverted -> NOT negated; horizontal IS.
        negate.assert_called_once_with(cg_event, 2)
        self.assertIs(result, cg_event)

    def test_both_axes_inverted_in_single_pass(self):
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.invert_hscroll = True
        hook.wheel_native_invert_vertical = False
        hook.wheel_native_invert_horizontal = False
        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        negate.assert_any_call(cg_event, 1)
        negate.assert_any_call(cg_event, 2)
        self.assertEqual(negate.call_count, 2)
        self.assertIs(result, cg_event)

    def test_os_inversion_skipped_when_no_logitech_connected(self):
        """The wheel-invert toggle is meant for Logitech scroll. When no
        Logitech is connected we have no source-of-truth that a scroll event
        came from a device the toggle applies to, so the OS-layer fallback
        must stand down rather than invert every trackpad / generic mouse
        scroll the OS forwards through us.
        """
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.invert_hscroll = True
        hook.wheel_native_invert_vertical = False
        hook.wheel_native_invert_horizontal = False
        hook._connected_device = None  # no Logitech detected
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        negate.assert_not_called()
        self.assertIs(result, cg_event)

    def test_os_inversion_skipped_for_unattributed_scroll(self):
        """A connected Logitech is not enough: only wheel events the IOHID
        monitor actually saw from a Logitech mouse may be inverted."""
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.wheel_native_invert_vertical = False
        hook._connected_device = self._logitech_stub()
        cg_event = MagicMock(name="cg_event")
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )
        with patch.object(hook, "_negate_scroll_axis") as negate:
            result = hook._event_tap_callback(
                None, self._kCGEventScrollWheel, cg_event, None
            )
        negate.assert_not_called()
        self.assertIs(result, cg_event)

    def test_os_inversion_resumes_when_logitech_reconnects(self):
        """Disconnect/reconnect transitions must not require Mouser restart:
        the very next event after ``_connected_device`` flips back to a
        ``ConnectedDeviceInfo`` is the one we start inverting again.
        """
        hook = self._mouse_hook_macos.MouseHook()
        hook._running = True
        hook._tap = MagicMock(name="tap")
        hook.invert_vscroll = True
        hook.wheel_native_invert_vertical = False
        hook.wheel_native_invert_horizontal = False
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = (
            self._mock_get_field(is_continuous=0)
        )

        hook._connected_device = None
        with patch.object(hook, "_negate_scroll_axis") as negate_off:
            hook._event_tap_callback(
                None, self._kCGEventScrollWheel, MagicMock(name="evt-off"), None
            )
        negate_off.assert_not_called()

        hook._connected_device = self._logitech_stub()
        self._mark_recent_logitech_scroll(hook)
        with patch.object(hook, "_negate_scroll_axis") as negate_on:
            hook._event_tap_callback(
                None, self._kCGEventScrollWheel, MagicMock(name="evt-on"), None
            )
        negate_on.assert_called_once()

    def test_negate_scroll_axis_flips_all_three_delta_fields_in_place(self):
        """Direct unit test: negate flips Delta, FixedPtDelta, and
        PointDelta for the requested axis. Apps read different fields,
        so all three must be consistent."""
        from unittest.mock import call
        hook = self._mouse_hook_macos.MouseHook()
        # Mock Quartz field-name attributes the negate loop reads.
        self.mock_quartz.kCGScrollWheelEventDeltaAxis1 = 0xA
        self.mock_quartz.kCGScrollWheelEventFixedPtDeltaAxis1 = 0xB
        self.mock_quartz.kCGScrollWheelEventPointDeltaAxis1 = 0xC
        cg_event = MagicMock(name="cg_event")
        # Field-id → mocked current value lookup.
        values = {0xA: 5, 0xB: 50_000, 0xC: 8}

        def _get_field(_event, field):
            return values.get(field, 0)
        self.mock_quartz.CGEventGetIntegerValueField.side_effect = _get_field
        sets = []

        def _set_field(_event, field, value):
            sets.append((field, value))
        self.mock_quartz.CGEventSetIntegerValueField.side_effect = _set_field

        hook._negate_scroll_axis(cg_event, 1)

        self.assertIn((0xA, -5), sets)
        self.assertIn((0xB, -50_000), sets)
        self.assertIn((0xC, -8), sets)


# ──────────────────────────────────────────────────────────────────────────────
# Protocol conformance
# ──────────────────────────────────────────────────────────────────────────────


class ProtocolConformanceTests(unittest.TestCase):
    def test_protocol_conformance(self):
        modules = []
        for name in ("mouse_hook_macos", "mouse_hook_windows", "mouse_hook_linux"):
            try:
                mod = __import__(f"core.{name}", fromlist=["MouseHook"])
                modules.append(mod.MouseHook)
            except Exception:
                continue
        if not modules:
            self.skipTest("No platform mouse hook importable")

        for cls in modules:
            try:
                inst = cls()
            except Exception:
                inst = cls.__new__(cls)
                BaseMouseHook.__init__(inst)
            for attr in (
                "wheel_native_invert_vertical",
                "wheel_native_invert_horizontal",
                "invert_vscroll",
                "invert_hscroll",
                "_physical_logitech_bound",
                "_scroll_event_targets_logitech",
            ):
                self.assertTrue(
                    hasattr(inst, attr),
                    f"{cls.__name__} missing {attr}",
                )


# ──────────────────────────────────────────────────────────────────────────────
# Engine driver
# ──────────────────────────────────────────────────────────────────────────────


class _FakeHook:
    def __init__(self):
        self.invert_vscroll = False
        self.invert_hscroll = False
        self.debug_mode = False
        self.connected_device = None
        self.device_connected = False
        self.divert_mode_shift = False
        self.divert_dpi_switch = False
        self.wheel_native_invert_vertical = False
        self.wheel_native_invert_horizontal = False
        self.wheel_divert_active = False  # back-compat alias
        self._hid_gesture = None
        self._blocked_events = set()

    def set_debug_callback(self, cb): pass
    def set_gesture_callback(self, cb): pass
    def set_status_callback(self, cb): pass
    def set_connection_change_callback(self, cb): pass
    def configure_gestures(self, **kwargs): pass
    def configure_wheel_multipliers(self, v, h): return None
    def block(self, event_type): pass
    def register(self, event_type, callback): pass
    def reset_bindings(self): pass
    def start(self): pass
    def stop(self): pass

    def _physical_logitech_bound(self):
        device = self.connected_device
        if device is None:
            return False
        return is_physical_device_source(getattr(device, "source", None))


class _FakeAppDetector:
    def __init__(self, callback):
        self.callback = callback
    def start(self): pass
    def stop(self): pass


class _FakeHidGesture:
    def __init__(self, *, ack=True, ack_v=None, ack_h=None, has_wheel=True, has_thumb=True):
        self.ack = ack
        self.ack_v = ack if ack_v is None else ack_v
        self.ack_h = ack if ack_h is None else ack_h
        self.requests = []
        self._hires_wheel_idx = 0x07 if has_wheel else None
        self._thumbwheel_idx = 0x08 if has_thumb else None
        self._hires_wheel_multiplier = 8 if has_wheel else None
        self._thumbwheel_multiplier = 120 if has_thumb else None
        self.connected_device = SimpleNamespace(
            has_hires_wheel=has_wheel, has_thumbwheel=has_thumb,
        )
        self.smart_shift_supported = False
        self.flags_set_to = None

    def request_wheel_native_invert(self, invert_v, invert_h, timeout_s=3.0):
        self.requests.append((bool(invert_v), bool(invert_h)))
        return (bool(self.ack_v), bool(self.ack_h))

    def set_wheel_divert_active_flags(self, vertical, thumb):
        self.flags_set_to = (vertical, thumb)


class EngineNativeInvertTests(unittest.TestCase):
    def _make_engine(self, *, wheel_divert="auto", invert_v=False, invert_h=False,
                     ack=True, has_wheel=True, has_thumb=True, capable=True):
        from core.engine import Engine

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["settings"]["wheel_divert"] = wheel_divert
        cfg["settings"]["invert_vscroll"] = invert_v
        cfg["settings"]["invert_hscroll"] = invert_h

        with (
            patch("core.engine.MouseHook", _FakeHook),
            patch("core.engine.AppDetector", _FakeAppDetector),
            patch("core.engine.load_config", return_value=cfg),
        ):
            engine = Engine()
        if capable:
            engine.hook._hid_gesture = _FakeHidGesture(
                ack=ack, has_wheel=has_wheel, has_thumb=has_thumb,
            )
            engine.hook.connected_device = SimpleNamespace(
                source="hidapi",
                has_hires_wheel=has_wheel,
                has_thumbwheel=has_thumb,
            )
        return engine

    def test_capable_device_drives_native_invert(self):
        engine = self._make_engine(invert_v=True, invert_h=False)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        self.assertEqual(hg.requests, [(True, False)])
        self.assertTrue(engine.wheel_native_invert_active)
        # Per-axis: vertical holds the firmware lease, horizontal does not.
        self.assertTrue(engine.hook.wheel_native_invert_vertical)
        self.assertFalse(engine.hook.wheel_native_invert_horizontal)

    def test_capable_device_resets_to_native_when_invert_off(self):
        # Even with both flags False, the engine still issues the reset write so
        # a stale invert lease from a prior crashed Mouser session gets cleared.
        # Nothing is actively inverted afterwards, so the active flag is False.
        engine = self._make_engine(invert_v=False, invert_h=False)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        self.assertEqual(hg.requests, [(False, False)])
        self.assertFalse(engine.wheel_native_invert_active)
        self.assertFalse(engine.hook.wheel_native_invert_vertical)
        self.assertFalse(engine.hook.wheel_native_invert_horizontal)

    def test_kill_switch_skips_firmware_invert(self):
        engine = self._make_engine(wheel_divert="off", invert_v=True)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        # No request issued when kill-switch is on.
        self.assertEqual(hg.requests, [])
        self.assertFalse(engine.wheel_native_invert_active)

    def test_incapable_device_skips_firmware_invert(self):
        engine = self._make_engine(invert_v=True, has_wheel=False, has_thumb=False)
        engine.hook.connected_device = SimpleNamespace(
            source="hidapi",
            has_hires_wheel=False, has_thumbwheel=False,
        )
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        self.assertEqual(hg.requests, [])
        self.assertFalse(engine.wheel_native_invert_active)

    def test_virtual_remote_device_skips_firmware_invert(self):
        engine = self._make_engine(invert_v=True, invert_h=True)
        engine.hook.connected_device = SimpleNamespace(
            source=DEVICE_SOURCE_REMOTE_VIRTUAL,
            has_hires_wheel=True,
            has_thumbwheel=True,
        )
        engine._apply_wheel_invert_setting()
        self.assertEqual(engine.hook._hid_gesture.requests, [])
        self.assertFalse(engine.wheel_native_invert_active)

    def test_virtual_deskflow_shim_skips_firmware_invert(self):
        engine = self._make_engine(invert_v=True)
        engine.hook.connected_device = SimpleNamespace(
            source=DEVICE_SOURCE_DESKFLOW_SHIM,
            has_hires_wheel=True,
            has_thumbwheel=True,
        )
        engine._apply_wheel_invert_setting()
        self.assertEqual(engine.hook._hid_gesture.requests, [])

    def test_virtual_remote_skips_horizontal_os_fallback(self):
        hook = BaseMouseHook()
        hook._connected_device = SimpleNamespace(source=DEVICE_SOURCE_REMOTE_VIRTUAL)
        hook.invert_hscroll = True
        self.assertFalse(hook._apply_hscroll_invert_fallback(linux_evdev=True))

    def test_failed_ack_falls_back_to_os_layer(self):
        engine = self._make_engine(invert_v=True, ack=False)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        self.assertEqual(hg.requests, [(True, False)])
        self.assertFalse(engine.wheel_native_invert_active)

    def test_horizontal_firmware_failure_keeps_vertical_lease(self):
        # Original MX Master end-to-end: vertical firmware invert succeeds,
        # horizontal firmware invert fails. Vertical must keep its firmware
        # lease (hook flag True) while horizontal falls to the OS layer (hook
        # flag False) -- the engine must never collapse both.
        engine = self._make_engine(invert_v=True, invert_h=True)
        engine.hook._hid_gesture = _FakeHidGesture(ack_v=True, ack_h=False)
        engine.hook.connected_device = SimpleNamespace(
            has_hires_wheel=True, has_thumbwheel=True,
        )
        engine._apply_wheel_invert_setting()
        self.assertTrue(engine.hook.wheel_native_invert_vertical)
        self.assertFalse(engine.hook.wheel_native_invert_horizontal)
        self.assertTrue(engine.wheel_native_invert_active)

    def test_fast_path_skips_redundant_apply(self):
        engine = self._make_engine(invert_v=True)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        hg.requests.clear()
        for _ in range(5):
            engine._apply_wheel_invert_setting()
        self.assertEqual(hg.requests, [])

    def test_force_replays_writes(self):
        engine = self._make_engine(invert_v=True)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        hg.requests.clear()
        engine._apply_wheel_invert_setting(force=True)
        self.assertEqual(hg.requests, [(True, False)])

    def test_toggle_invert_writes_new_state(self):
        engine = self._make_engine(invert_v=False)
        engine._apply_wheel_invert_setting()
        hg = engine.hook._hid_gesture
        hg.requests.clear()
        engine.cfg["settings"]["invert_vscroll"] = True
        engine._apply_wheel_invert_setting()
        self.assertEqual(hg.requests, [(True, False)])

    def test_change_callback_fires_on_transition(self):
        engine = self._make_engine(invert_v=True)
        seen = []
        engine.set_wheel_divert_change_callback(seen.append)
        self.assertEqual(seen, [False])
        engine._apply_wheel_invert_setting()
        self.assertEqual(seen, [False, True])
        engine.cfg["settings"]["wheel_divert"] = "off"
        engine._apply_wheel_invert_setting()
        self.assertEqual(seen, [False, True, False])


# ──────────────────────────────────────────────────────────────────────────────
# Scroll monitor + Linux attribution
# ──────────────────────────────────────────────────────────────────────────────


class LogitechScrollMonitorTests(unittest.TestCase):
    def test_recent_wheel_expires(self):
        monitor = LogitechScrollMonitor()
        monitor.mark_wheel()
        self.assertTrue(monitor.recent_wheel())
        monitor._last_wheel_monotonic -= 1.0
        self.assertFalse(monitor.recent_wheel())

    def test_stop_clears_recent_mark(self):
        monitor = LogitechScrollMonitor()
        monitor.mark_wheel()
        monitor.stop()
        self.assertFalse(monitor.recent_wheel())


class MacOSScrollMonitorLifecycleTests(unittest.TestCase):
    def test_sync_starts_only_when_physical_logitech_bound(self):
        try:
            from core import mouse_hook_macos as mhm
        except Exception:
            self.skipTest("macOS hook unavailable")
        hook = mhm.MouseHook()
        monitor = hook._logitech_scroll_monitor
        with (
            patch.object(mhm, "SCROLL_MONITOR_AVAILABLE", True),
            patch.object(monitor, "start") as start,
            patch.object(monitor, "stop") as stop,
        ):
            hook._connected_device = SimpleNamespace(source="hidapi")
            hook._sync_logitech_scroll_monitor()
            start.assert_called_once()
            stop.assert_not_called()

            start.reset_mock()
            hook._connected_device = SimpleNamespace(
                source=DEVICE_SOURCE_REMOTE_VIRTUAL
            )
            hook._sync_logitech_scroll_monitor()
            stop.assert_called_once()
            start.assert_not_called()

    def test_scroll_attribution_fail_closed_without_monitor(self):
        try:
            from core import mouse_hook_macos as mhm
        except Exception:
            self.skipTest("macOS hook unavailable")
        hook = mhm.MouseHook()
        with patch.object(mhm, "SCROLL_MONITOR_AVAILABLE", False):
            self.assertFalse(
                hook._scroll_event_targets_logitech(cg_event=MagicMock())
            )


class LinuxScrollAttributionTests(unittest.TestCase):
    def test_handle_rel_passes_linux_evdev_to_vertical_fallback(self):
        try:
            from core import mouse_hook_linux as mhl
        except Exception:
            self.skipTest("Linux hook unavailable")
        if not mhl._EVDEV_OK:
            self.skipTest("evdev not installed")
        hook = mhl.MouseHook()
        hook._ui_passthrough = False
        hook._evdev_remap_ready = True
        hook._uinput = SimpleNamespace(write_event=Mock(), write=Mock())
        event = SimpleNamespace(
            type=mhl._ecodes.EV_REL,
            code=mhl._ecodes.REL_WHEEL,
            value=1,
        )
        with patch.object(
            hook, "_apply_vscroll_invert_fallback", return_value=True
        ) as gate:
            hook._handle_rel(event)
        gate.assert_called_once_with(linux_evdev=True)


# ──────────────────────────────────────────────────────────────────────────────
# Config migration
# ──────────────────────────────────────────────────────────────────────────────


class ConfigMigrationTests(unittest.TestCase):
    def test_migration_adds_wheel_divert_default_auto(self):
        legacy = {
            "version": 1,
            "settings": {"invert_vscroll": False},
            "profiles": {
                "default": {"label": "Default", "apps": [], "mappings": {}},
            },
        }
        migrated = _migrate(legacy)
        self.assertEqual(migrated["settings"]["wheel_divert"], "auto")

    def test_migration_preserves_off_value(self):
        legacy = {
            "version": 9,
            "settings": {"wheel_divert": "off"},
            "profiles": {},
        }
        migrated = _migrate(legacy)
        self.assertEqual(migrated["settings"]["wheel_divert"], "off")

    def test_v11_migration_preserves_user_thumb_button_mapping(self):
        # Idempotency: a v11 config with a user-mapped thumb_button must
        # NOT be clobbered by a re-run of the migration chain.
        already_v11 = {
            "version": 11,
            "settings": {"wheel_divert": "auto"},
            "profiles": {
                "default": {
                    "label": "Default",
                    "apps": [],
                    "mappings": {"thumb_button": "alt_tab"},
                },
            },
        }
        migrated = _migrate(already_v11)
        self.assertEqual(
            migrated["profiles"]["default"]["mappings"]["thumb_button"],
            "alt_tab",
        )

    def test_v11_migration_adds_default_when_missing(self):
        # Cold-start: a sub-v11 config should be populated with the
        # "none" default, not have an existing mapping overwritten.
        pre_v11 = {
            "version": 10,
            "settings": {"wheel_divert": "auto"},
            "profiles": {
                "gaming": {
                    "label": "Gaming",
                    "apps": [],
                    "mappings": {"xbutton1": "browser_back"},
                },
            },
        }
        migrated = _migrate(pre_v11)
        self.assertEqual(
            migrated["profiles"]["gaming"]["mappings"]["thumb_button"],
            "none",
        )
        self.assertEqual(
            migrated["profiles"]["gaming"]["mappings"]["xbutton1"],
            "browser_back",
        )


if __name__ == "__main__":
    unittest.main()
