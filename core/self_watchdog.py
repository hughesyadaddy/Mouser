"""In-process self-check: catch "alive but useless" before the user does.

Three symptoms are sampled once a minute from the Qt main thread:

* process CPU time (user+sys) above 50 % for three ticks while the HID
  listener's empty-read counter climbs -- a read loop spinning on a dead
  backend (the 27 h / 100 % CPU incident);
* CGEventTap re-enables above 10 per hour -- macOS disables the tap when
  the callback stalls, so a climbing count means the main thread is starved;
* the tick itself arriving more than 2 s late -- the main thread was blocked.

First trip: one structured log line and a listener reconnect. A second
consecutive trip exits with status 3 so launchd (``KeepAlive
SuccessfulExit=false``) respawns a clean process; ``watchdog_exit=false`` in
config degrades that to log-only.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque

TICK_S = 60.0
CPU_TRIP_RATIO = 0.5
CPU_TRIP_TICKS = 3
# Empty reads per tick that mean the loop is not waiting: a healthy read-only
# session times out at most ~60 times a minute (1 s reads).
SPIN_EMPTY_READS_PER_TICK = 600
TAP_REENABLE_TRIP_PER_HOUR = 10
HEARTBEAT_DRIFT_S = 2.0
# A tick late by a whole interval or more is a suspend/resume, not a stall.
HEARTBEAT_SUSPEND_S = TICK_S
EXIT_STATUS = 3


class SelfWatchdog:
    def __init__(
        self,
        *,
        hid_listener=lambda: None,
        mouse_hook=lambda: None,
        reconnect=lambda: None,
        exit_enabled=lambda: True,
        exit_fn=os._exit,
        cpu_seconds=time.process_time,
        monotonic=time.monotonic,
        log=print,
        tick_s: float = TICK_S,
    ):
        self._hid_listener = hid_listener
        self._mouse_hook = mouse_hook
        self._reconnect = reconnect
        self._exit_enabled = exit_enabled
        self._exit_fn = exit_fn
        self._cpu_seconds = cpu_seconds
        self._monotonic = monotonic
        self._log = log
        self._tick_s = tick_s
        self._last_tick = monotonic()
        self._last_cpu = cpu_seconds()
        # Counters start at zero with the process, as does this watchdog.
        self._last_empty_reads = 0
        self._last_tap_reenables = 0
        self._hot_ticks = 0
        self._tap_reenable_window = deque(maxlen=int(3600 / tick_s) or 1)
        self.trips = 0
        self.consecutive_trips = 0

    def tick(self) -> list[str]:
        """Sample once; returns the reasons that tripped (empty = healthy)."""
        now = self._monotonic()
        drift = now - self._last_tick - self._tick_s
        elapsed = max(now - self._last_tick, 1e-6)
        self._last_tick = now

        cpu = self._cpu_seconds()
        cpu_ratio = (cpu - self._last_cpu) / elapsed
        self._last_cpu = cpu

        empty_reads = getattr(
            self._hid_listener(), "empty_read_total", self._last_empty_reads
        )
        empty_delta = empty_reads - self._last_empty_reads
        self._last_empty_reads = empty_reads

        tap_reenables = getattr(
            self._mouse_hook(), "tap_reenable_total", self._last_tap_reenables
        )
        tap_delta = tap_reenables - self._last_tap_reenables
        self._last_tap_reenables = tap_reenables
        self._tap_reenable_window.append(tap_delta)
        tap_per_hour = sum(self._tap_reenable_window)

        if cpu_ratio > CPU_TRIP_RATIO and empty_delta >= SPIN_EMPTY_READS_PER_TICK:
            self._hot_ticks += 1
        else:
            self._hot_ticks = 0

        reasons = []
        if self._hot_ticks >= CPU_TRIP_TICKS:
            reasons.append(
                f"cpu={cpu_ratio:.2f} empty_reads/tick={empty_delta} hot_ticks={self._hot_ticks}"
            )
        if tap_per_hour > TAP_REENABLE_TRIP_PER_HOUR:
            reasons.append(f"tap_reenables/h={tap_per_hour}")
        if HEARTBEAT_DRIFT_S < drift < HEARTBEAT_SUSPEND_S:
            reasons.append(f"heartbeat_drift={drift:.1f}s")

        if not reasons:
            # Still hot after a trip means the reconnect has not helped yet;
            # only a genuinely idle tick disarms the escalation.
            if self._hot_ticks == 0:
                self.consecutive_trips = 0
            return reasons

        self.trips += 1
        self.consecutive_trips += 1
        self._log(
            "[Watchdog] trip "
            f"n={self.trips} consecutive={self.consecutive_trips} "
            + " ".join(reasons)
        )
        if self.consecutive_trips >= 2:
            self._exit_for_respawn(reasons)
            return reasons
        self._hot_ticks = 0
        self._tap_reenable_window.clear()
        try:
            self._reconnect()
        except Exception as exc:  # noqa: BLE001 - recovery must not raise into Qt
            self._log(f"[Watchdog] reconnect request failed: {exc!r}")
        return reasons

    def _exit_for_respawn(self, reasons) -> None:
        if not self._exit_enabled():
            self._log("[Watchdog] exit disabled by config (watchdog_exit=false); staying up")
            return
        self._log(
            f"[Watchdog] exiting with status {EXIT_STATUS} for launchd respawn: "
            + " ".join(reasons)
        )
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - stdout may be the log stream
            pass
        self._exit_fn(EXIT_STATUS)
