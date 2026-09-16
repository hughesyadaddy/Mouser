"""
Mouser bridge server -- the single loopback endpoint Deskflow talks to.

Mouser OWNS one listener on ``127.0.0.1:19795`` for both directions of the
KVM integration ("lego" contract, protocol 2). Deskflow dials it, whichever
role it is playing:

  - ``role: client`` (mouse is elsewhere): Deskflow seizes the HID++
    interface on the host and relays raw reports here; Mouser attaches the
    in-process sink to the main HID listener (Tier 1.5 ingress).
  - ``role: server`` (mouse is physically here): Mouser publishes the live
    decode map so the far machine can interpret the raw reports.

Control plane (JSON lines, ``"t"`` key):

    -> {"t":"hello","proto":2,"app":"deskflow-core","ver":..,"pid":..,
        "role":"server|client","caps":[..],"token":"<bridge.token>"}
    <- {"ok":true,"proto":2,"caps":["focus","hidr","decode"]}
       | {"ok":false,"reason":"proto"|"auth"}
    <-> {"t":"ping"} / {"t":"pong"}        every 2 s, 3 misses -> drop
    -> {"t":"attach","session_id":..[,"device":{..},"decode":{..}]}
    -> {"t":"focus","screen":..,"here":bool}
    -> {"t":"role","role":..}
    -> {"t":"detach"}
    -> {"t":"bye","reason":..}
    -> {"t":"status"}   (from anyone, no hello needed; fleet-health uses it)
    <- {"attached":bool,"peer":..,"role":..,"session_id":..,"proto":2,..}

Data plane keeps the protocol-1 shapes byte-for-byte (``{"type":"report"}``,
``{"type":"update_decode"}``, DFHR binary frames, ``{"type":"decode"}`` on
the way out) so a Deskflow that has not been upgraded keeps working.

State machine::

    IDLE --hello--> HELLO --attach--> ATTACHED <--focus here=false/true--> PAUSED
                                         |                                  |
                                    bye(shutdown) / 60 s dead link ----> IDLE

The whole point: a screen switch is a *pause*, not a teardown. Before this,
every Deskflow focus flip dropped the socket, which detached the ingress and
rebuilt the HID listener session (thousands of rebuilds a day). Now only an
orderly shutdown or a link that stays dead for a minute reaches
``clear_deskflow_attach``.

Compatibility: a connection that does not send a proto-2 hello within 3 s
(or whose first line is a protocol-1 hello) is served with the old
connect/disconnect semantics on that connection only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time

from core.config import CONFIG_DIR
from core.remote_device import DEFAULT_PORT, RemoteDeviceServer, read_frames
from core.remote_protocol import MAX_LINE_BYTES

PROTO = 2
CAPS = ("focus", "hidr", "decode")
PEER_APP_DEFAULT = "deskflow-core"

HELLO_TIMEOUT_S = 3.0
HEARTBEAT_S = 2.0
HEARTBEAT_MISSES = 3
IDLE_PAUSE_S = 10.0
DEAD_LINK_S = 60.0
BYE_QUIET_S = 15.0

TOKEN_FILENAME = "bridge.token"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

IDLE = "IDLE"
HELLO = "HELLO"
ATTACHED = "ATTACHED"
PAUSED = "PAUSED"


# ── token file ────────────────────────────────────────────────────


def token_path() -> str:
    """``~/Library/Application Support/Mouser/bridge.token`` (macOS),
    ``%APPDATA%\\Mouser\\bridge.token`` (Windows), XDG on Linux."""
    return os.path.join(CONFIG_DIR, TOKEN_FILENAME)


def ensure_token_file(path=None) -> str:
    """Return the bridge token, creating ``bridge.token`` (mode 0600) when
    it is missing or unreadable. An existing well-formed token is reused
    so a Mouser restart does not invalidate a Deskflow that cached it."""
    path = path or token_path()
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if _TOKEN_RE.match(existing):
            _chmod_private(path)
            return existing
    except OSError:
        pass
    token = secrets.token_hex(16)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _chmod_private(path)
    return token


def _chmod_private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── helpers ───────────────────────────────────────────────────────


class _LockedSocket:
    """Socket proxy serialising ``sendall`` -- the reader thread's replies
    and the heartbeat thread's pings share one connection."""

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()

    def sendall(self, data):
        with self._lock:
            return self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Peer:
    def __init__(self, conn, addr, clock):
        self.conn = conn
        self.addr = addr
        self.app = PEER_APP_DEFAULT
        self.ver = None
        self.pid = None
        self.role = None
        self.caps = ()
        self.last_rx = clock()
        self.alive = True
        self.quiet = False          # bye seen: no error logs for this peer
        self.last_decode = None
        self.session_id = None      # last session this peer attached

    def send(self, payload) -> bool:
        try:
            self.conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            return True
        except OSError:
            return False

    def close(self):
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


# ── server ────────────────────────────────────────────────────────


class BridgeServer:
    """Loopback bridge: proto-2 control plane + protocol-1 data plane."""

    def __init__(self, hook, *, port=DEFAULT_PORT, host="127.0.0.1",
                 token=None, token_file=None, legacy_token="",
                 status_cb=None, decode_override=None,
                 transparent_transport=True, decode_supplier=None,
                 on_proto2_seen=None,
                 hello_timeout_s=HELLO_TIMEOUT_S, heartbeat_s=HEARTBEAT_S,
                 heartbeat_misses=HEARTBEAT_MISSES, idle_pause_s=IDLE_PAUSE_S,
                 dead_link_s=DEAD_LINK_S, bye_quiet_s=BYE_QUIET_S,
                 clock=time.monotonic):
        """``token`` overrides the token file (tests); ``legacy_token`` is
        the protocol-1 shared secret for un-upgraded peers (empty = reject
        legacy hellos). ``decode_supplier`` returns the live decode map to
        publish to ``role: server`` peers. ``on_proto2_seen`` fires once,
        after the first accepted proto-2 hello (persist ``bridge_proto``)."""
        self._hook = hook
        self._host = host
        self._port = int(port)
        self._token = token
        self._token_file = token_file
        self._status_cb = status_cb
        self._decode_supplier = decode_supplier or (lambda: None)
        self._on_proto2_seen = on_proto2_seen
        self._hello_timeout_s = float(hello_timeout_s)
        self._heartbeat_s = float(heartbeat_s)
        self._heartbeat_misses = int(heartbeat_misses)
        self._idle_pause_s = float(idle_pause_s)
        self._dead_link_s = float(dead_link_s)
        self._bye_quiet_s = float(bye_quiet_s)
        self._clock = clock

        # Data plane + legacy sessions reuse the protocol-1 server without
        # its listener: same connect/report/decode handlers, same hook
        # attach path, so the wire shapes stay byte-compatible.
        self._legacy = RemoteDeviceServer(
            hook,
            token=legacy_token or "",
            port=0,
            status_cb=status_cb,
            decode_override=decode_override,
            transparent_transport=transparent_transport,
        )
        self._legacy_active = 0

        self._listener = None
        self._thread = None
        self._stopped = threading.Event()
        self._state_lock = threading.RLock()
        self._peers: list[_Peer] = []
        self._state = IDLE
        self._session_id = None
        self._ingress_peer: _Peer | None = None
        self._last_device = None
        self._last_decode = None
        self._dead_link_timer = None
        self._quiet_until = 0.0
        self._proto2_seen = False
        # Metrics for tests / status.
        self.attach_calls = 0
        self.clear_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0

    # ── lifecycle ─────────────────────────────────────────────────

    @property
    def port(self):
        if self._listener is None:
            return None
        return self._listener.getsockname()[1]

    @property
    def state(self) -> str:
        return self._state

    @property
    def token(self):
        return self._token

    def start(self) -> bool:
        if not self._token:
            try:
                self._token = ensure_token_file(self._token_file)
            except OSError as exc:
                self._emit_status(f"Bridge token file unavailable: {exc}")
                return False
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self._port))
            listener.listen(8)
        except OSError as exc:
            self._emit_status(
                f"Bridge failed to bind {self._host}:{self._port}: {exc}"
            )
            return False
        self._listener = listener
        self._stopped.clear()
        self._legacy._stopped = self._stopped
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="BridgeServer"
        )
        self._thread.start()
        print(f"[Bridge] Listening on {self._host}:{self.port} (proto {PROTO})")
        return True

    def stop(self):
        self._stopped.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                listener.close()
            except OSError:
                pass
        with self._state_lock:
            peers = list(self._peers)
        for peer in peers:
            peer.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._cancel_dead_link()
        self._clear("stop")
        # Legacy sessions end via their own socket close; make sure no
        # ghost device survives the server.
        self._legacy._virtual_disconnect()

    # ── status / decode publish (any thread) ──────────────────────

    def status(self) -> dict:
        with self._state_lock:
            peer = self._ingress_peer or (self._peers[0] if self._peers else None)
            return {
                "attached": self._state == ATTACHED,
                "paused": self._state == PAUSED,
                "state": self._state,
                "peer": peer.app if peer is not None else None,
                "role": peer.role if peer is not None else None,
                "session_id": self._session_id,
                "proto": PROTO,
                "peers": len(self._peers),
                "legacy": self._legacy_active > 0,
            }

    def notify_decode_changed(self):
        """Push the live decode map to every ``role: server`` peer."""
        with self._state_lock:
            peers = [p for p in self._peers if p.role == "server"]
        for peer in peers:
            self._publish_decode(peer)

    def _publish_decode(self, peer):
        try:
            decode = self._decode_supplier()
        except Exception as exc:  # noqa: BLE001 - supplier boundary
            print(f"[Bridge] decode supplier raised: {exc!r}")
            return
        if not isinstance(decode, dict) or decode.get("feat_idx") is None:
            return
        if decode == peer.last_decode:
            return
        peer.last_decode = dict(decode)
        peer.send({"type": "decode", "decode": decode})

    # ── accept loop ───────────────────────────────────────────────

    def _accept_loop(self):
        while not self._stopped.is_set():
            try:
                conn, addr = self._listener.accept()
            except OSError:
                break
            threading.Thread(
                target=self._serve, args=(conn, addr), daemon=True,
                name="BridgeConn",
            ).start()

    def _serve(self, conn, addr):
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            first, buffer = self._read_first_line(conn)
            if first is not None and first.get("t") == "hello":
                self._serve_proto2(conn, addr, first, buffer)
            else:
                self._serve_legacy(conn, addr, buffer)
        except Exception as exc:  # noqa: BLE001 - session boundary
            print(f"[Bridge] connection error from {addr}: {exc!r}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_first_line(self, conn):
        """Wait up to ``hello_timeout_s`` for the first JSON line.

        Returns ``(msg, remaining_buffer)``; ``msg`` is None on timeout or
        when the first line is not a proto-2 message, in which case the
        buffer (still holding that line) is handed to the legacy path.
        ``{"t":"status"}`` is answered inline without consuming the hello
        window, so fleet-health never needs a hello.
        """
        deadline = self._clock() + self._hello_timeout_s
        buffer = b""
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = buffer[:newline]
                rest = buffer[newline + 1:]
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("t") == "status":
                    conn.sendall(json.dumps(self.status()).encode() + b"\n")
                    buffer = rest
                    continue
                if isinstance(msg, dict) and "t" in msg:
                    return msg, rest
                return None, buffer  # legacy hello (or junk) -> protocol 1
            if len(buffer) > MAX_LINE_BYTES:
                return None, buffer
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None, buffer
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return None, buffer
            finally:
                conn.settimeout(None)
            if not chunk:
                return None, buffer
            buffer += chunk

    # ── legacy (protocol 1) ───────────────────────────────────────

    def _serve_legacy(self, conn, addr, buffer):
        if not buffer and self._stopped.is_set():
            return
        with self._state_lock:
            if self._state in (ATTACHED, PAUSED):
                # A proto-2 session owns the ingress; do not let a stray
                # legacy connection tear it down on exit.
                self._legacy._send(conn, {"ok": False, "error": "busy"})
                return
            self._legacy_active += 1
        try:
            print(f"[Bridge] legacy (proto 1) peer {addr}")
            self._legacy.serve_connection(conn, addr, initial=buffer)
        finally:
            with self._state_lock:
                self._legacy_active -= 1

    # ── proto 2 ───────────────────────────────────────────────────

    def _serve_proto2(self, conn, addr, hello, buffer):
        conn = _LockedSocket(conn)
        peer = _Peer(conn, addr, self._clock)
        if not self._accept_hello(peer, hello):
            return
        with self._state_lock:
            self._peers.append(peer)
            if self._state == IDLE:
                self._state = HELLO
        hb = threading.Thread(
            target=self._heartbeat, args=(peer,), daemon=True, name="BridgeHB"
        )
        hb.start()
        try:
            for kind, item in read_frames(conn, addr, buffer, self._stopped):
                peer.last_rx = self._clock()
                if kind == "report":
                    self._legacy._handle_binary_report(item)
                elif item is None:
                    peer.send({"ok": False, "error": "malformed_json"})
                else:
                    self._on_peer_message(peer, item)
                if not peer.alive:
                    break
        finally:
            peer.alive = False
            self._on_peer_gone(peer)

    def _accept_hello(self, peer, hello) -> bool:
        proto = hello.get("proto")
        if proto != PROTO:
            peer.send({"ok": False, "reason": "proto"})
            print(f"[Bridge] rejected hello: proto={proto!r}")
            return False
        supplied = hello.get("token")
        if self._token and not (
            isinstance(supplied, str)
            and secrets.compare_digest(supplied, self._token)
        ):
            peer.send({"ok": False, "reason": "auth"})
            print("[Bridge] rejected hello: bad token")
            return False
        peer.app = str(hello.get("app") or PEER_APP_DEFAULT)
        peer.ver = hello.get("ver")
        peer.pid = hello.get("pid")
        peer.role = hello.get("role")
        caps = hello.get("caps")
        peer.caps = tuple(caps) if isinstance(caps, list) else ()
        if not peer.send({"ok": True, "proto": PROTO, "caps": list(CAPS)}):
            return False
        print(
            f"[Bridge] hello from {peer.app} ver={peer.ver} pid={peer.pid} "
            f"role={peer.role}"
        )
        if not self._proto2_seen:
            self._proto2_seen = True
            cb = self._on_proto2_seen
            if cb is not None:
                try:
                    cb()
                except Exception as exc:  # noqa: BLE001 - callback boundary
                    print(f"[Bridge] proto2-seen callback raised: {exc!r}")
        if peer.role == "server":
            self._publish_decode(peer)
        return True

    def _on_peer_message(self, peer, msg):
        if not isinstance(msg, dict):
            peer.send({"ok": False, "error": "malformed_message"})
            return
        t = msg.get("t")
        if t is None:
            self._on_legacy_shape(peer, msg)
            return
        if t == "ping":
            peer.send({"t": "pong"})
        elif t == "pong":
            pass
        elif t == "hello":
            peer.send({"ok": True, "proto": PROTO, "caps": list(CAPS)})
        elif t == "status":
            peer.send(self.status())
        elif t == "attach":
            peer.send(self._attach(
                peer, msg.get("session_id"), msg.get("device"), msg.get("decode")
            ))
        elif t == "focus":
            self._focus(peer, bool(msg.get("here", True)), msg.get("screen"))
        elif t == "role":
            peer.role = msg.get("role")
            if peer.role == "server":
                self._publish_decode(peer)
        elif t == "detach":
            self._pause(peer, "detach")
            peer.send({"ok": True})
        elif t == "bye":
            self._bye(peer, msg.get("reason"))
        else:
            peer.send({"ok": False, "error": "unknown_message_type"})

    def _on_legacy_shape(self, peer, msg):
        """Protocol-1 ``type`` messages on a proto-2 connection: the data
        plane is passed straight through; ``connect``/``disconnect`` take
        the proto-2 attach/pause meaning instead of tearing down."""
        msg_type = msg.get("type")
        if msg_type == "connect":
            device = msg.get("device")
            session_id = peer.session_id or self._session_id or f"conn-{id(peer):x}"
            decode = device.get("decode") if isinstance(device, dict) else None
            peer.send(self._attach(peer, session_id, device, decode))
        elif msg_type == "disconnect":
            self._pause(peer, "disconnect")
            peer.send({"ok": True})
        elif msg_type == "hello":
            peer.send({"ok": True, "proto": PROTO, "caps": list(CAPS)})
        else:
            self._legacy._handle_message(peer.conn, msg)

    # ── state machine ─────────────────────────────────────────────

    def _ingress_live(self) -> bool:
        listener = getattr(self._hook, "_hid_gesture", None)
        return bool(
            listener is not None
            and getattr(listener, "_deskflow_readonly", False)
            and getattr(listener, "_connected", False)
        )

    def _attach(self, peer, session_id, device, decode) -> dict:
        if session_id is None or session_id == "":
            return {"ok": False, "reason": "session_id"}
        session_id = str(session_id)
        with self._state_lock:
            if self._legacy_active:
                return {"ok": False, "reason": "busy"}
            if isinstance(device, dict):
                self._last_device = dict(device)
            if isinstance(decode, dict):
                self._last_decode = dict(decode)
            device = self._last_device or {}
            decode = self._last_decode or self._legacy._decode_override

            self._cancel_dead_link()
            peer.session_id = session_id
            self._ingress_peer = peer

            same_session = session_id == self._session_id
            live = self._legacy.ingress_attached and self._ingress_live()
            if live and (same_session or self._state in (ATTACHED, PAUSED)):
                # Same session: pure no-op. New id on a live ingress with
                # unchanged device/decode: resume, never rebuild.
                self._session_id = session_id
                if self._state == PAUSED:
                    self._resume(peer, "attach")
                self._state = ATTACHED
                return {"ok": True, "session_id": session_id, "resumed": True}

            connect_msg = {"type": "connect", "device": dict(device)}
            if isinstance(decode, dict):
                connect_msg["device"]["decode"] = decode
            else:
                connect_msg["device"].pop("decode", None)
            if not connect_msg["device"].get("product_id") and not connect_msg["device"].get("product_name"):
                return {"ok": False, "reason": "no_device"}
            self.attach_calls += 1
            reply = self._legacy._handle_connect(connect_msg)
            if not reply.get("ok"):
                return {"ok": False, "reason": reply.get("error", "attach_failed")}
            self._session_id = session_id
            self._legacy.resume_ingress()
            self._state = ATTACHED
            print(f"[Bridge] attached session={session_id} ({reply.get('display_name')})")
            reply = dict(reply)
            reply["session_id"] = session_id
            return reply

    def _focus(self, peer, here, screen):
        with self._state_lock:
            if self._state not in (ATTACHED, PAUSED):
                return
            if here:
                self._resume(peer, f"focus {screen}")
            else:
                self._pause(peer, f"focus {screen}")

    def _pause(self, peer, why):
        with self._state_lock:
            if self._state != ATTACHED:
                return
            self.pause_calls += 1
            self._legacy.pause_ingress()
            self._state = PAUSED

    def _resume(self, peer, why):
        with self._state_lock:
            if self._state != PAUSED:
                return
            self.resume_calls += 1
            self._legacy.resume_ingress()
            self._state = ATTACHED

    def _clear(self, why):
        with self._state_lock:
            was = self._state
            self._session_id = None
            self._ingress_peer = None
            self._cancel_dead_link()
            if was in (ATTACHED, PAUSED):
                self.clear_calls += 1
                print(f"[Bridge] ingress cleared ({why})")
                self._legacy._virtual_disconnect()
            elif self._legacy.ingress_attached:
                self._legacy._virtual_disconnect()
            # Publish the state last: status()/tests read it without the
            # lock and must never see IDLE before the teardown happened.
            self._state = IDLE

    def _bye(self, peer, reason):
        peer.quiet = True
        self._quiet_until = self._clock() + self._bye_quiet_s
        print(f"[Bridge] bye from {peer.app}: {reason!r}")
        if reason == "shutdown":
            self._clear("bye shutdown")
        else:
            self._pause(peer, f"bye {reason}")
        peer.close()

    def _on_peer_gone(self, peer):
        with self._state_lock:
            if peer in self._peers:
                self._peers.remove(peer)
            was_ingress = self._ingress_peer is peer
            if was_ingress:
                self._ingress_peer = None
            if self._state == HELLO and not self._peers:
                self._state = IDLE
            if not was_ingress:
                return
            if self._state == ATTACHED:
                self._pause(peer, "link lost")
            if self._state == PAUSED:
                self._arm_dead_link()
        if not peer.quiet and self._clock() >= self._quiet_until and not self._stopped.is_set():
            print(f"[Bridge] peer {peer.app} link lost; ingress paused")

    def _arm_dead_link(self):
        self._cancel_dead_link()
        if self._stopped.is_set():
            return
        timer = threading.Timer(self._dead_link_s, self._on_dead_link)
        timer.daemon = True
        self._dead_link_timer = timer
        timer.start()

    def _cancel_dead_link(self):
        timer = self._dead_link_timer
        self._dead_link_timer = None
        if timer is not None:
            timer.cancel()

    def _on_dead_link(self):
        with self._state_lock:
            if self._state == PAUSED and self._ingress_peer is None:
                self._clear(f"dead link > {self._dead_link_s:g}s")

    # ── heartbeat ─────────────────────────────────────────────────

    def _heartbeat(self, peer):
        limit = self._heartbeat_s * self._heartbeat_misses
        while peer.alive and not self._stopped.wait(self._heartbeat_s):
            now = self._clock()
            silent = now - peer.last_rx
            if silent > limit:
                if not peer.quiet and now >= self._quiet_until:
                    print(
                        f"[Bridge] {peer.app} missed {self._heartbeat_misses} "
                        f"heartbeats; dropping link"
                    )
                peer.close()  # reader unblocks -> _on_peer_gone -> pause
                return
            if silent > self._idle_pause_s and self._ingress_peer is peer:
                self._pause(peer, "heartbeat idle")
            if not peer.send({"t": "ping"}):
                peer.close()
                return

    # ── status plumbing ───────────────────────────────────────────

    def _emit_status(self, message):
        if self._status_cb is None:
            return
        try:
            self._status_cb(message)
        except Exception as exc:  # noqa: BLE001 - callback boundary
            print(f"[Bridge] status callback raised: {exc!r}")
