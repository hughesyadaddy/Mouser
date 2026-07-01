"""
Remote event forwarder (the machine the mouse is physically attached to).

Counterpart of ``core/remote_device.py``. Connects to a local "bridge"
listener (e.g. a Deskflow-fork server) over loopback TCP and:

  - receives focus notifications telling Mouser whether the KVM cursor is
    currently on this machine or a remote one;
  - announces the connected Logitech device's identity (so the bridge can
    virtually connect it on whichever machine has focus);
  - forwards HID++-only events (gesture button, rawXY, thumb button, ...)
    while focus is remote, during which local handling is suppressed.

Wire protocol (line-delimited JSON, mirrors core/remote_device.py):

    -> {"type": "hello", "token": "<token>", "version": 1, "role": "source"}
    <- {"ok": true, ...}
    <- {"type": "focus", "screen": "office-pc", "local": false}
    -> {"type": "connect", "device": {"product_id": "0xB042",
                                      "product_name": "MX Master 4"}}
    -> {"type": "event", "name": "gesture_down"}
    -> {"type": "disconnect"}

Fail-safe posture: if the bridge is unreachable or the connection drops,
``should_forward()`` is False and Mouser behaves exactly as if this module
did not exist -- events are handled locally, nothing is suppressed.
"""

from __future__ import annotations

import json
import socket
import threading

PROTOCOL_VERSION = 1
DEFAULT_BRIDGE_PORT = 19796
_MAX_LINE_BYTES = 64 * 1024
_RECONNECT_DELAYS_S = (1.0, 2.0, 5.0)


def _device_payload(device) -> dict:
    product_id = getattr(device, "product_id", None)
    payload = {
        "product_name": getattr(device, "product_name", None)
        or getattr(device, "display_name", None),
    }
    if product_id is not None:
        payload["product_id"] = f"0x{int(product_id):04X}"
    return payload


class RemoteForwarder:
    """Bridge client: focus tracking + device announcement + event relay."""

    def __init__(self, *, token, host="127.0.0.1", port=DEFAULT_BRIDGE_PORT,
                 device_supplier=None, decode_supplier=None, status_cb=None,
                 decode_only=False):
        """``device_supplier`` is a zero-arg callable returning the currently
        connected device (or None); used to announce the device on every
        (re)connect without holding a stale reference. ``decode_supplier`` is
        a zero-arg callable returning the live gesture decode map (or None),
        shipped inside ``connect`` so a slave can replay forwarded raw frames
        without a manual ``settings.remote_device.decode`` override.

        When ``decode_only`` is True (HID passthrough on the host), the
        forwarder publishes decode updates only and never relays events or
        suppresses local handling -- Deskflow owns the raw byte pipe."""
        self._token = str(token or "")
        self._host = host
        self._port = int(port)
        self._device_supplier = device_supplier or (lambda: None)
        self._decode_supplier = decode_supplier or (lambda: None)
        self._status_cb = status_cb
        self._decode_only = bool(decode_only)
        self._last_sent_decode = None
        self._sock = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False
        self._remote_focus = False
        self._focus_screen = None
        self._stopped = threading.Event()
        self._thread = None

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> bool:
        if not self._token:
            self._emit_status("Remote forwarder not started: no token configured")
            return False
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RemoteForwarder"
        )
        self._thread.start()
        return True

    def stop(self):
        self._stopped.set()
        # Only shut the socket down here: closing the makefile reader from
        # this thread while the session thread is blocked in readline()
        # deadlocks on the reader's internal buffer lock. The shutdown
        # unblocks that thread, and it closes its own reader on exit.
        self._shutdown_socket()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ── public state ──────────────────────────────────────────────

    def should_forward(self) -> bool:
        """True only while the bridge is connected AND focus is remote.
        Both flip to False the moment the connection drops, so a dead
        bridge can never suppress local handling.

        Always False in decode-only mode: Deskflow HID passthrough seizes
        the vendor interface when focus is remote."""
        if self._decode_only:
            return False
        with self._state_lock:
            return self._connected and self._remote_focus

    @property
    def focus_screen(self):
        with self._state_lock:
            return self._focus_screen

    @property
    def decode_published(self) -> bool:
        """True once a decode map has been sent on the current bridge session."""
        return self._last_sent_decode is not None

    # ── senders (any thread) ──────────────────────────────────────

    def send_event(self, name, **payload):
        if self._decode_only:
            return
        self._send({"type": "event", "name": name, **payload})

    def send_report(self, hex_data) -> bool:
        """Relay one raw HID++ input report (hex) to the focused remote."""
        if self._decode_only:
            return False
        return self._send({"type": "report", "data": hex_data})

    def notify_decode_changed(self):
        """Publish the live decode map for Deskflow HID passthrough."""
        decode = self._decode_supplier()
        if not isinstance(decode, dict) or decode.get("feat_idx") is None:
            return
        if decode == self._last_sent_decode:
            return
        self._last_sent_decode = dict(decode)
        self._send({"type": "decode", "decode": decode})

    def notify_device_connected(self, device):
        if self._decode_only:
            self.notify_decode_changed()
            return
        if device is None:
            return
        payload = _device_payload(device)
        decode = self._decode_supplier()
        if isinstance(decode, dict) and decode.get("feat_idx") is not None:
            payload["decode"] = decode
        self._send({"type": "connect", "device": payload})

    def notify_device_disconnected(self):
        if self._decode_only:
            return
        self._send({"type": "disconnect"})

    def _send(self, message) -> bool:
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
                return True
            except OSError as exc:
                print(f"[RemoteForward] send failed: {exc}")
                return False

    # ── connection loop ───────────────────────────────────────────

    def _run(self):
        attempt = 0
        while not self._stopped.is_set():
            sock = self._connect_and_hello()
            if sock is None:
                delay = _RECONNECT_DELAYS_S[
                    min(attempt, len(_RECONNECT_DELAYS_S) - 1)
                ]
                attempt += 1
                if self._stopped.wait(delay):
                    return
                continue
            attempt = 0
            try:
                self._session(sock)
            except Exception as exc:  # noqa: BLE001 - session boundary
                print(f"[RemoteForward] session error: {exc!r}")
            finally:
                self._on_session_end()

    def _connect_and_hello(self):
        try:
            sock = socket.create_connection((self._host, self._port), timeout=3)
        except OSError:
            return None
        try:
            sock.sendall(json.dumps({
                "type": "hello",
                "token": self._token,
                "version": PROTOCOL_VERSION,
                "role": "source",
            }).encode("utf-8") + b"\n")
            sock.settimeout(5)
            reader = sock.makefile("rb")
            reply_line = reader.readline(_MAX_LINE_BYTES)
            reply = json.loads(reply_line) if reply_line else None
            if not (isinstance(reply, dict) and reply.get("ok")):
                print(f"[RemoteForward] bridge rejected hello: {reply!r}")
                reader.close()
                sock.close()
                return None
            sock.settimeout(None)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._reader = reader
            return sock
        except (OSError, ValueError) as exc:
            print(f"[RemoteForward] bridge handshake failed: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            return None

    def _session(self, sock):
        with self._send_lock:
            self._sock = sock
        with self._state_lock:
            self._connected = True
        self._emit_status("Connected to KVM bridge")
        print(f"[RemoteForward] Connected to bridge {self._host}:{self._port}")
        # Fresh session: always republish decode (Deskflow clears its cache on
        # restart) and announce the device when not decode-only.
        self._last_sent_decode = None
        self.notify_device_connected(self._device_supplier())

        while not self._stopped.is_set():
            try:
                line = self._reader.readline(_MAX_LINE_BYTES + 1)
            except OSError:
                return
            if not line or len(line) > _MAX_LINE_BYTES:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self._handle_message(msg)

    def _handle_message(self, msg):
        if msg.get("type") == "focus":
            local = bool(msg.get("local", True))
            screen = msg.get("screen")
            with self._state_lock:
                was_remote = self._remote_focus
                self._remote_focus = not local
                self._focus_screen = screen
            if was_remote != (not local):
                state = "remote" if not local else "local"
                print(f"[RemoteForward] Focus -> {state} (screen={screen})")

    def _on_session_end(self):
        # Runs on the forwarder thread, which owns the reader.
        reader = getattr(self, "_reader", None)
        self._reader = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        self._shutdown_socket()
        with self._state_lock:
            was_connected = self._connected
            self._connected = False
            self._remote_focus = False  # fail-safe: never suppress while down
            self._focus_screen = None
        self._last_sent_decode = None
        if was_connected:
            self._emit_status("Disconnected from KVM bridge")
            print("[RemoteForward] Bridge connection lost")

    def _shutdown_socket(self):
        """Thread-safe socket teardown; never touches the reader (owned by
        the forwarder thread)."""
        with self._send_lock:
            sock = self._sock
            self._sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ── status plumbing ───────────────────────────────────────────

    def _emit_status(self, message):
        if self._status_cb is None:
            return
        try:
            self._status_cb(message)
        except Exception as exc:  # noqa: BLE001 - callback boundary
            print(f"[RemoteForward] status callback raised: {exc!r}")
