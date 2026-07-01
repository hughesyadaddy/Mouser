"""Binary HID sink framing for the Deskflow loopback port (DFHR protocol)."""

from __future__ import annotations

import struct

SINK_MAGIC = b"DFHR"
_HEADER = struct.Struct("<4sHI")  # magic, device_id, payload_len
_MAX_PAYLOAD = 4096


def encode_report_frame(device_id: int, payload: bytes) -> bytes:
    """Encode one raw HID report as a DFHR binary frame."""
    if not payload or len(payload) > _MAX_PAYLOAD:
        raise ValueError("invalid payload size")
    return _HEADER.pack(SINK_MAGIC, int(device_id) & 0xFFFF, len(payload)) + payload


def try_decode_report_frame(buffer: bytes):
    """Parse one DFHR frame from ``buffer``.

    Returns ``(device_id, payload, consumed)`` or ``None`` when incomplete.
    Raises ``ValueError`` on malformed frames.
    """
    if len(buffer) < _HEADER.size:
        return None
    magic, device_id, payload_len = _HEADER.unpack_from(buffer)
    if magic != SINK_MAGIC:
        raise ValueError("not a DFHR frame")
    if payload_len > _MAX_PAYLOAD:
        raise ValueError("payload too large")
    total = _HEADER.size + payload_len
    if len(buffer) < total:
        return None
    payload = buffer[_HEADER.size:total]
    return device_id, payload, total


def is_json_line_start(byte: int) -> bool:
    return byte in (ord("{"), ord("["))
