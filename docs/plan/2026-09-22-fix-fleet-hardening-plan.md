# fix: fleet hardening — Mouser memory, login-screen capitalization, stuck keys, single-launcher, signed deploys, settings survival

> Status: APPROVED 2026-09-22 — build input for `/build`.

## Context

Fleet: macbookpro (this Mac, usually Deskflow client), hackintosh (macOS VM 103, usually Deskflow server, owns the Logitech Bolt receiver), tiny11 (Windows VM 100). Two repos on branch `fleet/memory-program`: `~/Desktop/deskflow` (C++/Qt6) and `~/Desktop/Mouser` (Python + PySide6/QML + PyObjC). The 2026-09-16 memory program merged a set of fixes that are **not yet deployed** on any seat (hackintosh runs Mouser `ef43412`, macbookpro `4700b6a`).

The user's asks (2026-09-22):
1. Mouser memory: 6 GB on hackintosh; target ≤200 MB. Solve every leak.
2. LoginWindow: typing the password over the vhid bridge arrives **all lowercase** — Shift is lost. Also harden capitalization between machines.
3. No hotkeys/modifiers may ever stay stuck when the cursor switches machines.
4. Deskflow must never start twice on macbookpro ("already started" then starts again); login items current on every seat; exactly one launcher.
5. Deploys signed with the user's Apple Development / Authenticode credentials on every seat — never ad-hoc.
6. Mouser must tie into Deskflow with auto-reconnect / auto-apply on every path (core restart, role flip, unplug, sleep, login window, seat switch).
7. Reinstalling Deskflow or Mouser from the deploy scripts must never lose Mouser settings.
8. Brutal line-by-line adversarial audit of Mouser for memory; adversarial audits for 2–3.

## Established findings (MEASURED 2026-09-21, four adversarial agents)

### Mouser memory — the 6 GB is two ObjC/CF leaks, not Python objects
`heap` on hackintosh pid 3191 (ef43412, 81 h): 63 M objects / 5.87 GB.

| Leak | Live objects | Bytes | Rate | Root cause |
|---|---|---|---|---|
| A. CGEvent retained per **pass-through** tap event | 3.2 M CGEvent + 3.2 M CGSEventAppendix + 9.4 M HIDEvent | ~1.8 GB | 11/s | **PyObjC bug**: `Quartz/CoreGraphics/_callbacks*.so` `_m_CGEventTapCallBack` never `Py_DECREF`s the Python callback's return on the success path. `core/mouse_hook_macos.py:223-473` `return cg_event` → proxy (and its CFRetain) leaks. `return None` paths do not leak. |
| B. NSXPCConnection per frontmost-app query | 729 k NSXPCConnection + GPProcessMonitor/GPProcessInfo/GPAutoEDRParameters + queues/locks | ~1.9 GB | 2.5/s | `core/app_detector.py:184-199` polls `NSWorkspace.frontmostApplication().bundleIdentifier()` every 300 ms; on macOS 26 each `NSRunningApplication` query creates a GamePolicy XPC connection that is never invalidated. The existing `objc.autorelease_pool()` is irrelevant (leak is below the pool). |

A+B ≈ 12.7 KB/s ≈ 3.7 GB of the 5.0 GB MALLOC_NANO; remainder is their untyped payload buffers. Both run only on the seat that owns the tap/device → macbookpro flat (~0.2 MB/min), hackintosh 1–3.5 MB/min scaling with mouse activity. Uptime is not the variable.

HEAD (`d652305`) status:
- A: fixed **only when** `native/mac/mouser_tap.m` loads (`core/mouse_hook_macos.py:923-940`). Python tap remains a silent fallback (`_start_python_tap` :942); `Mouser-mac.spec:98-118` builds the dylib with `check=False` → a clang failure ships the leaking path with no error.
- B: poll replaced by `NSWorkspaceDidActivateApplicationNotification` (1a6b731), but 6cf994a added a 5 s idle watchdog (`core/app_detector.py:390-401` `_read_foreground()`) → ~1.9 MB/h, 19× the ≤0.1 MB/h bar; plus one `bundleIdentifier()` per activation (:225-236).
- No test in either tree measures memory or object counts over time.
- No `MallocNanoZone`/`PYTHONMALLOC`/`gc.*` tuning anywhere (irrelevant to these leaks).

Other measured facts:
- macbookpro Mouser (4700b6a): one thread at 100 % CPU for 27 h (closed-sink spin, fixed in HEAD, undeployed); host in swap. Footprint 406 MB, peak 844 MB; **3 × 32 MB CG image bitmaps** (display-sized) unexplained.
- hackintosh morning HID re-enumeration storm (27.8 k cycles at 3/s, 07:00–09:02) is CPU/log only (1.7 k HIDElement live); `_wait_reconnect` returns immediately while `_deskflow_attach` is set (ef43412 :3229-3246, still in HEAD :3850).
- Cold baselines (macbookpro, .venv): Python+Qt+PyObjC+hid ≈ 50 MB; QQmlApplicationEngine + rendered QtQuick Controls window ≈ +175 MB; **`hide()` releases 0**; deleting the engine returns ~110 MB. Mouser loads the 5.5 k-line `Main.qml` at startup, only ever hides it, and keeps a second always-alive HUD `Window`. → Floor as architected ≈ 250–300 MB; **≤200 MB requires tearing down the QML engine on window close**.

## Findings from explorers

### Login-screen capitalization (READ + MEASURED)
- The login window is served by `deskflow-vhid-bridge` (`src/apps/deskflow-vhid-bridge/deskflow-vhid-bridge.cpp`, 2016 lines), a standalone Deskflow-protocol client run as root in the LoginWindow session (`/Library/LaunchAgents/org.deskflow.vhid-bridge.plist`, `scripts/install-login-bridge-macos.sh:26-31,222-231`), posting raw HID reports to Karabiner DriverKit (`VirtualHidSink` :739-808). No CGEventPost anywhere in that path.
- Key→HID: `on_key_down` :1526-1587. Usages are always unshifted (`ascii_to_physical_usage` :816-885 maps `K` and `k` to 0x0E); shift lives only in the modifier byte (`BridgeCalibration.h:39-60`, left-side bits only). One atomic report per change (`emit_keyboard` :1712). Caps is an edge (`sync_caps_lock` :1596-1621).
- **Root cause (high confidence): line 1578** — for letters the bridge *discards the protocol's Shift bit* (`entry.modifier_bits &= ~left_shift`) and re-derives shift from `keyid_requires_shift(id) XOR target_caps_lock_state()`. Lowercase results whenever (1) the server sends a base KeyID (`'k'`) with mask 0x0001 — Windows `ToUnicodeEx` fallbacks, relay-normalised masks, dead-key/AltGr paths — or (2) `target_caps_lock_state()` (:1051) reads a wrong `true` from the **Karabiner virtual keyboard's own** `HIDCapsLockState` cached in a process-lifetime `static HidServiceHandle` (:1010), which the bridge itself toggles (:1616). Symbols keep the mask's shift → matches "letters all lowercase".
- Old logs on both seats show uppercase *did* work (`id=0x0044 mask=0x0001 → mods=0x0002`), but commit `97970b0aa` removed mask/mods from the key-down log (:1580-1583) — the failure is now **unobservable** on the seat where it happens.
- Live: installed bridge binaries are stale (macbookpro Sep 17, hackintosh Aug 31); both plists lack `--calibrate` (installer not re-run since `3b5cebf7b`); macbookpro bridge log tail = `vhid connect_failed: 61` (Karabiner daemon refusing) + `getaddrinfo` failures — the bridge is currently reaching neither daemon nor servers. Bridge log mode 644 (world-readable; previously leaked keystrokes).
- Normal session path is different: `OSXKeyState::fakeKey` :931 → `postHIDVirtualKey` / `postKeyboardKey` :918-929 with `CGEventSetFlags(getKeyboardEventFlags())` from shadow state; caps desync handling :715-735; server uses `Server::syncToggleStateToActive()` (`Server.cpp:227`) — no `kMsgCKeyboardSync`. Windows server `MSWindowsKeyState::getKeyID` :1443 uses `ToUnicodeEx` (:1473) so it should send `'K'`; `:688-689` strips Shift for shift-space (IME).
- Tests: `BridgeCalibrationTests.cpp` covers only the pure header helpers; **nothing tests `on_key_down`'s letter branch** (it lives in the .cpp, untestable as-is).

### Stuck keys on switch (READ)
- Server: `Server::switchScreen` `Server.cpp:723,789-802` — `leave()` then `releaseKeysHeldOnActive()` (:197-209, sends `kMsgDKeyUp` for the `m_keysHeldOnActive` ledger). **Gap a**: `kMsgCLeave` hits the wire *before* the key-up batch; a client that tears down on CLeave discards them (masked today only because clients self-release). Primary-held keys are swept only on `Screen::disable` (`Screen.cpp:176-184`).
- Client: `Screen::leaveSecondary` `Screen.cpp:583-588` → `KeyState::fakeAllKeysUp` `KeyState.cpp:973-986` releases only `m_syntheticKeys`; `OSXKeyState::fakeAllKeysUp` :860-865 then clears `m_injectedModifiers`. **Gap b1**: modifiers the OS holds that aren't in `m_syntheticKeys` are forgotten, not released; `sanitizeInjectedKeys` :818-845 only runs on `disable`. **Gap b2**: `setKeyboardModifiers` :868-889 has **no Fn/Globe case** and the sanitize loop iterates only shift/control/alt/super — CapsLock and Fn/Globe held across a switch have **no release path anywhere**. Fallback `postKeyboardKey` sets flags from the shadow state, so a stale shadow re-asserts a flag on the release event.
- Return: `Screen::enterSecondary` :538-572 is defensive (skips modifiers the OS already holds :561, one modifier at a time :553-557); `applyToggleMask` applies lock bits absolutely (by design). `enterPrimary` :575-580 re-reads OS.
- Bridge: `release_all()` :1730-1737 on `kLeave`, run start/end, Esc rescue — most robust path.
- History: `docs/plan/2026-07-20-fix-stuck-modifiers-caps-relay-plan.md`, `docs/brainstorm/2026-08-31-fleet-hardening-audit-brainstorm-doc.md`, runbook `64e07611e`, ~14 stuck-key commits (`1c27dc7ff`, `b9f0a25ee`, `93b803b8d`, `063007c85`…).
- **Tests gap**: no test names `fakeAllKeysUp`, `leaveSecondary`, or `enterSecondary`; CLeave/DKeyUp ordering, `m_reassertedModifiers` lifecycle and the Fn/Globe hole are untested. Existing: `ServerKeyLedgerTests.cpp`, `KeyStateLedgerTests.h`, `KeyboardRouterTests.cpp`, `KeyboardRescueTests.cpp`, `OSXKeyStateTests.cpp`.

### Launch ownership / duplicate start (READ + MEASURED)
- Launchers in repo: `deskflow-ctl start` (`scripts/deskflow-ctl:319-335`), GUI LaunchAgent template (`tools/launchd/io.github.hughesyadaddy.deskflow.plist:33-37`, sets `DESKFLOW_LAUNCHD=1`), core LaunchAgent, converge agent (60 s), prio LaunchDaemon, login bridge, **SMAppService self-registration** (`src/lib/gui/OSXHelpers.mm:131-144` ← `MainWindow.cpp:177-186`), GUI spawning core via QProcess/kickstart (`CoreProcess.cpp:289-309,523,539`).
- **Root cause of "already running → exit" on macbookpro (MEASURED)**: three registrations launch the GUI at login — the LaunchAgent + **two enabled BTM app login items** for the same bundle (`2.io.github.hughesyadaddy.deskflow` and orphaned `2.org.deskflow.deskflow`, both `/Applications/Deskflow.app`). The BTM/LaunchServices copy wins (pid 2141, `managed_by = com.apple.runningboard`, holds `gui.lock`). The LaunchAgent copy reaches `deskflow-gui.cpp:116-140`, attempts takeover — but `macLaunchdOwnsGui()` (`OSXHelpers.mm:160-164`) is a **plist-existence test** so the incumbent also says "launchd owns me", `MainWindow.cpp:140` set `honoursQuit=false`, `InstanceHandoff.cpp:46` downgrades quit→show; takeover fails → `"%1 is already running"` → `s_exitDuplicate` (=5; `launchctl print` shows `last exit code = 5`). Self-cleanup at `MainWindow.cpp:177-183` calls `macSetStartAtLogin(false)` = `[SMAppService mainAppService]` only → cannot remove the `org.deskflow.deskflow` record.
- Compounding on macbookpro: installed GUI plist stale (Sep 16, no `DESKFLOW_LAUNCHD=1`); **converge agent not installed**; core is launchd-owned (pid 2117 ✅). Remnants: `/usr/local/bin/deskflow-prio-apply.sh`, `~/Library/Logs/deskflow-keepalive.log`.
- hackintosh: **no LaunchAgents at all**; GUI launched by BTM (pid 3113, 4 d), core is a **child of the GUI** (ppid 3113) → `deskflow-ctl assert-single` would fail (`:594`); prio daemon points at retired `/usr/local/bin/deskflow-prio-apply.sh` and is not running; stray `/Library/LaunchAgents/com.symless.synergy-agent.plist` symlink; no `~/Library/Logs/Deskflow`.
- tiny11: SCM service `Deskflow` AUTO_START (LocalSystem) → `deskflow-daemon.exe` → `deskflow-core.exe`; HKCU Run `Deskflow` GUI (written by `MainWindow.cpp:192-201`); one launcher each — fine.

### Signing per seat (MEASURED)
| Seat | Deskflow | Mouser | Notes |
|---|---|---|---|
| macbookpro | 72/72 Mach-O Apple Development (J5KPG8ZR5C), hardened runtime | 233/233 Apple Dev | ✅ |
| hackintosh | 71/71 Apple Dev but `flags=0x0` — **no hardened runtime** (stale build) | 230/230 Apple Dev | behind current build |
| tiny11 | 4 exes valid, `CN=Deskflow Fleet Code Signing` thumb `FBB4…FAEC` — **self-signed** (Subject==Issuer, exp 2036), trusted only via local root store | Mouser.exe same | not a CA-issued Authenticode identity |
- Nothing is ad-hoc today. Deploy scripts already hard-fail on empty/`-` identity (`fleet-deploy-macos.sh:160-166,112,243`; `install-macos.sh:131-158`; `sign-windows.ps1:24-25,218-230`; Mouser `build_and_install.py:218-267`). **Only escape hatch**: `cmake/MacCodesign.cmake:20-27` downgrades to ad-hoc with a WARNING if `-DFLEET_STRICT_SIGNING=OFF`, and a direct cmake build bypassing `install-macos.sh` would install it unchecked. `sign-windows.ps1 -NoTimestampOk` (:38) yields signatures that die with the cert.

### Mouser settings persistence (READ + MEASURED)
- All state lives outside the bundle: macOS `~/Library/Application Support/Mouser/{config.json,config.json.bak,bridge.token,last_device.json,mouser.lock,mouser.sock}` (`core/config.py:16-24,307-328`; atomic `os.replace` + fsync + `.bak` copy every save; schema v12 with forward-only `_migrate()` `:396-513`; corrupt → recover from `.bak` → defaults). Windows `%APPDATA%\Mouser\...`.
- Installers only touch the bundle: `scripts/build_and_install.py:323-325` rmtree+ditto `/Applications/Mouser.app`; `scripts/windows_install.py:261-296,315-350` replaces install root with a timestamped backup; `deskflow/scripts/fleet-deploy-macos.sh:284-322 deploy_mouser` touches no settings dir. **No installer ever writes or resets config** (`load_config()` is create-on-first-use).
- Live: config.json present and current on all three seats; hackintosh also has manual snapshots; stale `io.github.tombadash.mouser.plist` on both Macs (old bundle id, harmless).
- Verdict: **already reinstall-safe**; what is missing is a *proof* — a deploy-time settings hash check and a test that runs the installer against a fixture and asserts the settings dir is byte-identical.

### Mouser ↔ Deskflow reconnect coverage (READ)
- Mouser owns `127.0.0.1:19795` (`core/bridge_server.py`); Deskflow dials it from `src/lib/deskflow/MouserLink.{h,cpp}` — one process-wide, role-agnostic link owned by `AutoModeRunner` (`MouserLink.h:6-11`), so role flips/screen switches never drop it. Backoff 1→30 s, ping 2 s/3 misses, retries forever (`MouserLink.cpp:464-473,566-572`).
- Mouser side: legacy dial-out ladder 0.25→2 s re-reading host each attempt (`core/remote_forward.py:206-218`, `engine.py:1288-1290`); HID `_wait_reconnect()` interruptible on attach/arrival (`hid_gesture.py:3838-3893`); state re-applied after reconnect: Smart Shift, DPI, wheel-invert with retry pass (`engine.py:747-850`); ingress starts paused until focus known (`bridge_server.py:765-768`).
- Coverage matrix: core-not-running ✅, core restart ✅, role flip ✅, unplug/replug ✅, sleep/wake ✅, Mouser restart ✅, two Mousers ✅ (`single_instance.py`), seat switch mid-gesture ✅ (`_STALE_HOLD_LIMIT` :3908). **Gaps**: lock/user-switch only partially (`_on_session_resign` logs only, `mouse_hook_macos.py:836-840`); Windows service-context core cannot read per-user `%APPDATA%` token; native-tap build/load failure is silent (`Mouser-mac.spec:112 check=False`, `mouse_hook_macos.py:938`). Stale doc: `MouserLink.h:109` still says `%LOCALAPPDATA%` (fixed in code `:237-244`).
- Tests exist for every path (`tests/test_hid_deskflow_backend.py`, `test_hid_gesture_reconnect.py`, `test_hid_reconnect_backoff.py`, `test_remote_forward.py`, `test_remote_end_to_end.py`, `test_deskflow_listener_ingress.py`, `test_deskflow_integration.py`, `test_self_watchdog.py`; deskflow `MouserLinkTests.cpp`). CI = `python -m unittest discover -s tests` (`.github/workflows/ci.yml:40`), no pyproject/Makefile.

### Memory guards in HEAD (READ)
- `self_watchdog.py` samples CPU ratio / tap re-enables / tick drift only — **no RSS/footprint**. `deskflow/tools/fleet-soak:546-558` does sample `phys_footprint` for mouser; `fleet-health` is liveness-only.
- Python version = whatever the seat `.venv` holds (3.12 hackintosh, 3.13 macbookpro; `build_and_install.py:111-126`); PyObjC unpinned (`requirements.txt:5-6`, `>=10.0`), no lockfile.

## Decisions (user, 2026-09-22)
- Memory target: **≤200 MB idle (window closed) via QML engine teardown on close + recreate on open**; ≤350 MB window open; growth ≤0.1 MB/h.
- Windows signing: **keep the self-signed fleet cert** (`CN=Deskflow Fleet Code Signing`, FBB4…FAEC), harden it (timestamp mandatory, per-exe thumbprint verify, fleet-health check). No CA cert purchase.
- Orphaned BTM record `2.org.deskflow.deskflow` on macbookpro: **manual removal in System Settings → General → Login Items**; scripts detect via `sfltool dumpbtm` and block until clean. No `resetbtm`.

## Plan

Ground rules for the build phase (unchanged from the memory program): builders work in git worktrees, never install/deploy/`launchctl`/`sudo`/touch `/Applications` or live processes; every PR gets an adversarial reviewer; deploy only when the user gates it via `scripts/fleet-deploy.sh` from macbookpro. Test frameworks: deskflow = QTest (`private Q_SLOTS`, ctest) + bats (`tools/tests/*.bats`) + Pester; Mouser = `python -m unittest discover -s tests`.

### Workstream M — Mouser memory (target ≤200 MB idle, ≤350 MB open, ≤0.1 MB/h growth)

**M1. Leak A (CGEvent via PyObjC trampoline)** — root cause is upstream; belt-and-braces:
- `Mouser-mac.spec:98-118`: `check=False` → `check=True`; `_native_tap_binaries` raises `SystemExit` when the dylib is missing (no more `[]` + warning). Test in `tests/test_native_hook_build.py`.
- `core/mouse_hook_macos.py:922-962`: keep the Python-tap fallback (Mouser is not launchd-managed, `tools/fleet-health:309-313`, so refusing to start = dead seat) but log `[MouseHook] DEGRADED: python tap in use`, expose `hook.tap_kind = "native"|"python"` for the status line, and apply a **deferred-release guard** in the Python tap: store `_prev_passthrough = cg_event` on every pass-through; at the next callback entry call `ctypes.pythonapi.Py_DecRef` on it **only if** `sys.getrefcount(prev) == 3` (attr + getrefcount arg + the leaked ref). Never `Py_DecRef` before `return cg_event` — the trampoline still dereferences the result after the arg tuple is freed (use-after-free). Version-independent: if PyObjC fixes upstream the count is 2 and nothing happens.
- `native/mac/mouser_tap.m` audited clean (no per-event CFRetain/Copy; 256-slot fixed ring; autorelease pool on the run loop). No change.
- Pin `pyobjc-*==12.1`, `PySide6==6.11.0`, Python 3.13 (`requirements.lock`, `.python-version`, `.github/workflows/ci.yml:16` 3.12→3.13); `scripts/build_and_install.py:164` `log_python_provenance` → `verify_python_provenance` that fails on minor/PyObjC/PySide mismatch (hackintosh's `.venv` is 3.12 today — must be rebuilt). Test in `tests/test_build_and_install.py`.
- File the upstream bug (`pyobjc-framework-Quartz/Modules/_callbacks.m`, `m_CGEventTapCallBack` missing `Py_DECREF(result)`); reference it in the guard's comment.

**M2. Leak B (NSXPCConnection per NSRunningApplication query)** — LaunchServices-free foreground detection:
- New `core/macos_frontmost.py` (ctypes, no PyObjC): `AXUIElementCreateSystemWide` → `kAXFocusedApplicationAttribute` → `AXUIElementGetPid` (CFRelease both); fallback `CGWindowListCopyWindowInfo` first on-screen `kCGWindowOwnerPID`. pid → bundle id via `proc_pidpath` → nearest `*.app/Contents/Info.plist` `CFBundleIdentifier` (plistlib), 64-entry LRU keyed `(pid, path, mtime)`, evicted on `NSWorkspaceDidTerminateApplicationNotification`.
- `core/app_detector.py`: activation handler (:213-241) reads only `processIdentifier()` from `userInfo[NSWorkspaceApplicationKey]` — never `bundleIdentifier()`/`frontmostApplication()`; `_run_observer` (:385-405) watchdog becomes AX-only pid compare at 30 s; delete `_read_foreground()`'s LS path; both funnel through `_deliver(pid)`.
- Test (`tests/test_app_detector.py`, extend `_FakeRunningApp` with call counters): 10 000 idle ticks + 1 000 activations over 5 pids → `bundleIdentifier`/`frontmostApplication` calls == 0, resolver calls ≤ 5.

**M3. QML floor — engine teardown on close (user decision)**
- `main_qml.py:1524-1605`: extract `MainWindowHost(QObject)`: `ensure()` builds the engine (image providers kept as Python refs, the 8 `setContextProperty`s :1533-1541, `load(Main.qml)`, `visibilityChanged`, re-bind `_MacOSQuitToTrayFilter` via `set_window`); `hide()` arms a 30 s `QTimer`; on fire, if no key-capture dialog/modal is open → drop every Python ref to QML objects, `engine.deleteLater()`, `engine=None`. `show_main_window` calls `ensure()` first (recreate always visible; `launchHidden` context prop must reflect this).
- Persist `currentPage` (`Main.qml:31`) as a `backend` Q_PROPERTY so re-open restores the page.
- Move `Window { id: gestureHud }` (`Main.qml:648-703`) to `ui/qml/GestureHud.qml` on a tiny second engine that never tears down.
- `MousePage` behind a `Loader` like ScrollPage; drop the `|| item` stickiness at `Main.qml:275`; `MousePage.qml:988-1000` `Image` gets `sourceSize` = `width*Screen.devicePixelRatio`.
- Risk list to cover in review: `QTimer.singleShot` lambdas holding `root_window` (:741-742, :796-800, :1573) → resolve via `host.window()`; `_MACOS_QUIT_FILTER` global (:1604); set every Python ref to QML objects to `None` before `deleteLater()`; tray menu is Python-owned (:1628) and unaffected.
- Diagnosis task (not a fix): the 3×32 MB display-sized bitmaps on macbookpro — `vmmap --summary` before/after teardown, `QSG_INFO=1`, `QSG_RHI_BACKEND=opengl` A/B; if they are Metal drawables of the HUD window they go away with the HUD refactor.

**M4. Prevention**
- `core/self_watchdog.py`: sample `phys_footprint` via ctypes `proc_pid_rusage(RUSAGE_INFO_V4)` (copy `deskflow/tools/fleet-soak:196-255`) each tick, 60-sample deque, least-squares slope, log `[mem] footprint_mb=… growth_mb_h=…`; trip when growth > 20 MB/h for 3 h or footprint > 1.5 GB (existing trip/exit policy: exit only when launchd-owned, else `statusMessage` + log). Extend `_Fixture` in `tests/test_self_watchdog.py`.
- `tests/test_memory_soak.py` (unittest, Linux-safe via `tests/support/fake_mouse_hook.py` harness): 10 000 events through `_event_tap_callback`, 10 000 activations/idle ticks through `AppDetector`, 10 000 `MouseEvent`s through `Engine._dispatch`; after `gc.collect()` assert `gc.get_objects()` delta ≤ 100 and `tracemalloc` delta ≤ 256 KB; macOS-only case drives a real `Quartz.CGEventCreate(None)` proxy through the deferred-release guard 10 000× asserting a stable refcount (proves no over-release).
- `tools/mouser-heap-classes` (stdlib): parse `heap -s <pid>` → JSON counts for `CGEvent, CGSEventAppendix, HIDEvent, NSXPCConnection, GPProcessMonitor, CGImage`; `deskflow/tools/fleet-soak --heap-classes` per-sample field + `--class-slope-max 10/h` report rule (needs `get-task-allow` entitlement note in `build_resources/Mouser.entitlements` or sudo).
- Deploy gate in `deskflow/scripts/fleet-deploy-macos.sh deploy_mouser` (:284-322): record log size before install; after, poll ≤60 s for `CGEventTap created (native tap:` past that offset; `fail` if absent or if `CGEventTap enabled on its own run loop` appears. Same in `build_macos_gui_session.py` path.

**M5. Brutal line-by-line audit (build phase)** — six adversarial reviewer agents, ~6 k lines each, macOS-relevant files only:
| Reviewer | Files (lines) |
|---|---|
| R1 | `core/hid_gesture.py` 4093, `hid_sink.py`, `hid_deskflow_backend.py` |
| R2 | `core/mouse_hook_macos.py` 1279, `mouse_hook_base.py` 991, `native_hook_mac.py` 502, `macos_iokit_scroll.py` 406, `native/mac/mouser_tap.m`, `mouse_hook_types/contract/stub` |
| R3 | `core/key_simulator.py` 1927 (21 `CGEventCreate*/Post` sites), `engine.py` 1408, `app_detector.py`, `accessibility.py` |
| R4 | `ui/backend.py` 2436, `main_qml.py` 1783, `ui/locale_manager.py` 991, `ui/macos_screenshot.py` 431, `screenshot_common.py` |
| R5 | `core/bridge_server.py` 915, `single_instance.py` 824, `remote_device.py` 642, `remote_forward.py` 357, `logi_devices.py` 989, `logi_device_catalog.py` 965, `deskflow_integration.py` |
| R6 | `core/app_catalog.py` 1040, `update_installer.py` 1120, `config.py` 550, `startup.py` 662, `updater.py`, `self_watchdog.py`, `log_setup.py`; QML `Main.qml`, `MousePage.qml` 2692, `ScrollPage.qml` 1488 |

Checklist per file: per-event allocations; PyObjC proxies stored beyond the call; unbounded dict/list/deque/Queue; QObjects without parents; `connect` inside loops/handlers; QTimers created per call; threads touching Foundation without `objc.autorelease_pool()`; CF objects from ctypes without `CFRelease`; IOHID/hidapi handles not closed on reconnect; QML `Connections`/`Loader`/`Image` without `sourceSize`. Finding format: `file:line — what — bytes × rate × 3600 = MB/h — fix`. Builders fix; a second reviewer verifies each fix with `test_memory_soak` plus a 2 h `fleet-soak` on one seat. Known items to close in this pass: `_wait_reconnect` immediate return while `_deskflow_attach` is set (`hid_gesture.py:3850`) — add the 1→30 s backoff there too; `_on_session_resign` logs only (`mouse_hook_macos.py:836-840`) — pause tap/HID on resign, resync on activate.

### Workstream K — Keyboard: login-screen capitalization + stuck keys (deskflow)

**K1. Bridge capitalization** (`src/apps/deskflow-vhid-bridge/`)
- Move `keyid_requires_shift`, `keyid_is_letter` (:933-971) and a new pure `decide_letter_modifiers(id16, mask32, std::optional<bool> localCaps) → {uint8_t bits; bool capsEdge}` into `BridgeCalibration.h`. Rule for letters: `M = mask & CapsLock` (server truth), `S = mask & Shift`; `wantUpper = isUpper(id) || (S && !M)`; if `localCaps` known and `localCaps != M` → request a caps edge first (via existing `sync_caps_lock` :1600, ≤1 edge per key-down), then `shift = wantUpper XOR M`. Non-letters unchanged. This never lowercases an uppercase KeyID, still uppercases a base KeyID that arrives with Shift held (defensive against any sender path), and keeps Shift+Caps (`k`) lowercase.
- `target_caps_lock_state()` (:1008-1060): order `cg-flags` (:1035) → `iohidsystem` → virtual-keyboard service **last and uncached** (drop the `static HidServiceHandle` :1012; hold it on `VirtualHidSink` and reset in the `connected` slot :746). `on_enter` (:1389): parse the enter mask and call `sync_caps_lock(0, mask, 0)` so caps is synced at every crossing.
- Rewrite `on_key_down` :1574-1584 to call the pure function.
- Observability without keystroke logging: counters `letters_shifted_/letters_unshifted_/caps_edges_` printed in the enter/leave/`warn_stuck_keys` lines and at disconnect (`run()` :1191): `[keys] session letters shifted=N unshifted=M caps-edges=E`; `--debug-keys` prints `mask=… mods=…` only (never id/usage — preserves `97970b0aa`).
- Tests (`BridgeCalibrationTests.cpp`, new `letterModifiers_data`): `0x44/0x0001→0x02`, `0x21/0x0001→0x02`, `'k'/0x0001→0x02`, `'K'/0x0000→0x02`, `'K'/0x1000→0x00`, `'K'/0x1000,local=false→0x00+edge`, `'K'/0x0000,local=true→0x02+edge`, `'k'/0x1000→0x02`, `'k'/0x1001→0x02` (server caps on + Shift held: target caps on + Shift = lowercase), `CapsLock id→0x00`.
- Cross-machine vectors (normal session): `KeyMapTests`: `mapKey('K', desired=0)` presses Shift; `mapKey('k', desired=Shift)` → no Shift keystroke; `mapKey('K', desired=Shift|Caps)` → no extra caps press (`KeyMap.cpp:505-530`). `OSXKeyStateTests`: `getKeyboardEventFlags()` carries Shift for `'K'` with caps on. `MSWindowsKeyStateTests`: `VK_CAPITAL` toggled → `'K'` + `KeyModifierCapsLock` in mask.

**K2. Stuck keys**
- `Server.cpp:793-798` `switchScreen`: for `m_active != m_primaryClient` call `releaseKeysHeldOnActive()` **before** `m_active->leave()` (ClientProxy1_0::leave never refuses); keep leave-first for the primary. No protocol bump.
- `Screen.cpp:582-588` `leaveSecondary`: add `m_screen->sanitizeInjectedKeys()` after `fakeAllKeysUp()` (Windows already does at `MSWindowsScreen::leave` :335-341). `Screen.cpp:533` `enterPrimary`: call `sanitizeInjectedKeys()`.
- `OSXKeyState.cpp`: `sanitizeInjectedKeys` :797-852 loops add `s_capsLockVK` (release only if `m_capsPressed` was injected — never on OS-reported lock) and Fn (`kVK_Function` 0x3F / `NX_SECONDARYFNMASK`); add the Fn case to `modifierFlagForVirtualKey` (:618) and `setKeyboardModifiers` (:867); new `m_fnPressed` in `OSXKeyState.h:246-250` + `reseedShadowFlagsFromOS` (:653).
- `MSWindowsKeyState.h:66` `injectedKeyCandidates`: add `VK_LWIN/VK_RWIN`, `VK_CAPITAL` (ledger-gated).
- Post-switch verifier in the client: `Screen::enterSecondary` (:538) arms a 250 ms one-shot via `m_events` → log `[keys] post-switch held=0x%04x` (`pollActiveModifiers() & ~lockMask`); if non-zero and no key-down since (`m_lastKeyDownAt`) re-arm 2 s → `sanitizeInjectedKeys()` + `[keys] stuck-release 0x%04x`. `tools/fleet-health:693` greps `stuck-release` (FAIL if any in 24 h).
- Tests: `KeyStateTests::fakeAllKeysUp_releasesOnlySyntheticThenReseeds`; `OSXKeyStateTests::sanitizeReleasesInjectedFnAndCaps`, `sanitizeLeavesOsCapsLockAlone`; `ServerKeyLedgerTests::switch_releasesBeforeLeave` (RecordingClient :255 records leave index; every `Up` precedes it).

**K3. Bridge ops**
- `deskflow-vhid-bridge.cpp:1948-1951`: `wait_ready(10 s)` + `return 1` → unbounded wait with logged progress (pqrs client retries internally), `--vhid-wait-s` flag; this is the `connect_failed: 61` (ECONNREFUSED — daemon not up yet at loginwindow start).
- `scripts/install-login-bridge-macos.sh`: keep IP fallbacks next to hostnames in the plist (getaddrinfo failures pre-network); it already renders `--calibrate` (:127) and chmod 600 (:246) — the seats are simply stale.
- `tools/fleet-health`: new check `loginbridge` — newest `virtual HID ready` line ≤30 s after the newest loginwindow start, no `connect_failed` after it, log mode 600, plist contains `--calibrate`.

**K4. Adversarial audit pairs**: A `deskflow-vhid-bridge.cpp` 2016 + `BridgeCalibration.h` (every `modifier_bits` write, every cached HID handle :249/:1012, every `return true` in `dispatch`/`on_key_down`); B `KeyState.cpp` 1174 + `KeyMap.cpp` 1241 (every `m_mask` clear, `desired` vs `required` Shift/Caps :505-530, :740-750); C `OSXKeyState.cpp` 1278 + `MSWindowsKeyState.cpp` 1512 (every `m_*Pressed`/`m_injectedModifiers` set/clear, every VK loop); D `Screen.cpp` 596 + `Server.cpp` 3037 (every `return` in leave/enter/switchScreen, every `releaseKeys*` caller).

### Workstream L — Single launcher, signing, settings proof (deskflow + Mouser)

**L1. GUI launch ownership (C++)**
- New pure `src/lib/gui/LaunchOwnership.h`: `decideLaunchOwnership(envLaunchd, agentPlistInstalled, loginItemEnabled) → {isLaunchdProcess, honoursQuit, mayTakeOver, loginItem∈{None,Unregister}}` with `isLaunchdProcess = honoursQuit' = mayTakeOver = envLaunchd`, `honoursQuit = !envLaunchd`, `loginItem = (plist||env) && enabled ? Unregister : None`. **Never Register** — delete the register branch at `MainWindow.cpp:182-185`.
- `OSXHelpers.mm:160-164`: `macLaunchdOwnsGui()` = env `DESKFLOW_LAUNCHD==1` only; add `macGuiLaunchAgentInstalled()`. `deskflow-gui.cpp:117` takeover only when `mayTakeOver`; `MainWindow.cpp:140,177-186` consume the decision. The launchd copy wins (KeepAlive + converge respawn it); the BTM copy honours `quit` and dies. `CoreProcess.cpp:283` `macLaunchdOwnsCore()` stays plist-based on purpose.
- QTest `src/unittests/gui/LaunchOwnershipTests.cpp`: `BtmCopyWithPlistHonoursQuit`, `LaunchdCopyNeverHonoursQuit`, `LaunchdCopyMayTakeOver`, `BtmCopyNeverTakesOver`, `PlistInstalledUnregistersLoginItem`, `NeverRegisters`, `NoPlistNoEnvLeavesLoginItemAlone`.

**L2. `scripts/deskflow-ctl`**
- New verb `login-items audit|print-steps`: pure awk `parse_btm_dump` over `sfltool dumpbtm` stdin → `identifier\ttype\tdisposition\turl`; policy: the four fleet agent labels allowed; any enabled app/login-item record for deskflow|synergy|barrier → FAIL with the exact manual step; >1 app record → FAIL; dumpbtm privilege error → SKIP with sudo hint (never PASS). Try `osascript -e 'tell application "System Events" to delete login item "Deskflow"'` first (works only for legacy LSSharedFileList records) then re-dump.
- `cmd_assert_single` (:580-603) adds: GUI pid == `agent_pid "$GUI_LABEL"`; core `ppid == 1`; no `application.io.github.hughesyadaddy.deskflow.*` in `launchctl print gui/$UID`; `login-items audit` rc 0; no retired files. `cmd_converge` (:489-556) runs the audit; FAIL → `CONVERGE_ERRORS` → exit 1 + toast.
- `bootstrap_agent` (:240-253): stale **and loaded** → `bootout` then `bootstrap` (today a stale loaded definition is kept — macbookpro's GUI plist without `DESKFLOW_LAUNCHD=1`).
- New verb `retire`: rm `~/Library/Logs/Deskflow/deskflow-keepalive.log`; print root steps for `/usr/local/bin/deskflow-prio-apply.sh` and `/Library/LaunchAgents/com.symless.synergy-agent.plist`.
- `install-macos.sh:248-250` and `fleet-deploy-macos.sh:234-249`: after start → `retire` → `assert-single` (fatal).
- Windows `scripts/deskflow-ctl.ps1 Get-DeskflowInventory :336` + `Get-AssertSingleProblems :376`: exactly one HKCU Run entry at the canonical exe, zero HKLM/Startup/scheduled-task launchers, service is the only core launcher; `fleet-health.ps1 Test-Instances :193` inherits.

**L3. Signing**
- `cmake/MacCodesign.cmake:20-27`: `FATAL_ERROR` unless `FLEET_ALLOW_ADHOC_DEV_BUILD=ON`; when allowed, write `${CMAKE_BINARY_DIR}/ADHOC-DEV-BUILD`. `install-macos.sh verify_signature :131-158`: refuse if the marker exists; walk every Mach-O (same find as `tools/fleet-health:255-270`); require no `Signature=adhoc`, `TeamIdentifier=${DESKFLOW_EXPECT_TEAM:-J5KPG8ZR5C}`, and `flags=…(runtime)` on `Contents/MacOS/*`. Mouser `scripts/build_and_install.py:327-330`: same walk (confirm `build_macos_app.sh` uses `--options runtime`; add entitlements if PyInstaller needs `allow-unsigned-executable-memory`).
- Windows (user decision: keep fleet cert): `scripts/sign-windows.ps1:38` throw on `-NoTimestampOk` when `FLEET_DEPLOY=1`; `Test-SignedFiles :102` + `fleet-health.ps1 Test-Authenticode :84` require `TimeStamperCertificate` present and thumbprint `FBB49069…FAEC (full thumbprint lives in scripts/fleet.env)`.
- `fleet-deploy.sh` report (:527-533): per-seat `applecount/adhoc/hardened` columns from `fleet-health check_sign` (:548-567, add `hardened`); `ALL_OK=0` on adhoc>0 or hardened=false. hackintosh's `flags=0x0` build fails until the deploy rebuilds it (it always does).

**L4. Settings-survival proof**
- `fleet-deploy-macos.sh deploy_mouser`: `mouser_settings_snapshot` = sha256 of `~/Library/Application Support/Mouser/{config.json,last_device.json}`; copy `config.json → config.json.pre-deploy-<ts>` (keep 5); hash after install (must be identical) and after 30 s of Mouser running (may differ **only** if `.version` strictly increased and `jq 'del(.version)'` pre ⊆ post); `fail` otherwise. Windows: same in `fleet-deploy-windows.ps1 Deploy-Mouser :132-174` over `%APPDATA%\Mouser` with `Get-FileHash`. Note `save_config` copies the *new* file to `.bak` (`config.py:321`), so the pre-deploy copy is the authoritative restore point.
- Mouser `tests/test_install_lifecycle.py`: `test_build_and_install_macos_leaves_config_dir_byte_identical` (HOME→tmp, stubbed `run_command`/`ditto`), `test_sync_login_startup_never_writes_config`.
- Fix stale doc `MouserLink.h:109` (`%LOCALAPPDATA%` → `%APPDATA%`).

**L5. bats/Pester tests** — `test_deskflow_ctl.bats`: audit parses fixture & fails on enabled app record; allows the four agents; SKIP on privilege error; assert-single fails on non-launchd GUI pid / core ppid≠1 / runningboard instance; converge exits 1 on BTM app record; start boots out stale loaded agent; retire. `test_install_macos.bats`: ends with retire+assert-single; adhoc Mach-O anywhere → exit 1; missing hardened runtime → exit 1; ADHOC-DEV-BUILD marker refuses. `test_fleet_deploy_macos.bats`: settings hashes equal at three checkpoints; version bump superset passes, shrunk config fails; backups pruned to 5; native-tap gate passes/fails. `test_fleet_deploy.bats`: report fails on adhoc>0 / hardened=false. Pester: two HKCU entries / HKLM entry → assert-single fails.

### PR sequence
1. **M1+M2** Mouser leaks A/B + pins (unblocks the 6 GB immediately).
2. **M4** Mouser prevention (watchdog `[mem]`, soak test, heap-classes tool).
3. **K1** bridge capitalization + **K3** bridge ops.
4. **K2** stuck keys.
5. **L1+L2** launch ownership + deskflow-ctl audit/assert-single/retire.
6. **L3+L4** signing gates + settings proof + fleet-deploy report + M4 deploy gate + fleet-soak heap classes.
7. **M3** QML teardown (largest refactor; lands after leaks are proven flat so its effect is measurable).
8. **M5+K4** adversarial audits (run continuously against each PR; final pass after 7).

### Human steps (root/manual, with detection)
1. macbookpro: System Settings → General → Login Items: remove both "Deskflow" rows (try the `osascript` delete first). Detected: `deskflow-ctl login-items audit` rc 0. Fallback only if `2.org.deskflow.deskflow` survives: `sfltool resetbtm` + reboot (user chose manual first).
2. hackintosh root: `sudo rm /usr/local/bin/deskflow-prio-apply.sh /Library/LaunchAgents/com.symless.synergy-agent.plist`; `deskflow-ctl prio` sudo lines; `sudo scripts/install-login-bridge-macos.sh` on both Macs (renders `--calibrate`, log mode 600). Detected: `retire`/`assert-single`/`fleet-health --check loginbridge`.
3. Rebuild hackintosh `.venv` on Python 3.13 (provenance check will refuse 3.12).
4. `scripts/fleet-deploy.sh` from macbookpro for all seats (needs `DESKFLOW_KEYCHAIN_PASSWORD` in each Mac's `.env`, mode 600 — already planned). TCC clicks only if prompted (identity unchanged).
5. Log out once on each Mac to exercise the LoginWindow bridge; run the capitalization check below.

## Success criteria
- Mouser footprint: `fleet-soak report --in soak.jsonl --proc mouser --window 24 --slope-max 0.1 --cap 200` exit 0 on every Mac seat with the window closed; `--cap 350` with it open. verify: that command.
- Native tap in use: `grep -c 'CGEventTap created (native tap:' ~/Library/Logs/Mouser/mouser.log` ≥ 1 and `grep -c 'enabled on its own run loop'` = 0 after deploy. verify: deploy gate in `fleet-deploy-macos.sh`.
- Heap classes flat: `tools/mouser-heap-classes` sampled by fleet-soak, `--class-slope-max 10` passes for CGEvent/NSXPCConnection. verify: fleet-soak report.
- `[mem] growth_mb_h` ≤ 0.1 in the last watchdog line. verify: `grep '\[mem\]' mouser.log | tail -1`.
- Mouser suites: `python -m unittest tests.test_memory_soak tests.test_app_detector tests.test_self_watchdog tests.test_install_lifecycle tests.test_build_and_install` green.
- Capitalization: ctest `BridgeCalibrationTests` (letterModifiers rows), `KeyMapTests`, `OSXKeyStateTests`, `MSWindowsKeyStateTests` green; at the login window, from tiny11 and from hackintosh, type `Ab1!K` → bridge log `letters shifted=2 unshifted=1 caps-edges<=1`; `fleet-health --check loginbridge` PASS. verify: manual (1) log out, (2) cross over, (3) type the string into the password field, (4) read the counters in `/var/log/deskflow-vhid-bridge.log`.
- Stuck keys: ctest `KeyStateTests`, `OSXKeyStateTests::sanitize*`, `ServerKeyLedgerTests::switch_releasesBeforeLeave` green; 100 scripted switches with Cmd held → 0 `stuck-release`, `post-switch held=0x0000` on every entry. verify: `grep -c stuck-release ~/Library/Logs/Deskflow/deskflow-core.log` = 0.
- Single launcher: `deskflow-ctl assert-single` rc 0 on both Macs; `fleet-health --check instances` rc 0 on all three seats. verify: those commands.
- Signing: `fleet-health --check sign --host all` rc 0 with `hardened=true adhoc=0` per Mac and `authenticode` PASS with timestamp on tiny11. verify: that command.
- Settings: hashes identical at all three checkpoints on every seat during the deploy. verify: `fleet-deploy.sh` report.
- Deskflow suites: ctest, `bats tools/tests`, Pester green.

## Verification (end-to-end)
1. Build both repos in worktrees; run all unit suites.
2. User-gated `scripts/fleet-deploy.sh` from macbookpro; read the per-seat report (signing table, settings hashes, native-tap gate, assert-single).
3. 24 h `fleet-soak` on hackintosh + macbookpro with heap-class sampling; then `fleet-soak report` against the caps above.
4. Login-window typing check and 100-switch stuck-key script on each Mac; `fleet-health --check all --host all`.
