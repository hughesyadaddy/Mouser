"""Tests for binary DFHR reports on the remote-device loopback socket."""

import json
import socket
import time
import unittest

from core.hid_sink import encode_report_frame
from core.mouse_hook_base import BaseMouseHook
from core.remote_device import PROTOCOL_VERSION, RemoteDeviceServer

TOKEN = "binary-token"
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


class RemoteBinaryFrameTests(unittest.TestCase):
    def test_binary_report_after_connect(self):
        hook = _StubHook()
        server = RemoteDeviceServer(hook, token=TOKEN, port=0)
        self.assertTrue(server.start())
        port = server.port
        client = _Client(port)
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
        client.send_frame(encode_report_frame(1, FRAME_GESTURE_DOWN))
        for _ in range(40):
            if any(call[1] == "gesture_down" for call in hook.calls):
                break
            time.sleep(0.05)
        self.assertTrue(any(call[1] == "gesture_down" for call in hook.calls))
        client.close()
        server.stop()


if __name__ == "__main__":
    unittest.main()
