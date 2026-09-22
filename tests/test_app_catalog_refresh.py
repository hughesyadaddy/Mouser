"""M5 audit R6: ``get_app_catalog(refresh=True)`` re-walks the platform app
locations at most once per CATALOG_REFRESH_MIN_INTERVAL_S; ``force=True``
(explicit user rescan) bypasses the throttle."""

import unittest
from unittest.mock import patch

from core import app_catalog


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class CatalogRefreshThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.builds = 0

        def build():
            self.builds += 1
            return [{"id": f"app{self.builds}", "label": "App"}]

        self._patches = [
            patch.object(app_catalog, "_build_catalog", side_effect=build),
            patch.object(app_catalog, "time", self.clock),
            patch.object(app_catalog, "_CATALOG_CACHE", None),
            patch.object(app_catalog, "_CATALOG_BUILT_AT", None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_first_call_builds_and_plain_calls_reuse(self):
        first = app_catalog.get_app_catalog()
        self.assertEqual(self.builds, 1)
        for _ in range(5):
            self.assertEqual(app_catalog.get_app_catalog(), first)
        self.assertEqual(self.builds, 1)

    def test_refresh_is_throttled_to_the_interval(self):
        app_catalog.get_app_catalog(refresh=True)
        self.assertEqual(self.builds, 1)
        for _ in range(20):                       # 20 picker opens
            app_catalog.get_app_catalog(refresh=True)
        self.assertEqual(self.builds, 1)
        self.clock.now += app_catalog.CATALOG_REFRESH_MIN_INTERVAL_S - 1
        app_catalog.get_app_catalog(refresh=True)
        self.assertEqual(self.builds, 1)
        self.clock.now += 1
        app_catalog.get_app_catalog(refresh=True)
        self.assertEqual(self.builds, 2)

    def test_force_bypasses_throttle(self):
        app_catalog.get_app_catalog(refresh=True)
        app_catalog.get_app_catalog(refresh=True, force=True)
        app_catalog.get_app_catalog(force=True)
        self.assertEqual(self.builds, 3)
        # A forced walk also restarts the throttle window.
        app_catalog.get_app_catalog(refresh=True)
        self.assertEqual(self.builds, 3)

    def test_entries_are_copies(self):
        entries = app_catalog.get_app_catalog()
        entries[0]["label"] = "mutated"
        self.assertEqual(app_catalog.get_app_catalog()[0]["label"], "App")


if __name__ == "__main__":
    unittest.main()
