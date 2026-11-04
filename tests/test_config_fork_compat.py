import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import config, config_fork_compat as fc


def fork_cfg():
    """Shape of the fork's v12 config on Alex's Macs (values from his seat)."""
    return {
        "version": 12,
        "active_profile": "default",
        "profiles": {"default": {"label": "Default (All Apps)", "apps": [], "mappings": {
            "middle": "none", "gesture": "none",
            "gesture_left": "space_left", "gesture_right": "space_right",
            "gesture_up": "mission_control", "gesture_down": "app_expose",
            "xbutton1": "mouse_back_click", "xbutton2": "mouse_forward_click",
            "thumb_button": "none", "hscroll_left": "none", "hscroll_right": "none",
            "mode_shift": "switch_scroll_mode",
        }}},
        "settings": {
            "invert_vscroll": True, "invert_hscroll": True, "dpi": 1000,
            "gesture_threshold": 100, "hscroll_threshold": 1,
            "smart_shift_enabled": True, "smart_shift_mode": "ratchet",
            "smart_shift_threshold": 25, "start_at_login": True,
            "start_minimized": True, "ignore_trackpad": True, "wheel_divert": "auto",
            "bridge_proto": 2, "deskflow": {"auto": True, "transparent_transport": True},
            "user_overrides": [],
        },
    }


class ForkLineageTests(unittest.TestCase):
    def _slots(self):
        return ["a", "b"]

    def test_detection(self):
        self.assertTrue(fc.is_fork_lineage(fork_cfg()))
        self.assertFalse(fc.is_fork_lineage({"version": 11}))
        c = fork_cfg()
        c["settings"][fc.LINEAGE_KEY] = {}
        self.assertFalse(fc.is_fork_lineage(c))
        self.assertFalse(fc.is_fork_lineage({"version": "x"}))
        self.assertFalse(fc.is_fork_lineage(None))

    def test_gestures_mapped_to_upstream_model(self):
        c = fc.migrate_fork_lineage(fork_cfg(), self._slots)
        m = c["profiles"]["default"]["mappings"]
        self.assertEqual(m["gesture"], "gesture_swipe")
        self.assertEqual(m["gesture_tap"], "none")
        self.assertEqual(m["actions_ring"], "gesture_swipe")
        self.assertEqual(m["actions_ring_tap"], "none")
        for d, want in (("left", "space_left"), ("right", "space_right"),
                        ("up", "mission_control"), ("down", "app_expose")):
            self.assertEqual(m[f"gesture_{d}"], want)
            self.assertEqual(m[f"actions_ring_{d}"], want)
        self.assertEqual(m["xbutton1"], "mouse_back_click")
        self.assertEqual(m["xbutton2"], "mouse_forward_click")
        self.assertEqual(m["thumb_button"], "none")

    def test_settings_kept_and_upstream_defaults_added(self):
        before = fork_cfg()["settings"]
        c = fc.migrate_fork_lineage(fork_cfg(), self._slots)
        s = c["settings"]
        for k, v in before.items():
            if k == "hscroll_threshold":
                continue
            self.assertEqual(s[k], v, k)
        self.assertEqual(s["hscroll_threshold"], 0.1)
        self.assertEqual(s["scroll_force"], 50)
        self.assertEqual(c["version"], 11)
        self.assertEqual(s[fc.LINEAGE_KEY]["fork_version"], 12)

    def test_runs_once(self):
        c = fc.migrate_fork_lineage(fork_cfg(), self._slots)
        snap = copy.deepcopy(c)
        self.assertEqual(fc.migrate_fork_lineage(c, self._slots), snap)

    def test_existing_ring_mapping_kept(self):
        c = fork_cfg()
        c["profiles"]["default"]["mappings"]["actions_ring"] = "copy"
        fc.migrate_fork_lineage(c, self._slots)
        self.assertEqual(c["profiles"]["default"]["mappings"]["actions_ring"], "copy")

    def test_no_swipes_keeps_tap(self):
        c = fork_cfg()
        m = c["profiles"]["default"]["mappings"]
        for d in ("left", "right", "up", "down"):
            m[f"gesture_{d}"] = "none"
        m["gesture"] = "alt_tab"
        fc.migrate_fork_lineage(c, self._slots)
        self.assertEqual(m["gesture"], "alt_tab")
        self.assertEqual(m["actions_ring"], "alt_tab")

    def test_windows_profile(self):
        c = fork_cfg()
        c["profiles"]["default"]["mappings"].update({
            "gesture_up": "task_view", "gesture_down": "win_d",
            "xbutton1": "alt_tab", "hscroll_left": "browser_back"})
        fc.migrate_fork_lineage(c, self._slots)
        m = c["profiles"]["default"]["mappings"]
        self.assertEqual(m["actions_ring_up"], "task_view")
        self.assertEqual(m["hscroll_left"], "browser_back")


class LoadConfigTests(unittest.TestCase):
    def _load(self, data, raw=None, platform="darwin"):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            f.write(raw if raw is not None else json.dumps(data))
        with patch.object(config, "CONFIG_DIR", d), patch.object(config, "CONFIG_FILE", path), \
                patch("core.swipe_actions.sys.platform", platform):
            return config.load_config(), d, path

    def test_fork_v12_loads_without_losing_fields(self):
        cfg, _d, path = self._load(fork_cfg(), platform="win32")
        s = cfg["settings"]
        self.assertTrue(s["invert_vscroll"])
        self.assertTrue(s["invert_hscroll"])
        self.assertEqual(s["gesture_threshold"], 100)
        self.assertEqual(s["dpi"], 1000)
        self.assertEqual(s["deskflow"], {"auto": True, "transparent_transport": True})
        self.assertEqual(cfg["profiles"]["default"]["mappings"]["gesture_left"], "space_left")
        self.assertEqual(json.load(open(path))["version"], 12)  # file untouched

    def test_mac_load_also_applies_swipe_layer(self):
        cfg, _d, _p = self._load(fork_cfg())
        m = cfg["profiles"]["default"]["mappings"]
        self.assertEqual(m["actions_ring_left"], "swipe_space_left")
        self.assertEqual(m["gesture_up"], "swipe_mission_control")
        self.assertEqual(m["gesture"], "gesture_swipe")  # sentinel not touched

    def test_unreadable_config_preserved_and_backup_recovered(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path + ".bak", "w") as f:
            json.dump(fork_cfg(), f)
        with open(path, "w") as f:
            f.write("{not json")
        with patch.object(config, "CONFIG_DIR", d), patch.object(config, "CONFIG_FILE", path), \
                patch("core.swipe_actions.sys.platform", "win32"):
            cfg = config.load_config()
        self.assertTrue(cfg["settings"]["invert_vscroll"])
        self.assertEqual(open(path).read(), "{not json")  # never overwritten
        self.assertTrue(any(n.startswith("config.json.unreadable-") for n in os.listdir(d)))

    def test_unreadable_without_backup_still_preserved(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            f.write("{bad")
        with patch.object(config, "CONFIG_DIR", d), patch.object(config, "CONFIG_FILE", path):
            cfg = config.load_config()
        self.assertEqual(cfg["version"], config.DEFAULT_CONFIG["version"])
        self.assertTrue(any(n.startswith("config.json.unreadable-") for n in os.listdir(d)))

    def test_save_rolls_backup_and_uses_excl_tmp(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with patch.object(config, "CONFIG_DIR", d), patch.object(config, "CONFIG_FILE", path):
            config.save_config({"version": 11, "settings": {"x": 1}})
        self.assertEqual(json.load(open(path + ".bak"))["settings"]["x"], 1)
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
