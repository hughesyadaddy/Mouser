"""Tests for the proto-2 loopback bridge (core/bridge_server.py).

Everything here runs against ephemeral loopback sockets with a stub hook;
the HID listener is a mock so the assertions can count rebuilds directly.
"""

import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.bridge_server import (
    ATTACHED,
    HELLO,
    IDLE,
    PAUSED,
    PROTO,
    BridgeServer,
    ensure_token_file,
)
from core.hid_deskflow_backend import (
    flush_deskflow_sink,
    get_deskflow_sink,
    reset_deskflow_sink_for_tests,
)
from core.mouse_hook_base import BaseMouseHook

TOKEN = "0123456789abcdef0123456789abcdef"
LEGACY_TOKEN = "legacy-token"
DECODE = {"feat_idx": 11, "gesture_cid": "0x01A0", "rawxy": True}
DEVICE = {"product_id": "0xB042", "product_name": "MX Master 4"}


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _FakeListener:
    """Stands in for HidGestureListener: records the attach/pause API."""

    def __init__(self):
        self._deskflow_readonly = False
        self._connected = False
        self._deskflow_paused = False
        self.rebuilds = 0
        self.attach_requests = []
        self.clears = 0
        self.pauses = 0
        self.resumes = 0
        self.connected_device = None
        self.queue_decode_update = Mock(return_value=True)

    # HidGestureListener surface used by the bridge / hook
    def request_deskflow_attach(self, decode, product_id=None, product_name=None):
        self.attach_requests.append((dict(decode), product_id, product_name))
        if not (self._deskflow_readonly and self._connected):
            self._try_connect_deskflow({"decode": decode})
        return True

    def _try_connect_deskflow(self, attach):
        self.rebuilds += 1
        self._deskflow_readonly = True
        self._connected = True
        self._deskflow_paused = False
        return True

    def clear_deskflow_attach(self):
        self.clears += 1
        self._deskflow_readonly = False
        self._connected = False
        flush_deskflow_sink()

    def pause_deskflow_ingress(self):
        self.pauses += 1
        self._deskflow_paused = True
        flush_deskflow_sink()

    def resume_deskflow_ingress(self):
        self.resumes += 1
        self._deskflow_paused = False


class _StubHook(BaseMouseHook):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._hid_gesture = _FakeListener()

    def _dispatch(self, event):
        self.calls.append(("dispatch", event.event_type))

    def _accumulate_gesture_delta(self, dx, dy, source):
        self.calls.append(("accumulate", dx, dy, source))

    def _start_hid_listener(self):
        return self._hid_gesture

    def attach_deskflow_ingress(self, decode, product_id=None, product_name=None):
        ok = self._hid_gesture.request_deskflow_attach(
            decode, product_id=product_id, product_name=product_name
        )
        if ok:
            self._connected_device = SimpleNamespace(
                display_name=product_name, product_id=product_id
            )
            self._set_device_connected(True)
        return ok

    def detach_deskflow_ingress(self):
        self._hid_gesture.clear_deskflow_attach()
        self._connected_device = None
        self._set_device_connected(False)


class _Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.sock.settimeout(3)
        self._buffer = b""

    def send_raw(self, payload):
        self.sock.sendall(json.dumps(payload).encode() + b"\n")

    def send(self, payload) -> dict:
        self.send_raw(payload)
        return self.recv()

    def readline(self) -> bytes:
        # Own line buffer (not makefile): a timed-out BufferedReader is
        # poisoned for good, and _assert_silent relies on timing out.
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                return b""
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def recv(self) -> dict:
        line = self.readline()
        if not line:
            raise ConnectionError("peer closed")
        return json.loads(line)

    def recv_skip_pings(self) -> dict:
        while True:
            msg = self.recv()
            if msg.get("t") == "ping":
                self.send_raw({"t": "pong"})
                continue
            return msg

    def hello(self, token=TOKEN, proto=PROTO, role="client", **extra) -> dict:
        return self.send({
            "t": "hello", "proto": proto, "app": "deskflow-core", "ver": "2.0",
            "pid": 4242, "role": role, "caps": ["hidr"], "token": token, **extra,
        })

    def attach(self, session_id, device=DEVICE, decode=DECODE) -> dict:
        self.send_raw({"t": "attach", "session_id": session_id,
                       "device": device, "decode": decode})
        return self.recv_skip_pings()

    def close(self):
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR),
                   self.sock.close):
            try:
                fn()
            except OSError:
                pass


def _device(product_id=0xB042, name="MX Master 4"):
    """What ``hook.connected_device`` looks like to the bridge."""
    return SimpleNamespace(product_id=product_id, product_name=name,
                           display_name=name)


class _BridgeCase(unittest.TestCase):
    heartbeat_s = 5.0
    dead_link_s = 60.0
    hello_timeout_s = 3.0

    def setUp(self):
        reset_deskflow_sink_for_tests()
        self.hook = _StubHook()
        self.listener = self.hook._hid_gesture
        self.statuses = []
        self.proto2_seen = Mock()
        self.decode = {"feat_idx": 7, "gesture_cid": "0x00C3", "rawxy": True}
        self.device = None  # the physically attached mouse (server seat)
        self.server = BridgeServer(
            self.hook,
            port=0,
            token=TOKEN,
            legacy_token=LEGACY_TOKEN,
            status_cb=self.statuses.append,
            transparent_transport=True,
            decode_supplier=lambda: self.decode,
            device_supplier=lambda: self.device,
            on_proto2_seen=self.proto2_seen,
            heartbeat_s=self.heartbeat_s,
            dead_link_s=self.dead_link_s,
            hello_timeout_s=self.hello_timeout_s,
        )
        self.assertTrue(self.server.start())
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.stop()
        reset_deskflow_sink_for_tests()

    def client(self):
        client = _Client(self.server.port)
        self.clients.append(client)
        return client

    def attached_client(self, session_id="s1"):
        client = self.client()
        self.assertTrue(client.hello()["ok"])
        reply = client.attach(session_id)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.server.state, ATTACHED)
        return client

    def _assert_silent(self, client, timeout=0.3):
        """No line other than heartbeat pings within ``timeout``."""
        client.sock.settimeout(timeout)
        try:
            with self.assertRaises((socket.timeout, ConnectionError)):
                client.recv_skip_pings()
        finally:
            client.sock.settimeout(3)


class HelloTests(_BridgeCase):
    def test_hello_ok_reply_shape(self):
        client = self.client()
        reply = client.hello()
        self.assertEqual(reply, {"ok": True, "proto": 2,
                                 "caps": ["focus", "hidr", "decode"]})
        self.assertEqual(self.server.state, HELLO)
        self.proto2_seen.assert_called_once()

    def test_hello_wrong_proto_rejected(self):
        client = self.client()
        reply = client.hello(proto=3)
        self.assertEqual(reply, {"ok": False, "reason": "proto"})
        with self.assertRaises(ConnectionError):
            client.recv()
        self.proto2_seen.assert_not_called()
        self.assertEqual(self.server.state, IDLE)

    def test_hello_bad_token_rejected(self):
        client = self.client()
        reply = client.hello(token="nope")
        self.assertEqual(reply, {"ok": False, "reason": "auth"})
        self.proto2_seen.assert_not_called()

    def test_proto2_seen_fires_once(self):
        for _ in range(3):
            self.client().hello()
        self.proto2_seen.assert_called_once()

    def test_server_role_peer_gets_no_connect_without_a_device(self):
        # Deskflow drops a standalone {"type":"decode"} as unknown, so the
        # decode map only ever rides inside connect -- and there is nothing
        # to announce until a device is attached.
        client = self.client()
        self.assertTrue(client.hello(role="server")["ok"])
        self._assert_silent(client)

    def test_notify_decode_changed_resends_connect_to_server_peers_only(self):
        self.device = _device()
        server_peer = self.client()
        server_peer.hello(role="server")
        first = server_peer.recv_skip_pings()  # initial connect (hello replay)
        self.assertEqual(first["type"], "connect")
        self.assertEqual(first["device"]["decode"], self.decode)
        client_peer = self.client()
        client_peer.hello(role="client")
        self.decode = {"feat_idx": 9, "gesture_cid": "0x00C3", "rawxy": False}
        self.server.notify_decode_changed()
        msg = server_peer.recv_skip_pings()
        self.assertEqual(msg["type"], "connect")
        self.assertEqual(msg["device"]["decode"]["feat_idx"], 9)
        self.assertEqual(msg["device"]["product_id"], "0xB042")
        self._assert_silent(client_peer)
        # Unchanged decode: no duplicate line.
        self.server.notify_decode_changed()
        self._assert_silent(server_peer)


class StatusTests(_BridgeCase):
    def test_status_before_hello(self):
        client = self.client()
        reply = client.send({"t": "status"})
        self.assertEqual(reply["attached"], False)
        self.assertEqual(reply["proto"], 2)
        self.assertIn("peer", reply)
        self.assertIn("role", reply)
        self.assertIn("session_id", reply)

    def test_status_while_attached(self):
        self.attached_client("sess-42")
        probe = self.client()
        reply = probe.send({"t": "status"})
        self.assertEqual(reply["attached"], True)
        self.assertEqual(reply["peer"], "deskflow-core")
        self.assertEqual(reply["role"], "client")
        self.assertEqual(reply["session_id"], "sess-42")
        self.assertEqual(reply["proto"], 2)


class AttachTests(_BridgeCase):
    def test_attach_builds_ingress_once(self):
        client = self.attached_client()
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.server.attach_calls, 1)
        self.assertTrue(self.hook.device_connected)
        # Same id again: pure no-op.
        for _ in range(5):
            reply = client.attach("s1")
            self.assertTrue(reply["ok"])
            self.assertTrue(reply.get("resumed"))
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.server.attach_calls, 1)
        self.assertEqual(len(self.listener.attach_requests), 1)

    def test_new_session_id_on_live_ingress_resumes_not_rebuilds(self):
        client = self.attached_client("s1")
        client.send_raw({"t": "focus", "screen": "other", "here": False})
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        reply = client.attach("s2")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.server.state, ATTACHED)
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.listener.clears, 0)
        self.assertEqual(self.server.status()["session_id"], "s2")

    def test_attach_requires_session_id(self):
        client = self.client()
        client.hello()
        reply = client.send({"t": "attach"})
        self.assertEqual(reply, {"ok": False, "reason": "session_id"})
        self.assertEqual(self.server.state, HELLO)

    def test_attach_without_device_identity_fails_closed(self):
        client = self.client()
        client.hello()
        reply = client.attach("s1", device={}, decode=DECODE)
        self.assertFalse(reply["ok"])
        self.assertEqual(self.listener.rebuilds, 0)

    def test_legacy_connect_shape_on_proto2_attaches(self):
        client = self.client()
        client.hello()
        reply = client.send({"type": "connect",
                             "device": {**DEVICE, "decode": DECODE}})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["device_key"], "mx_master_4")
        self.assertEqual(self.server.state, ATTACHED)
        # legacy "disconnect" now pauses rather than tearing down
        self.assertEqual(client.send({"type": "disconnect"}), {"ok": True})
        self.assertEqual(self.server.state, PAUSED)
        self.assertEqual(self.listener.clears, 0)


class FocusTests(_BridgeCase):
    def test_hundred_focus_flips_zero_rebuilds(self):
        client = self.attached_client()
        with patch.object(self.listener, "_try_connect_deskflow",
                          wraps=self.listener._try_connect_deskflow) as connect:
            for i in range(100):
                client.send_raw({"t": "focus", "screen": "a" if i % 2 else "b",
                                 "here": bool(i % 2)})
            # Round-trip a status so every focus line has been processed.
            reply = client.send({"t": "status"})
            self.assertEqual(reply["attached"], True)
            connect.assert_not_called()
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.listener.clears, 0)
        self.assertEqual(self.server.clear_calls, 0)
        self.assertEqual(self.server.pause_calls, 50)
        self.assertEqual(self.server.resume_calls, 50)
        self.assertEqual(self.listener.pauses, 50)
        self.assertEqual(self.listener.resumes, 50)
        self.assertEqual(self.server.state, ATTACHED)
        self.assertTrue(self.hook.device_connected)

    def test_focus_away_drops_reports_focus_back_accepts(self):
        client = self.attached_client()
        sink = get_deskflow_sink()
        client.send_raw({"t": "focus", "screen": "other", "here": False})
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        reply = client.send({"type": "report", "data": "11ff0b00c30000"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply.get("paused"))
        self.assertIsNone(sink.read(64, timeout_ms=50))
        client.send_raw({"t": "focus", "screen": "here", "here": True})
        reply = client.send({"type": "report", "data": "11ff0b00c30000"})
        self.assertEqual(reply, {"ok": True})
        self.assertEqual(sink.read(64, timeout_ms=500),
                         bytes.fromhex("11ff0b00c30000"))

    def test_explicit_detach_pauses(self):
        client = self.attached_client()
        self.assertEqual(client.send({"t": "detach"}), {"ok": True})
        self.assertEqual(self.server.state, PAUSED)
        self.assertEqual(self.listener.clears, 0)

    def test_focus_before_attach_is_applied_after_attach(self):
        client = self.client()
        client.hello()
        client.send_raw({"t": "focus", "screen": "x", "here": False})
        client.send({"t": "status"})
        self.assertEqual(self.server.state, HELLO)  # nothing to pause yet
        reply = client.attach("s1")
        self.assertTrue(reply["ok"], reply)
        # The attach landed, then the remembered here:false paused it.
        self.assertEqual(self.server.state, PAUSED)
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.listener.pauses, 1)
        # And a later here:true resumes as usual.
        client.send_raw({"t": "focus", "screen": "here", "here": True})
        self.assertTrue(_wait_until(lambda: self.server.state == ATTACHED))

    def test_focus_here_before_attach_leaves_attach_live(self):
        client = self.client()
        client.hello()
        client.send_raw({"t": "focus", "screen": "here", "here": True})
        client.send({"t": "status"})
        self.assertTrue(client.attach("s1")["ok"])
        self.assertEqual(self.server.state, ATTACHED)
        self.assertEqual(self.listener.pauses, 0)


class ServerSeatTests(_BridgeCase):
    """Mouser-A: the mouse is physically here, Deskflow is ``role: server``.

    The bridge must announce the device (``connect``/``disconnect``, decode
    inside the connect) and stand the local hook down while the cursor is
    on another screen (``should_forward``)."""

    def _server_peer(self):
        client = self.client()
        self.assertTrue(client.hello(role="server")["ok"])
        return client

    def test_device_connect_is_sent_to_server_peers_with_decode(self):
        server_peer = self._server_peer()
        client_peer = self.client()
        client_peer.hello(role="client")
        self.server.notify_device_connected(_device())
        msg = server_peer.recv_skip_pings()
        self.assertEqual(msg, {"type": "connect", "device": {
            "product_name": "MX Master 4", "product_id": "0xB042",
            "decode": self.decode,
        }})
        self._assert_silent(client_peer)
        # No standalone decode line, ever.
        self.server.notify_decode_changed()
        self._assert_silent(server_peer)

    def test_connect_omits_decode_until_feat_idx_known(self):
        self.decode = None
        server_peer = self._server_peer()
        self.server.notify_device_connected(_device())
        msg = server_peer.recv_skip_pings()
        self.assertNotIn("decode", msg["device"])
        # feat_idx lands later -> full connect again, now carrying decode.
        self.decode = {"feat_idx": 3, "gesture_cid": "0x00C3", "rawxy": True}
        self.server.notify_decode_changed()
        msg = server_peer.recv_skip_pings()
        self.assertEqual(msg["type"], "connect")
        self.assertEqual(msg["device"]["decode"]["feat_idx"], 3)

    def test_connect_replayed_on_reconnect_and_role_flip(self):
        self.server.notify_device_connected(_device())
        first = self._server_peer()
        self.assertEqual(first.recv_skip_pings()["type"], "connect")
        first.close()
        # New server peer (Deskflow restarted): gets the cached connect.
        second = self._server_peer()
        msg = second.recv_skip_pings()
        self.assertEqual(msg["type"], "connect")
        self.assertEqual(msg["device"]["product_id"], "0xB042")
        # A client-role peer that becomes the server mid-link also gets it.
        third = self.client()
        third.hello(role="client")
        self._assert_silent(third)
        third.send_raw({"t": "role", "role": "server"})
        self.assertEqual(third.recv_skip_pings()["type"], "connect")
        # ...and again on the next epoch, even if nothing changed.
        third.send_raw({"t": "role", "role": "none"})
        third.send_raw({"t": "role", "role": "server"})
        self.assertEqual(third.recv_skip_pings()["type"], "connect")

    def test_disconnect_sent_on_device_removal(self):
        server_peer = self._server_peer()
        self.server.notify_device_connected(_device())
        server_peer.recv_skip_pings()
        self.server.notify_device_disconnected()
        self.assertEqual(server_peer.recv_skip_pings(), {"type": "disconnect"})
        # Nothing announced -> nothing to retract; no duplicate disconnect.
        self.server.notify_device_disconnected()
        self._assert_silent(server_peer)
        # A fresh peer after removal is told nothing until the mouse returns.
        late = self._server_peer()
        self._assert_silent(late)
        self.server.notify_device_connected(_device())
        self.assertEqual(late.recv_skip_pings()["type"], "connect")

    def test_should_forward_follows_server_peer_focus(self):
        flips = []
        self.server.on_focus_change = lambda: flips.append(self.server.should_forward())
        self.assertFalse(self.server.should_forward())
        server_peer = self._server_peer()
        server_peer.send_raw({"t": "focus", "screen": "office-pc", "here": False})
        self.assertTrue(_wait_until(self.server.should_forward))
        self.assertEqual(self.server.focus_screen, "office-pc")
        self.assertEqual(self.server.status()["remote_focus"], True)
        server_peer.send_raw({"t": "focus", "screen": "mac", "here": True})
        self.assertTrue(_wait_until(lambda: not self.server.should_forward()))
        self.assertIsNone(self.server.focus_screen)
        self.assertEqual(flips, [True, False])
        # send_event swallows: Deskflow relays the raw reports itself.
        self.assertTrue(self.server.send_event("gesture_down"))

    def test_client_peer_focus_does_not_flip_should_forward(self):
        client = self.attached_client()
        client.send_raw({"t": "focus", "screen": "other", "here": False})
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        self.assertFalse(self.server.should_forward())

    def test_should_forward_fails_safe_when_server_peer_drops(self):
        server_peer = self._server_peer()
        server_peer.send_raw({"t": "focus", "screen": "x", "here": False})
        self.assertTrue(_wait_until(self.server.should_forward))
        server_peer.close()
        self.assertTrue(_wait_until(lambda: not self.server.should_forward()))

    def test_should_forward_fails_safe_when_role_leaves_server(self):
        server_peer = self._server_peer()
        server_peer.send_raw({"t": "focus", "screen": "x", "here": False})
        self.assertTrue(_wait_until(self.server.should_forward))
        server_peer.send_raw({"t": "role", "role": "none"})
        self.assertTrue(_wait_until(lambda: not self.server.should_forward()))
        # Re-elected server: the last focus notice counts again.
        server_peer.send_raw({"t": "role", "role": "server"})
        self.assertTrue(_wait_until(self.server.should_forward))


class HeartbeatTests(_BridgeCase):
    heartbeat_s = 0.05

    def test_ping_answered_with_pong(self):
        client = self.client()
        client.hello()
        client.send_raw({"t": "ping"})
        msg = client.recv()
        while msg.get("t") == "ping":
            msg = client.recv()
        self.assertEqual(msg, {"t": "pong"})

    def test_server_pings_peer(self):
        client = self.client()
        client.hello()
        self.assertEqual(client.recv(), {"t": "ping"})

    def test_heartbeat_expiry_pauses_not_rebuilds(self):
        client = self.attached_client()
        # Stop answering: 3 missed beats -> link dropped -> PAUSED.
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED, 3.0))
        with self.assertRaises((ConnectionError, OSError)):
            for _ in range(200):
                client.recv()
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.listener.clears, 0)
        self.assertEqual(self.server.clear_calls, 0)
        self.assertEqual(self.listener.pauses, 1)
        # Reconnect + same session: resumes on the existing listener.
        client2 = self.client()
        client2.hello()
        reply = client2.attach("s1")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.server.state, ATTACHED)
        self.assertEqual(self.listener.rebuilds, 1)


class DeadLinkTests(_BridgeCase):
    dead_link_s = 0.2

    def test_socket_drop_pauses_then_dead_link_clears(self):
        client = self.attached_client()
        client.close()
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        self.assertEqual(self.listener.clears, 0)
        self.assertTrue(_wait_until(lambda: self.server.state == IDLE, 2.0))
        self.assertEqual(self.listener.clears, 1)
        self.assertEqual(self.server.clear_calls, 1)
        self.assertFalse(self.hook.device_connected)

    def test_reattach_before_dead_link_cancels_clear(self):
        client = self.attached_client()
        client.close()
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        client2 = self.client()
        client2.hello()
        self.assertTrue(client2.attach("s1")["ok"])
        time.sleep(0.4)
        self.assertEqual(self.server.state, ATTACHED)
        self.assertEqual(self.listener.clears, 0)


class ByeTests(_BridgeCase):
    def test_stop_sends_bye_shutdown_to_every_peer(self):
        client = self.attached_client()
        server_peer = self.client()
        server_peer.hello(role="server")
        self.server.stop()
        self.assertEqual(client.recv_skip_pings(), {"t": "bye", "reason": "shutdown"})
        self.assertEqual(server_peer.recv_skip_pings(), {"t": "bye", "reason": "shutdown"})
        with self.assertRaises(ConnectionError):
            client.recv_skip_pings()
        self.assertEqual(self.server.state, IDLE)
        self.assertFalse(self.server.should_forward())

    def test_stop_restart_reason_is_relayed(self):
        client = self.client()
        client.hello()
        self.server.stop(reason="restart")
        self.assertEqual(client.recv_skip_pings(), {"t": "bye", "reason": "restart"})

    def test_bye_shutdown_clears(self):
        client = self.attached_client()
        client.send_raw({"t": "bye", "reason": "shutdown"})
        self.assertTrue(_wait_until(lambda: self.server.state == IDLE))
        self.assertEqual(self.listener.clears, 1)
        self.assertGreater(self.server._quiet_until, 0)

    def test_bye_other_reason_pauses(self):
        client = self.attached_client()
        client.send_raw({"t": "bye", "reason": "restart"})
        self.assertTrue(_wait_until(lambda: self.server.state == PAUSED))
        self.assertEqual(self.listener.clears, 0)


class LegacyPeerTests(_BridgeCase):
    hello_timeout_s = 0.2

    def test_legacy_hello_served_with_v1_semantics(self):
        client = self.client()
        reply = client.send({"type": "hello", "token": LEGACY_TOKEN, "version": 1})
        self.assertEqual(reply, {"ok": True, "server": "mouser", "version": 1})
        reply = client.send({"type": "connect",
                             "device": {**DEVICE, "decode": DECODE}})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.listener.rebuilds, 1)
        self.assertEqual(self.server.state, IDLE)  # proto-2 machine untouched
        self.assertTrue(self.server.status()["legacy"])
        # v1: dropping the socket tears the ingress down (old behaviour).
        client.close()
        self.assertTrue(_wait_until(lambda: self.listener.clears == 1))
        self.proto2_seen.assert_not_called()

    def test_legacy_wrong_token_rejected(self):
        client = self.client()
        reply = client.send({"type": "hello", "token": "bad", "version": 1})
        self.assertEqual(reply, {"ok": False, "error": "unauthorized"})

    def test_silent_peer_falls_back_to_legacy_after_timeout(self):
        client = self.client()
        time.sleep(0.4)  # past hello_timeout_s without a line
        reply = client.send({"type": "hello", "token": LEGACY_TOKEN, "version": 1})
        self.assertEqual(reply["version"], 1)

    def test_legacy_peer_rejected_while_proto2_attached(self):
        self.attached_client()
        legacy = self.client()
        reply = legacy.send({"type": "hello", "token": LEGACY_TOKEN, "version": 1})
        self.assertEqual(reply, {"ok": False, "error": "busy"})
        self.assertEqual(self.server.state, ATTACHED)


class ReportPlaneTests(_BridgeCase):
    def test_binary_dfhr_frame_reaches_sink(self):
        from core.hid_sink import encode_report_frame

        client = self.attached_client()
        payload = bytes.fromhex("11ff0b00c30000")
        client.sock.sendall(encode_report_frame(1, payload))
        sink = get_deskflow_sink()
        self.assertEqual(sink.read(64, timeout_ms=1000), payload)

    def test_update_decode_routes_to_listener(self):
        client = self.attached_client()
        reply = client.send({"type": "update_decode", "decode": DECODE})
        self.assertEqual(reply, {"ok": True})
        self.listener.queue_decode_update.assert_called_once()


class TokenFileTests(unittest.TestCase):
    def test_token_file_created_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "bridge.token")
            token = ensure_token_file(path)
            self.assertRegex(token, r"^[0-9a-f]{32}$")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), token)
            if sys.platform != "win32":
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode, 0o600)
            # Reused on the next start, and re-tightened.
            os.chmod(path, 0o644)
            self.assertEqual(ensure_token_file(path), token)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_garbage_token_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bridge.token")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not a token\n")
            token = ensure_token_file(path)
            self.assertRegex(token, r"^[0-9a-f]{32}$")

    def test_server_start_writes_token_file(self):
        reset_deskflow_sink_for_tests()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bridge.token")
            server = BridgeServer(_StubHook(), port=0, token_file=path)
            self.assertTrue(server.start())
            try:
                self.assertTrue(os.path.isfile(path))
                self.assertEqual(server.token, open(path).read().strip())
                client = _Client(server.port)
                self.assertTrue(client.hello(token=server.token)["ok"])
                client.close()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
