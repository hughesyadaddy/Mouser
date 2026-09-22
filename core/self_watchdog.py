"""In-process self-check: catch "alive but useless" before the user does.

Four symptoms are sampled once a minute from the Qt main thread:

* process CPU time (user+sys) above 50 % for three ticks while the HID
  listener's empty-read counter climbs -- a read loop spinning on a dead
  backend (the 27 h / 100 % CPU incident);
* CGEventTap re-enables above 10 per hour -- macOS disables the tap when
  the callback stalls, so a climbing count means the main thread is starved;
* the tick itself arriving more than 2 s late on two consecutive ticks --
  the main thread is blocked (one late tick is timer coalescing / App Nap);
* physical memory footprint growing faster than 20 MB/h for three hours, or
  above 1.5 GB outright -- the 81 h / 1.8 GB CGEvent leak and the 6 GB
  NSXPCConnection leak (see ``docs/memory-guards.md``).

First trip: one structured log line and a listener reconnect. A second
consecutive trip exits with status 3 so launchd (``KeepAlive
SuccessfulExit=false``) respawns a clean process; ``watchdog_exit=false`` in
config or a process launchd does not own degrades that to a log line plus a
status-bar message asking for a restart.
"""

from __future__ import annotations

import ctypes
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
HEARTBEAT_TRIP_TICKS = 2
# A tick late by a whole interval or more is a suspend/resume, not a stall.
HEARTBEAT_SUSPEND_S = TICK_S
EXIT_STATUS = 3

# Memory guard. The ``[mem]`` line reports a least-squares slope over the
# last MEM_WINDOW_SAMPLES ticks (one hour at TICK_S). The growth trip is a
# windowed rule over a MEM_GROWTH_SUSTAIN_S ring (three hours) that no
# small periodic release can defeat: the ring must be full, the footprint
# must be at least MEM_NET_GROWTH_TRIP_MB above the ring's minimum, the
# three-hour slope must be at least MEM_GROWTH_TRIP_MB_H, and every third
# of the ring must itself slope at least MEM_PART_SLOPE_MB_H, so a single
# level step (opening the window, an update check) inside an otherwise
# flat window does not count as growth. The success criterion for a fixed
# build is <= 0.1 MB/h, so 20 MB/h is unambiguous: it is the 2026-09 rate.
MEM_WINDOW_SAMPLES = 60
MEM_MIN_SLOPE_SAMPLES = 10
MEM_GROWTH_TRIP_MB_H = 20.0
MEM_GROWTH_SUSTAIN_S = 3 * 3600.0
MEM_SUSTAIN_SAMPLES = int(MEM_GROWTH_SUSTAIN_S / TICK_S)
MEM_NET_GROWTH_TRIP_MB = MEM_GROWTH_TRIP_MB_H * MEM_GROWTH_SUSTAIN_S / 3600.0
MEM_SUSTAIN_PARTS = 3
MEM_PART_SLOPE_MB_H = MEM_GROWTH_TRIP_MB_H / 2
# A gap between samples this long is a suspend: the ring restarts, since a
# fit across the gap says nothing about the rate on either side of it.
MEM_GAP_RESET_S = 2 * TICK_S
MEM_FOOTPRINT_TRIP_MB = 1500.0
MEM_LOG_INTERVAL_S = 60.0
# After a memory trip that could not exit (no supervisor), stay quiet for
# an hour rather than nagging the status bar every tick.
MEM_TRIP_MUTE_S = 3600.0

_RUSAGE_INFO_V4 = 4
_RUSAGE_INFO_V4_SIZE = 16 + 35 * 8


class _RusageInfoV4(ctypes.Structure):
    """``struct rusage_info_v4`` from <sys/resource.h>: ``uint8_t ri_uuid[16]``
    followed by 35 ``uint64_t``; ``ri_phys_footprint`` is the 8th uint64.
    Layout shared with ``deskflow/tools/fleet-soak``."""

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


assert ctypes.sizeof(_RusageInfoV4) == _RUSAGE_INFO_V4_SIZE

_proc_pid_rusage = None


def phys_footprint_mb(pid: int | None = None) -> float | None:
    """``ri_phys_footprint`` of ``pid`` (default: this process) in MB via
    ``proc_pid_rusage(RUSAGE_INFO_V4)``; None off macOS or on any failure.
    This is the number Activity Monitor's "Memory" column shows."""
    global _proc_pid_rusage
    if sys.platform != "darwin":
        return None
    try:
        if _proc_pid_rusage is None:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            fn = libproc.proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int
            _proc_pid_rusage = fn
        buf = _RusageInfoV4()
        rc = _proc_pid_rusage(
            int(os.getpid() if pid is None else pid), _RUSAGE_INFO_V4, ctypes.byref(buf)
        )
        if rc != 0:
            return None
        return buf.ri_phys_footprint / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001 - a broken sampler must never trip anything
        return None


def growth_slope_mb_h(samples) -> float | None:
    """Least-squares slope of ``(t_seconds, mb)`` samples in MB/h, or None
    when there are too few samples or no spread in time."""
    n = len(samples)
    if n < MEM_MIN_SLOPE_SAMPLES:
        return None
    mean_t = sum(t for t, _ in samples) / n
    mean_mb = sum(mb for _, mb in samples) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in samples)
    if var_t <= 0.0:
        return None
    cov = sum((t - mean_t) * (mb - mean_mb) for t, mb in samples)
    return cov / var_t * 3600.0


def sustained_growth_mb_h(ring) -> float | None:
    """The three-hour growth rate when the ring proves a sustained leak,
    else None. Conditions (all required): the ring is full; the last
    sample sits at least MEM_NET_GROWTH_TRIP_MB above the ring's minimum
    (net growth that small periodic releases cannot hide); the full-ring
    slope is at least MEM_GROWTH_TRIP_MB_H; and each of the
    MEM_SUSTAIN_PARTS consecutive slices slopes at least
    MEM_PART_SLOPE_MB_H, which rules out one level step in a flat window
    (a step's least-squares slope is confined to the slice holding it)."""
    if len(ring) < ring.maxlen:
        return None
    samples = list(ring)
    net = samples[-1][1] - min(mb for _, mb in samples)
    if net < MEM_NET_GROWTH_TRIP_MB:
        return None
    slope = growth_slope_mb_h(samples)
    if slope is None or slope < MEM_GROWTH_TRIP_MB_H:
        return None
    part = len(samples) // MEM_SUSTAIN_PARTS
    for i in range(MEM_SUSTAIN_PARTS):
        part_slope = growth_slope_mb_h(samples[i * part:(i + 1) * part])
        if part_slope is None or part_slope < MEM_PART_SLOPE_MB_H:
            return None
    return slope


def _is_mem_reason(reason: str) -> bool:
    return reason.startswith("mem ")


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
        footprint=phys_footprint_mb,
        log=print,
        status=lambda message: None,
        tick_s: float = TICK_S,
    ):
        self._hid_listener = hid_listener
        self._mouse_hook = mouse_hook
        self._reconnect = reconnect
        self._exit_enabled = exit_enabled
        self._exit_fn = exit_fn
        self._cpu_seconds = cpu_seconds
        self._monotonic = monotonic
        self._footprint = footprint
        self._log = log
        self._status = status
        self._tick_s = tick_s
        self._last_tick = monotonic()
        self._last_cpu = cpu_seconds()
        # Counters start at zero with the process, as does this watchdog.
        self._last_empty_reads = 0
        self._last_tap_reenables = 0
        self._hot_ticks = 0
        self._late_ticks = 0
        self._tap_reenable_window = deque(maxlen=int(3600 / tick_s) or 1)
        # Memory: one hour of (monotonic, MB) samples for the reported
        # slope, three hours for the sustained-growth trip.
        self._mem_samples = deque(maxlen=MEM_WINDOW_SAMPLES)
        self._mem_ring = deque(maxlen=MEM_SUSTAIN_SAMPLES)
        self._mem_last_log = None
        self._mem_muted_until = None
        self.footprint_mb = None
        self.peak_mb = None
        self.growth_mb_h = None
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
            self._late_ticks += 1
        else:
            self._late_ticks = 0
        if self._late_ticks >= HEARTBEAT_TRIP_TICKS:
            reasons.append(f"heartbeat_drift={drift:.1f}s late_ticks={self._late_ticks}")
        reasons.extend(self._sample_memory(now))

        if not reasons:
            # Still hot/late after a trip means the reconnect has not helped
            # yet; only a genuinely idle tick disarms the escalation.
            if self._hot_ticks == 0 and self._late_ticks == 0:
                self.consecutive_trips = 0
            return reasons

        self.trips += 1
        self.consecutive_trips += 1
        # tap= says which CGEventTap callback is live ("python" = DEGRADED,
        # the PyObjC trampoline path; see mouse_hook_macos.MouseHook.tap_kind).
        tap_kind = getattr(self._mouse_hook(), "tap_kind", None) or "unknown"
        self._log(
            "[Watchdog] trip "
            f"n={self.trips} consecutive={self.consecutive_trips} tap={tap_kind} "
            + " ".join(reasons)
        )
        if self.consecutive_trips >= 2:
            self._exit_for_respawn(reasons, now)
            return reasons
        if not any(_is_mem_reason(reason) for reason in reasons):
            # Only a spinning/stalled listener is helped by a reconnect;
            # memory is fixed by nothing short of a restart.
            self._hot_ticks = 0
            self._late_ticks = 0
            self._tap_reenable_window.clear()
            try:
                self._reconnect()
            except Exception as exc:  # noqa: BLE001 - recovery must not raise into Qt
                self._log(f"[Watchdog] reconnect request failed: {exc!r}")
        return reasons

    def _sample_memory(self, now: float) -> list[str]:
        """Record one footprint sample, refresh the slope, emit the ``[mem]``
        line (rate-limited) and return the memory trip reasons, if any."""
        try:
            mb = self._footprint()
        except Exception:  # noqa: BLE001 - the sampler is a diagnostic, never a fault
            mb = None
        if mb is None:
            return []
        mb = float(mb)
        self.footprint_mb = mb
        self.peak_mb = mb if self.peak_mb is None else max(self.peak_mb, mb)
        if self._mem_ring and now - self._mem_ring[-1][0] > MEM_GAP_RESET_S:
            self._mem_samples.clear()
            self._mem_ring.clear()
        self._mem_samples.append((now, mb))
        self._mem_ring.append((now, mb))
        self.growth_mb_h = growth_slope_mb_h(self._mem_samples)

        if self._mem_last_log is None or now - self._mem_last_log >= MEM_LOG_INTERVAL_S:
            self._mem_last_log = now
            growth = "n/a" if self.growth_mb_h is None else f"{self.growth_mb_h:.1f}"
            self._log(
                f"[mem] footprint_mb={mb:.1f} peak_mb={self.peak_mb:.1f} growth_mb_h={growth}"
            )

        if self._mem_muted_until is not None:
            if now < self._mem_muted_until:
                return []
            self._mem_muted_until = None

        reasons = []
        sustained = sustained_growth_mb_h(self._mem_ring)
        if sustained is not None:
            span_h = (now - self._mem_ring[0][0]) / 3600.0
            reasons.append(f"mem growth/h={sustained:.1f} over {span_h:.1f}h")
        if mb > MEM_FOOTPRINT_TRIP_MB:
            reasons.append(f"mem footprint_mb={mb:.0f} > {MEM_FOOTPRINT_TRIP_MB:.0f}")
        return reasons

    def _exit_for_respawn(self, reasons, now: float | None = None) -> None:
        if not self._exit_enabled():
            self._log("[Watchdog] exit disabled (no supervisor or watchdog_exit=false); staying up")
            self._status("Mouser needs a restart: " + ", ".join(reasons))
            if any(_is_mem_reason(reason) for reason in reasons):
                # Nothing short of a restart fixes memory; do not re-trip
                # every minute on a seat that cannot respawn.
                self._mem_muted_until = (
                    self._monotonic() if now is None else now
                ) + MEM_TRIP_MUTE_S
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
