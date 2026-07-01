# Test Quality Review — KVM Transparency

**Scope:** Mouser `test_deskflow_*`, `test_hid_sink`, `test_remote_binary_frames`; Deskflow `HidConsumerTests`, `HidppProbeTests`.

**Verdict:** Needs work — happy paths pass; critical modules and failure modes under-tested.

**Test run:** Mouser 8/8 pass (`pytest` scoped). Deskflow `HidConsumerTests` 8/8 where built; `HidppProbeTests` not in local `build-test` target.

---

## Critical — Must Fix Before Merge

### 1. `hid_deskflow_backend.py` — zero tests

Queue adapter on DFHR ingress hot path (`feed_report`, `read`, backpressure). No unit tests.

- **Fix:** Test queue feed/read, overflow behavior, flush on disconnect.

### 2. `probeHidppDecode()` — completely untested

Deskflow host decode context (feat index, catalog defaults) has no unit tests. Wrong context fails silently at runtime.

- **Fix:** Add `HidppProbeTests` to CI build; test catalog fallback, JSON round-trip, parse helpers.

### 3. `resolve_integration()` — partial coverage

Only manifest path tested in `test_deskflow_integration.py`. `deskflow.conf` fallback and `host_bridge` branches untested.

- **Fix:** Temp conf file fixtures for each integration mode.

### 4. Integration tests use fixed `time.sleep`

**Files:** `test_remote_binary_frames.py`, `test_deskflow_listener_ingress.py`

Fixed sleeps instead of `_wait_until` / event predicates → CI flakiness risk.

- **Fix:** Poll on connection flag or queue depth with timeout.

### 5. Tautological assertion in Deskflow tests

`deliverRawHidReportToMouserSkipsNullClient` uses `QVERIFY(true)` — verifies nothing.

- **Fix:** Assert client not called / no frame queued when client is null.

---

## Important — Should Fix

### 6. Binary wire under-tested vs JSON path

`test_remote_binary_frames` covers one `gesture_down`; `test_remote_raw_frames` has nine scenarios including failures.

- **Fix:** Port failure/malformed cases to DFHR tests.

### 7. `test_hid_sink` missing edge cases

No tests for: malformed magic, oversize payload, empty payload, multiple frames in one buffer.

- **Fix:** Parametrize decode error and partial-buffer cases.

### 8. Listener ingress — no disconnect/multi-event tests

`test_deskflow_listener_ingress` does not assert post-disconnect detach or move/thumb sequences.

- **Fix:** Extend with disconnect + multi-report sequence.

### 9. No cross-repo DFHR golden vector

No test proving Mouser `try_decode_report_frame` accepts bytes from Deskflow `encodeHidReportFrame`.

- **Fix:** Shared hex fixture in Mouser test (document C++ source of truth).

### 10. Duplicated `_StubHook` / `_Client` harness

Copied across three remote test modules — drift risk.

- **Fix:** Extract `tests/helpers/remote_socket.py`.

### 11. `use_transparent_transport` positive path untested

Override `transparent_transport: false` not tested.

- **Fix:** Unit test on `deskflow_integration.use_transparent_transport`.

### 12. `HidConsumerTests` anchors on stale JSON encoder

No test of actual `MouserClient::deliverReport` DFHR delivery path.

- **Fix:** Mock client; assert `sendFrame` payload matches DFHR layout.

---

## Suggestions

- Add `is_json_line_start` unit tests in `test_hid_sink`.
- Build and run `HidppProbeTests` in Deskflow CI.
- Remove JSON compat test once DFHR is sole wire format (or mark `@deprecated`).

---

## Coverage Audit Summary

| Module | Test file | Status |
| --- | --- | --- |
| `hid_sink.py` | `test_hid_sink.py` | Partial |
| `deskflow_integration.py` | `test_deskflow_integration.py` | Partial |
| `remote_device.py` (DFHR) | `test_remote_binary_frames.py` | Partial |
| `hid_gesture.py` (ingress) | `test_deskflow_listener_ingress.py` | Partial |
| `hid_deskflow_backend.py` | — | Missing |
| `HidSink.cpp` / `HidConsumer` | `HidConsumerTests` | Partial (JSON bias) |
| `HidppProbe` | `HidppProbeTests` | Not built in CI |
