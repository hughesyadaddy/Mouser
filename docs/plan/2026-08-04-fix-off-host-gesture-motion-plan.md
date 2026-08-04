---
title: "fix: decode off-host swipes from client-local motion"
type: fix
date: 2026-08-04
---

## fix: decode off-host swipes from client-local motion - Standard

## Overview

A swipe held while KVM focus is on a Windows client fires nothing. The gesture
button crosses the wire; the motion does not, so every swipe resolves as a tap.

This plan gives the Windows hook the OS-level motion fallback macOS has had all
along, sourced from the pointer motion Deskflow is already injecting on the
client. While a capture is active the native procedure accumulates that motion
natively and swallows it so the cursor freezes, matching the host. It also
stops the Deskflow ingress session from re-attaching every few seconds, which
would break gestures regardless.

Design rationale, the rejected alternatives, and the probe evidence live in
[the brainstorm](../brainstorm/2026-08-04-off-host-gesture-motion-relay-brainstorm-doc.md).

## Problem Statement / Motivation

Observed on tiny11 (`%APPDATA%\Mouser\logs\mouser.log`):

```
17:00:34 [Gesture] decoded tap (dx=0 dy=0, src=None)
17:00:36 [Gesture] decoded tap (dx=0 dy=0, src=None)
```

`src=None` means `_accumulate_gesture_delta` was never called at all — the
button arrived, no motion did.

The host cannot supply it. On every connect it logs:

```
[HidGesture] Divert 0x00C3 (Mouse Gesture Button) (button-only (no rawXY capability)): OK
[HidGesture]   caps[gesture]: 0x00C3 rawXY divert not confirmed -> event_tap
```

So the host decodes from its own CGEventTap. That is an OS event, not a HID++
report: Deskflow HID passthrough has nothing to carry, and the DMSR bridge
never sees it either because [core/mouse_hook_macos.py:315](../../core/mouse_hook_macos.py)
feeds event-tap deltas straight into `_accumulate_gesture_delta`, bypassing the
`_hid_event_entry` wrapper that is the only thing that relays.

The motion *is* available on the client. Deskflow injects with `SendInput`
([MSWindowsDesks.cpp:651](../../../deskflow/src/lib/platform/MSWindowsDesks.cpp)
absolute, `:683` relative), and a probe run in tiny11's interactive session saw
every injected move on both paths:

| Source | Injected motion seen | Payload |
|---|---|---|
| `WH_MOUSE_LL` | 12/12, `flags=0x01` (`LLMHF_INJECTED`) | `pt` screen coords, uniform across both modes |
| Raw Input | 12/12, `hDevice == NULL` | absolute → 0–65535 positions; relative → deltas |

Mouser discards it at both doors: `_process_raw_input` rejects the null handle
via `_is_logitech`, and the hook procedures bail on `LLMHF_INJECTED`.

## Proposed Solution

**Phase 1 — stop the ingress churn.** `remote_device._handle_connect` calls
`attach_deskflow_ingress` on every `connect`, and
[core/hid_gesture.py:1085](../../core/hid_gesture.py) sets
`_reconnect_requested` whenever an attach arrives while already connected, so
repeated identical connects tear down a healthy session. Make the attach
idempotent: under `_deskflow_control_lock`, compare the incoming request
against the **previous** `_deskflow_attach` (dict equality on `decode`,
`product_id`, `product_name`) *before* overwriting the field. Identical →
signal ready and return without setting `_reconnect_requested`. Different →
behave exactly as today.

**Phase 2 — decode the swipe on the client.** Add a capture-active bit to the
packed filter word. While set, the native procedure treats `WM_MOUSEMOVE`
(including injected) as gesture motion: it diffs `pt` against the previous
point, accumulates dx/dy in C, and returns 1 to swallow so the cursor freezes.
Python drains the accumulated delta on button-up and feeds
`_accumulate_gesture_delta(dx, dy, "kvm_pointer")`, which classifies it.

`hid_rawxy` stays authoritative — the existing source arbitration in
[core/mouse_hook_base.py:568](../../core/mouse_hook_base.py) already discards a
lesser source the moment real rawXY appears, and this new source must lose that
race like the others.

### Why accumulate in C rather than queue points to Python

The swallow decision has to be made in the procedure anyway (`return 1`), so
the code is already in C at the moment each move arrives. Given that, the
choice is between four arithmetic ops per move, or a ring push plus a
semaphore release per move that also wakes the drain thread. A fast swipe at a
1000 Hz polling rate would push hundreds of entries through a 256-slot ring in
under a second — overflowing it and dropping exactly the motion we are trying
to capture. Accumulation is both cheaper and the only option that cannot drop
samples.

The cost is honest: it forces an ABI bump (below) that a same-process Python
diff would avoid.

## Technical Considerations

**The latency win must not regress.** The move fast-path bail is the whole
point of the native filter (`SendInput` 76–187 ms → 1.3–1.6 ms). The new work
sits behind the capture-active bit: when no gesture is held, the procedure
bails on `WM_MOUSEMOVE` exactly as it does today, before reading `lParam`.

**The bit rides the existing packed word.** `mouser_hook_set_filter` already
writes flags/interest/block as one `InterlockedExchange64` and the procedure
reads it with one `InterlockedCompareExchange64`, so the capture bit is
atomic with the rest by construction. A partial push mid-stroke is not
possible. The accumulator must reset on the **false→true** transition of the
bit, or a stale delta from an aborted stroke seeds the next one — mirroring
how `g_blocked_down_active` resets on install/uninstall
([native/win/mouser_hook.c:454](../../native/win/mouser_hook.c)).

**ABI cost.** Adding the bit and the drain export bumps `MOUSER_HOOK_ABI` /
`ABI_VERSION` 2 → 3, so an older DLL is rejected and the client silently falls
back to the Python procedure. That is a real ongoing cost given tiny11 cannot
rebuild the DLL itself, and it is why "does the log say `(native filter)`" is
step 1 of every manual check below.

**Scale.** `_gesture_threshold` defaults to 50 in HID++ sensor counts
([core/mouse_hook_base.py:46](../../core/mouse_hook_base.py)); the client's
deltas are screen pixels at a different order of magnitude, DPI- and
acceleration-dependent. Add a per-`_gesture_input_source` scale factor applied
before the threshold compare in `_classify_gesture`, with the `kvm_pointer`
factor derived once by measuring a deliberate full-width swipe on tiny11 and
pinned as a named constant. A unit test must fix a plausible pixel delta to a
swipe classification, or this ships "drains a delta" while still silently
reproducing the original bug.

**`LLMHF_INJECTED` is not Deskflow-specific.** Any `SendInput` caller sets it,
so during a capture the client will absorb another app's synthetic motion too.
Accepted: a capture is user-initiated and short, and the alternative (matching
on `dwExtraInfo`) would couple Mouser to a Deskflow build detail.

**Verification needs the interactive session.** Under SSH (session 0)
`SetWindowsHookExW` fails and Raw Input delivers nothing, with no error
surfaced — gesture support silently degrades to today's tap-only behaviour.
On-machine checks must run in console session 1.

**No compiler on tiny11.** It has no MSVC, mingw, winget, scoop or choco, so
`build.bat` cannot rebuild the DLL there; it must be cross-compiled and copied.

## Implementation Phases

### Phase 1: Make the Deskflow ingress attach idempotent

- **Status:** Done
- **Scope:** Stop a repeated identical `connect` from tearing down a live
  ingress session, so the client holds a stable device long enough to gesture.
  Ships on its own merit — the churn degrades the client whether or not
  gestures work.
- **Files touched:** `core/hid_gesture.py`, `tests/test_deskflow_listener_ingress.py`
- **Acceptance criteria:** A second `request_deskflow_attach` carrying the same
  decode and device identity does not set `_reconnect_requested` and still
  signals its ready event; a changed decode or device still forces the
  reconnect. The comparison reads the previous `_deskflow_attach` under
  `_deskflow_control_lock` before the field is overwritten.
- **Validation:** `cd ~/Desktop/Mouser && python3 -m pytest tests/test_deskflow_listener_ingress.py tests/test_hid_gesture.py -q`

### Phase 2: Decode off-host swipes from client-local pointer motion

- **Status:** Done
- **Scope:** Native capture-active bit with in-C accumulation and cursor
  swallow, the drain export and its Python wiring, the per-source scale factor,
  and the capture-lifetime guards that make a frozen cursor impossible to get
  stuck with.
- **Files touched:** `native/win/mouser_hook.c`, `core/native_hook_filter.py`,
  `core/native_hook_win.py`, `core/mouse_hook_windows.py`,
  `core/mouse_hook_base.py`, `tests/test_native_hook_filter.py`,
  `tests/test_native_hook_win.py`, `tests/test_windows_native_hook.py`
- **Acceptance criteria:**
  1. With no capture active the pushed filter word and the `WM_MOUSEMOVE` bail
     are unchanged from today (`test_windows_native_hook.py`).
  2. The drain returns the accumulated delta, and the accumulator resets on the
     false→true transition of the bit (`test_native_hook_filter.py` for the
     constants, `test_windows_native_hook.py` for the wiring).
  3. A plausible screen-pixel delta classifies as the expected swipe through
     the new scale factor (`test_native_hook_base`-level test).
  4. Every capture-exit path clears the bit — named tests for: normal
     button-up, focus flip to another machine, ingress drop, device
     disconnect, hook uninstall, and the native watchdog expiry.
- **Validation:** `cd ~/Desktop/Mouser && python3 -m pytest tests -q`

### Capture lifetime — the guard set (part of Phase 2)

The swallow happens in C, off the GIL, so nothing Python does can unfreeze the
pointer in real time. A button-down with no matching release would leave the
machine unusable. This is the failure class `ec7fc04` had to revert, so it is
specified here rather than left to implementation:

- **Native watchdog is the backstop.** `ll_mouse_proc` records the tick at
  which the bit went true and self-clears the capture after a hard bound
  (~3 s). Only the native side can unfreeze the native side; every guard below
  is an optimisation on top of this one.
- **Focus flip ends the capture.** `_apply_hook_state` today only *defers*
  uninstall while `_gesture_active` and re-arms `SYNC_RETRY_TIMER_ID` every
  200 ms, uncapped — with focus gone that spins forever and no accumulator
  drains. The focus-change callback must call `_end_gesture_capture` directly.
- **Ingress teardown ends the capture.** A reconnect firing between button-down
  and button-up orphans the accumulator with no host-side release.
- **One authority.** During the churn window before focus settles, the host's
  event-tap fallback and the client's accumulator can both be live. Gate the
  client's capture on focus state, not on receipt of the button divert alone.

## Success Criteria

```success-criteria
GOAL: A swipe held while KVM focus is on a Windows client fires its mapped action, with the cursor frozen for the duration and never stuck, and the client's Deskflow ingress session stays up between gestures.

SUCCESS CRITERIA:
- The suite passes, including the C/Python ABI agreement tests. | verify: cd ~/Desktop/Mouser && python3 -m pytest tests -q
- The native procedure compiles clean. Fails outright when no toolchain is present, rather than skipping green. | verify: cd ~/Desktop/Mouser && python3 native/win/build.py
- With no gesture held, the pushed filter word and the move fast-path bail are unchanged from today. | verify: cd ~/Desktop/Mouser && python3 -m pytest tests/test_native_hook_filter.py tests/test_windows_native_hook.py -q
- A capture left open with no release self-clears and unfreezes the cursor. | verify: cd ~/Desktop/Mouser && python3 -m pytest tests/test_windows_native_hook.py -q -k capture_lifetime
- A swipe performed while focus is on tiny11 fires its mapped action. | verify: manual 1) confirm the log says `Hook installed (native filter)` 2) move KVM focus to tiny11 3) hold the gesture button and swipe left 4) confirm the mapped action fires and the log shows `decoded gesture_swipe_left` with a non-zero dx
- The cursor stays put for an off-host hold and moves freely the moment it ends. | verify: manual 1) with focus on tiny11, hold the gesture button and move the mouse 2) confirm the pointer does not travel 3) release 4) confirm the pointer moves normally again
- Releasing focus mid-hold does not strand a frozen cursor. | verify: manual 1) hold the gesture button on tiny11 2) without releasing, move KVM focus back to the host 3) confirm the client's pointer is usable within ~3s
- Gestures still work on the host, unchanged. | verify: manual 1) move KVM focus back to the host 2) perform a side-swipe 3) confirm the mapped action fires
- The client's ingress session survives at least 60s without re-attaching. | verify: manual 1) leave Mouser running on tiny11 for 60s 2) `findstr /C:"reconnect requested" "%APPDATA%\Mouser\logs\mouser.log"` 3) confirm no new entries
- Cursor latency has not regressed. | verify: manual 1) run WinStream on tiny11 2) wiggle the cursor 15s 3) `Select-String 'worst SendInput' C:\ProgramData\Deskflow\deskflow-daemon.log | Select -Last 8` 4) every sample < 5000us

NON-GOALS:
- Making rawXY divert work on CID 0x00C3 — the device does not offer it.
- Relaying the host's resolved swipe over the DMSR bridge.
- Changing the Python fallback procedure's behaviour; it stays the safety net for a missing DLL.
- Eliminating the cursor jump on release: Deskflow keeps tracking absolute position while we swallow, so the pointer snaps by roughly the swipe length when the hold ends. Accepted for this change; revisit only if it proves distracting in use.
- Distinguishing Deskflow's injected motion from any other application's.
- macOS and Linux clients — they should already work via event_tap and evdev; verify, do not rebuild.

VERIFICATION COMMAND: cd ~/Desktop/Mouser && python3 -m pytest tests -q && python3 native/win/build.py
```

## Dependencies & Risks

- **A stuck capture freezes the machine's pointer.** The highest-severity
  failure here and the reason the guard set above is specified rather than
  delegated. The native watchdog is the non-negotiable part; everything else is
  a faster path to the same outcome.
- **Phase 2 cannot be manually verified without Phase 1.** A 3-second session
  is too short to hold a gesture through. Phase 1 is independently mergeable —
  it neither compiles nor tests against Phase 2.
- **Scale factor is empirical.** The `kvm_pointer` factor has to be measured on
  a real swipe on tiny11; a wrong value reproduces the original bug in a new
  disguise (deltas that never cross the threshold), which is why acceptance
  criterion 3 pins it with a test rather than trusting the measurement.
- **Cross-compile dependency.** tiny11 has no compiler; the DLL is built with
  mingw-w64 on the Mac and copied. A future rebuild on tiny11 silently drops to
  the Python procedure and gestures revert to tap-only.

## References & Research

- Brainstorm: [docs/brainstorm/2026-08-04-off-host-gesture-motion-relay-brainstorm-doc.md](../brainstorm/2026-08-04-off-host-gesture-motion-relay-brainstorm-doc.md)
- Motion source arbitration: [core/mouse_hook_base.py:545](../../core/mouse_hook_base.py)
- The pattern to mirror (macOS event_tap fallback): [core/mouse_hook_macos.py:315](../../core/mouse_hook_macos.py)
- Ingress re-attach trigger: [core/hid_gesture.py:1085](../../core/hid_gesture.py)
- Mid-gesture uninstall deferral (precedent for capture-lifetime care): [core/mouse_hook_windows.py:927](../../core/mouse_hook_windows.py)
- Deskflow injection modes: `deskflow/src/lib/platform/MSWindowsDesks.cpp:651`, `:683`
- Deskflow on synthesized input in Raw Input: `deskflow/src/lib/coordination/MSWindowsLocalInputMonitor.cpp:24`
- Native filter contract: [core/native_hook_filter.py](../../core/native_hook_filter.py)
- Prior gesture-loss regression: commit `ec7fc04`
