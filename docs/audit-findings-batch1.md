# M5/K4 audit findings queue (for the fix batch)
## R3 (key_simulator/engine/app_detector) — 0 MB/h live
- key_simulator.py:1016-1027 `_send_media_key` lacks its own @_autoreleased (+996 B/iter pool-less). Decorate.
- app_detector.py:188-193 `_autoreleased` unused; apply to `_idle_check`/`_deliver` or delete.
## R2 (hooks/native tap) — 0.000 MB/h measured
- mouse_hook_macos.py:493-500 guard no-op path silent → add `passthrough_leaked_total` counter + watchdog line + log-once.
- mouse_hook_macos.py:1049-1062 native tap not enforced at runtime → fleet-health FAIL on `tap_kind != native` (log line), dylib build in dev flow.
- mouse_hook_macos.py:917,930 stale `hg` closure in wake observers → read `self._hid_gesture` inside closure.
- mouse_hook_base.py:476-479,681-692 f-strings before `debug_mode` gate → guard.
- mouser_tap.m:640-646 / mouse_hook_macos.py:1320-1325 drop-warning per 50 ms → rate-limit 1/s.
## R6 (catalog/updater/config/startup/QML) — items 1-5 forwarded to PR7 builder
- HotspotDot.qml:122-148 full-area Canvas per hotspot = 4.8 MB each (29 MB MX Master) → Rectangle/Shape line. [PR7]
- MousePage.qml:988-999 sourceSize + drop mipmap. [PR7]
- MousePage.qml:1293,1326,1609 ActionChip Repeaters → Loader on selectedButton. [PR7]
- HotspotDot.qml:87-92 infinite pulse while hidden → clear selectedButton in dismiss(). [PR7]
- MousePage.qml:2035-2108 debug card → Loader on debugMode. [PR7 optional]
- app_catalog refresh=True per Add-Profile open re-walks /Applications (transient) → throttle 10 min. [fix batch]
- log_setup: no qInstallMessageHandler; Qt warnings go to launchd.err.log unrotated → bridge with dedupe. [fix batch]
## R1 (hid_gesture/sinks) — 0 MB/h; CPU/reconnect churn
- hid_gesture.py:3850 `_wait_reconnect` returns instantly when `_deskflow_attach is not None` → 3131 readonly reconnect cycles/s possible. Fix: `_attach_gen` counter captured on entry, early-return only on change; apply 1→30 s ladder to readonly reconnects. Test in tests/test_hid_gesture_reconnect.py (prove2 experiment B in scratchpad/audit-r1).
- hid_gesture.py:1617/1626 `request_deskflow_attach` fresh dict per call defeats `is attach` clear at :3347-3355 → compare by content; memoise `_vendor_hid_infos` ≥1 s unless `_device_arrival`.
- hid_gesture.py:1104 `_MacHidManager.pump` no rc==1 guard → busy-spin risk; add sleep.
- hid_gesture.py:222/3836 sleeping-mouse: 3 timeouts → forced reconnect but "healthy" session resets backoff → re-probe every ~47 s; count timeouts as unhealthy regardless of session age.
