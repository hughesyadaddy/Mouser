import copy
import unittest
import unittest.mock

from core import swipe_actions as sa


def _cfg():
    return {
        "version": 12,
        "profiles": {
            "default": {"mappings": {
                "gesture_left": "space_left", "gesture_right": "space_right",
                "gesture_up": "mission_control", "gesture_down": "app_expose",
                "xbutton1": "mouse_back_click", "hscroll_left": "browser_back",
                "mode_shift": "switch_scroll_mode",
            }},
            "safari": {"mappings": {"gesture_left": "browser_back", "gesture_up": "none"}},
        },
        "settings": {"gesture_threshold": 100},
    }


class MigrationTests(unittest.TestCase):
    def test_maps_gesture_slots_only(self):
        cfg = _cfg()
        changes = sa.migrate_to_swipes(cfg, "darwin")
        m = cfg["profiles"]["default"]["mappings"]
        self.assertEqual(m["gesture_left"], "swipe_space_left")
        self.assertEqual(m["gesture_right"], "swipe_space_right")
        self.assertEqual(m["gesture_up"], "swipe_mission_control")
        self.assertEqual(m["gesture_down"], "swipe_app_expose")
        self.assertEqual(m["xbutton1"], "mouse_back_click")
        self.assertEqual(m["hscroll_left"], "browser_back")
        self.assertEqual(m["mode_shift"], "switch_scroll_mode")
        self.assertEqual(cfg["profiles"]["safari"]["mappings"]["gesture_left"], "swipe_back")
        self.assertEqual(cfg["profiles"]["safari"]["mappings"]["gesture_up"], "none")
        self.assertEqual(len(changes), 5)
        self.assertEqual(cfg["settings"]["gesture_threshold"], 100)

    def test_idempotent_and_respects_user_reverts(self):
        cfg = _cfg()
        sa.migrate_to_swipes(cfg, "darwin")
        cfg["profiles"]["default"]["mappings"]["gesture_up"] = "mission_control"
        self.assertEqual(sa.migrate_to_swipes(cfg, "darwin"), [])
        self.assertEqual(cfg["profiles"]["default"]["mappings"]["gesture_up"], "mission_control")

    def test_not_on_windows_or_linux(self):
        for plat in ("win32", "linux"):
            cfg = _cfg()
            before = copy.deepcopy(cfg)
            self.assertEqual(sa.migrate_to_swipes(cfg, plat), [])
            self.assertEqual(cfg, before)

    def test_restore(self):
        cfg = _cfg()
        before = copy.deepcopy(cfg)
        sa.migrate_to_swipes(cfg, "darwin")
        sa.restore_from_migration(cfg)
        self.assertEqual(cfg, before)

    def test_malformed_config_is_safe(self):
        self.assertEqual(sa.migrate_to_swipes(None, "darwin"), [])
        cfg = {"profiles": {"p": None, "q": {"mappings": None}}}
        self.assertEqual(sa.migrate_to_swipes(cfg, "darwin"), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        sa._state["enabled"] = False

    def tearDown(self):
        sa._state["enabled"] = False

    def test_disabled_runs_builtin(self):
        self.assertTrue(sa.run("swipe_mission_control", self.calls.append,
                               post_native=lambda s: self.fail("posted")))
        self.assertEqual(self.calls, ["mission_control"])

    def test_non_swipe_ids_pass_through(self):
        self.assertFalse(sa.run("alt_tab", self.calls.append))
        wrapped = sa.wrap_execute_action(self.calls.append)
        wrapped("alt_tab")
        self.assertEqual(self.calls, ["alt_tab"])

    def test_enabled_native_success_skips_builtin(self):
        sa.configure({"settings": {sa.SETTING_ENABLED: True}})
        with unittest.mock.patch.object(sa.sys, "platform", "darwin"):
            sa.run("swipe_space_left", self.calls.append, post_native=lambda s: True)
        self.assertEqual(self.calls, [])

    def test_enabled_native_failure_falls_back(self):
        sa.configure({"settings": {sa.SETTING_ENABLED: True}})

        def boom(spec):
            raise RuntimeError("x")
        with unittest.mock.patch.object(sa.sys, "platform", "darwin"):
            sa.run("swipe_back", self.calls.append, post_native=boom)
            sa.run("swipe_forward", self.calls.append, post_native=lambda s: False)
        self.assertEqual(self.calls, ["browser_back", "browser_forward"])

    def test_action_entries_darwin_only(self):
        actions = {"mission_control": {"label": "MC", "keys": [1], "category": "Navigation"}}
        self.assertEqual(sa.action_entries(actions, "win32"), {})
        rows = sa.action_entries(actions, "darwin")
        self.assertEqual(list(rows), ["swipe_mission_control"])
        self.assertEqual(rows["swipe_mission_control"]["keys"], [1])


if __name__ == "__main__":
    unittest.main()


class LoadConfigHookTests(unittest.TestCase):
    def test_load_config_migrates_in_memory_only(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        from core import config
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                json.dump(_cfg(), f)
            raw_before = open(path).read()
            with patch.object(config, "CONFIG_DIR", d), \
                    patch.object(config, "CONFIG_FILE", path), \
                    patch.object(sa.sys, "platform", "darwin"):
                cfg = config.load_config()
            self.assertEqual(cfg["profiles"]["default"]["mappings"]["gesture_up"],
                             "swipe_mission_control")
            self.assertEqual(cfg["settings"]["gesture_threshold"], 100)
            self.assertEqual(open(path).read(), raw_before)  # file untouched
            self.assertFalse(sa.native_enabled())


class KeySimulatorHookTests(unittest.TestCase):
    def test_execute_action_is_wrapped(self):
        from core import key_simulator as ks
        self.assertTrue(hasattr(ks.execute_action, "__wrapped__"))
        if ks.sys.platform == "darwin":
            for aid in sa.SWIPE_ACTIONS:
                self.assertIn(aid, ks.ACTIONS)
