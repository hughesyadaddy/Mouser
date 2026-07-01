"""End-to-end loopback simulation of the cross-machine gesture path.

machine A (mouse attached)                machine B (KVM focus target)
  StubHookA + RemoteForwarder  ->  bridge relay  ->  RemoteDeviceServer + StubHookB

The bridge here is a minimal stand-in for the Deskflow fork: it accepts the
forwarder, answers hello, pushes a focus notification, and relays connect /
event / disconnect lines verbatim to machine B's RemoteDeviceServer after
authenticating with B's token -- exactly what the fork's server+client pair
will do across the network.
"""

import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace

from core.mouse_hook_base import BaseMouseHook
from core.remote_device import RemoteDeviceServer
from core.remote_forward import RemoteForwarder

A_TOKEN = "token-bridge-a"
B_TOKEN = "token-mouser-b"


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _StubHookA(BaseMouseHook):
    """Sender hook: records anything that (wrongly) runs locally."""

    def __init__(self):
        super().__init__()
        self.local_calls = []

    def _on_hid_gesture_down(self):
        self.local_calls.append("gesture_down")

    def _on_hid_gesture_up(self):
        self.local_calls.append("gesture_up")

    def _on_hid_gesture_move(self, dx, dy):
        self.local_calls.append(("gesture_move", dx, dy))


class _StubHookB(BaseMouseHook):
    """Receiver hook: records the pipeline entry points the server drives."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def _dispatch(self, event):
        self.calls.append(("dispatch", event.event_type))

    def _accumulate_gesture_delta(self, dx, dy, source):
        self.calls.append(("accumulate", dx, dy, source))


class _BridgeRelay:
    """Deskflow-fork stand-in: forwarder in, RemoteDeviceServer out."""

    def __init__(self, mouser_b_port):
        self._b_port = mouser_b_port
        self._b_sock = None
        self._conn = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.b_replies = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _connect_b(self):
        self._b_sock = socket.create_connection(("127.0.0.1", self._b_port), timeout=2)
        self._b_sock.settimeout(2)
        self._b_reader = self._b_sock.makefile("rb")
        self._b_sock.sendall(json.dumps(
            {"type": "hello", "token": B_TOKEN, "version": 1}
        ).encode() + b"\n")
        reply = json.loads(self._b_reader.readline())
        assert reply["ok"], reply

    def _serve(self):
        try:
            conn, _ = self._listener.accept()
            self._conn = conn
            reader = conn.makefile("rb")
            hello = json.loads(reader.readline())
            assert hello.get("token") == A_TOKEN
            conn.sendall(b'{"ok": true}\n')
            self._connect_b()
            for line in reader:
                # Verbatim relay to machine B's Mouser, recording replies.
                self._b_sock.sendall(line)
                self.b_replies.append(json.loads(self._b_reader.readline()))
        except OSError:
            pass

    def push_focus(self, screen, local):
        self._conn.sendall(json.dumps(
            {"type": "focus", "screen": screen, "local": local}
        ).encode() + b"\n")

    def close(self):
        for sock in (self._conn, self._b_sock, self._listener):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


class RemoteEndToEndTests(unittest.TestCase):
    def setUp(self):
        # Machine B: receiving Mouser.
        self.hook_b = _StubHookB()
        self.server_b = RemoteDeviceServer(self.hook_b, token=B_TOKEN, port=0)
        self.assertTrue(self.server_b.start())

        # Bridge relay (Deskflow stand-in).
        self.bridge = _BridgeRelay(self.server_b.port)

        # Machine A: sending Mouser.
        self.hook_a = _StubHookA()
        self.device = SimpleNamespace(
            product_id=0xB042,
            product_name="MX Master 4",
            display_name="MX Master 4",
        )
        self.forwarder = RemoteForwarder(
            token=A_TOKEN,
            port=self.bridge.port,
            device_supplier=lambda: self.device,
        )
        self.hook_a.set_remote_forwarder(self.forwarder)
        self.assertTrue(self.forwarder.start())

        # Wait until A's device announcement has been relayed into B.
        self.assertTrue(
            _wait_until(lambda: self.hook_b.device_connected),
            "device announcement must propagate A -> bridge -> B",
        )

    def tearDown(self):
        self.forwarder.stop()
        self.server_b.stop()
        self.bridge.close()

    def test_full_gesture_round_trip(self):
        # B sees the exact device A described.
        device_b = self.hook_b.connected_device
        self.assertEqual(device_b.key, "mx_master_4")
        self.assertEqual(device_b.transport, "remote")

        # Focus moves to B; A starts forwarding.
        self.bridge.push_focus("machine-b", local=False)
        self.assertTrue(_wait_until(self.forwarder.should_forward))

        # A's HID listener fires a full gesture through the wrapped entries.
        down = self.hook_a._hid_event_entry(
            "gesture_down", self.hook_a._on_hid_gesture_down)
        move = self.hook_a._hid_event_entry(
            "gesture_move", self.hook_a._on_hid_gesture_move)
        up = self.hook_a._hid_event_entry(
            "gesture_up", self.hook_a._on_hid_gesture_up)
        down()
        move(64, -5)
        up()

        # Nothing ran locally on A...
        self.assertEqual(self.hook_a.local_calls, [])
        # ...and A's OS-level intercept gate stands down while remote.
        self.hook_a._connected_device = self.device
        self.assertFalse(self.hook_a._should_intercept_events())

        # The full gesture arrived in B's pipeline.
        self.assertTrue(_wait_until(
            lambda: ("dispatch", "gesture_up") in self.hook_b.calls
        ))
        self.assertIn(("dispatch", "gesture_down"), self.hook_b.calls)
        self.assertIn(("accumulate", 64.0, -5.0, "hid_rawxy"), self.hook_b.calls)
        # Every relayed line was accepted by B's server.
        self.assertTrue(all(reply["ok"] for reply in self.bridge.b_replies))

    def test_focus_back_to_local_restores_a(self):
        self.bridge.push_focus("machine-b", local=False)
        self.assertTrue(_wait_until(self.forwarder.should_forward))

        self.bridge.push_focus("machine-a", local=True)
        self.assertTrue(_wait_until(lambda: not self.forwarder.should_forward()))

        down = self.hook_a._hid_event_entry(
            "gesture_down", self.hook_a._on_hid_gesture_down)
        down()

        self.assertEqual(self.hook_a.local_calls, ["gesture_down"])
        self.hook_a._connected_device = self.device
        self.assertTrue(self.hook_a._should_intercept_events())


if __name__ == "__main__":
    unittest.main()
