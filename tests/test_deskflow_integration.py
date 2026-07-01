"""Tests for Deskflow auto-detection integration."""

import json
import os
import tempfile
import unittest
from unittest import mock

from core.deskflow_integration import resolve_integration, use_transparent_transport


class DeskflowIntegrationTests(unittest.TestCase):
    def test_auto_disabled_returns_none(self):
        cfg = {"settings": {"deskflow": {"auto": False}}}
        self.assertIsNone(resolve_integration(cfg))

    def test_manifest_client_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "version": 1,
                "port": 19795,
                "token": "secret-token",
                "hid_passthrough": True,
            }
            path = os.path.join(tmp, "mouser-sink.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with mock.patch(
                "core.deskflow_integration._deskflow_config_dir",
                return_value=tmp,
            ):
                out = resolve_integration({})
        self.assertIsNotNone(out)
        self.assertTrue(out.get("client_sink"))
        self.assertEqual(out.get("token"), "secret-token")

    def test_transparent_transport_follows_integration(self):
        cfg = {"settings": {"deskflow": {"auto": False}}}
        self.assertFalse(use_transparent_transport(cfg))

    def test_deskflow_conf_host_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "deskflow.conf")
            with open(conf, "w", encoding="utf-8") as handle:
                handle.write(
                    "[server]\n"
                    "mouserBridgeEnabled=true\n"
                    "mouserBridgePort=19800\n"
                    "mouserBridgeToken=bridge-token\n"
                )
            with mock.patch(
                "core.deskflow_integration._deskflow_config_dir",
                return_value=tmp,
            ):
                out = resolve_integration({})
        self.assertIsNotNone(out)
        self.assertTrue(out.get("host_bridge"))
        self.assertEqual(out.get("bridge_port"), 19800)
        self.assertEqual(out.get("bridge_token"), "bridge-token")

    def test_macos_prefers_library_deskflow_dir(self):
        home = os.path.expanduser("~")
        expected = os.path.join(home, "Library", "Deskflow")
        with mock.patch.object(os.path, "isdir", side_effect=lambda p: p == expected):
            from core.deskflow_integration import _deskflow_config_dir

            self.assertEqual(_deskflow_config_dir(), expected)

    def test_reads_deskflow_conf_with_capitalized_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "Deskflow.conf")
            with open(conf, "w", encoding="utf-8") as handle:
                handle.write("[client]\nmouserEnabled=true\nmouserToken=abc\n")
            with mock.patch(
                "core.deskflow_integration._deskflow_config_dir",
                return_value=tmp,
            ):
                out = resolve_integration({})
        self.assertIsNotNone(out)
        self.assertTrue(out.get("client_sink"))
        self.assertEqual(out.get("token"), "abc")


if __name__ == "__main__":
    unittest.main()
