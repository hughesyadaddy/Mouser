# Settings window memory: engine teardown (M3) — what was built and what was measured

Scope: plan M3 (`docs/plan/2026-09-22-fix-fleet-hardening-plan.md`, Workstream M)
plus the R4/R6 audit findings folded into the same PR. Goal: idle ≤ 200 MB
with the settings window closed, ≤ 350 MB open, no growth.

## What is in the code

- `main_qml.py` `MainWindowHost`: owns the `QQmlApplicationEngine` for
  `ui/qml/Main.qml`. `ensure()` builds it (image providers, context
  properties, `launchHidden`), every hide arms a 30 s single-shot timer, and
  on fire — if the window is still hidden and no modal / key-capture overlay
  is open (`Main.qml` `shortcutsBlocked`) — every Python reference to a QML
  object is dropped, `engine.deleteLater()` runs and `gc.collect()` follows.
  `show_main_window()` rebuilds it visible. **Teardown is off by default**
  (`MOUSER_QML_TEARDOWN=1` enables it); see "Why teardown is opt-in".
- `GestureHudHost`: `ui/qml/GestureHud.qml` on a tiny second engine that is
  never torn down, so `Connections { target: backend }` keeps flashing
  gestures while the window engine is gone.
- `_MacOSQuitToTrayFilter.set_window()`; the filter tolerates a missing window.
- `uiState.currentPage` persists the page across re-creation.
- `Main.qml`: both pages are `Loader`s active only while current (no
  `|| item` stickiness); `dismiss()` clears the hotspot selection.
- `MousePage.qml`: device image decodes at display size
  (`sourceSize = size × Screen.devicePixelRatio`, no mipmaps); the action
  picker and the debug card are `Loader`s (`pickerLoader`, `debugCardLoader`).
- `HotspotDot.qml`: the page-sized `Canvas` leader line is a `Shape`
  (curve renderer, dashed); the pulse animation stops while the window is
  hidden.
- Dock icon (R4): set at most once per process from `images/AppIcon.icns`,
  never from a frozen bundle; the 0 / 250 ms refresh paths and the
  never-matching "is current" guard are gone. Every
  `NSApp.setApplicationIconImage_` call leaked a 32 MiB `CG image` Dock tile —
  that was the "3 × 32 MB CG image" mystery. `QApplication.setWindowIcon`
  is skipped on macOS too (`_install_window_icon`): libqcocoa forwards it to
  the same AppKit call, so even a frozen bundle paid one tile at startup.
  macOS has no title-bar icon and `CFBundleIconFile` covers the Dock.
- `_icon_request_size` clamps provider requests to 128 px.
- `dismiss()` clears the hotspot selection only when teardown is enabled
  (`windowTeardownEnabled` context property); hide-only mode keeps the
  previous UX (last hotspot / picker survive a re-open).
- Tools (all offscreen-only; none opens a real window):
  `tools/measure_qml_teardown.py` (lifecycle, seeds the `mx_master` layout so
  the page renders its image + 6 hotspots), `tools/measure_hotspot_dots.py`
  (N real `HotspotDot.qml` instances, fresh process per N),
  `tools/qt_window_leak_repro.py` (the per-window leak repro; its `--cocoa`
  flag is documented as focus-stealing and is for a human only).
- Tests: `tests/test_main_window_host.py` (live offscreen engine tests, re-run
  in a clean subprocess when a bare `QCoreApplication` already exists),
  `tests/test_status_item.py::DockIconTests`, `tests/test_icon_providers.py`.

## Measurements

`phys_footprint` (`proc_pid_rusage`), harness = real `Backend(engine=None)`
+ `UiState` + `LocaleManager`, real QML, no tray / hooks / installed app.
macOS 26.6, PySide6 6.11.0, Apple M3 Max, 2× display. **All numbers below
except the cocoa reference set were taken with `QT_QPA_PLATFORM=offscreen`
(software renderer), so they are lower than the real Metal path and valid
for relative comparison only.** The cocoa set was captured once before the
"offscreen only" rule and is not to be repeated on a seat.

### Lifecycle, offscreen, real page (mx_master layout: image + 6 hotspots)

`tools/measure_qml_teardown.py --cycles 2 --settle 1.5` (`--qml-dir` on a
pre-change snapshot for "before"). An earlier revision of this table was
taken with a disconnected backend, i.e. an empty page; these are the real
numbers.

| step | before (old QML) | after |
|---|---:|---:|
| baseline (app + backend) | 56.4 | 53.1 |
| + HUD engine | 61.3 | 58.5 |
| cycle 1 shown, settled | 144.6 | 111.3 |
| cycle 1 hidden | 145.4 | 111.3 |
| cycle 1 torn down (engine deleted) | 141.8 | 109.0 |
| cycle 2 shown, settled | 154.0 | 118.0 |
| cycle 2 torn down | 151.3 | 115.9 |
| re-shown (page restored) | 152.5 | 117.1 |

Window cost over the HUD floor: 83 → 53 MB (−36 %). Offscreen teardown
returns only ~2–3 MB because the software renderer holds no GPU resources;
the second cycle's +7–9 MB is the same warm-up seen in every configuration
(fonts, glyph cache, JIT) and does not continue.

### HotspotDot Canvas → Shape (`tools/measure_hotspot_dots.py`, offscreen, DPR 1)

Fresh interpreter per row; "over N=0" subtracts the harness floor.

| hotspots | Canvas (old) | Shape (new) |
|---:|---:|---:|
| 0 | 36.4 (floor) | 36.8 (floor) |
| 6 | +13.0 (2.17 each) | +5.5 (0.92 each) |
| 12 | +21.0 (1.75 each) | +7.5 (0.62 each) |

At DPR 2 the Canvas bitmap is 4× larger (audit R6 measured 4.8 MB per
hotspot on cocoa); the Shape cost is resolution-independent geometry.

### Cocoa / Metal reference set (captured once, pre-directive)

Full lifecycle, old QML, 2 cycles: baseline 62 → shown 179 → hidden 182 →
**torn down 151** → re-shown 197 → torn down 168. `vmmap` deltas
shown → torn down: `IOSurface` 34.9 → 1.2 MB (the 3 × 11.6 MiB
`CAMetalLayer` drawables go away), `MALLOC_LARGE` −5.9 MB,
`owned unmapped (graphics)` −140 to −268 MB. No `CG image` region exists in
this harness at all: the 32 MB bitmaps come from the Dock icon path, which
the harness never calls (fixed as described above).

Strategy comparison, fresh process each, 6 show/hide cycles, current QML
(`shown` = 1.2 s after show, `hidden` = 1.5 s after the action):

| strategy on hide | hidden floor per cycle (MB) | growth / cycle |
|---|---|---:|
| hide only | 154 154 154 154 154 154 | 0.0 |
| hide + unload pages | 151 155 158 158 158 159 | ~0 after warm-up |
| hide + unload + `releaseResources()` | 151 158 159 159 159 159 | ~0 |
| hide + unload + non-persistent SG/graphics | 149 151 152 152 153 153 | ~0 |
| `window.destroy()` | 128 141 152 165 177 188 | **+12.0** |
| engine teardown (this PR's mechanism) | 122 136 148 160 173 184 | **+12.4** |

A trivial `Window { Rectangle { Text {} } }` shows the same +11.8 MB per
create/destroy under `QSG_RHI_BACKEND=metal`, `=opengl`,
`QT_QUICK_BACKEND=software` and `QSG_RENDER_LOOP=basic`. 11.8 MB is exactly
one 2120 × 1400 BGRA display drawable. Offscreen, the same cycle is flat
(`tools/qt_window_leak_repro.py --cycles 6`: 40.6 40.6 40.6 40.5 MB, no
Python object growth either), so this is a per-native-window leak in
Qt 6.11 / PySide6 on macOS, not Mouser code. **No upstream report found**:
bugreports.qt.io searched 2026-09-22 for "QQuickWindow leak per window
macOS Metal 6.11", "CAMetalLayer drawable leak", "IOSurface leak
destroy"; the nearest hits are QTBUG-114721 (`nextDrawable` blocking the
render thread, not a leak) and forum threads about QML window
create/destroy growth without a resolution. Repro for a report: run
`tools/qt_window_leak_repro.py --cycles 8 --cocoa` (human at the keyboard
only, it opens a real window) and `--keep-window` for the `destroy()`
variant; both print +11.8 MB per cycle on the hardware above.

## Why teardown is opt-in

Teardown (or `destroy()`) buys ~30 MB once (154 → 122 hidden) and then
costs ~12 MB on every open/close forever; it is worse than hide-only after
three cycles and would trip the M4 growth watchdog on a user who toggles
the window. A plain hide is flat. So `MainWindowHost` ships with
`teardown_enabled=False` (env `MOUSER_QML_TEARDOWN=1` flips it) and the
30 s / modal-guard mechanism stays tested for a Qt that fixes the leak.
Re-check with `tools/measure_qml_teardown.py` after a PySide6 bump.

## Open observations (not fixed here)

- Instantiating `MousePage` while the window is visible allocates a
  transient +250–400 MB of `owned unmapped (graphics)` that settles back
  within a few seconds; hiding right after a page switch keeps it resident
  until the next frame (hidden 409 MB vs 160 MB settled). Worth an audit of
  MousePage's instantiation (text/glyph, Shape, images) in M5.
- The `Shortcut: Only binding to one of multiple key bindings` warning at
  `Main.qml` `StandardKey.Close` predates this PR.
