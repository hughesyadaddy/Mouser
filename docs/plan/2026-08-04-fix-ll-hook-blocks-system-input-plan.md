# fix: low-level mouse hook must never block system input

## Problem (proven, not suspected)

Mouser's `WH_MOUSE_LL` callback is Python. Windows calls it **inside Mouser's
process and blocks the entire system input pipeline until it returns**, up to
`LowLevelHooksTimeout` (300 ms). A Python callback must take the **GIL**, so
whenever another Mouser thread is mid-work (HID listener, bridge reconnect,
GC) the hook waits — and every application's input waits with it.

Measured on tiny11 with Deskflow's probe (`SendInput` duration + queue age):

| Metric | Mouser running | Mouser stopped |
|---|---|---|
| moves/s | 8–25 | **126–133** |
| queue age | 109–250 ms | **0–16 ms** |
| **SendInput** | **76–187 ms** | **1.3–1.6 ms** |

~100× difference, with the load source (WinStream) running in both cases.
Ruled out by measurement: network, CPU starvation, DPC/driver latency,
duplicate processes, RadeonSoftware, Wispr Flow, and WinStream's own code
(it installs no hook — it only supplies the load that stretches the GIL wait).

## The rule this must satisfy

**No system input event may ever wait on the Python GIL.**

## HARD CONSTRAINT: gestures must never stop working

Gestures are non-negotiable. They were lost once already (commit `ec7fc04`)
by standing the hook down off-host while assuming the bridge relayed them --
it was not configured, so those machines had no hook AND no relay.

Rules for whoever builds this:

1. **Never remove the hook before the replacement path is PROVEN on that
   machine.** Verify gestures work off-host with the hook still installed,
   then stand it down, then re-verify immediately.
2. **Abort condition:** if gestures regress at any point, revert the
   standdown (one condition in `_scroll_invert_fallback_enabled` /
   `_should_intercept_events`) and go to Phase 2 instead.
3. **If the bridge cannot be made to relay reliably, skip Phase 1 entirely.**
   Phase 2 fixes the blocking WITHOUT removing the hook, so it cannot cost
   gestures by construction. It is the safer target whenever "don't lose
   gestures" outranks "ship sooner".

## Approach

### Phase 1 — do not install the hook when there is nothing to swallow (recommended first)

The hook exists for exactly one job: *swallowing* remapped buttons. Detection
(gestures, buttons) already runs off the Raw Input path, which cannot block
input. So when Mouser has nothing to swallow on this machine, the hook must
not exist.

`_hook_should_be_installed()` already encodes this idea. Two gaps:

- [ ] `core/mouse_hook_base.py` — off-host standdown was added then reverted
      (commit `ec7fc04`) because with no bridge configured, off-host machines
      lost gestures entirely. Restore the standdown **and** make it safe:
      stand down only when the bridge is actually relaying.
- [ ] Bridge config gap: `settings.remote_forward.enabled = false`, empty
      `token`, and no `deskflow.host_bridge`/`bridge_token` — so the relay
      never runs and off-host machines have no gesture path. Fix the config
      so Phase 1 is safe to enable.
- [ ] Verify the local-focus case still installs the hook (remaps must work
      on the machine holding the mouse).

### Phase 2 — native hook filter (the complete fix)

Even when installed, the callback must not enter Python for events Mouser
does not act on.

- [ ] Move the hook procedure into a small native extension that decides
      natively from a shared, lock-free table (which buttons are remapped)
      and calls into Python **only** for events that are actually remapped.
- [ ] Keep the existing early-outs as defence in depth.

Phase 2 is the durable answer; Phase 1 removes the pain immediately and is
already mostly written.

### Rejected

- **Lower `LowLevelHooksTimeout` (registry).** Machine-wide policy that
  silently evicts *any* slow hook — including Mouser's own and PowerToys' —
  breaking those apps with no error. Masks a defect we own, does not travel
  with the code, and will be forgotten. Mitigation only, not a fix.
- **Raw Input instead of the hook.** Raw Input cannot swallow events, so it
  cannot replace the hook for remapping.

## Success criteria

- [ ] With Mouser running under load, Deskflow's probe reports
      `worst SendInput` < 5000us sustained.
      verify: manual 1) run WinStream on tiny11 2) wiggle the cursor 15s
      3) `Select-String 'worst SendInput' C:\ProgramData\Deskflow\deskflow-daemon.log | Select -Last 8`
      4) every sample < 5000us
- [ ] Cursor movement stays >100 moves/s with Mouser running.
      verify: manual same log lines show `moves/s` >= 100
- [ ] Gestures still work on the machine holding the mouse.
      verify: manual perform a side-swipe on the host; confirm the mapped action fires
- [ ] Gestures still work on a machine that is NOT the host.
      verify: manual switch KVM focus to a client, perform a side-swipe, confirm it fires
- [ ] No regression in the suite.
      verify: `cd ~/Desktop/Mouser && python3 -m pytest tests -q`

## Notes

- Deskflow's `move latency` probe (`MSWindowsDesks.cpp`) is the measurement
  instrument for all of the above; strip it once these criteria pass.
- Related brainstorm: `deskflow/docs/brainstorm/2026-08-04-tiny11-cursor-lag-brainstorm-doc.md`
