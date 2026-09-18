import unittest
from types import SimpleNamespace

from core import self_watchdog
from core.self_watchdog import SelfWatchdog


class _Fixture:
    def __init__(self, *, exit_enabled=True):
        self.now = 1000.0
        self.cpu = 0.0
        self.hg = SimpleNamespace(empty_read_total=0)
        self.hook = SimpleNamespace(tap_reenable_total=0)
        self.reconnects = 0
        self.exits = []
        self.logs = []
        self.exit_enabled = exit_enabled
        self.wd = SelfWatchdog(
            hid_listener=lambda: self.hg,
            mouse_hook=lambda: self.hook,
            reconnect=self._reconnect,
            exit_enabled=lambda: self.exit_enabled,
            exit_fn=self.exits.append,
            cpu_seconds=lambda: self.cpu,
            monotonic=lambda: self.now,
            log=self.logs.append,
        )

    def _reconnect(self):
        self.reconnects += 1

    def tick(self, *, cpu_ratio=0.02, empty_reads=10, tap_reenables=0, late=0.0):
        self.now += self_watchdog.TICK_S + late
        self.cpu += cpu_ratio * (self_watchdog.TICK_S + late)
        self.hg.empty_read_total += empty_reads
        self.hook.tap_reenable_total += tap_reenables
        return self.wd.tick()


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

    def test_late_heartbeat_trips_but_a_suspend_does_not(self):
        fx = _Fixture()
        self.assertEqual(fx.tick(late=1.0), [])
        self.assertEqual(fx.tick(late=3.0), ["heartbeat_drift=3.0s"])
        fx.tick()
        self.assertEqual(fx.tick(late=3600.0), [])

    def test_missing_counters_do_not_trip(self):
        wd = SelfWatchdog(
            cpu_seconds=lambda: 0.0,
            monotonic=lambda: 0.0,
            exit_fn=lambda code: (_ for _ in ()).throw(AssertionError("exit")),
        )
        self.assertEqual(wd.tick(), [])


if __name__ == "__main__":
    unittest.main()
