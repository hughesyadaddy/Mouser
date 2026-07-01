# Gestures per device — how the HID++ differs, and why Mouser stays adaptive

Mouser never chooses a gesture path from the device *model*. It reads the
device's **runtime HID++ profile** and resolves the path from that, so a mouse
we have never seen still behaves correctly. This doc records how the HID differs
across the families we support and which path each resolves to.

The resolution lives in one pure function — `core/device_capabilities.py`
`resolve_capabilities()` — fed by the real connect path
(`_choose_gesture_candidates` → `_divert` → feature discovery in
`core/hid_gesture.py`). It is validated by `tests/test_gesture_per_device.py`.

## The three things that actually differ

1. **Is there a divertable gesture control?** REPROG_V4 (feature `0x1B04`) must
   advertise a gesture CID (`0x00C3`/`0x00D7`, or `0x01A0` on MX Master 4). No
   such control → `gesture_source = none` (e.g. gaming mice).
2. **Does that control advertise rawXY?** The key-flag bit `0x0100` (`raw_xy`).
   - set → the firmware streams swipe motion over HID++ and pins the cursor
     itself → `gesture_source = rawxy`.
   - clear → Mouser reads OS cursor motion via the event tap and warp-restores
     the pointer so it doesn't drift → `gesture_source = event_tap`.
3. **Sense Panel split (MX Master 4).** Gestures move to the haptic Sense Panel
   (`0x01A0`, rawXY) and the small button (`0x00C3`) becomes a Thumb button. If
   the panel divert is rejected, `0x00C3` becomes the gesture CID (event_tap)
   and the Thumb button falls back to the OS button.

## Matrix

| Device (PID) | Gesture control | rawXY? | Resolved `gesture_source` | Cursor during swipe | Notes |
|---|---|---|---|---|---|
| MX Master (0xB012) | `0x00C3` flags `0x0031` | **no** | `event_tap` | pinned via warp-restore | **confirmed in live logs**; this is the device the fix started from |
| MX Master 2S (0xB019) | `0x00C3` | depends on fw flag | `event_tap` or `rawxy` | adaptive | pre-3 era; resolves from the runtime flag |
| MX Master 3 / 3S (0xB023/0xB034/0xB043) | `0x00C3` | **yes** (`raw_XY: yes`, libratbag) | `rawxy` | pinned by firmware | gesture button streams rawXY |
| MX Master 4 (0xB042/0xB048) | `0x01A0` Sense Panel | **yes** | `rawxy` (panel) | pinned by firmware | `0x00C3` = Thumb button (HID++); panel-reject → `event_tap` + thumb via OS |
| MX Anywhere 2S/3/3S (0xB01A/0xB025/0xB037…) | `0x00C3` if present | depends on fw flag | adaptive | adaptive | smaller mice; resolved from runtime flag |
| MX Vertical (0xB020) | `0x00C3` if present | depends on fw flag | adaptive | adaptive | resolved from runtime flag |
| G502 / G602 (gaming) | none | n/a | `none` | n/a | no thumb gesture control advertised |

"depends on fw flag / adaptive" = we don't pin a value in the catalog because
the path is decided by the live `raw_xy` flag at connect; the resolver and tests
cover both outcomes so either is correct without code changes.

## Why this can't regress per-model

`tests/test_gesture_per_device.py::AdaptivityInvariantTests` feeds the *same*
control profile to every MX model and asserts the resolved source follows the
`raw_xy` flag, not the model name. A rawXY-capable control resolves to `rawxy`
on every model; a non-rawXY control resolves to `event_tap` on every model.
Adding a new mouse is a new test row, never a new code branch.

## Sources
- libratbag device data — MX Master 3S gesture button `0xC3` `raw_XY: yes`
  (github.com/libratbag/libratbag)
- Solaar issue #3039 — original MX Master thumbwheel/gesture behavior
- Live `~/Library/Logs/Mouser/mouser.log` — original MX Master `0x00C3`
  `flags=0x0031` (no raw_xy) and resolved-capabilities audit lines
