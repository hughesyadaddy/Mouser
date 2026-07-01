---
date: 2026-07-01
topic: hid-passthrough-regression
---

# HID Passthrough Regression — Restore Legacy DMSR Relay

## What We're Building

Restore the **known-working legacy gesture path** (Mouser `RemoteForwarder` → Deskflow `MouserBridge` → DMSR → client `RemoteDevice`) while **disabling Tier 1.5 HID passthrough seize**. The user reports that with KVM + passthrough ON, the host still executes gestures and slave machines do not — behavior that worked before passthrough was enabled.

This is a **diagnosis + rollback strategy**, not a passthrough fix. Passthrough remains a future track once host yield and client DFHR delivery are proven.

## Log Evidence (2026-06-30 / 2026-07-01)

### Host still fires gestures while focus is remote

| Timestamp | Event |
|-----------|-------|
| 23:34:37 | `RemoteForward Focus -> remote (screen=tiny11)` |
| 23:34:37 | `hid passthrough: seized device 1` **but** `seize of device 2 failed (0xe00002c5)` |
| 23:34:37 | `[Gesture] → gesture_swipe_right -> Next Desktop [fired]` (`src=hid_rawxy`) on **hackintosh** |
| 23:34:50 | Same pattern: remote focus + partial seize + Mission Control fired on host |

`0xe00002c5` is macOS exclusive-access failure — another process (Mouser) holds the vendor interface.

### Two Logitech receivers amplify contention

Host enumerates **both** `046D:C548` (MX Master 4 dongle) and `046D:C52B` (second USB receiver). With `hidPassthroughDevices=046D:*`, Deskflow registers two passthrough devices. Seize wins on device 1, fails on device 2; Mouser keeps reading the uncontested interface and continues `hid_rawxy` gesture decode.

### Passthrough seize can succeed yet host Mouser still reconnects

At 08:20:20 both devices seized successfully, but five seconds later host Mouser lost its handle (`IOHIDDeviceSetReport failed: 0x-1FFFFD3B`), entered reconnect loop, and opened `PID=0xC52B UP=0xFFBC` — a **non-vendor** collection passthrough does not seize.

### Client path connects but never decodes gestures

**macbookpro** at 08:20:20:

```
Deskflow ingress connected: USB Receiver (feat=0x0D read-only)
RemoteDevice Deskflow ingress attach: USB Receiver
Listening for gesture events…
```

No `[Gesture] … [fired]` lines ever appear on the client. No `mouser-sink.json` on client (expected for passthrough manifest path). Battery/DPI init timeouts suggest readonly ingress without live HID++ report stream.

**tiny11** Mouser log stalled on local USB probe (`PID=0xC548`) — never reached RemoteDevice listener state during prior soak.

### Architectural gap: `decode_only` never suppresses host

When `deskflow.auto` detects host bridge + passthrough, Mouser sets `RemoteForwarder(decode_only=True)`. In that mode `should_forward()` is **always False** — host gesture suppression relies entirely on Deskflow seize removing USB access. Partial seize breaks that contract.

## Why This Approach (Legacy DMSR Relay)

Three approaches were considered:

**Legacy DMSR relay** ← **Selected**

Turn off `hidPassthroughEnabled` in Deskflow. Re-enable full `RemoteForwarder` event/report relay (`should_forward()` true when focus remote). Host Mouser keeps USB, suppresses local handling via bridge focus signal, forwards decoded events to focused client. This matches the pre–Tier 1.5 working behavior documented in `deskflow/docs/mouser-bridge.md`.

- Pros: Proven path; no seize contention; works with dual receivers; minimal code change (config only for Phase 0)
- Cons: Does not achieve "host Options+ blocked" goal; raw HID++ frames not forwarded (decoded events only)
- Best when: Need gestures working on slaves immediately; passthrough can wait

**Host yield on remote focus** ← Recommended long-term passthrough fix (not selected now)

When passthrough + remote focus, host Mouser pauses USB listener entirely instead of fighting seize / reconnecting to alternate interfaces.

- Pros: Makes passthrough design work as documented; fixes root contention
- Cons: Requires Mouser code change; still need client DFHR delivery debug
- Best when: Committed to Tier 1.5 after legacy restored

**Config/hardware pin** ← Quick adjunct, not standalone

Pin `hidPassthroughDevices=046D:C548`, unplug second dongle.

- Pros: May fix device-2 seize failure immediately
- Cons: Does not fix `decode_only` suppression gap or client non-delivery; fragile
- Best when: Combined with host-yield or as soak prep

## Key Decisions

1. **Disable HID passthrough for production use** until host-yield + client delivery are implemented and soak-signed. Set `hidPassthroughEnabled=false` in Deskflow server config on hackintosh.

2. **Re-enable legacy RemoteForward relay on host.** Explicit `settings.remote_forward.enabled=true` in Mouser `config.json` (required because `host_bridge` auto-detection only activates when passthrough is ON). Keep `deskflow.auto=true` for client sink auto-start. Ensure `passthrough_decode_only` is false / not forced.

3. **Keep MouserBridge + tokens aligned** across hackintosh and all clients (`mouserBridgeToken` / `mouserToken` / Mouser `remote_forward.token` / `remote_device.token`).

4. **Verify success via logs**, not feel alone:
   - Host remote focus: `[RemoteForward] Focus -> remote` then `[Gesture] … [fired]` should **stop** on host; `[RemoteForward]` should show event/report relay lines (if logged) or client receives DMSR.
   - Client: `[RemoteDevice]` connect + `[Gesture] … [fired]` on slave when gesturing with cursor there.
   - Deskflow: **no** `hid passthrough: seized` lines; **yes** DMSR relay activity.

5. **Defer passthrough re-enable** to a follow-up plan implementing host USB pause on remote focus, single-receiver pinning, and Windows client probe fix (tiny11 stuck local enumerate).

## Target Config Sketch (planning reference — do not apply blindly)

**Deskflow server (`hackintosh`):**
```
[server]
hidPassthroughEnabled=false
mouserBridgeEnabled=true
mouserBridgeToken=<shared>
```

**Mouser host (`hackintosh` config.json):**
```json
"remote_forward": {
  "enabled": true,
  "token": "<same as mouserBridgeToken>",
  "passthrough_decode_only": false
}
```

**Clients:** unchanged — `mouserEnabled=true`, `mouserToken=<shared>`, Mouser `deskflow.auto=true`.

## Open Questions

- Should `deskflow_integration.resolve_integration` gain a `host_bridge_legacy` mode so `remote_forward` auto-enables when bridge is on but passthrough is off? (Reduces manual config drift.)
- After legacy restore, is full event relay sufficient for thumb wheel / Smart Shift, or do some features require raw report relay?
- tiny11 Windows client: does legacy DMSR path avoid the stuck local USB probe, or does client still need "no local Logitech → skip probe" logic?
