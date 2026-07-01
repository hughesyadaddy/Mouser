"""
Remote virtual-device server.

Lets a trusted local process (e.g. a Deskflow fork relaying events from the
machine the mouse is physically attached to) present a Logitech device to
Mouser *as data*: the sender supplies the real device's identity, Mouser
builds the same ``ConnectedDeviceInfo`` it would for local hardware, and
HID++-only events (gesture button, rawXY swipes, thumb button, ...) are fed
into the existing hook pipeline. No HID emulation, no drivers.

Protocol: line-delimited JSON plus optional binary DFHR HID report frames
on the same loopback TCP socket after authentication. One client at a time.

    -> {"type": "hello", "token": "<shared token>", "version": 1}
    <- {"ok": true, "server": "mouser", "version": 1}
    -> {"type": "connect", "device": {"product_id": "0xB042",
                                      "product_name": "MX Master 4"}}
    <- {"ok": true, "device_key": "mx_master_4", "display_name": "MX Master 4"}
    -> {"type": "event", "name": "gesture_down"}
    -> {"type": "event", "name": "gesture_move", "dx": 12, "dy": -3}
    -> {"type": "event", "name": "gesture_up"}
    -> {"type": "report", "device_id": 1, "data": "11ff0b00c30000"}
    -> {"type": "disconnect"}

``report`` carries one raw HID++ input report (hex), as shipped by a
Deskflow HID pass-through host that seized the device; it is decoded
locally by a detached parser when decode context is available (see
``_build_raw_decoder``). ``event`` carries host-decoded events (the
original Mouser-to-Mouser relay); both may be mixed on one connection.

Security posture (fail-closed):
  - server refuses to start without a configured token;
  - binds to loopback only;
  - first message must authenticate, compared in constant time;
  - events are an allowlist of known hook entry points -- there is no
    generic key/input injection in this protocol;
  - events are rejected unless the *virtual* device currently owns the
    connection slot (a physically connected Logitech always wins).
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time

from core.hid_deskflow_backend import get_deskflow_sink
from core.hid_sink import SINK_MAGIC, is_json_line_start, try_decode_report_frame
from core.logi_devices import build_connected_device_info
from core.mouse_hook_types import (
    DEVICE_SOURCE_DESKFLOW_SHIM,
    DEVICE_SOURCE_REMOTE_VIRTUAL,
)

PROTOCOL_VERSION = 1
DEFAULT_PORT = 19795  # 0x4D53 "MS"
_MAX_LINE_BYTES = 64 * 1024

# MX Master 4's Sense Panel CID. The sender resolves gesture/thumb roles on
# its side before forwarding, so the receiving hook's OS-level fallback
# rerouting (``_gesture_via_sense_panel``) must stay off; claiming the panel
# divert is active does exactly that and is harmless for other devices.
_SENSE_PANEL_CID = 0x01A0

# Allowlisted event name -> BaseMouseHook entry point. ``gesture_move`` is
# handled separately because it carries deltas.
_EVENT_HANDLERS = {
    "gesture_down": "_on_hid_gesture_down",
    "gesture_up": "_on_hid_gesture_up",
    "thumb_button_down": "_on_hid_thumb_button_down",
    "thumb_button_up": "_on_hid_thumb_button_up",
    "mode_shift_down": "_on_hid_mode_shift_down",
    "mode_shift_up": "_on_hid_mode_shift_up",
    "dpi_switch_down": "_on_hid_dpi_switch_down",
    "dpi_switch_up": "_on_hid_dpi_switch_up",
}


def _coerce_product_id(value):
    """Accept int or "0xB042"-style string; None for absent/garbage."""
    if value in (None, ""):
        return None
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


class RemoteDeviceServer:
    """Loopback TCP server that connects a remote-described Logitech device
    into a platform mouse hook and relays its HID++-only events."""

    def __init__(self, hook, *, token, port=DEFAULT_PORT, host="127.0.0.1",
                 status_cb=None, decode_override=None, transparent_transport=False):
        self._hook = hook
        self._token = str(token or "")
        self._host = host
        self._port = int(port)
        self._status_cb = status_cb
        self._listener = None
        self._thread = None
        self._stopped = threading.Event()
        self._client = None
        self._client_lock = threading.Lock()
        self._virtual_device = None
        # Raw-frame decode context (``settings.remote_device.decode``):
        # used when the sender ships raw HID++ report frames instead of
        # decoded events and its ``connect`` carries no ``decode`` object.
        self._decode_override = (
            dict(decode_override) if isinstance(decode_override, dict) else None
        )
        self._transparent_transport = bool(transparent_transport)
        self._raw_decoder = None
        self._listener_ingress = False

    # ── lifecycle ─────────────────────────────────────────────────

    @property
    def port(self):
        """Bound port (useful when constructed with port=0 in tests)."""
        if self._listener is None:
            return None
        return self._listener.getsockname()[1]

    def start(self) -> bool:
        if not self._token:
            self._emit_status(
                "Remote device server not started: no token configured"
            )
            return False
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(1)
        except OSError as exc:
            self._emit_status(
                f"Remote device server failed to bind {self._host}:{self._port}: {exc}"
            )
            return False
        self._listener = listener
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._serve_forever,
            daemon=True,
            name="RemoteDeviceServer",
        )
        self._thread.start()
        print(
            f"[RemoteDevice] Listening on {self._host}:{self.port} "
            f"(protocol v{PROTOCOL_VERSION})"
        )
        return True

    def stop(self):
        self._stopped.set()
        with self._client_lock:
            client = self._client
        for sock in (client, self._listener):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._listener = None
        self._virtual_disconnect()

    # ── server loop ───────────────────────────────────────────────

    def _serve_forever(self):
        while not self._stopped.is_set():
            try:
                conn, addr = self._listener.accept()
            except OSError:
                break  # listener closed by stop()
            with self._client_lock:
                self._client = conn
            try:
                self._handle_client(conn, addr)
            except Exception as exc:  # noqa: BLE001 - session boundary
                print(f"[RemoteDevice] client session error: {exc!r}")
            finally:
                with self._client_lock:
                    self._client = None
                try:
                    conn.close()
                except OSError:
                    pass
                # A vanished client must never leave a ghost device
                # connected -- that would hold the intercept gate open.
                self._virtual_disconnect()

    def _handle_client(self, conn, addr):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        buffer = b""
        buffer, ok = self._read_hello(conn, buffer)
        if not ok:
            return
        while not self._stopped.is_set():
            if not buffer:
                chunk = conn.recv(8192)
                if not chunk:
                    return
                buffer += chunk
            if is_json_line_start(buffer[0]):
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > _MAX_LINE_BYTES:
                        return
                    chunk = conn.recv(8192)
                    if not chunk:
                        return
                    buffer += chunk
                    continue
                line = buffer[:newline]
                buffer = buffer[newline + 1:]
                if len(line) > _MAX_LINE_BYTES:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._send(conn, {"ok": False, "error": "malformed_json"})
                    continue
                self._handle_message(conn, msg)
            elif buffer.startswith(SINK_MAGIC):
                try:
                    decoded = try_decode_report_frame(buffer)
                except ValueError:
                    print(f"[RemoteDevice] malformed DFHR frame from {addr}")
                    buffer = buffer[1:]
                    continue
                if decoded is None:
                    chunk = conn.recv(8192)
                    if not chunk:
                        return
                    buffer += chunk
                    continue
                _device_id, payload, consumed = decoded
                buffer = buffer[consumed:]
                self._handle_binary_report(payload)
            else:
                print(f"[RemoteDevice] unknown wire data from {addr}")
                return

    def _read_hello(self, conn, buffer):
        while b"\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return buffer, False
            buffer += chunk
            if len(buffer) > _MAX_LINE_BYTES:
                return buffer, False
        newline = buffer.find(b"\n")
        line = buffer[:newline]
        buffer = buffer[newline + 1:]
        try:
            msg = json.loads(line)
        except ValueError:
            return buffer, False
        if (
            not isinstance(msg, dict)
            or msg.get("type") != "hello"
            or not isinstance(msg.get("token"), str)
            or not hmac.compare_digest(msg["token"], self._token)
        ):
            print("[RemoteDevice] Rejected unauthenticated client")
            self._send(conn, {"ok": False, "error": "unauthorized"})
            return buffer, False
        self._send(
            conn,
            {"ok": True, "server": "mouser", "version": PROTOCOL_VERSION},
        )
        return buffer, True

    def _handle_binary_report(self, payload):
        if not self._owns_connection():
            return
        if self._listener_ingress:
            get_deskflow_sink().feed_report(payload)
            return
        if self._raw_decoder is None:
            return
        try:
            self._raw_decoder._on_report(payload)
        except Exception as exc:  # noqa: BLE001 - decode boundary
            print(f"[RemoteDevice] binary report decode error: {exc!r}")

    def _send(self, conn, payload):
        try:
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError:
            pass

    # ── message handling ──────────────────────────────────────────

    def _handle_message(self, conn, msg):
        if not isinstance(msg, dict):
            self._send(conn, {"ok": False, "error": "malformed_message"})
            return
        msg_type = msg.get("type")
        if msg_type == "connect":
            self._send(conn, self._handle_connect(msg))
        elif msg_type == "disconnect":
            self._virtual_disconnect()
            self._send(conn, {"ok": True})
        elif msg_type == "event":
            self._send(conn, self._handle_event(msg))
        elif msg_type == "report":
            self._send(conn, self._handle_report(msg))
        elif msg_type == "update_decode":
            self._send(conn, self._handle_update_decode(msg))
        elif msg_type == "hello":
            self._send(conn, {"ok": True, "server": "mouser",
                              "version": PROTOCOL_VERSION})
        elif msg_type == "ping":
            self._send(conn, {"ok": True})
        else:
            self._send(conn, {"ok": False, "error": "unknown_message_type"})

    def _owns_connection(self) -> bool:
        if self._listener_ingress:
            hg = getattr(self._hook, "_hid_gesture", None)
            return bool(
                getattr(self._hook, "_device_connected", False)
                and hg is not None
                and getattr(hg, "_deskflow_readonly", False)
            )
        return (
            self._virtual_device is not None
            and self._hook.connected_device is self._virtual_device
        )

    def _handle_connect(self, msg):
        if self._hook.connected_device is not None and not self._owns_connection():
            # A physically attached Logitech always wins over a remote one.
            return {"ok": False, "error": "physical_device_present"}

        device = msg.get("device")
        if not isinstance(device, dict):
            return {"ok": False, "error": "missing_device"}
        product_id = _coerce_product_id(device.get("product_id"))
        product_name = device.get("product_name")
        product_name = str(product_name) if product_name else None
        if product_id is None and not product_name:
            return {"ok": False, "error": "missing_device_identity"}

        decode = device.get("decode") or self._decode_override
        if (
            self._transparent_transport
            and isinstance(decode, dict)
            and hasattr(self._hook, "attach_deskflow_ingress")
        ):
            if not self._hook.attach_deskflow_ingress(
                decode, product_id=product_id, product_name=product_name
            ):
                return {"ok": False, "error": "deskflow_attach_failed"}
            self._listener_ingress = True
            self._virtual_device = None
            self._raw_decoder = None
            preview = build_connected_device_info(
                product_id=product_id,
                product_name=product_name or "Logitech Mouse",
                transport="USB Receiver",
                source=DEVICE_SOURCE_DESKFLOW_SHIM,
                active_gesture_cid=_SENSE_PANEL_CID,
            )
            self._emit_status(f"Deskflow ingress: {preview.display_name}")
            print(
                f"[RemoteDevice] Deskflow ingress attach: {preview.display_name} "
                f"(key={preview.key})"
            )
            return {
                "ok": True,
                "device_key": preview.key,
                "display_name": preview.display_name,
            }

        if self._transparent_transport:
            transport = "USB Receiver"
            source = DEVICE_SOURCE_DESKFLOW_SHIM
        else:
            transport = "remote"
            source = DEVICE_SOURCE_REMOTE_VIRTUAL

        try:
            info = build_connected_device_info(
                product_id=product_id,
                product_name=product_name,
                transport=transport,
                source=source,
                active_gesture_cid=_SENSE_PANEL_CID,
            )
        except Exception as exc:  # noqa: BLE001 - input boundary
            print(f"[RemoteDevice] connect failed to build device info: {exc!r}")
            return {"ok": False, "error": "invalid_device"}

        self._virtual_device = info
        self._hook._connected_device = info
        self._hook._set_device_connected(True)
        self._raw_decoder = self._build_raw_decoder(
            device.get("decode") or self._decode_override
        )
        self._emit_status(f"Remote device connected: {info.display_name}")
        print(
            f"[RemoteDevice] Virtual connect: {info.display_name} "
            f"(key={info.key})"
        )
        return {
            "ok": True,
            "device_key": info.key,
            "display_name": info.display_name,
        }

    def _handle_update_decode(self, msg):
        if not self._owns_connection():
            return {"ok": False, "error": "not_connected"}
        decode = msg.get("decode")
        if not isinstance(decode, dict):
            return {"ok": False, "error": "missing_decode"}
        if self._listener_ingress:
            hg = getattr(self._hook, "_hid_gesture", None)
            if hg is None or not hg.queue_decode_update(decode):
                return {"ok": False, "error": "invalid_decode"}
            return {"ok": True}
        decoder = self._build_raw_decoder(decode)
        if decoder is None:
            return {"ok": False, "error": "invalid_decode"}
        self._raw_decoder = decoder
        return {"ok": True}

    def _handle_event(self, msg):
        if not self._owns_connection():
            # Either no virtual device is connected, or physical hardware
            # has displaced it. Never drive the pipeline in that state.
            return {"ok": False, "error": "not_connected"}

        name = msg.get("name")
        if name == "gesture_move":
            try:
                dx = float(msg.get("dx", 0))
                dy = float(msg.get("dy", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "malformed_deltas"}
            self._hook._on_hid_gesture_move(dx, dy)
            return {"ok": True}

        handler_name = _EVENT_HANDLERS.get(name)
        if handler_name is None:
            return {"ok": False, "error": "unknown_event"}
        getattr(self._hook, handler_name)()
        return {"ok": True}

    def _build_raw_decoder(self, decode):
        """Build a detached HID++ parser for raw report frames.

        The sender (e.g. a Deskflow HID pass-through host) seized the
        device and ships its input reports verbatim; decoding them needs
        the device's REPROG_V4 feature index and the diverted gesture CID,
        which only the machine that armed the diverts knows. That context
        arrives in ``connect.decode`` (or the local
        ``settings.remote_device.decode`` override):

            {"feat_idx": 11, "gesture_cid": "0x01A0",
             "extra_diverts": {"0x00C4": "thumb_button"},
             "rawxy": true}

        Returns None (raw frames rejected) when the context is absent —
        guessing feature indexes would misfire on other features' events.
        The decoder is a never-started HidGestureListener: ``_on_report``
        is the exact code path local hidapi reads use, so the held-state
        machine and callbacks behave identically.
        """
        if not isinstance(decode, dict):
            return None
        feat_idx = decode.get("feat_idx")
        # HID++ feature indexes are a single report byte; anything else is
        # config garbage that would silently match nothing.
        if not isinstance(feat_idx, int) or not 0 < feat_idx <= 0xFF:
            return None

        from core.hid_gesture import HidGestureListener

        hook = self._hook
        role_map = {
            "thumb_button": (hook._on_hid_thumb_button_down,
                             hook._on_hid_thumb_button_up),
            "mode_shift": (hook._on_hid_mode_shift_down,
                           hook._on_hid_mode_shift_up),
            "dpi_switch": (hook._on_hid_dpi_switch_down,
                           hook._on_hid_dpi_switch_up),
        }
        extra = {}
        for cid_text, role in (decode.get("extra_diverts") or {}).items():
            cid = _coerce_product_id(cid_text)
            handlers = role_map.get(role)
            if cid is None or handlers is None:
                continue
            extra[cid] = {"on_down": handlers[0], "on_up": handlers[1]}

        listener = HidGestureListener(
            on_down=hook._on_hid_gesture_down,
            on_up=hook._on_hid_gesture_up,
            on_move=hook._on_hid_gesture_move,
            extra_diverts=extra,
        )
        listener._feat_idx = feat_idx
        gesture_cid = _coerce_product_id(decode.get("gesture_cid"))
        if gesture_cid is not None:
            listener._gesture_cid = gesture_cid
        listener._rawxy_enabled = bool(decode.get("rawxy", True))
        return listener

    def _handle_report(self, msg):
        if not self._owns_connection():
            return {"ok": False, "error": "not_connected"}
        data = msg.get("data")
        if not isinstance(data, str):
            return {"ok": False, "error": "missing_data"}
        try:
            raw = bytes.fromhex(data)
        except ValueError:
            return {"ok": False, "error": "malformed_hex"}
        if self._listener_ingress:
            get_deskflow_sink().feed_report(raw)
            return {"ok": True}
        if self._raw_decoder is None:
            return {"ok": False, "error": "no_decode_context"}
        try:
            self._raw_decoder._on_report(raw)
        except Exception as exc:  # noqa: BLE001 - decode boundary
            print(f"[RemoteDevice] raw report decode error: {exc!r}")
            return {"ok": False, "error": "decode_error"}
        return {"ok": True}

    def _virtual_disconnect(self):
        if self._listener_ingress:
            self._listener_ingress = False
            if hasattr(self._hook, "detach_deskflow_ingress"):
                self._hook.detach_deskflow_ingress()
            self._emit_status("Deskflow ingress disconnected")
            print("[RemoteDevice] Deskflow ingress disconnect")
            return
        if self._owns_connection():
            self._hook._on_hid_disconnect()
            self._emit_status("Remote device disconnected")
            print("[RemoteDevice] Virtual disconnect")
        self._virtual_device = None
        self._raw_decoder = None

    # ── status plumbing ───────────────────────────────────────────

    def _emit_status(self, message):
        if self._status_cb is None:
            return
        try:
            self._status_cb(message)
        except Exception as exc:  # noqa: BLE001 - callback boundary
            print(f"[RemoteDevice] status callback raised: {exc!r}")
