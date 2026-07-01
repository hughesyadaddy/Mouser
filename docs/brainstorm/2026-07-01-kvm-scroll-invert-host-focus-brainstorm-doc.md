---
date: 2026-07-01
topic: kvm-scroll-invert-host-focus
---

# KVM Scroll Invert + Host Remote Focus — Gap Analysis

## What We're Building

Clarify the **split responsibility** between host and clients when Deskflow shares one physical Logitech mouse across machines:

| Traffic | Path | Who handles it |
|---------|------|----------------|
| Pointer + scroll | Deskflow KVM (normal input forwarding) | Deskflow only — agnostic to Mouser scroll settings |
| Scroll invert (shared mouse) | Host Mouser on the machine USB is attached to | Always applied on host, **including when KVM focus is remote** |
| Scroll invert (local mice) | Each machine's own Mouser | Only when a **physically attached** Logitech is on that machine |
| Custom Logi gestures/buttons | DMSR bridge (`RemoteForwarder` → `MouserBridge` → client `:19795`) | Decoded HID++ events only — not scroll, not invert state |

The user confirmed: **host scroll must stay inverted during remote focus**, and **clients must not know about host invert**. Clients keep their own invert for local hardware only.

## Why This Approach

Three models were considered for scroll during KVM:

**Host-only invert for shared mouse** ← Partial fit

Host inverts before Deskflow sees scroll; clients pass through. Simple mental model but breaks when a client also has invert ON.

- Pros: One knob for the shared mouse
- Cons: Clients cannot tune local trackpad/mouse independently without policy exceptions
- Best when: Single-user, host is source of truth

**Independent per-machine invert** ← Rejected

Each machine applies its own `invert_vscroll` / `invert_hscroll` to all scroll it processes.

- Pros: Uniform settings UI
- Cons: **Double invert** when host firmware/OS already flipped scroll and client also inverts KVM-forwarded events
- Best when: Each machine has its own mouse (not our setup)

**Physical-Logitech-only invert** ← **Selected**

Invert applies only to scroll from a **physically attached** Logitech on that machine. KVM-forwarded scroll from Deskflow is never touched by the receiving client's Mouser.

- Pros: Host invert survives remote focus; clients stay independent for local hardware; no double-flip; Deskflow stays agnostic
- Cons: Requires reliable attribution (physical vs forwarded) on each platform; needs explicit policy in code/docs
- Best when: Shared host mouse + optional local mice per client (hackintosh + macbookpro + tiny11)

## Key Decisions

1. **Host scroll invert is always on** while the Logitech is USB-attached to the host — including when `RemoteForwarder.should_forward()` is true (KVM focus remote). Deskflow must not participate in or wait for invert state.

2. **DMSR relays decoded gesture/button events only** — not raw HID reports, not scroll, not invert settings. `should_forward()` suppresses host gesture handling; scroll passes through the normal KVM pipe.

3. **Per-event Logitech attribution for OS-layer invert** — "Logitech connected" is not enough. macOS (IOHID wheel monitor + trackpad filters), Windows (`GetRawInputBuffer` + vendor `046d`), Linux (evdev grab) must only flip scroll from the physical Logitech.

4. **Client invert is physical-only** — when cursor is on a client receiving forwarded scroll from the host, that client's Mouser must **not** apply invert to those events. Client invert remains meaningful only for a Logitech physically plugged into that client.

5. **HID passthrough stays off** — legacy DMSR relay is the production path until host-yield + client delivery are proven separately.

6. **Firmware invert stays host-local** — HID++ `0x2121` / `0x2150` writes happen on the machine holding USB. Forwarded scroll bytes already carry inverted direction; clients must not re-invert.

## Gaps You Might Be Missing

### A. Client double-invert (policy decided, code may not fully enforce)

**Risk:** Client with virtual remote device connected (`source=remote-virtual`) still has `_connected_device` set. OS-layer invert fallback may treat KVM-forwarded scroll as "Logitech scroll" and flip again.

**Mitigation:** Gate client invert on `transport != "remote"` / `source != "remote-virtual"` (and Deskflow-injected event markers if needed). Document: shared-mouse clients should not need invert ON.

### B. macOS IOHID ↔ CGEvent timing

**Risk:** OS-layer invert correlates IOHID wheel callbacks with CGEventTap scroll via a ~50 ms window. Rare miss = one tick wrong direction.

**Mitigation:** Soak on hackintosh; prefer firmware invert on MX-class devices (vertical especially). Monitor logs for missed invert reports.

### C. Windows `GetRawInputBuffer` empty

**Risk:** If raw-input buffer is empty for a wheel message, OS-layer invert skips (safe) rather than inverting a non-Logitech device (correct) — but Logitech invert might not apply in edge cases.

**Mitigation:** Verify on tiny11; firmware path covers most MX vertical scroll.

### D. What DMSR does *not* carry

Still local-only unless explicitly added later:

| Feature | Crosses DMSR? |
|---------|----------------|
| Gesture swipes / Sense panel | Yes |
| Thumb / mode shift / DPI (HID++ diverted) | Yes |
| Scroll wheel direction | **No** (host firmware/OS) |
| OS back/forward (btn 3/4) remaps | **No** |
| Smart Shift state | **No** |
| Horizontal scroll *actions* (hscroll → volume etc.) | **No** — host hook stands down on remote focus |

Confirm this matches intent: only **HID++-diverted custom Logi controls** cross KVM, not every Mouser remap.

### E. Operational / soak gaps (not architecture, but block "done")

- Phase 2 log matrix (H1–H4, C1–C3) not fully signed off after redeploy
- macbookpro Mouser install may still be pending (codesign)
- tiny11 Mouser build / local USB probe stall
- Second Logitech dongle (`C52B`) still causes dual enumeration noise — unplug recommended
- Per-event scroll attribution + DMSR decoded-only changes may be **uncommitted** on `working`

### F. Config drift watchlist

| Setting | Host | Client |
|---------|------|--------|
| `hidPassthroughEnabled` | `false` | `false` (if server section exists) |
| `mouserBridgeEnabled` | `true` | n/a |
| `remote_forward.enabled` | `true` | `false` |
| `passthrough_decode_only` | `false` | n/a |
| `deskflow.auto` | `true` | `true` |

### G. Deskflow doc staleness

`deskflow/docs/mouser-bridge.md` still describes `decode` messages for passthrough-era host behavior. Update when passthrough path is formally retired.

## Success Criteria

**Host (remote focus):**
- `[RemoteForward] Focus -> remote`
- Scroll feels inverted on all screens (host firmware/OS)
- **No** `[Gesture] … [fired]` on host during remote focus
- Trackpad / generic mouse scroll on host **not** inverted

**Client (focused):**
- `[RemoteDevice] Listening on :19795` + virtual connect on focus
- `[Gesture] … [fired]` on gesture with cursor there
- Scroll direction matches host (already inverted at source) — **client invert does not re-flip**

**Deskflow:**
- No `hid passthrough: seized` lines
- DMSR relay activity on focus switch

## Open Questions

- Should client virtual-device connect set `capabilities` flags so UI shows invert as "N/A — remote device"?
- Do any custom remaps bind to OS-level buttons only (not HID++ divert) that users expect on clients?
- After physical-only invert is enforced, is explicit documentation in Scroll page / KVM tooltip enough, or do we need a `invert_scope: physical` setting?
