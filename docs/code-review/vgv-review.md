# VGV Code Review — KVM Transparency (Tier 1.5)

**Scope:** Uncommitted KVM transparency work across Mouser and Deskflow (DFHR binary sink, deskflow auto-detect, HidGestureListener ingress, HidppProbe host decode).

**Verdict:** Needs work before merge — protocol and platform correctness issues on the hot path; tests exist but miss failure modes and platform probe logic.

---

## Summary

The Tier 1.5 architecture (JSON control + DFHR data on one loopback port, manifest-driven auto-start, listener ingress instead of virtual device) is sound and well-tested on happy paths (Mouser 8/8 scoped tests pass). However, several production-path bugs remain: non-blocking `sendAll` can drop DFHR frames, malformed DFHR can kill the TCP session, decode seeding races across threads, macOS HID++ probe cannot receive callbacks without a run loop, and Windows probe code calls a non-existent HID API. Address critical items before merge; important items affect first-connection reliability and operational safety.

---

## Critical — Must Fix Before Merge

### 1. `MouserClient::sendAll` — drops frames on non-blocking socket

**File:** `deskflow/src/lib/client/MouserClient.cpp:44-55`, `241-265`

After hello, the socket is set `O_NONBLOCK` (lines 241–248). `sendFrame` / `sendLine` call `sendAll`, which treats `wrote <= 0` as hard failure without retrying on `EAGAIN`/`EWOULDBLOCK`. `drainReplies` correctly handles non-blocking recv, but send does not.

- **Why:** High-frequency DFHR reports under backpressure will disconnect the client or drop reports silently.
- **Fix:** Retry `send` with `poll`/`select` on writable, or use a send queue in the worker thread (mirror pre-refactor behavior).

### 2. Malformed DFHR kills client session

**File:** `Mouser/core/remote_device.py:232-242`, `Mouser/core/hid_sink.py:28-31`

`try_decode_report_frame` raises `ValueError` when magic is wrong or payload too large. `_handle_client` does not catch this around line 233 — only JSON parse errors are caught.

- **Why:** Corrupt or mixed wire data terminates the entire client handler instead of resyncing or logging once.
- **Fix:** Wrap decode in `try/except ValueError`, log, discard one byte or disconnect gracefully with metric.

### 3. Cross-thread decode mutation race

**Files:** `Mouser/core/remote_device.py:446-456`, `Mouser/core/hid_gesture.py:1037+`

`_handle_update_decode` calls `HidGestureListener._seed_from_decode` on the remote-device server thread while the listener thread concurrently reads the same decode/thumb state in `_on_report`.

- **Why:** Undefined behavior on gesture routing; intermittent wrong thumb/wheel mapping.
- **Fix:** Post decode updates to the listener thread (queue + `call_soon_threadsafe`) or hold a shared lock for decode reads/writes.

### 4. macOS HID++ probe never receives IOHID callbacks

**File:** `deskflow/src/lib/server/HidppProbeMac.mm:207-218`

Probe schedules callbacks on `CFRunLoopGetCurrent()` but `waitForReport` only blocks on a `condition_variable`. No `CFRunLoopRun` / `CFRunLoopRunInMode` is called on that thread.

- **Why:** Host decode cache from probe always times out on macOS; forces Mouser bridge fallback.
- **Fix:** Run the current run loop during `waitForReport`, or use a dedicated probe thread with its own run loop (pattern: `OSXHidGrabber.mm`).

### 5. Windows probe uses invalid `HidD_GetUsagePage`

**File:** `deskflow/src/lib/server/HidppProbeWin.cpp:147-151`

`HidD_GetUsagePage` is not a standard HID API. Windows build will fail or behave incorrectly.

- **Why:** Phase 3 host probe non-functional on Windows.
- **Fix:** Use `HidD_GetPreparsedData` + `HidP_GetCaps` / `HidP_GetValueCaps` for usage page filtering (match macOS `kIOHIDPrimaryUsagePageKey` logic).

---

## Important — Should Fix

### 6. Early `connect` ACK before Deskflow ingress is ready

**Files:** `Mouser/core/mouse_hook_base.py`, `Mouser/core/hid_gesture.py`, `Mouser/core/remote_device.py`

Transparent `connect` returns `ok` before `_try_connect_deskflow` completes. DFHR frames arriving before `_device_connected` / listener attach are dropped.

- **Fix:** Defer `connect` ACK until ingress attach succeeds, or buffer early frames in `DeskflowSinkDevice`.

### 7. `request_deskflow_attach` control flags without synchronization

**File:** `Mouser/core/hid_gesture.py`

Attach/clear flags mutated from engine thread while `_main_loop` / USB probe runs without a shared lock.

- **Fix:** Use the same lock as reconnect/USB probe paths.

### 8. Stale `mouser-sink.json` after abnormal exit

**File:** `deskflow/src/lib/client/ServerProxy.cpp:60`

`clearMouserSinkManifest()` only in destructor. Crash or kill leaves manifest → Mouser auto-starts sink when Deskflow is down.

- **Fix:** Clear on disconnect/stop; add manifest TTL or `pid` field validation.

### 9. `deskflow.auto` defaults true

**File:** `Mouser/core/deskflow_integration.py`, `Mouser/core/engine.py`

Auto-enables loopback server/forwarder from manifest without explicit user opt-in beyond default.

- **Fix:** Document clearly; consider `deskflow.auto: false` default until soak passes.

### 10. Coordinated DFHR deploy — no mixed-version fallback

**File:** `deskflow/src/lib/client/HidConsumer.cpp`

Hot path always emits DFHR; JSON line encoder is test-only shim.

- **Fix:** Document upgrade order (Deskflow + Mouser together); or keep server-side JSON accept for one release.

### 11. Global sink queue not flushed on disconnect

**File:** `Mouser/core/hid_deskflow_backend.py`

`DeskflowSinkDevice` queue retains stale reports across reconnect.

- **Fix:** `flush()` on detach/disconnect.

### 12. HID device contention — host probe vs Mouser bridge

**Files:** `deskflow/src/lib/server/HidppProbe*.cpp/mm`, Mouser USB hook

Probe opens physical device while Mouser may hold it on bridge host.

- **Fix:** Serialize access or probe only when Mouser bridge is off.

### 13. Connect-line VID/PID parse failure → silent probe miss

**File:** `deskflow/src/lib/server/Server.cpp:740+`

Wrong identity from connect line yields empty decode cache (bridge fallback only).

- **Fix:** Log at warn level; validate against attached HID device list.

### 14. Non-transparent DFHR path feeds global sink unnecessarily

**File:** `Mouser/core/remote_device.py:279-287`

`get_deskflow_sink().feed_report()` always called even when using detached `_raw_decoder`.

- **Fix:** Feed sink only in `listener_ingress` / transparent mode.

---

## Suggestions — Nice to Have

- Remove dead `_authenticate` / `_read_message` in `remote_device.py` (superseded by `_read_hello`).
- Remove empty `finally: pass` in `_handle_client`.
- Remove redundant `_passthrough_locked` in `remote_forward.py`.
- Document or use DFHR `device_id` field (currently ignored in `_handle_binary_report`).
- Add malformed-DFHR session test, MouserClient backpressure test, cross-repo DFHR golden vector.

---

## Simplicity Assessment

- **Lines that could be removed:** ~80–110 (dead auth, JSON shim, duplicate flags)
- **Unnecessary abstractions:** `encodeHidReportAsSinkFrame` one-liner; unused `deskflow_sink_hid_info()`
- **YAGNI violations:** Stale JSON compat shim; `_passthrough_locked` never toggled
- **Complexity verdict:** Minor tweaks needed on glue; probe + mixed wire dominate

---

## Testing Assessment

- **New code with tests:** Partial — `hid_sink`, integration, listener ingress, binary frames covered; `hid_deskflow_backend`, probe logic, conf fallback not covered
- **Test quality:** Meaningful happy paths; missing failure/resync/backpressure cases
- **State management test coverage:** N/A (Python listener, not Bloc)
- **UI component test coverage:** N/A
