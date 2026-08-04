"""Deskflow ingress routes through the main HidGestureListener read loop."""

import json
import socket
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.hid_deskflow_backend import flush_deskflow_sink, reset_deskflow_sink_for_tests
from core.hid_gesture import HidGestureListener
from core.hid_sink import encode_report_frame
from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import DEVICE_SOURCE_DESKFLOW_SHIM
from core.remote_device import PROTOCOL_VERSION, RemoteDeviceServer

TOKEN = "listener-ingress-token"
DECODE = {
    "feat_idx": 0x0B,
    "gesture_cid": "0x01A0",
    "extra_diverts": {"0x00C4": "thumb_button"},
}
FRAME_GESTURE_DOWN = bytes.fromhex("11ff0b0001a00000")


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _StubHook(BaseMouseHook):
    def __init__(self):
        super().__init__()
        self.calls = []

    def _dispatch(self, event):
        self.calls.append(("dispatch", event.event_type))

    def start(self):
        self._start_hid_listener()
        return True


class _Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.settimeout(2)

    def hello(self):
        self.sock.sendall(
            json.dumps(
                {"type": "hello", "token": TOKEN, "version": PROTOCOL_VERSION}
            ).encode()
            + b"\n"
        )
        return json.loads(self.sock.recv(4096).decode().splitlines()[0])

    def send_json(self, payload):
        self.sock.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(self.sock.recv(4096).decode().splitlines()[0])

    def send_frame(self, frame: bytes):
        self.sock.sendall(frame)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class _IngressFixture:
    def __init__(self, mock_infos):
        self.mock_infos = mock_infos
        self.hook = _StubHook()
        self.server = None
        self.client = None

    def __enter__(self):
        self.hook.start()
        self.server = RemoteDeviceServer(
            self.hook,
            token=TOKEN,
            port=0,
            transparent_transport=True,
        )
        if not self.server.start():
            raise AssertionError("RemoteDeviceServer failed to start")
        self.client = _Client(self.server.port)
        if not self.client.hello().get("ok"):
            raise AssertionError("hello handshake failed")
        reply = self.client.send_json(
            {
                "type": "connect",
                "device": {
                    "product_id": "0xB042",
                    "product_name": "MX Master 4",
                    "decode": DECODE,
                },
            }
        )
        if not reply.get("ok"):
            raise AssertionError(f"connect failed: {reply!r}")
        if not _wait_until(
            lambda: (
                self.hook._hid_gesture is not None
                and self.hook._hid_gesture.connected_device is not None
                and self.hook._connected_device is not None
            )
        ):
            raise AssertionError(
                "Deskflow ingress did not propagate connected device to hook"
            )
        return self

    def __exit__(self, *_exc):
        if self.client is not None:
            try:
                self.client.send_json({"type": "disconnect"})
            except OSError:
                pass
            self.client.close()
        if self.server is not None:
            self.server.stop()
        flush_deskflow_sink()


class DeskflowListenerIngressTests(unittest.TestCase):
    def setUp(self):
        reset_deskflow_sink_for_tests()

    def tearDown(self):
        reset_deskflow_sink_for_tests()

    @patch.object(HidGestureListener, "_vendor_hid_infos", return_value=[])
    def test_transparent_connect_uses_main_listener(self, mock_infos):
        with _IngressFixture(mock_infos) as fx:
            hg = fx.hook._hid_gesture
            self.assertTrue(getattr(hg, "_deskflow_readonly", False))
            self.assertEqual(
                fx.hook._connected_device.source, DEVICE_SOURCE_DESKFLOW_SHIM
            )
            fx.client.send_frame(encode_report_frame(1, FRAME_GESTURE_DOWN))
            self.assertTrue(
                _wait_until(
                    lambda: ("dispatch", "gesture_down") in fx.hook.calls,
                    timeout=5.0,
                ),
                "gesture frame was not dispatched",
            )

    @patch.object(HidGestureListener, "_vendor_hid_infos", return_value=[])
    def test_deskflow_ingress_skips_kvm_scroll_invert(self, mock_infos):
        """KVM-forwarded scroll on the client must not be OS-inverted again."""
        with _IngressFixture(mock_infos) as fx:
            hook = fx.hook
            hook.invert_vscroll = True
            self.assertEqual(
                hook._connected_device.source, DEVICE_SOURCE_DESKFLOW_SHIM
            )
            self.assertFalse(hook._physical_logitech_bound())
            self.assertFalse(hook._apply_vscroll_invert_fallback(linux_evdev=True))

    def test_hidapi_source_would_invert_kvm_scroll(self):
        hook = _StubHook()
        hook.invert_vscroll = True
        hook._connected_device = SimpleNamespace(source="hidapi")
        self.assertTrue(hook._physical_logitech_bound())
        self.assertTrue(hook._apply_vscroll_invert_fallback(linux_evdev=True))


class _NeverReadyEvent:
    """A ready event no listener thread will ever set -- returns the timeout
    verdict immediately so the test does not sit through the real 5s wait."""

    def set(self):
        pass

    def wait(self, timeout=None):
        del timeout
        return False


def _instant_timeout():
    return patch("core.hid_gesture.threading.Event", _NeverReadyEvent)


class DeskflowAttachIdempotencyTests(unittest.TestCase):
    """Deskflow re-announces the device on a cadence.

    Every announcement used to force a reconnect, so the client's ingress
    session died every 3-6 seconds -- never long enough to hold a gesture
    across, which is how a swipe lost its motion mid-stroke.
    """

    def setUp(self):
        self.listener = HidGestureListener()
        # Stand in for a live read-only ingress session.
        self.listener._deskflow_readonly = True
        self.listener._connected = True

    def _attach(self, decode=None, product_id=0xB042, product_name="MX Master 4"):
        return self.listener.request_deskflow_attach(
            decode if decode is not None else dict(DECODE),
            product_id=product_id,
            product_name=product_name,
        )

    def test_repeat_of_a_live_attach_does_not_force_a_reconnect(self):
        self.listener._deskflow_attach = {
            "decode": dict(DECODE),
            "product_id": 0xB042,
            "product_name": "MX Master 4",
        }

        self.assertTrue(self._attach())

        self.assertFalse(self.listener._reconnect_requested)

    def test_a_changed_decode_still_reconnects(self):
        self.listener._deskflow_attach = {
            "decode": dict(DECODE),
            "product_id": 0xB042,
            "product_name": "MX Master 4",
        }
        moved = dict(DECODE)
        moved["feat_idx"] = 0x0D

        self._attach(decode=moved)

        self.assertTrue(self.listener._reconnect_requested)

    def test_a_changed_device_still_reconnects(self):
        self.listener._deskflow_attach = {
            "decode": dict(DECODE),
            "product_id": 0xB042,
            "product_name": "MX Master 4",
        }

        self._attach(product_id=0xB034, product_name="MX Master 3S")

        self.assertTrue(self.listener._reconnect_requested)

    def test_a_repeat_while_the_previous_attach_is_still_pending_falls_through(self):
        """Short-circuiting here would report success for an attach that never
        happened -- the ingress is not live yet."""
        self.listener._deskflow_readonly = False
        self.listener._deskflow_attach = {
            "decode": dict(DECODE),
            "product_id": 0xB042,
            "product_name": "MX Master 4",
        }

        # No listener thread is running, so the ready event never fires.
        with _instant_timeout():
            self.assertFalse(
                self.listener.request_deskflow_attach(
                    dict(DECODE), product_id=0xB042, product_name="MX Master 4"
                )
            )
        self.assertTrue(self.listener._reconnect_requested)

    def test_first_attach_is_never_short_circuited(self):
        self.assertIsNone(self.listener._deskflow_attach)

        with _instant_timeout():
            self.assertFalse(self._attach())

        self.assertTrue(self.listener._reconnect_requested)

    def test_a_non_dict_decode_is_refused(self):
        self.assertFalse(self.listener.request_deskflow_attach(None))


if __name__ == "__main__":
    unittest.main()
