# Code Simplicity Review — KVM scroll invert

## Verdict

Acceptable complexity for the problem; macOS IOHID block is the largest cost. Consider deduplication follow-up, not blocking merge.

## Important

1. **Duplicated ctypes** — `mouse_hook_macos.py` re-binds IOKit/CoreFoundation already present in `hid_gesture.py` (~65 lines).
2. **Per-callback sync** — `_sync_logitech_scroll_monitor()` on every CGEvent is unnecessary; lifecycle hooks are cleaner.
3. **Engine DRY** — Physical check duplicated; should call `hook._physical_logitech_bound()`.
4. **Heuristic coupling** — macOS 50 ms IOHID↔CGEvent window is inherent complexity without public CGEvent device API.

## Suggestions

- Extract shared virtual-source constant to `mouse_hook_base` / device types (partially done).
- Long-term: reuse HidGesture wheel reports instead of second IOHID manager.
- Comment Windows buffer API choice to prevent mistaken merge with `GetRawInputData`.
