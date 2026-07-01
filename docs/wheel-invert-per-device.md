# Wheel invert per device — per-axis firmware vs OS fallback

Scroll inversion is resolved and applied **per axis**, independently, from the
device's runtime wheel-feature profile. A device may firmware-invert one axis,
the other, both, or neither — and a feature being *present* does not mean the
firmware *honors* the invert write. Verify-after-write confirms each write by
read-back, so an axis the firmware can't honor cleanly falls back to OS-level
inversion without disturbing the other axis.

Resolution: `core/device_capabilities.py` (capability) →
`core/hid_gesture.py::_set_native_wheel_invert_{vertical,horizontal}` +
`_apply_pending_native_wheel_invert` (per-axis write+verify) →
`core/engine.py::_apply_wheel_invert_setting` (per-axis hook flags) →
`core/mouse_hook_base.py::_apply_{v,h}scroll_invert_fallback` (per-axis OS gate).
Validated by `tests/test_wheel_per_device.py` and `tests/test_wheel_divert.py`.

## The two independent axes

| Axis | HID++ feature | How invert is set |
|---|---|---|
| Vertical | Hi-Res Wheel Enhanced `0x2121` | setWheelMode (fn 2) invert bit `0x04`, verified by read-back |
| Horizontal | Thumbwheel `0x2150` | setThumbwheelReporting (fn 2) `[reportingMode, invert]`, verified by read-back |

## Matrix

| Device | `0x2121` (vert) | `0x2150` (horiz) | Vertical invert | Horizontal invert |
|---|---|---|---|---|
| MX Master (0xB012) | present | present but **rejects** invert | firmware | **OS fallback** (firmware NAK caught by read-back) |
| MX Master 3/3S/4 | present | present + honors | firmware | firmware |
| MX Anywhere (runtime) | present | absent | firmware | OS fallback (no feature) |
| MX Vertical / others | depends on runtime | usually absent | adaptive | OS fallback when absent |
| G502 / G602 (gaming) | absent | absent | OS fallback | OS fallback |

The original MX Master is the headline case: both wheel features are present, but
the thumbwheel firmware ACKs yet ignores `invertDirection=1` (its thumbwheel is
gesture-based — Solaar #3039). The command Mouser sends is already spec-correct
(matches Solaar's `ThumbInvert`); there is no firmware variant that works, so
read-back reports the axis as failed and horizontal inverts at the OS layer while
vertical keeps its firmware lease.

## Why this can't regress / double-invert

The old code coupled the axes (`success = ok_v AND ok_h`) and reverted the
working axis on any failure — enabling horizontal invert silently broke vertical.
Now each axis carries its own firmware-lease flag end-to-end
(`wheel_native_invert_vertical` / `wheel_native_invert_horizontal`), and the
OS-fallback gate for each axis reads only its own flag. Tests pin the asymmetric
case at the listener, engine, and event-tap layers.

## Sources
- HID++ 2.0: Solaar `ThumbInvert`/`ThumbMode` (`0x2150` fn `0x20`, `b"\x00\x01"`);
  Logitech `cpg-docs/hidpp20`
- Solaar issue #3039 — original MX Master thumbwheel is gesture-based
- Live `mouser.log` — `vertical=OK thumb=FAIL` on every `h=True` for 0xB012
