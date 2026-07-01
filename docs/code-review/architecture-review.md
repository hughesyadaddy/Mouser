# Architecture Review — KVM scroll invert

## Verdict

Architecture matches stated policy. Layering is sound; attribution is the weak point on Windows/macOS.

## Policy compliance

| Requirement | Status |
|-------------|--------|
| Host invert during remote focus | ✅ Pass-through paths invert on macOS/Windows |
| Client physical-only invert | ✅ `_physical_logitech_bound` + engine firmware gate |
| DMSR gestures only | ✅ (prior commit; unchanged here) |
| Deskflow agnostic | ✅ No protocol changes |

## Important

1. **Windows attribution fragility** — `GetRawInputBuffer` in LL hook vs existing `WM_INPUT` path; silent invert miss possible.
2. **Denylist maintenance** — `_VIRTUAL_DEVICE_SOURCES` must stay in sync with `remote_device.py` transport strings.
3. **macOS heuristic** — IOHID side-channel is best-effort; firmware invert remains primary for MX vertical.
4. **Platform parity** — Linux attribution is structurally correct (evdev grab); Windows lacks tests and may lack reliability.
5. **Engine coupling** — Firmware gate should use hook helper, not `MouseHook` class constant.

## Suggestions

- Move virtual source set to `ConnectedDeviceInfo` or `mouse_hook_types`.
- Document three-platform attribution contract in `DEVELOPMENT.md`.
- Windows: consider flagging wheel events in `_process_raw_input` (already Logitech-filtered).
