"""Tests for the remote virtual-device server (core/remote_device.py)."""

import json
import socket
import time
import unittest

from core.mouse_hook_base import BaseMouseHook
from core.remote_device import PROTOCOL_VERSION, RemoteDeviceServer

TOKEN = "test-token-123"


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
        # shutdown() forces the FIN out even though the makefile reader
        # still holds a reference to the underlying fd.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.reader.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RemoteDeviceServerTests(unittest.TestCase):
    def setUp(self):
        self.hook = _StubHook()
        self.statuses = []
        self.server = RemoteDeviceServer(
            self.hook,
            token=TOKEN,
            port=0,  # ephemeral
            status_cb=self.statuses.append,
        )
        self.assertTrue(self.server.start())
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()
        self.server.stop()

    def _authed_client(self) -> _Client:
        self.client = _Client(self.server.port)
        resp = self.client.hello()
        self.assertTrue(resp["ok"])
        return self.client

    def _connect_device(self, client, **device) -> dict:
        device = device or {"product_id": "0xB034"}
        return client.send({"type": "connect", "device": device})

    # ── lifecycle / security ──────────────────────────────────────

    def test_start_refuses_without_token(self):
        server = RemoteDeviceServer(_StubHook(), token="", port=0)
        self.assertFalse(server.start())

    def test_binds_loopback_only(self):
        self.assertEqual(self.server._listener.getsockname()[0], "127.0.0.1")

    def test_rejects_wrong_token(self):
        self.client = _Client(self.server.port)
        resp = self.client.hello(token="wrong")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "unauthorized")
        # Connection must be closed: next read returns EOF.
        self.assertEqual(self.client.reader.readline(), b"")

    def test_rejects_event_before_hello(self):
        self.client = _Client(self.server.port)
        resp = self.client.send({"type": "event", "name": "gesture_down"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "unauthorized")

    # ── connect / disconnect ──────────────────────────────────────

    def test_connect_builds_known_device(self):
        client = self._authed_client()
        resp = self._connect_device(client)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["device_key"], "mx_master_3s")
        self.assertTrue(self.hook.device_connected)
        device = self.hook.connected_device
        self.assertEqual(device.display_name, "MX Master 3S")
        self.assertEqual(device.transport, "remote")
        self.assertEqual(device.source, "remote-virtual")

    def test_connect_unknown_device_uses_generic_fallback(self):
        client = self._authed_client()
        resp = client.send({
            "type": "connect",
            "device": {"product_name": "Mystery Logitech Mouse"},
        })

        self.assertTrue(resp["ok"])
        self.assertEqual(self.hook.connected_device.ui_layout, "generic_mouse")

    def test_connect_requires_identity(self):
        client = self._authed_client()
        resp = client.send({"type": "connect", "device": {}})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "missing_device_identity")
        self.assertFalse(self.hook.device_connected)

    def test_physical_device_wins_over_remote_connect(self):
        self.hook._connected_device = object()  # physical sentinel
        client = self._authed_client()
        resp = self._connect_device(client)

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "physical_device_present")

    def test_explicit_disconnect_clears_device(self):
        client = self._authed_client()
        self._connect_device(client)
        resp = client.send({"type": "disconnect"})

        self.assertTrue(resp["ok"])
        self.assertFalse(self.hook.device_connected)
        self.assertIsNone(self.hook.connected_device)

    def test_client_drop_disconnects_virtual_device(self):
        client = self._authed_client()
        self._connect_device(client)
        self.assertTrue(self.hook.device_connected)

        client.close()

        self.assertTrue(
            _wait_until(lambda: not self.hook.device_connected),
            "virtual device must auto-disconnect when the client vanishes",
        )

    def test_server_stop_disconnects_virtual_device(self):
        client = self._authed_client()
        self._connect_device(client)
        self.server.stop()
        self.assertFalse(self.hook.device_connected)

    # ── events ────────────────────────────────────────────────────

    def test_gesture_events_reach_hook_pipeline(self):
        client = self._authed_client()
        self._connect_device(client)

        self.assertTrue(client.send({"type": "event", "name": "gesture_down"})["ok"])
        self.assertTrue(client.send(
            {"type": "event", "name": "gesture_move", "dx": 12, "dy": -3}
        )["ok"])
        self.assertTrue(client.send({"type": "event", "name": "gesture_up"})["ok"])

        self.assertIn(("dispatch", "gesture_down"), self.hook.calls)
        self.assertIn(("accumulate", 12.0, -3.0, "hid_rawxy"), self.hook.calls)
        self.assertIn(("dispatch", "gesture_up"), self.hook.calls)

    def test_button_events_reach_hook_pipeline(self):
        client = self._authed_client()
        self._connect_device(client)

        for name in ("thumb_button_down", "thumb_button_up",
                     "mode_shift_down", "mode_shift_up",
                     "dpi_switch_down", "dpi_switch_up"):
            with self.subTest(event=name):
                self.assertTrue(client.send({"type": "event", "name": name})["ok"])
                self.assertIn(("dispatch", name), self.hook.calls)

    def test_event_rejected_before_connect(self):
        client = self._authed_client()
        resp = client.send({"type": "event", "name": "gesture_down"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "not_connected")
        self.assertNotIn(("dispatch", "gesture_down"), self.hook.calls)

    def test_event_rejected_after_physical_displacement(self):
        client = self._authed_client()
        self._connect_device(client)
        # A real device connecting overwrites the slot (physical wins).
        self.hook._connected_device = object()

        resp = client.send({"type": "event", "name": "gesture_down"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "not_connected")

    def test_unknown_event_rejected(self):
        client = self._authed_client()
        self._connect_device(client)
        resp = client.send({"type": "event", "name": "execute_arbitrary_keys"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "unknown_event")

    def test_malformed_deltas_rejected(self):
        client = self._authed_client()
        self._connect_device(client)
        resp = client.send(
            {"type": "event", "name": "gesture_move", "dx": "lots", "dy": 1}
        )

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "malformed_deltas")

    def test_unknown_message_type_rejected(self):
        client = self._authed_client()
        resp = client.send({"type": "teleport"})

        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "unknown_message_type")


if __name__ == "__main__":
    unittest.main()
