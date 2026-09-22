import unittest
from types import SimpleNamespace

from core import self_watchdog
from core.self_watchdog import SelfWatchdog


class _Fixture:
    def __init__(self, *, exit_enabled=True, footprint_mb=None):
        self.now = 1000.0
        self.cpu = 0.0
        self.hg = SimpleNamespace(empty_read_total=0)
        self.hook = SimpleNamespace(tap_reenable_total=0, tap_kind="native")
        self.reconnects = 0
        self.exits = []
        self.logs = []
        self.statuses = []
        self.exit_enabled = exit_enabled
        # None = sampler unavailable (Linux): the memory guard stays inert.
        self.footprint_mb = footprint_mb
        self.wd = SelfWatchdog(
            hid_listener=lambda: self.hg,
            mouse_hook=lambda: self.hook,
            reconnect=self._reconnect,
            exit_enabled=lambda: self.exit_enabled,
            exit_fn=self.exits.append,
            cpu_seconds=lambda: self.cpu,
            monotonic=lambda: self.now,
            footprint=lambda: self.footprint_mb,
            log=self.logs.append,
            status=self.statuses.append,
        )

    def _reconnect(self):
        self.reconnects += 1

    def tick(self, *, cpu_ratio=0.02, empty_reads=10, tap_reenables=0, late=0.0,
             grow_mb_h=0.0):
        self.now += self_watchdog.TICK_S + late
        self.cpu += cpu_ratio * (self_watchdog.TICK_S + late)
        self.hg.empty_read_total += empty_reads
        self.hook.tap_reenable_total += tap_reenables
        if self.footprint_mb is not None:
            self.footprint_mb += grow_mb_h * (self_watchdog.TICK_S + late) / 3600.0
        return self.wd.tick()

    def run_hours(self, hours, **kwargs):
        """Tick for ``hours`` of wall time; returns the reasons of the last tick."""
        reasons = []
        for _ in range(int(hours * 3600 / self_watchdog.TICK_S)):
            reasons = self.tick(**kwargs)
        return reasons

    @property
    def mem_lines(self):
        return [line for line in self.logs if line.startswith("[mem] ")]

    @property
    def trip_lines(self):
        return [line for line in self.logs if line.startswith("[Watchdog] trip")]


class SelfWatchdogTests(unittest.TestCase):
    def test_healthy_ticks_never_trip(self):
        fx = _Fixture()
        for _ in range(120):
            self.assertEqual(fx.tick(), [])
        self.assertEqual(fx.reconnects, 0)
        self.assertEqual(fx.exits, [])
        self.assertEqual(fx.logs, [])

    def test_spinning_read_loop_trips_after_three_hot_ticks_then_reconnects(self):
        fx = _Fixture()
        self.assertEqual(fx.tick(cpu_ratio=0.99, empty_reads=100_000), [])
        self.assertEqual(fx.tick(cpu_ratio=0.99, empty_reads=100_000), [])
        reasons = fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(len(reasons), 1)
        self.assertIn("cpu=0.99", reasons[0])
        self.assertEqual(fx.reconnects, 1)
        self.assertEqual(fx.exits, [])
        self.assertEqual(len(fx.logs), 1)
        self.assertIn("[Watchdog] trip n=1 consecutive=1", fx.logs[0])

    def test_high_cpu_without_empty_reads_is_not_a_spin(self):
        fx = _Fixture()
        for _ in range(10):
            self.assertEqual(fx.tick(cpu_ratio=0.99, empty_reads=5), [])
        self.assertEqual(fx.reconnects, 0)

    def test_second_consecutive_trip_exits_with_status_3(self):
        fx = _Fixture()
        for _ in range(3):
            fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.reconnects, 1)
        # The reconnect did not help: the loop stays hot. The hot-tick
        # counter restarts after the first trip, so three more ticks.
        fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.exits, [])
        fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.exits, [self_watchdog.EXIT_STATUS])
        self.assertEqual(fx.reconnects, 1)

    def test_recovery_between_trips_resets_the_escalation(self):
        fx = _Fixture()
        for _ in range(3):
            fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.wd.consecutive_trips, 1)
        fx.tick()
        self.assertEqual(fx.wd.consecutive_trips, 0)
        for _ in range(3):
            fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.reconnects, 2)
        self.assertEqual(fx.exits, [])

    def test_exit_disabled_by_config_only_logs(self):
        fx = _Fixture(exit_enabled=False)
        for _ in range(6):
            fx.tick(cpu_ratio=0.99, empty_reads=100_000)
        self.assertEqual(fx.exits, [])
        self.assertTrue(any("watchdog_exit=false" in line for line in fx.logs))

    def test_tap_reenables_over_ten_per_hour_trip(self):
        fx = _Fixture()
        for _ in range(10):
            self.assertEqual(fx.tick(tap_reenables=1), [])
        reasons = fx.tick(tap_reenables=1)
        self.assertEqual(reasons, ["tap_reenables/h=11"])
        self.assertEqual(fx.reconnects, 1)

    def test_tap_storm_on_consecutive_ticks_exits(self):
        fx = _Fixture()
        fx.tick(tap_reenables=11)
        self.assertEqual(fx.reconnects, 1)
        fx.tick(tap_reenables=11)
        self.assertEqual(fx.exits, [self_watchdog.EXIT_STATUS])

    def test_tap_trip_clears_the_window_so_a_quiet_hour_disarms(self):
        fx = _Fixture()
        fx.tick(tap_reenables=11)
        self.assertEqual(fx.tick(tap_reenables=1), [])
        self.assertEqual(fx.wd.consecutive_trips, 0)

    def test_tap_reenables_age_out_of_the_hour_window(self):
        fx = _Fixture()
        for _ in range(10):
            fx.tick(tap_reenables=1)
        for _ in range(60):
            fx.tick()
        self.assertEqual(fx.tick(tap_reenables=1), [])

    def test_late_heartbeat_trips_on_two_consecutive_late_ticks(self):
        fx = _Fixture()
        self.assertEqual(fx.tick(late=1.0), [])
        self.assertEqual(fx.tick(late=3.0), [])
        self.assertEqual(fx.tick(late=3.0), ["heartbeat_drift=3.0s late_ticks=2"])
        self.assertEqual(fx.reconnects, 1)

    def test_one_late_tick_is_timer_coalescing_not_a_stall(self):
        fx = _Fixture()
        for _ in range(20):
            self.assertEqual(fx.tick(late=3.0), [])
            self.assertEqual(fx.tick(), [])
        self.assertEqual(fx.reconnects, 0)

    def test_a_suspend_never_counts_as_drift(self):
        fx = _Fixture()
        self.assertEqual(fx.tick(late=3600.0), [])
        self.assertEqual(fx.tick(late=3600.0), [])
        self.assertEqual(fx.reconnects, 0)

    def test_trip_line_names_the_live_tap_kind(self):
        fx = _Fixture()
        fx.hook.tap_kind = "python"
        fx.tick(tap_reenables=11)
        self.assertEqual(len(fx.logs), 1)
        self.assertIn(" tap=python ", fx.logs[0])

    def test_trip_line_says_unknown_without_a_tap_kind(self):
        fx = _Fixture()
        fx.hook = SimpleNamespace(tap_reenable_total=0)
        fx.tick(tap_reenables=11)
        self.assertIn(" tap=unknown ", fx.logs[0])

    def test_missing_counters_do_not_trip(self):
        wd = SelfWatchdog(
            cpu_seconds=lambda: 0.0,
            monotonic=lambda: 0.0,
            footprint=lambda: None,
            exit_fn=lambda code: (_ for _ in ()).throw(AssertionError("exit")),
        )
        self.assertEqual(wd.tick(), [])


class MemoryGuardTests(unittest.TestCase):
    def test_no_sampler_means_no_mem_lines_and_no_trip(self):
        fx = _Fixture()
        fx.run_hours(4)
        self.assertEqual(fx.logs, [])
        self.assertIsNone(fx.wd.footprint_mb)
        self.assertIsNone(fx.wd.growth_mb_h)

    def test_flat_footprint_never_trips(self):
        fx = _Fixture(footprint_mb=180.0)
        self.assertEqual(fx.run_hours(6), [])
        self.assertEqual(fx.trip_lines, [])
        self.assertEqual(fx.reconnects, 0)
        self.assertEqual(fx.exits, [])
        self.assertAlmostEqual(fx.wd.growth_mb_h, 0.0)

    def test_growth_for_two_hours_does_not_trip(self):
        fx = _Fixture(footprint_mb=180.0)
        self.assertEqual(fx.run_hours(2, grow_mb_h=25.0), [])
        self.assertEqual(fx.trip_lines, [])
        self.assertAlmostEqual(fx.wd.growth_mb_h, 25.0, places=3)

    def test_growth_sustained_three_and_a_half_hours_trips(self):
        fx = _Fixture(footprint_mb=180.0)
        # Two hours in: still quiet (slope needs an hour to be fitted, then
        # must hold for three).
        fx.run_hours(2, grow_mb_h=25.0)
        self.assertEqual(fx.trip_lines, [])
        tripped = [r for r in (fx.tick(grow_mb_h=25.0) for _ in range(90)) if r]
        self.assertTrue(tripped, "no trip within 3.5 h of sustained growth")
        first = tripped[0][0]
        self.assertRegex(first, r"^mem growth/h=25\.0 over 3\.[0-9]h$")
        self.assertIn(" tap=native mem growth/h=25.0 over ", fx.trip_lines[0])

    def test_growth_that_stops_resets_the_sustain_clock(self):
        fx = _Fixture(footprint_mb=180.0)
        fx.run_hours(2.5, grow_mb_h=25.0)
        fx.run_hours(1.5)  # flat: the fitted slope falls below the rate
        self.assertEqual(fx.run_hours(2.5, grow_mb_h=25.0), [])
        self.assertEqual(fx.trip_lines, [])

    def test_footprint_over_1500_mb_trips_at_once(self):
        fx = _Fixture(footprint_mb=1600.0)
        self.assertEqual(fx.tick(), ["mem footprint_mb=1600 > 1500"])
        self.assertEqual(fx.reconnects, 1)
        self.assertEqual(fx.exits, [])
        self.assertIn("[Watchdog] trip n=1 consecutive=1 tap=native mem footprint_mb=1600 > 1500", fx.logs)

    def test_footprint_trip_escalates_to_exit_when_launchd_owned(self):
        fx = _Fixture(footprint_mb=1600.0)
        fx.tick()
        fx.tick()
        self.assertEqual(fx.exits, [self_watchdog.EXIT_STATUS])
        self.assertEqual(fx.statuses, [])

    def test_footprint_trip_without_supervisor_posts_status_then_mutes(self):
        fx = _Fixture(exit_enabled=False, footprint_mb=1600.0)
        fx.tick()
        fx.tick()
        self.assertEqual(fx.exits, [])
        self.assertEqual(
            fx.statuses, ["Mouser needs a restart: mem footprint_mb=1600 > 1500"]
        )
        trips = len(fx.trip_lines)
        # Muted for an hour: [mem] lines continue, trips do not.
        fx.run_hours(0.9)
        self.assertEqual(len(fx.trip_lines), trips)
        self.assertGreater(len(fx.mem_lines), 50)
        fx.run_hours(0.2)
        self.assertGreater(len(fx.trip_lines), trips)

    def test_mem_line_format_and_rate(self):
        fx = _Fixture(footprint_mb=180.0)
        fx.tick()
        self.assertEqual(fx.mem_lines, ["[mem] footprint_mb=180.0 peak_mb=180.0 growth_mb_h=n/a"])
        fx.run_hours(1, grow_mb_h=30.0)
        self.assertRegex(
            fx.mem_lines[-1],
            r"^\[mem\] footprint_mb=210\.0 peak_mb=210\.0 growth_mb_h=30\.0$",
        )
        # One line per tick, never more: 1 + 60 ticks.
        self.assertEqual(len(fx.mem_lines), 61)
        fx.footprint_mb = 150.0
        fx.tick()
        self.assertTrue(fx.mem_lines[-1].startswith("[mem] footprint_mb=150.0 peak_mb=210.0 "))

    def test_sampler_exception_is_ignored(self):
        fx = _Fixture()
        fx.wd._footprint = lambda: (_ for _ in ()).throw(OSError("no libproc"))
        self.assertEqual(fx.tick(), [])
        self.assertEqual(fx.logs, [])

    def test_slope_fit_is_least_squares(self):
        samples = [(t * 60.0, 100.0 + t) for t in range(10)]  # 1 MB/min
        self.assertAlmostEqual(self_watchdog.growth_slope_mb_h(samples), 60.0)
        self.assertIsNone(self_watchdog.growth_slope_mb_h(samples[:5]))
        self.assertIsNone(self_watchdog.growth_slope_mb_h([(0.0, 1.0)] * 10))

    def test_real_sampler_is_none_off_macos_and_a_small_number_on_it(self):
        import sys

        mb = self_watchdog.phys_footprint_mb()
        if sys.platform == "darwin":
            self.assertIsNotNone(mb)
            self.assertGreater(mb, 1.0)
            self.assertLess(mb, 2000.0)
            self.assertIsNone(self_watchdog.phys_footprint_mb(pid=-99999))
        else:
            self.assertIsNone(mb)


if __name__ == "__main__":
    unittest.main()
