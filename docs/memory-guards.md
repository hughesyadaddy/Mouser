# Memory guards

Mouser is a resident process that handles every mouse event on the seat.
Two leaks found in 2026-09 turned it into the largest process on the
fleet within days; this page records what was measured, the guard that
now trips on the same shape, the soak test that keeps the per-event paths
flat, the tool that names the leaking classes, and the numbers a build
must hit before it is deployed.

## Measured leak history

Source: `tests/fixtures/heap-s.txt`, the first 200 lines of `heap -s`
on hackintosh's Mouser 3.6.0 (pid 3191) on 2026-09-21 18:21, 81.1 h after
launch, footprint 6.0 GB, 63.3 M malloc nodes / 5.87 GB. Rates below are
that report divided by the uptime; the workstream-M section of
`docs/plan/2026-09-22-fix-fleet-hardening-plan.md` has the diagnosis.

| Leak | Fingerprint classes (count after 81 h) | Rate | Bytes | Root cause | Fix |
|---|---|---|---|---|---|
| A: CGEvent via the PyObjC tap trampoline | `CGEvent` 3 217 274, `CGSEventAppendix` 3 217 274, `HIDEvent` 9 357 423 (~2.9 per event) | ~11 events/s, 39 700/h | ~1.6 GB (0.10 + 0.46 + 1.05) | `m_CGEventTapCallBack` in pyobjc-framework-Quartz 12.1 never `Py_DECREF`s the callback result, so every pass-through leaks one proxy and its CFRetain (`docs/upstream/pyobjc-cgeventtap-leak.md`) | Native tap dylib mandatory in the build; the Python fallback runs the deferred-release guard in `MouseHook._drop_prev_passthrough` and logs `DEGRADED` |
| B: NSXPCConnection per foreground query | `NSXPCConnection` 729 457, `GPProcessMonitor` 729 442, `xpc_connection_t` 729 477, plus a `dispatch_queue_t`, `dispatch_mach_t`, `NSLock` and `GPAutoEDRParameters` each | ~2.5/s, 9 000/h (the 0.3 s AppDetector poll) | ~1.1 GB of objects + most of the 2.24 GB `non-object` | Every `NSRunningApplication.bundleIdentifier()` / `frontmostApplication()` opens a LaunchServices XPC connection that GamePolicy keeps alive | `core/macos_frontmost.py`: AX focused-pid + `proc_pidpath` + `Info.plist`, no LaunchServices on the hot path |
| C: QML floor | `CGImage` 158 (stable), 3 x 32 MB display-sized bitmaps on macbookpro | none | ~100 MB | Engine and window kept alive after close | M3 (engine teardown on close), separate PR |

The watchdog already caught the 27 h / 100 % CPU spin and the tap
re-enable storm; neither of the leaks above tripped anything because
nothing sampled memory. That is the gap the guard closes.

## The guard (`core/self_watchdog.py`)

Every 60 s tick, on the Qt main thread, the watchdog samples
`ri_phys_footprint` through `proc_pid_rusage(getpid(), RUSAGE_INFO_V4)`
(ctypes, no PyObjC; the struct layout is the one `deskflow/tools/fleet-soak`
uses, so both read the number Activity Monitor shows). The sampler is
injected (`footprint=`), returns `None` off macOS or on any failure, and
a `None` sample disables the memory checks for that tick without touching
the CPU, tap and heartbeat checks.

* Samples go into a 60-entry deque of `(monotonic, MB)`; a least-squares
  fit over the window gives `growth_mb_h` once at least 10 samples exist.
* One log line per tick, never more often than every 60 s:

      [mem] footprint_mb=182.4 peak_mb=190.1 growth_mb_h=0.3

  `growth_mb_h=n/a` for the first ten minutes. `fleet-health` and
  `fleet-soak` grep this line.
* Trip reasons, appended to the existing `[Watchdog] trip n=… consecutive=… tap=…` line:
  * `mem growth/h=25.0 over 3.1h` when the fitted slope has stayed above
    20 MB/h for three hours without interruption (a burst that stops
    resets the clock; a fixed build sits at 0.1 MB/h, the 2026-09 leaks ran
    at 20 to 70 MB/h);
  * `mem footprint_mb=1600 > 1500` immediately when the footprint passes
    1.5 GB.
* Policy is the existing one. First trip: log line and a listener
  reconnect. Second consecutive trip: exit 3 for the launchd respawn when
  launchd owns the process and `watchdog_exit` is not `false`. Otherwise
  the process stays up, logs `exit disabled`, posts
  `Mouser needs a restart: …` to the status bar (`Backend.statusMessage`)
  and mutes the memory reasons for an hour so the seat is not nagged every
  minute. Memory is the one symptom a reconnect cannot fix, so a memory
  trip escalates on the very next tick.

Tests: `tests/test_self_watchdog.py::MemoryGuardTests` (flat, 2 h of
growth, 3.5 h of growth, growth that stops, 1.6 GB, escalation with and
without a supervisor, line format and rate, sampler failure, slope fit,
the real sampler on macOS).

## The soak test (`tests/test_memory_soak.py`)

    ../Mouser/.venv/bin/python -m unittest tests.test_memory_soak -v

Each case warms a path up 1 000 times, then runs it 10 000 more and
asserts `gc.collect()` leaves at most 100 new tracked objects and
`tracemalloc` at most 256 KB more live bytes. Measured on 2026-09-22: 0
objects and 2.4 KB / 5.1 KB / 0.1 KB for the three paths.

| Case | Path | Fakes | CI |
|---|---|---|---|
| `TapCallbackSoak` | `MouseHook._event_tap_callback`, moves and wheel ticks passing through, a blocked xbutton pair swallowed | `_MacOSHookCase` harness with a Quartz stand-in that records nothing (a `MagicMock` call log would itself read as a leak) | Linux |
| `AppDetectorSoak` | 10 000 activations plus 10 000 idle ticks through `AppDetector` | the M2 AppKit fakes from `tests/test_app_detector.py` | Linux |
| `EngineDispatchSoak` | 10 000 `MouseEvent`s through `BaseMouseHook._dispatch` with the engine's default-profile handlers registered (alt_tab, browser back/forward, unmapped middle, unmapped swipe) | `FakeMouseHook` plus the real dispatch, `execute_action` stubbed | Linux |
| `DeferredReleaseGuardSoak` | 10 000 real `Quartz.CGEventCreate(None)` proxies through the guard, emulating PyObjC's trampoline (`Py_IncRef` on the result) in three modes: leaking (12.1), fixed upstream, and with another Python holder | real PyObjC, `CFGetRetainCount` on the raw event | macOS only |

The guard case is the proof against over-release: with the leak every
event's retain count returns to our own +1 at the next entry; with the
leak fixed the guard is a no-op and nothing is freed twice; with another
holder the guard skips the event (a leak of one proxy, never a crash).

## Naming the leak (`tools/mouser-heap-classes`)

    tools/mouser-heap-classes --name Mouser
    tools/mouser-heap-classes --pid 3191
    tools/mouser-heap-classes --parse heap-s.txt

Prints one JSON object with the counts of the fingerprint classes above
(`CGEvent`, `CGSEventAppendix`, `HIDEvent`, `NSXPCConnection`,
`GPProcessMonitor`, `CGImage`, `non-object`) and `total_bytes`.
`deskflow/tools/fleet-soak --heap-classes` records it per sample and
reports a per-class slope (`--class-slope-max 10/h`), which says *which*
leak is back rather than only that memory grows. `heap` is part of the
Xcode command-line tools and needs to attach to the process: a hardened
Mouser build needs the `get-task-allow` entitlement (debug builds) or the
tool must run under `sudo`; either failure exits 2 with the reason.

## Success criteria

A build is deployable when, on every seat, after a 24 h `fleet-soak`:

| Metric | Limit | Where it is checked |
|---|---|---|
| Idle footprint (window closed) | <= 200 MB | `[mem] footprint_mb` in the log, `fleet-soak` |
| Footprint with the window open | <= 350 MB | `fleet-soak` |
| Growth | <= 0.1 MB/h over the soak | `[mem] growth_mb_h`, `fleet-soak` slope |
| Fingerprint classes | flat (<= 10/h per class) | `fleet-soak --heap-classes` |
| Watchdog | no `mem` trip, `tap=native` on every trip line | `fleet-health` |

The trip thresholds (20 MB/h for 3 h, 1.5 GB) sit two orders of magnitude
above the success criteria on purpose: the guard is the backstop for a
regression that escaped review and soak, not a substitute for them.
