"""Tests for raw HID++ report frames over the remote-device socket.

The sender (a Deskflow HID pass-through host) seizes the physical device
and ships its input reports verbatim as {"type": "report", "data": hex};
the receiving Mouser decodes them with a detached HidGestureListener
seeded from the connect message's decode context (or the local
settings.remote_device.decode override).
"""

import json
import socket
import time
import unittest

from core.mouse_hook_base import BaseMouseHook
from core.remote_device import PROTOCOL_VERSION, RemoteDeviceServer

TOKEN = "test-token-123"

# Decode context for the synthetic device below: REPROG_V4 at feature
# index 0x0B, Sense-Panel-style gesture CID 0x01A0, thumb button 0x00C4.
DECODE = {
    "feat_idx": 0x0B,
    "gesture_cid": "0x01A0",
    "extra_diverts": {"0x00C4": "thumb_button"},
}

# HID++ long reports (0x11), device index 0xFF, feature index 0x0B.
# func 0 = diverted-buttons CID list; func 1 = rawXY deltas (while held).
FRAME_GESTURE_DOWN = "11ff0b0001a00000"
FRAME_MOVE_12_M3 = "11ff0b10000cfffd"  # dx=12, dy=-3
FRAME_ALL_UP = "11ff0b000000"
FRAME_THUMB_DOWN = "11ff0b0000c40000"
FRAME_OTHER_FEATURE = "11ff0c0001a00000"  # feature 0x0C: not ours


class _StubHook(BaseMouseHook):
    """Real BaseMouseHook semantics with recorded dispatch/accumulate."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def _dispatch(self, event):
        self.calls.append(("dispatch", event.event_type))

    def _accumulate_gesture_delta(self, dx, dy, source):
        self.calls.append(("accumulate", dx, dy, source))


class _Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.settimeout(2)
        self.reader = self.sock.makefile("rb")

    def send(self, payload) -> dict:
        self.sock.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(self.reader.readline())

    def hello(self, token=TOKEN) -> dict:
        return self.send({"type": "hello", "token": token,
                          "version": PROTOCOL_VERSION})

    def close(self):
        for closer in (lambda: self.sock.shutdown(socket.SHUT_RDWR),
                       self.reader.close, self.sock.close):
            try:
                closer()
            except OSError:
                pass


class RemoteRawFrameTests(unittest.TestCase):
    def setUp(self):
        self.hook = _StubHook()
        self.server = None
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()
        if self.server is not None:
            self.server.stop()

    def _start(self, **server_kwargs):
        self.server = RemoteDeviceServer(
            self.hook, token=TOKEN, port=0, **server_kwargs
        )
        self.assertTrue(self.server.start())
        self.client = _Client(self.server.port)
        self.assertTrue(self.client.hello()["ok"])
        return self.client

    def _connect(self, client, device=None) -> dict:
        device = device or {"product_id": "0xB042",
                            "product_name": "MX Master 4"}
        return client.send({"type": "connect", "device": device})

    def test_raw_frames_decode_gesture_sequence(self):
        client = self._start()
        device = {"product_id": "0xB042", "decode": DECODE}
        self.assertTrue(self._connect(client, device)["ok"])

        for frame in (FRAME_GESTURE_DOWN, FRAME_MOVE_12_M3, FRAME_ALL_UP):
            self.assertTrue(
                client.send({"type": "report", "data": frame})["ok"]
            )

        self.assertIn(("dispatch", "gesture_down"), self.hook.calls)
        self.assertIn(("accumulate", 12.0, -3.0, "hid_rawxy"),
                      self.hook.calls)
        self.assertIn(("dispatch", "gesture_up"), self.hook.calls)

    def test_extra_divert_decodes_thumb_button(self):
        client = self._start()
        self.assertTrue(
            self._connect(client, {"product_id": "0xB042",
                                   "decode": DECODE})["ok"]
        )

        self.assertTrue(
            client.send({"type": "report", "data": FRAME_THUMB_DOWN})["ok"]
        )
        self.assertTrue(
            client.send({"type": "report", "data": FRAME_ALL_UP})["ok"]
        )

        self.assertIn(("dispatch", "thumb_button_down"), self.hook.calls)
        self.assertIn(("dispatch", "thumb_button_up"), self.hook.calls)

    def test_other_features_frames_are_ignored(self):
        client = self._start()
        self.assertTrue(
            self._connect(client, {"product_id": "0xB042",
                                   "decode": DECODE})["ok"]
        )

        self.assertTrue(
            client.send({"type": "report", "data": FRAME_OTHER_FEATURE})["ok"]
        )
        self.assertEqual(self.hook.calls, [])

    def test_decode_override_applies_when_connect_has_none(self):
        client = self._start(decode_override=DECODE)
        self.assertTrue(self._connect(client)["ok"])  # no decode in connect

        self.assertTrue(
            client.send({"type": "report", "data": FRAME_GESTURE_DOWN})["ok"]
        )
        self.assertIn(("dispatch", "gesture_down"), self.hook.calls)

    def test_update_decode_enables_late_raw_frames(self):
        client = self._start()
        self.assertTrue(
            self._connect(client, {"product_id": "0xB042"})["ok"]
        )
        self.assertFalse(
            client.send({"type": "report", "data": FRAME_GESTURE_DOWN})["ok"]
        )

        self.assertTrue(
            client.send({"type": "update_decode", "decode": DECODE})["ok"]
        )
        self.assertTrue(
            client.send({"type": "report", "data": FRAME_GESTURE_DOWN})["ok"]
        )
        self.assertIn(("dispatch", "gesture_down"), self.hook.calls)

    def test_rejected_without_decode_context(self):
        client = self._start()
        self.assertTrue(self._connect(client)["ok"])

        resp = client.send({"type": "report", "data": FRAME_GESTURE_DOWN})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "no_decode_context")
        self.assertEqual(self.hook.calls, [])

    def test_rejected_before_connect(self):
        client = self._start()
        resp = client.send({"type": "report", "data": FRAME_GESTURE_DOWN})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "not_connected")

    def test_malformed_hex_rejected(self):
        client = self._start(decode_override=DECODE)
        self.assertTrue(self._connect(client)["ok"])

        resp = client.send({"type": "report", "data": "not-hex!"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "malformed_hex")

    def test_decoder_cleared_on_disconnect(self):
        client = self._start(decode_override=DECODE)
        self.assertTrue(self._connect(client)["ok"])
        self.assertTrue(client.send({"type": "disconnect"})["ok"])

        resp = client.send({"type": "report", "data": FRAME_GESTURE_DOWN})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "not_connected")


if __name__ == "__main__":
    unittest.main()
