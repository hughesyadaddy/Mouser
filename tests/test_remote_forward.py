"""Tests for the KVM-bridge event forwarder (core/remote_forward.py)."""

import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import (
    DEVICE_SOURCE_DESKFLOW_SHIM,
    DEVICE_SOURCE_REMOTE_VIRTUAL,
)
from core.remote_forward import RemoteForwarder
from tests.support.windows_hook_import import import_windows_hook

TOKEN = "bridge-token"


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _FakeBridge:
    """Minimal bridge: accepts one client, answers hello, records lines,
    and can push focus messages."""

    def __init__(self, *, accept_token=TOKEN):
        self.accept_token = accept_token
        self.received = []
        self.hello = None
        self._conn = None
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            while True:
                conn, _ = self._listener.accept()
                reader = conn.makefile("rb")
                hello = json.loads(reader.readline())
                with self._lock:
                    self.hello = hello
                if hello.get("token") != self.accept_token:
                    conn.sendall(b'{"ok": false, "error": "unauthorized"}\n')
                    conn.close()
                    continue
                conn.sendall(b'{"ok": true}\n')
                with self._lock:
                    self._conn = conn
                for line in reader:
                    with self._lock:
                        self.received.append(json.loads(line))
        except OSError:
            pass

    def push_focus(self, screen, local):
        with self._lock:
            conn = self._conn
        conn.sendall(json.dumps(
            {"type": "focus", "screen": screen, "local": local}
        ).encode() + b"\n")

    def messages(self):
        with self._lock:
            return list(self.received)

    def drop_client(self):
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self):
        self.drop_client()
        try:
            self._listener.close()
        except OSError:
            pass


class _StubHook(BaseMouseHook):
    def __init__(self):
        super().__init__()
        self.local_calls = []

    def _on_hid_gesture_down(self):
        self.local_calls.append("gesture_down")

    def _on_hid_gesture_move(self, dx, dy):
        self.local_calls.append(("gesture_move", dx, dy))


class RemoteForwarderTests(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeBridge()
        self.device = SimpleNamespace(
            product_id=0xB042,
            product_name="MX Master 4",
            display_name="MX Master 4",
        )
        self.forwarder = RemoteForwarder(
            token=TOKEN,
            port=self.bridge.port,
            device_supplier=lambda: self.device,
        )

    def tearDown(self):
        self.forwarder.stop()
        self.bridge.close()

    def _start_connected(self):
        self.assertTrue(self.forwarder.start())
        self.assertTrue(
            _wait_until(lambda: any(
                m.get("type") == "connect" for m in self.bridge.messages()
            )),
            "forwarder must announce the device after hello",
        )

    # ── lifecycle / handshake ─────────────────────────────────────

    def test_start_refuses_without_token(self):
        self.assertFalse(RemoteForwarder(token="", port=self.bridge.port).start())

    def test_hello_carries_token_and_role(self):
        self._start_connected()
        self.assertEqual(self.bridge.hello["token"], TOKEN)
        self.assertEqual(self.bridge.hello["role"], "source")

    def test_announces_device_on_connect(self):
        self._start_connected()
        connect = next(
            m for m in self.bridge.messages() if m.get("type") == "connect"
        )
        self.assertEqual(connect["device"]["product_id"], "0xB042")
        self.assertEqual(connect["device"]["product_name"], "MX Master 4")

    # ── focus tracking ────────────────────────────────────────────

    def test_should_forward_follows_focus(self):
        self._start_connected()
        self.assertFalse(self.forwarder.should_forward())

        self.bridge.push_focus("office-pc", local=False)
        self.assertTrue(_wait_until(self.forwarder.should_forward))
        self.assertEqual(self.forwarder.focus_screen, "office-pc")

        self.bridge.push_focus("this-mac", local=True)
        self.assertTrue(
            _wait_until(lambda: not self.forwarder.should_forward())
        )

    def test_bridge_loss_fails_safe(self):
        self._start_connected()
        self.bridge.push_focus("office-pc", local=False)
        self.assertTrue(_wait_until(self.forwarder.should_forward))

        self.bridge.drop_client()

        self.assertTrue(
            _wait_until(lambda: not self.forwarder.should_forward()),
            "a dead bridge must never keep suppression active",
        )

    # ── event relay ───────────────────────────────────────────────

    def test_send_event_reaches_bridge(self):
        self._start_connected()
        self.forwarder.send_event("gesture_down")
        self.forwarder.send_event("gesture_move", dx=42, dy=-7)

        self.assertTrue(_wait_until(lambda: {
            "type": "event", "name": "gesture_move", "dx": 42, "dy": -7,
        } in self.bridge.messages()))
        self.assertIn(
            {"type": "event", "name": "gesture_down"}, self.bridge.messages()
        )

    def test_disconnect_notification(self):
        self._start_connected()
        self.forwarder.notify_device_disconnected()
        self.assertTrue(_wait_until(
            lambda: {"type": "disconnect"} in self.bridge.messages()
        ))


class RemoteForwarderDecodeOnlyTests(unittest.TestCase):
    """HID passthrough host: publish decode context only."""

    def setUp(self):
        self.bridge = _FakeBridge()
        self.decode = {
            "feat_idx": 11,
            "gesture_cid": "0x01A0",
            "rawxy": True,
        }
        self.forwarder = RemoteForwarder(
            token=TOKEN,
            port=self.bridge.port,
            decode_supplier=lambda: self.decode,
            decode_only=True,
        )

    def tearDown(self):
        self.forwarder.stop()
        self.bridge.close()

    def _start_connected(self):
        self.assertTrue(self.forwarder.start())
        self.assertTrue(
            _wait_until(lambda: any(
                m.get("type") == "decode" for m in self.bridge.messages()
            )),
            "decode-only forwarder must publish decode after hello",
        )

    def test_decode_only_sends_decode_not_connect(self):
        self._start_connected()
        types = {m.get("type") for m in self.bridge.messages()}
        self.assertIn("decode", types)
        self.assertNotIn("connect", types)

    def test_decode_only_never_sends_events(self):
        self._start_connected()
        self.bridge.push_focus("office-pc", local=False)
        self.forwarder.send_event("gesture_down")
        self.forwarder.send_report("11ff0b0001a00000")
        time.sleep(0.05)
        types = {m.get("type") for m in self.bridge.messages()}
        self.assertNotIn("event", types)
        self.assertNotIn("report", types)

    def test_decode_only_never_suppresses_locally(self):
        self._start_connected()
        self.bridge.push_focus("office-pc", local=False)
        self.assertTrue(_wait_until(lambda: not self.forwarder.should_forward()))
        self.assertFalse(self.forwarder.should_forward())

    def test_decode_only_dedupes_identical_decode(self):
        self._start_connected()
        before = len(self.bridge.messages())
        self.forwarder.notify_decode_changed()
        time.sleep(0.05)
        self.assertEqual(len(self.bridge.messages()), before)

    def test_decode_only_republishes_when_decode_changes(self):
        self._start_connected()
        self.decode["feat_idx"] = 12
        self.forwarder.notify_decode_changed()
        self.assertTrue(_wait_until(
            lambda: sum(1 for m in self.bridge.messages()
                        if m.get("type") == "decode") >= 2
        ))

    def test_decode_only_skips_disconnect(self):
        self._start_connected()
        self.forwarder.notify_device_disconnected()
        time.sleep(0.05)
        types = {m.get("type") for m in self.bridge.messages()}
        self.assertNotIn("disconnect", types)

    def test_decode_republished_after_bridge_reconnect(self):
        self._start_connected()
        initial = sum(
            1 for m in self.bridge.messages() if m.get("type") == "decode"
        )
        self.assertEqual(initial, 1)

        self.bridge.drop_client()
        self.assertTrue(_wait_until(
            lambda: sum(1 for m in self.bridge.messages()
                        if m.get("type") == "decode") > initial
        ))


class HookForwardingGateTests(unittest.TestCase):
    """The hook-side gating: forward instead of local handling, and the
    intercept gate stands down while focus is remote."""

    def _make_hook_with_forwarder(self, *, forwarding):
        hook = _StubHook()
        sent = []
        forwarder = SimpleNamespace(
            should_forward=lambda: forwarding,
            send_event=lambda name, **payload: sent.append((name, payload)),
        )
        hook.set_remote_forwarder(forwarder)
        return hook, sent

    def test_entry_forwards_when_active(self):
        hook, sent = self._make_hook_with_forwarder(forwarding=True)
        entry = hook._hid_event_entry("gesture_down", hook._on_hid_gesture_down)
        move = hook._hid_event_entry("gesture_move", hook._on_hid_gesture_move)

        entry()
        move(10, -4)

        self.assertEqual(sent, [
            ("gesture_down", {}),
            ("gesture_move", {"dx": 10, "dy": -4}),
        ])
        self.assertEqual(hook.local_calls, [])

    def test_entry_runs_locally_when_inactive(self):
        hook, sent = self._make_hook_with_forwarder(forwarding=False)
        entry = hook._hid_event_entry("gesture_down", hook._on_hid_gesture_down)

        entry()

        self.assertEqual(sent, [])
        self.assertEqual(hook.local_calls, ["gesture_down"])

    def test_entry_falls_back_locally_when_send_fails(self):
        hook = _StubHook()
        forwarder = SimpleNamespace(
            should_forward=lambda: True,
            send_event=lambda name, **payload: False,  # send failed
        )
        hook.set_remote_forwarder(forwarder)
        entry = hook._hid_event_entry("gesture_down", hook._on_hid_gesture_down)

        entry()

        self.assertEqual(hook.local_calls, ["gesture_down"])

    def test_intercept_gate_stands_down_while_forwarding(self):
        hook, _ = self._make_hook_with_forwarder(forwarding=True)
        hook._connected_device = object()
        self.assertFalse(hook._should_intercept_events())

    def test_intercept_gate_normal_without_forwarding(self):
        hook, _ = self._make_hook_with_forwarder(forwarding=False)
        hook._connected_device = object()
        self.assertTrue(hook._should_intercept_events())

    def test_intercept_gate_requires_device_regardless(self):
        hook, _ = self._make_hook_with_forwarder(forwarding=False)
        self.assertFalse(hook._should_intercept_events())

    def test_scroll_invert_fallback_active_while_forwarding(self):
        hook, _ = self._make_hook_with_forwarder(forwarding=True)
        hook._connected_device = SimpleNamespace(source="hidapi")
        hook.invert_vscroll = True
        hook.invert_hscroll = True
        self.assertFalse(hook._should_intercept_events())
        self.assertTrue(hook._apply_vscroll_invert_fallback(linux_evdev=True))
        self.assertTrue(hook._apply_hscroll_invert_fallback(linux_evdev=True))

    def test_scroll_invert_fallback_requires_logitech_event(self):
        hook, _ = self._make_hook_with_forwarder(forwarding=True)
        hook._connected_device = object()
        hook.invert_vscroll = True
        self.assertFalse(hook._apply_vscroll_invert_fallback())

    def test_scroll_invert_skipped_for_virtual_devices(self):
        hook = _StubHook()
        hook.invert_vscroll = True
        for source in (DEVICE_SOURCE_REMOTE_VIRTUAL, DEVICE_SOURCE_DESKFLOW_SHIM):
            with self.subTest(source=source):
                hook._connected_device = SimpleNamespace(source=source)
                self.assertFalse(
                    hook._apply_vscroll_invert_fallback(linux_evdev=True)
                )
                self.assertFalse(
                    hook._apply_hscroll_invert_fallback(linux_evdev=True)
                )

    def test_scroll_invert_allowed_for_physical_device(self):
        hook = _StubHook()
        hook._connected_device = SimpleNamespace(source="hidapi")
        hook.invert_vscroll = True
        self.assertTrue(hook._apply_vscroll_invert_fallback(linux_evdev=True))

    def test_raw_report_never_forwarded(self):
        hook = _StubHook()
        sent = []
        forwarder = SimpleNamespace(
            should_forward=lambda: True,
            send_report=lambda data: sent.append(data) or True,
        )
        hook.set_remote_forwarder(forwarder)
        self.assertFalse(hook._maybe_forward_raw_report(b"\x11\xff"))
        self.assertEqual(sent, [])


class WindowsScrollAttributionTests(unittest.TestCase):
    """Windows per-event scroll attribution via GetRawInputBuffer.

    Imports the Windows hook itself rather than hoping another test module
    already did -- see tests/support/windows_hook_import.py.
    """

    @classmethod
    def setUpClass(cls):
        cls.mhw = import_windows_hook()

    def test_returns_false_when_raw_buffer_empty(self):
        mhw = self.mhw
        hook = mhw.MouseHook()
        with patch.object(mhw, "GetRawInputBuffer", return_value=0):
            self.assertFalse(
                hook._scroll_event_targets_logitech(
                    wParam=mhw.WM_MOUSEWHEEL, lParam=0
                )
            )

    def test_returns_true_for_logitech_wheel_packet(self):
        import ctypes

        mhw = self.mhw
        header_size = ctypes.sizeof(mhw.RAWINPUTHEADER)
        mouse_size = ctypes.sizeof(mhw.RAWMOUSE)
        total = header_size + mouse_size
        buf = ctypes.create_string_buffer(total)
        mhw.RAWINPUTHEADER(
            dwType=mhw.RIM_TYPEMOUSE,
            dwSize=total,
            hDevice=0x1234,
            wParam=None,
        )
        # Pack header + mouse into buffer manually
        ctypes.memmove(
            buf,
            bytes(mhw.RAWINPUTHEADER(
                dwType=mhw.RIM_TYPEMOUSE,
                dwSize=total,
                hDevice=0x1234,
                wParam=None,
            )),
            header_size,
        )
        mouse = mhw.RAWMOUSE(
            usFlags=0,
            usButtonFlags=mhw.RI_MOUSE_WHEEL,
            usButtonData=120,
            ulRawButtons=0,
            lLastX=0,
            lLastY=0,
            ulExtraInformation=0,
        )
        ctypes.memmove(
            ctypes.addressof(buf) + header_size,
            ctypes.byref(mouse),
            mouse_size,
        )

        def _fill_buffer(out, size_ref, hdr_size):
            # The hook passes byref(size), which is a CArgObject -- the c_uint
            # it wraps is reachable as _obj, not .contents.
            if out is None:
                size_ref._obj.value = total
                return 0
            ctypes.memmove(out, buf, total)
            size_ref._obj.value = total
            return 1

        hook = mhw.MouseHook()
        with (
            patch.object(hook, "_is_logitech", return_value=True),
            patch.object(mhw, "GetRawInputBuffer", side_effect=_fill_buffer),
        ):
            self.assertTrue(
                hook._scroll_event_targets_logitech(
                    wParam=mhw.WM_MOUSEWHEEL, lParam=0
                )
            )

    def test_falls_back_to_recent_wm_input_wheel_mark(self):
        mhw = self.mhw
        hook = mhw.MouseHook()
        hook._last_logitech_wheel_monotonic = time.monotonic()
        with patch.object(mhw, "GetRawInputBuffer", return_value=0):
            self.assertTrue(
                hook._scroll_event_targets_logitech(
                    wParam=mhw.WM_MOUSEWHEEL, lParam=0
                )
            )

    def test_vid_match_requires_vid_token_not_bare_substring(self):
        mhw = self.mhw
        hook = mhw.MouseHook()
        hook._device_name_cache[1] = r"\\?\HID#VID_DEAD&PID_BEEF"
        hook._device_name_cache[2] = r"\\?\HID#VID_046D&PID_C52B"
        self.assertFalse(hook._is_logitech(1))
        self.assertTrue(hook._is_logitech(2))


if __name__ == "__main__":
    unittest.main()
