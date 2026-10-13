"""Baked fleet settings are authoritative unless overridden in the UI."""
import json, os, tempfile, unittest
from unittest.mock import patch

import tests.support  # noqa: F401
from core import config, fleet_defaults as fd


class BakedTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.f = os.path.join(d, "config.json")
        for p in (patch.object(config, "CONFIG_DIR", d), patch.object(config, "CONFIG_FILE", self.f),
                  patch.object(config, "BAKED_ENABLED", True)):
            p.start(); self.addCleanup(p.stop)

    def _write(self, cfg):
        with open(self.f, "w") as fh: json.dump(cfg, fh)

    def test_fresh_install_gets_baked(self):
        cfg = config.load_config()
        for k, v in fd.BAKED_SETTINGS.items():
            self.assertEqual(cfg["settings"][k], v, k)
        self.assertEqual(cfg["profiles"]["default"]["mappings"]["xbutton1"], "mouse_back_click")

    def test_drifted_file_without_overrides_is_corrected(self):
        cfg = json.loads(json.dumps(config.DEFAULT_CONFIG))
        cfg["settings"]["invert_vscroll"] = False
        cfg["profiles"]["default"]["mappings"]["xbutton1"] = "alt_tab"
        self._write(cfg)
        out = config.load_config()
        self.assertTrue(out["settings"]["invert_vscroll"])
        self.assertEqual(out["profiles"]["default"]["mappings"]["xbutton1"], "mouse_back_click")

    def test_ui_change_survives_reload(self):
        cfg = config.load_config()
        cfg["settings"]["dpi"] = 1600          # user changes DPI in the UI
        config.save_config(cfg)
        self.assertEqual(config.load_config()["settings"]["dpi"], 1600)

    def test_changing_back_to_baked_clears_override(self):
        cfg = config.load_config(); cfg["settings"]["dpi"] = 1600; config.save_config(cfg)
        cfg = config.load_config(); cfg["settings"]["dpi"] = 1000; config.save_config(cfg)
        self.assertNotIn("settings:dpi", config.load_config()["settings"][fd.OVERRIDES_KEY])


if __name__ == "__main__":
    unittest.main()
