---
date: 2026-08-04
topic: off-host-gesture-motion-relay
---

# Off-host gesture motion: let the focused client decode its own swipe

## What We're Building

A swipe held on the MX Master 4 while KVM focus is on a Windows client fires
nothing. The button arrives; the motion never does, so every swipe resolves as
a tap (`[Gesture] decoded tap (dx=0 dy=0, src=None)` on tiny11). The cause is
structural, not a bug: this device's gesture CID diverts **button-only**, so
the host has no HID++ rawXY to forward.

The fix is to give the Windows hook the OS-level motion fallback macOS already
has, and to source it from the pointer motion Deskflow is already injecting on
the client. While a gesture capture is active, the native hook procedure
accumulates the motion natively and swallows it so the cursor freezes, exactly
as the host pins its cursor today. Separately, the Deskflow ingress session on
the client is re-attaching every 3-6 seconds and has to stop doing that, or
nothing above survives long enough to matter.

## Why This Approach

**The host cannot relay what it never has.** On every connect the host logs:

```
[HidGesture] Divert 0x00C3 (Mouse Gesture Button) (button-only (no rawXY capability)): OK
[HidGesture]   caps[gesture]: 0x00C3 rawXY divert not confirmed -> event_tap
```

So the host decodes swipes from its own CGEventTap. That motion is an OS
event, not a HID++ report, so Deskflow HID passthrough has nothing to carry —
and the DMSR decoded-event bridge never sees it either, because
`mouse_hook_macos.py:315` feeds event-tap deltas straight into
`_accumulate_gesture_delta`, bypassing the `_hid_event_entry` wrapper that is
the only thing that relays. Worse, once Deskflow captures the cursor for a
client, the host's tap has no motion to observe at all.

**macOS already solves this; Windows just never got it.**
`mouse_hook_macos.py:315` accumulates event-tap motion during *any* active
capture, deferring to `hid_rawxy` only when that source is already live. The
Windows hook only accumulates OS-level motion when `sense_panel_fallback` is
true. A macOS client would already work today. This is not a new subsystem —
it is parity.

**The motion is provably available on the client.** Deskflow's own source
settles how it injects ([MSWindowsLocalInputMonitor.cpp:24](../../../deskflow/src/lib/coordination/MSWindowsLocalInputMonitor.cpp)):
*"SendInput-synthesized events (deskflow client injection) carry a null device
handle and are ignored"* — Deskflow filters them out precisely because they
arrive. A probe run in tiny11's interactive session confirmed it against both
of Deskflow's injection modes (`MSWindowsDesks.cpp:651` absolute,
`MSWindowsDesks.cpp:683` relative):

| Source | Sees injected motion | Payload |
|---|---|---|
| `WH_MOUSE_LL` | 12/12, `flags=0x01` (`LLMHF_INJECTED`) | `pt` screen coords — uniform across both modes |
| Raw Input | 12/12, `hDevice == NULL` | absolute → 0–65535 positions; relative → true deltas |

Both work. The LL hook wins on two counts: it is the only one that can
**swallow** an event, which is what freezes the cursor; and its `pt` is uniform
across both injection modes, so one diffing path covers everything, whereas
Raw Input needs mode-dependent handling and diffing anyway for the absolute
case.

Note the probe had to run inside the interactive session — under SSH
(session 0) `SetWindowsHookExW` fails and Raw Input delivers nothing. Any
future on-machine verification has to account for that.

**Rejected: chase rawXY at the HID++ level.** Attractive because passthrough
would then carry motion natively and both ends would just work. But the
divert decision is `rawXY divert not confirmed` on every single connect in the
host log; the device does not offer it on this CID. (The `src=hid_rawxy`
swipes in the host log are all stamped one second, with values like `dx=2000`
— a replay, not live data.)

**Rejected: host relays a resolved swipe over the bridge.** Simplest wire
change, but it depends on the host's event tap still seeing motion after
Deskflow has captured the cursor — unverified, and probably false. Keep as a
fallback only if the client-side approach hits a wall.

## Key Decisions

- **Decode on the client, from local motion.** Whoever holds the cursor
  decodes the swipe. Mirrors the host's own event_tap/raw_mouse pattern, needs
  no protocol change, and works for any device whose rawXY divert is
  unavailable rather than special-casing this one.
- **Source it from the LL hook, not Raw Input.** Required for the cursor
  freeze, and its uniform `pt` payload means one code path instead of two.
- **Accumulate in C, inside the native filter.** Add a "gesture capture
  active" flag to the packed filter word; while set, the procedure diffs
  successive `pt` values, accumulates dx/dy, and returns 1 to swallow. Python
  drains the total on button-up. No GIL on the move path, and when the flag is
  off the existing fast bail is untouched — the latency work just shipped must
  not regress.
- **Freeze the cursor during the hold**, matching the host.
- **Keep `hid_rawxy` authoritative.** If real rawXY ever arrives mid-capture it
  must still win, exactly as `_accumulate_gesture_delta` already arranges.
- **Fix the ingress churn as a separate commit in the same effort.**
  `remote_device._handle_connect` calls `attach_deskflow_ingress` on every
  `connect`, and `hid_gesture.request_deskflow_attach:1085` sets
  `_reconnect_requested` whenever one arrives while already connected — so
  repeated identical connects tear down a healthy session. Making the attach
  idempotent when the decode and device are unchanged is the likely fix. A
  3-second session breaks gestures regardless of the motion work.
- **The Python fallback procedure keeps its current behaviour.** It stays the
  safety net for a missing DLL; parity there is not worth reintroducing
  per-move Python work.

## Open Questions

- **Cursor jump on release.** Deskflow keeps computing absolute positions
  server-side while we swallow, so the first move after the hold snaps the
  cursor to wherever Deskflow believes it is — a jump roughly the length of
  the swipe. Options: accept it; warp back on release like the host does and
  let Deskflow resync; or don't swallow at all and accept a travelling cursor.
  Needs a real swipe on tiny11 to judge how bad it looks.
- **What counts as "capture active" on the client?** The gesture button
  arrives over the HID++ ingress, so `_gesture_active` is already set. Confirm
  no path sets it without a matching release, or the cursor could freeze
  permanently — the mid-gesture uninstall deferral in `_apply_hook_state` is
  the existing precedent for how careful this needs to be.
- **Threshold units.** `_gesture_threshold` is tuned for HID++ rawXY sensor
  counts; screen-pixel deltas from a KVM client are a different scale, and
  Deskflow may apply its own acceleration. Likely needs a per-source scale
  factor, which the existing `_gesture_input_source` already distinguishes.
- **Exact churn trigger.** Confirmed that repeated identical `connect` messages
  cause it; not yet confirmed what makes Deskflow send them so often.
- **Other clients.** A macOS client should already work via event_tap and a
  Linux client via evdev — neither verified. Worth a check before assuming the
  fix is Windows-only.
