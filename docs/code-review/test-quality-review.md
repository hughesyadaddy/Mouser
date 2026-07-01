# Test Quality Review — KVM scroll invert

## Verdict

macOS happy-path and base gating covered; Windows and engine firmware gating undertested. Treat test gaps as merge blockers for multi-platform soak, not for hackintosh-only trial.

## Critical (test coverage gaps)

1. **Engine virtual source** — No test that `_apply_wheel_invert_setting` skips firmware when `source=remote-virtual` or `deskflow-shim`.
2. **Windows attribution** — `_scroll_event_targets_logitech` entirely untested (tiny11 path).
3. **IOHID monitor** — No test of start/stop or callback; tests poke `_last_wheel_monotonic` directly.

## Important

- `deskflow-shim` never tested (only `remote-virtual`).
- Linux: no test that `_handle_rel` passes `linux_evdev=True`.
- macOS phase/momentum filters untested.
- `_sync_logitech_scroll_monitor` patched to no-op in all macOS scroll tests.
- Stubs omit `source` — physical gate passes accidentally via `None`.

## Suggestions

- Parametrize virtual sources.
- Add `source="hidapi"` to `_logitech_stub()`.
- Mirror horizontal virtual-device test for `invert_hscroll`.
