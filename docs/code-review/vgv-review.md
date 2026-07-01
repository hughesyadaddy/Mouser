# VGV Review — KVM scroll invert (uncommitted)

**Scope:** `core/engine.py`, `core/mouse_hook_*.py`, `tests/test_remote_forward.py`, `tests/test_wheel_divert.py`, `ui/locale_manager.py`

## Verdict

Policy implementation is directionally correct: host scroll invert during remote focus, physical-only client invert, per-event Logitech attribution. No ship-blocking logic bugs found in static review; main risks are platform attribution reliability and test gaps.

## Important

1. **macOS IOHID timing** — `recent_wheel()` uses a 50 ms window; first scroll after connect or CGEvent-before-IOHID ordering may skip OS invert once.
2. **macOS fail-closed** — If `_SCROLL_MONITOR_OK=False`, OS-layer invert never runs (firmware path still works on capable devices).
3. **Windows GetRawInputBuffer** — Untested; may return empty from WH_MOUSE_LL or correlate stale packets vs `WM_INPUT` gesture path.
4. **Engine layering** — `engine.py` references `MouseHook._VIRTUAL_DEVICE_SOURCES` instead of `BaseMouseHook` or shared device types.
5. **Test gaps** — No engine test for firmware skip on virtual source; no Windows attribution tests.

## Suggestions

- Call `_sync_logitech_scroll_monitor` on connect/disconnect, not every tap callback.
- CF object cleanup in `_LogitechScrollMonitor.start()`.
- Windows vendor match via device info vs name substring.
- Stubs should set `source="hidapi"` explicitly in tests.
- Soak-tune 50 ms window under KVM load.
