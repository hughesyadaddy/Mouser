# Architecture Review — KVM Transparency

**Scope:** DFHR binary sink, deskflow_integration auto-detect, HidGestureListener deskflow attach, HidppProbe host decode, remote_device listener ingress.

**Verdict:** Needs work — Tier 1.5 layering is sound; lifecycle, mode ownership, and error boundaries need tightening.

---

## Summary

The intended data flow is clear and appropriately layered:

```
Deskflow DFHR → RemoteDeviceServer → DeskflowSinkDevice queue
  → HidGestureListener._rx / _on_report → hook / engine
```

JSON remains the control plane (hello, connect, update_decode). This matches the technical review (single port, no parallel hidSinkPort). Architecture is **not** Tier 2 OS-level HID — internal coupling to `remote_device` and deskflow modules remains acceptable for Tier 1.5.

**Critical architecture violations:** 0 (no layer inversion; Python monolith pattern is consistent with Mouser).

**Important gaps:** mode ownership, async attach timing, error resync, singleton lifecycle.

---

## Important — Should Fix

### 1. Non-transparent DFHR always feeds global sink

**File:** `Mouser/core/remote_device.py:279-287`

When using detached `_raw_decoder` (non-listener ingress), `get_deskflow_sink().feed_report()` still runs, orphaning frames in the global queue.

- **Why:** Violates single-owner principle for report routing.
- **Fix:** Gate `feed_report` on `self._listener_ingress` or transparent transport mode.

### 2. Duplicate decode-context application

**Files:** `remote_device.py:_build_raw_decoder`, `hid_gesture.py:_seed_from_decode`

Same role/CID/thumb mapping logic in two places — drift risk between host bridge and client ingress.

- **Fix:** Extract `apply_decode_dict(listener, decode) -> bool` shared helper.

### 3. Malformed DFHR aborts entire TCP session

**File:** `Mouser/core/remote_device.py:232-242`

No resync boundary — one bad header kills multiplexed JSON+DFHR session.

- **Fix:** Frame-level error handling; optional byte-shift resync after logging.

### 4. `connect` ACK before async Deskflow ingress attach

**Files:** `mouse_hook_base.py:655-664`, `hid_gesture.py:1018-1028`

Control plane reports success while data plane listener may not be reading yet.

- **Fix:** Synchronous attach handshake or startup frame buffer in sink device.

### 5. Global singleton `get_deskflow_sink()` coupling

**File:** `Mouser/core/hid_deskflow_backend.py`

Transport, listener lifecycle, and legacy virtual-device paths share one queue without explicit mode enum.

- **Fix:** Introduce `SinkMode` (OFF / INGRESS / LEGACY_VIRTUAL) with assert on transitions.

---

## Suggestions

- Extract shared decode-dict helper (see #2).
- Add `deskflow.conf` host-bridge auto-detect tests (manifest-only today).
- Clear/isolate sink queue on ingress attach/detach.
- Remove dead `_authenticate` / `_read_message` paths.
- Document: physical-device-wins on connect vs `_deskflow_attach` blocking USB probe.

---

## Layer Separation (Mouser)

| Layer | Modules | Assessment |
| --- | --- | --- |
| Transport | `remote_device`, `remote_forward`, `hid_sink` | OK — framing isolated |
| Integration | `deskflow_integration`, `engine` | OK — config discovery |
| Device adapter | `hid_deskflow_backend` | OK — thin queue shim |
| Gesture / hook | `hid_gesture`, `mouse_hook_base` | OK — ingress attach point |

No presentation-layer violations detected. `engine.py` orchestration is appropriate for this codebase.

---

## Layer Separation (Deskflow)

| Layer | Modules | Assessment |
| --- | --- | --- |
| Wire codec | `HidSink` | OK |
| Client delivery | `MouserClient`, `HidConsumer` | OK — worker thread queue |
| Manifest | `MouserSinkManifest` | OK — filesystem contract |
| Host probe | `HidppProbe*` | OK — platform split; run-loop wiring incomplete |

---

## Dependency Direction

- Mouser does not import Deskflow (good).
- Deskflow does not import Mouser (good).
- Contract is filesystem manifest + TCP protocol (documented in roadmap).

---

## State Management

Listener thread owns HID read loop; remote server thread owns TCP. Cross-thread `_seed_from_decode` without marshaling is the primary concurrency concern (see VGV review #3).
