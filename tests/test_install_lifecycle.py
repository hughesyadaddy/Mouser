import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import install_lifecycle


class InstallLifecycleTests(unittest.TestCase):
    def test_restart_enabled_defaults_true(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(install_lifecycle.restart_enabled())

    def test_restart_enabled_honors_opt_out(self):
        with mock.patch.dict(os.environ, {"MOUSER_RESTART": "0"}, clear=False):
            self.assertFalse(install_lifecycle.restart_enabled())

    def test_iter_known_install_roots_includes_override(self):
        with mock.patch.object(install_lifecycle.sys, "platform", "darwin"):
            with mock.patch.dict(
                os.environ,
                {"MOUSER_INSTALL_DIR": "~/Apps/Mouser.app"},
                clear=False,
            ):
                roots = install_lifecycle.iter_known_install_roots()
        self.assertIn(Path("~/Apps/Mouser.app").expanduser(), roots)

    def test_stop_macos_runs_quit_and_pkill(self):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append([str(arg) for arg in args])
            return mock.Mock(returncode=0)

        with mock.patch.object(install_lifecycle.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(install_lifecycle.time, "sleep"):
                install_lifecycle._stop_macos_instances(
                    [Path("/Applications/Mouser.app")]
                )

        joined = [" ".join(call) for call in calls]
        self.assertTrue(any("osascript" in call for call in joined))
        self.assertTrue(any("pkill" in call for call in joined))

    def test_launch_macos_uses_open(self):
        app = Path("/Applications/Mouser.app")
        with mock.patch.object(install_lifecycle.subprocess, "run") as run:
            with mock.patch.object(
                install_lifecycle,
                "_macos_bundle_executable",
                return_value=app / "Contents/MacOS/Mouser",
            ):
                with mock.patch.object(Path, "is_file", return_value=True):
                    with mock.patch.object(Path, "is_dir", return_value=True):
                        install_lifecycle.launch_installed_application(app)
        run.assert_called_once_with(["open", str(app)], check=True)


if __name__ == "__main__":
    unittest.main()
