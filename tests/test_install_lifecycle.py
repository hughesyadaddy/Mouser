import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.config  # noqa: F401  (patched by the login-sync test)
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

    def test_stop_uses_ctl_stop_once_with_every_known_root(self):
        roots = [Path("/Applications/Mouser.app"), Path("/tmp/dist/Mouser.app")]
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(install_lifecycle, "iter_known_install_roots", return_value=roots),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch("core.single_instance.ctl_stop", return_value=0) as ctl_stop,
            mock.patch.object(install_lifecycle, "run_ctl", wraps=install_lifecycle.run_ctl) as run_ctl,
        ):
            install_lifecycle.stop_running_instances()
        run_ctl.assert_called_once_with("stop")
        ctl_stop.assert_called_once_with(
            [
                "/Applications/Mouser.app/Contents/MacOS/Mouser",
                "/tmp/dist/Mouser.app/Contents/MacOS/Mouser",
            ]
        )

    def test_stop_never_uses_osascript_or_pkill(self):
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch("core.single_instance.subprocess.run") as run,
            mock.patch("core.single_instance.request_quit", return_value=False),
            mock.patch("core.single_instance.list_instance_pids", return_value=[]),
        ):
            install_lifecycle.stop_running_instances()
        for call in run.call_args_list:
            argv = [str(a) for a in call.args[0]]
            self.assertNotIn("osascript", argv)
            self.assertNotIn("pkill", argv)
            self.assertNotIn("open", argv)

    def test_launch_macos_calls_ctl_start_exactly_once_never_open(self):
        app = Path("/Applications/Mouser.app")
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "resolve", lambda self: self),
            mock.patch("core.single_instance.ctl_start", return_value=0) as ctl_start,
            mock.patch("core.single_instance.subprocess.run") as run,
        ):
            install_lifecycle.launch_installed_application(app)
        ctl_start.assert_called_once_with("/Applications/Mouser.app/Contents/MacOS/Mouser")
        run.assert_not_called()

    def test_launch_raises_when_ctl_start_fails(self):
        app = Path("/Applications/Mouser.app")
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "resolve", lambda self: self),
            mock.patch("core.single_instance.ctl_start", return_value=1),
        ):
            with self.assertRaises(RuntimeError):
                install_lifecycle.launch_installed_application(app)

    def test_launch_missing_executable_raises(self):
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.object(Path, "resolve", lambda self: self),
        ):
            with self.assertRaises(FileNotFoundError):
                install_lifecycle.launch_installed_application(Path("/Applications/Mouser.app"))

    def test_windows_login_sync_removes_stale_scheduled_tasks(self):
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "win32"),
            mock.patch("core.startup.supports_login_startup", return_value=True),
            mock.patch("core.startup.remove_stale_scheduled_tasks", return_value=[]) as cleanup,
            mock.patch("core.config.load_config", return_value={"settings": {}}),
        ):
            install_lifecycle.sync_login_startup_after_install(Path(r"C:\Program Files\Mouser"))
        cleanup.assert_called_once()

    def test_restart_disabled_skips_start_in_installer(self):
        from scripts import install_from_dist

        with (
            mock.patch.object(install_from_dist.sys, "platform", "darwin"),
            mock.patch.object(install_from_dist, "stop_running_instances") as stop,
            mock.patch.object(install_from_dist, "launch_installed_application") as launch,
            mock.patch.object(install_from_dist, "sync_login_startup_after_install"),
            mock.patch.object(install_from_dist.shutil, "which", return_value=None),
            mock.patch.object(install_from_dist.subprocess, "run"),
            mock.patch.object(install_from_dist.shutil, "rmtree"),
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "exists", return_value=False),
            mock.patch.object(Path, "mkdir"),
            mock.patch.dict(os.environ, {"MOUSER_RESTART": "0", "MOUSER_INSTALL_DIR": "/tmp/x"}),
        ):
            install_from_dist.install_macos_from_dist()
        stop.assert_called_once_with()
        launch.assert_not_called()

    def test_restart_enabled_starts_exactly_once(self):
        from scripts import install_from_dist

        with (
            mock.patch.object(install_from_dist.sys, "platform", "darwin"),
            mock.patch.object(install_from_dist, "stop_running_instances") as stop,
            mock.patch.object(install_from_dist, "launch_installed_application") as launch,
            mock.patch.object(install_from_dist, "sync_login_startup_after_install"),
            mock.patch.object(install_from_dist.shutil, "which", return_value=None),
            mock.patch.object(install_from_dist.subprocess, "run"),
            mock.patch.object(install_from_dist.shutil, "rmtree"),
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "exists", return_value=False),
            mock.patch.object(Path, "mkdir"),
            mock.patch.dict(os.environ, {"MOUSER_RESTART": "1", "MOUSER_INSTALL_DIR": "/tmp/x"}),
        ):
            install_from_dist.install_macos_from_dist()
        stop.assert_called_once_with()
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
