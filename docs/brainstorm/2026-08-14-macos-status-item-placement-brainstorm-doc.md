# macOS menu-bar status item fails to render — brainstorm

**Date:** 2026-08-14
**Status:** mechanism identified and reproducible; the specific triggering call is still unknown
**Goal:** the menu-bar icon renders correctly on every macOS device, not just some

## Problem

Mouser's menu-bar status item does not appear on one machine (the Hackintosh,
Odyssey G95NC, 9216x2592 physical / 4608x1296 logical). The `NSStatusItem` object
is created successfully and reports `isVisible=True`, but its button's window is
laid out off the menu bar — near the screen origin instead of the top edge.

Because the app is `LSUIElement` (no Dock tile), the menu-bar icon is the only
way to reach the UI. When it is missing and `start_minimized` is set, the app is
running with no way in at all.

Measured, same commit and same package:

| machine | display | status item |
|---|---|---|
| macbookpro | 3456x2234 Retina | `x=976  y=2` — correct |
| Hackintosh | 9216x2592 | `x=-1  y=1288` — off-menubar |

## What this is NOT

Each of these was tested and eliminated. They are recorded because several are
the obvious first guesses, and three of them were guesses that turned out wrong
during this investigation.

| Hypothesis | Test | Result |
|---|---|---|
| Qt's `QSystemTrayIcon` vs native `NSStatusItem` | both paths | **both fail** on the affected machine |
| The SVG/template image | title-only vs image probe | both place correctly standalone |
| `LSUIElement` | bundle with and without | both behaved the same |
| Activation-policy flips (`.accessory` <-> `.regular`) | probe flipping both ways | stays on menu bar through both |
| Creating the item before the run loop | probe deferring to `QTimer.singleShot(0, ...)` | **still off-menubar** |
| PyInstaller packaging | minimal app packaged with PyInstaller | **lands on menu bar** |
| LaunchServices (`open`) vs direct exec | PyInstaller bundle, both ways | **both land on menu bar** |
| The display / the machine | minimal packaged app on the affected machine | **lands on menu bar** |
| Fork divergence from upstream | `_install_native_macos_status_item` diffed | **byte-identical to upstream** |

Upstream's own fix for a related symptom (`681a4a1`, re-attach after an
activation-policy flip) does not apply here: their premise is that the flip drops
the menu-bar slot, and the probe shows the flip is harmless on this machine.
A follow-up was written on top of it (`ae7ee42`) because upstream handles only
the promotion and not the demotion — that remains a genuine bug fix in its own
right, but it does not address this.

## What this IS

Something in **Mouser's own startup** breaks status-item placement, and it only
manifests on this machine's configuration. Established by having both a working
baseline and a failing target on the same machine:

- minimal PyInstaller `LSUIElement` app, launched via `open` -> **on menu bar**
- Mouser, same packaging, same machine -> **off menu bar**
- Mouser **from source** on the same machine -> **on menu bar**

The last line matters: identical code, so the trigger is an interaction between
packaging and something Mouser's startup does — not the code alone.

## Bisection so far

A probe was built that adds Mouser's startup pieces one stage at a time.

| stage | adds | result |
|---|---|---|
| 0 | status item only | on menu bar |
| 1 | + single-instance `QLocalServer` | on menu bar |
| 2 | + `QQmlApplicationEngine` | on menu bar |
| 3 | + CGEventTap (inline Quartz, verified `eventtap-ACTIVE`) | on menu bar |
| 4 | + HID listener thread | on menu bar |

All four are cleared. The first attempt at stage 3 was invalid — Mouser's hook
module failed to import in the frozen probe (`eventtap-ERR:ImportError`), so the
tap never installed; it was rebuilt with an inline Quartz tap and confirmed
active before being cleared.

**Remaining startup work to bisect**, in rough order of suspicion:

1. **A visible QML window.** The probe never creates or shows one. Mouser builds
   its window and, unless started hidden, shows it — before the status item is
   installed. Window creation is the largest untested difference.
2. **`_install_macos_dock_icon`** — calls AppKit `setApplicationIconImage_`,
   which touches the same application-level UI state as the status bar.
3. **The Deskflow bridge / remote-forward threads.**
4. **`_maybe_relaunch_with_mouser_process_name`** — the process re-exec through a
   renamed symlink. Note this correlates with *working*, not failing: the source
   build re-execs and its icon is fine.

## Approaches considered

**A. Bisect to the root cause** <- chosen

Continue adding Mouser's startup pieces to the working baseline until placement
breaks; fix the specific interaction.

- Pros: yields the real mechanism; the fix is targeted and holds everywhere
- Cons: needs a few build/test cycles
- Best when: the goal is a fix rather than a workaround — which it is

**B. Defensive retry in the app**

After layout settles, check whether the item landed on the menu bar; if not, tear
it down and recreate, bounded by a retry count.

- Pros: does not require knowing the cause; would help any machine that hits this
- Cons: treats the symptom; may never converge against an unknown mechanism;
  risks visible flicker. An earlier version of this idea was implemented and
  rejected — checking placement *synchronously* reports zero height on every Mac,
  so it would have forced every machine onto the square Qt fallback
- Best when: the cause proves environmental and genuinely unfixable

**C. Drop `LSUIElement`**

Give Mouser a Dock icon so the app is always reachable regardless of the menu bar.

- Pros: trivial; removes the "no way into the app" failure mode entirely
- Cons: does not fix the icon; changes the app from menu-bar utility to regular app
- Best when: as insurance alongside A or B, not as the fix

## Decisions

- Pursue **A**. Do not ship a workaround before the mechanism is known — two
  workarounds have already been written against wrong hypotheses in this
  investigation, and one of them would have degraded every other Mac.
- Any placement check must run **after** AppKit lays the window out. Pre-layout
  the frame is zero-height universally, which makes a synchronous check useless
  and actively harmful.
- Treat "the icon is working" as unverified unless it is confirmed against the
  **packaged** build. On the affected machine the working icon currently comes
  from a source build, which is a stopgap that does not survive a reboot.

## Open questions

1. Does an active CGEventTap break status-item placement in a packaged app?
2. If so, is it ordering (tap installed before the item) or the tap itself?
3. Why does this reproduce only on this machine — is the display geometry a
   precondition, or does it just make the window-server timing more likely to lose?
4. Does the source build escape it because it never re-execs through the
   PyInstaller bootloader, or for some other reason?

## MECHANISM FOUND

Mouser **actively corrupts SystemUIServer's per-bundle-identifier menu-bar state
at runtime**. The corruption persists for that identifier until the menu-bar
services restart, so every later launch -- including the packaged app itself --
lands off the menu bar.

Proven by this sequence:

| step | action | result |
|---|---|---|
| 1 | probe under `io.github.tombadash.mouser` | OFF-MENUBAR |
| 2 | `killall SystemUIServer ControlCenter` | state cleared |
| 3 | probe under the same identifier | **ON-MENUBAR** |
| 4 | run the real Mouser once | re-poisons |
| 5 | probe under the same identifier | **OFF-MENUBAR** |
| 5b | probe under `io.github.tombadash.mouser2` | ON-MENUBAR (unaffected) |

This is why every on-disk fix failed: deleting the preference file, flushing
`cfprefsd`, de-duplicating LaunchServices registrations, and rebuilding the LS
database all target static state. The corruption is live, in the running
menu-bar services, scoped to the exact identifier string and independent of the
code signature (an ad-hoc signed probe fails identically, which also rules out
TCC).

It explains the two facts that never fit anything else: the icon broke roughly
three weeks ago with no commit behind it (the first poisoning event), and
macbookpro is unaffected (it has not hit the trigger).

**Stopgap:** `killall SystemUIServer ControlCenter` restores the icon until
Mouser next runs. Not a fix.

**Still unknown:** which Mouser operation does the poisoning. Already eliminated:
the engine (HID, hooks, Deskflow bridge), the Dock icon, the window icon, item
construction, the CGEventTap, the HID listener, the QML engine, a visible window,
the single-instance server, and the Qt-tray-then-native sequence -- a minimal
title-only item inside Mouser with the engine disabled still poisons.

## Next step

Add a stage that **creates and shows a QML window before installing the status
item**. That is the largest remaining difference between the working probe and
Mouser, and it is consistent with everything observed so far: the failure needs
both the packaged bootloader and something Mouser does at startup, and window
creation is the one heavyweight AppKit interaction not yet reproduced.

If that reproduces the failure, the fix is likely an ordering change — install
the status item before the window exists, or after the window has finished
its first layout — and it can be verified against the probe before touching
Mouser.

If it does not reproduce, continue with the Dock icon install, then the
Deskflow bridge threads.
