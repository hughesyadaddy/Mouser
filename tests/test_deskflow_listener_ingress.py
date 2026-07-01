"""Deskflow ingress routes through the main HidGestureListener read loop."""

import json
import socket
import time
import unittest
from unittest.mock import patch

from core.hid_gesture import HidGestureListener
from core.hid_sink import encode_report_frame
from core.mouse_hook_base import BaseMouseHook
from core.remote_device import PROTOCOL_VERSION, RemoteDeviceServer

TOKEN = "listener-ingress-token"
DECODE = {
    "feat_idx": 0x0B,
    "gesture_cid": "0x01A0",
    "extra_diverts": {"0x00C4": "thumb_button"},
}
FRAME_GESTURE_DOWN = bytes.fromhex("11ff0b0001a00000")


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


class DeskflowListenerIngressTests(unittest.TestCase):
    @patch.object(HidGestureListener, "_vendor_hid_infos", return_value=[])
    def test_transparent_connect_uses_main_listener(self, _mock_infos):
        hook = _StubHook()
        hook.start()
        server = RemoteDeviceServer(
            hook,
            token=TOKEN,
            port=0,
            transparent_transport=True,
        )
        self.assertTrue(server.start())
        client = _Client(server.port)
        self.assertTrue(client.hello().get("ok"))
        reply = client.send_json(
            {
                "type": "connect",
                "device": {
                    "product_id": "0xB042",
                    "product_name": "MX Master 4",
                    "decode": DECODE,
                },
            }
        )
        self.assertTrue(reply.get("ok"))
        for _ in range(40):
            hg = hook._hid_gesture
            if hg is not None and hg.connected_device is not None:
                break
            time.sleep(0.05)
        hg = hook._hid_gesture
        self.assertIsNotNone(hg)
        self.assertTrue(getattr(hg, "_deskflow_readonly", False))
        self.assertEqual(hg.connected_device.source, "hidapi")
        client.send_frame(encode_report_frame(1, FRAME_GESTURE_DOWN))
        time.sleep(0.15)
        self.assertIn(("dispatch", "gesture_down"), hook.calls)
        client.send_json({"type": "disconnect"})
        client.close()
        server.stop()


if __name__ == "__main__":
    unittest.main()
