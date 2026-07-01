"""hidapi-compatible ingress adapter for Deskflow DFHR sink frames."""

from __future__ import annotations

import queue
import threading

DESKFLOW_SINK_PATH = b"deskflow://127.0.0.1/sink"


class DeskflowSinkDevice:
    """Presents Deskflow HID reports through a hidapi-like read/write API."""

    def __init__(self, path=DESKFLOW_SINK_PATH, read_timeout_ms: int = 50):
        self._path = path
        self._read_timeout_ms = max(0, int(read_timeout_ms))
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
        self._closed = False
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path

    def set_nonblocking(self, enabled):
        # Blocking read with short timeout mimics hidapi poll behaviour.
        self._read_timeout_ms = 0 if enabled else 50

    def write(self, data):
        # Firmware writes cannot reach the physical device through Tier 1/1.5.
        return len(data) if data else 0

    def read(self, size, timeout_ms=0):
        if self._closed:
            return None
        wait = self._read_timeout_ms if timeout_ms == 0 else int(timeout_ms)
        wait_sec = None if wait <= 0 else wait / 1000.0
        try:
            data = self._queue.get(timeout=wait_sec)
        except queue.Empty:
            return None
        if data is None:
            return None
        return data[:size]

    def close(self):
        with self._lock:
            self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def feed_report(self, payload: bytes):
        """Enqueue one raw HID++ report from the Deskflow loopback sink."""
        if self._closed or not payload:
            return
        try:
            self._queue.put_nowait(bytes(payload))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(bytes(payload))
            except queue.Full:
                pass

    def flush(self):
        """Drop queued reports (e.g. on ingress disconnect)."""
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break


_GLOBAL_SINK: DeskflowSinkDevice | None = None
_SINK_LOCK = threading.Lock()


def get_deskflow_sink() -> DeskflowSinkDevice:
    global _GLOBAL_SINK
    with _SINK_LOCK:
        if _GLOBAL_SINK is None:
            _GLOBAL_SINK = DeskflowSinkDevice()
        return _GLOBAL_SINK


def flush_deskflow_sink():
    """Clear any queued DFHR reports in the global sink."""
    with _SINK_LOCK:
        if _GLOBAL_SINK is not None:
            _GLOBAL_SINK.flush()


def reset_deskflow_sink_for_tests():
    """Drop the process-global sink (tests only)."""
    global _GLOBAL_SINK
    with _SINK_LOCK:
        if _GLOBAL_SINK is not None:
            _GLOBAL_SINK.close()
        _GLOBAL_SINK = None
