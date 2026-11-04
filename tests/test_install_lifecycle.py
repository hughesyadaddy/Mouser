import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        # iter_known_install_roots is mocked too (not just list_instance_pids)
        # so this never evaluates Path.is_file() against the real
        # /Applications/Mouser.app, matching the pattern above -- fragile
        # otherwise if a future refactor makes ctl_stop act on paths rather
        # than only PIDs.
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(install_lifecycle, "iter_known_install_roots", return_value=[]),
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


def hash_tree(root: Path) -> dict[str, str]:
    """{relative path: sha256} for every file under root (byte-identical proof)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class SettingsSurvivalTests(unittest.TestCase):
    """The installer and the login-startup sync never write Mouser's config dir."""

    CONFIG = (
        '{"version": 12, "settings": {"start_at_login": true, "scroll": {"speed": 3}},'
        ' "buttons": ["a", "b"]}'
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.config_dir = self.home / "Library" / "Application Support" / "Mouser"
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "config.json").write_text(self.CONFIG, encoding="utf-8")
        (self.config_dir / "last_device.json").write_text('{"vid": 1133}', encoding="utf-8")
        (self.config_dir / "config.json.bak").write_text("{}", encoding="utf-8")
        # HOME -> tmp, and core.config's import-time paths re-pointed the same way.
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False))
        self.enterContext(mock.patch.object(core.config, "CONFIG_DIR", str(self.config_dir)))
        self.enterContext(
            mock.patch.object(core.config, "CONFIG_FILE", str(self.config_dir / "config.json"))
        )
        self.before = hash_tree(self.config_dir)

    def test_build_and_install_macos_leaves_config_dir_byte_identical(self):
        from scripts import build_and_install as installer

        root = Path(self.tmp.name) / "repo"
        root.mkdir()
        install_dir = Path(self.tmp.name) / "Applications"
        install_dir.mkdir()
        dist = root / "dist" / installer.MACOS_APP_NAME
        commands = []

        def fake_run_command(args, **kwargs):
            argv = [str(a) for a in args]
            commands.append(argv)
            if argv and argv[-1].endswith("build_macos_app.sh"):
                (dist / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
                (dist / "Contents" / "MacOS" / "Mouser").write_bytes(b"\xcf\xfa\xed\xfe")
            elif argv and argv[0] == "ditto":
                # a real ditto copies the bundle byte-for-byte; ctl start needs the executable
                dest = install_dir / installer.MACOS_APP_NAME / "Contents" / "MacOS"
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "Mouser").write_bytes(b"\xcf\xfa\xed\xfe")
                (dest / "Mouser").chmod(0o755)

        signed = (
            "Executable={path}\nCodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+1 location=embedded\n"
            "Authority=Apple Development: X (J5KPG8ZR5C)\nTeamIdentifier=J5KPG8ZR5C\n"
        )
        with (
            mock.patch.object(installer, "ROOT", root),
            mock.patch.object(installer.sys, "platform", "darwin"),
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch.object(installer, "resolve_macos_sign_identity", return_value="A" * 40),
            mock.patch.object(installer, "resolve_python", return_value=(Path("/py"), "test")),
            mock.patch.object(installer, "verify_python_provenance"),
            mock.patch.object(installer, "run_command", side_effect=fake_run_command),
            mock.patch.object(installer.shutil, "which", return_value="/usr/bin/codesign"),
            mock.patch.object(installer, "codesign_info", side_effect=lambda p: signed.format(path=p)),
            # ctl stop / ctl start are stubbed: no process is touched, but the
            # real sync_login_startup_after_install runs against the tmp config.
            mock.patch.object(install_lifecycle, "run_ctl", return_value=0) as run_ctl,
            mock.patch("core.startup.supports_login_startup", return_value=True),
            mock.patch("core.startup.apply_login_startup") as apply_login,
            mock.patch("core.config.save_config") as save_config,
            mock.patch.dict(os.environ, {"MOUSER_INSTALL_DIR": str(install_dir), "MOUSER_RESTART": "1"}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            installer.build_and_install_macos()

        self.assertEqual(hash_tree(self.config_dir), self.before)
        self.assertEqual([c.args[0] for c in run_ctl.call_args_list], ["stop", "start"])
        self.assertTrue(any(c[0] == "ditto" for c in commands))
        save_config.assert_not_called()
        # start_at_login is true in the fixture, so the login item is (re)applied
        # from the config -- read-only with respect to the config dir.
        apply_login.assert_called_once()
        self.assertTrue(apply_login.call_args.args[0])

    def test_sync_login_startup_never_writes_config(self):
        app = self.home / "Applications" / "Mouser.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "Mouser").write_bytes(b"\xcf\xfa\xed\xfe")
        with (
            mock.patch.object(install_lifecycle.sys, "platform", "darwin"),
            mock.patch("core.startup.supports_login_startup", return_value=True),
            mock.patch("core.startup.apply_login_startup") as apply_login,
            mock.patch("core.config.save_config") as save_config,
            redirect_stdout(io.StringIO()),
        ):
            install_lifecycle.sync_login_startup_after_install(app)
            apply_login.assert_called_once()
            # and with start_at_login false nothing at all happens
            (self.config_dir / "config.json").write_text(
                self.CONFIG.replace('"start_at_login": true', '"start_at_login": false'), encoding="utf-8"
            )
            before = hash_tree(self.config_dir)
            install_lifecycle.sync_login_startup_after_install(app)
            apply_login.assert_called_once()
        save_config.assert_not_called()
        self.assertEqual(hash_tree(self.config_dir), before)
        self.assertEqual(sorted(p.name for p in self.config_dir.iterdir()),
                         ["config.json", "config.json.bak", "last_device.json"])

    def test_sync_login_startup_after_install_survives_a_broken_import(self):
        # 2026-09-26 (hackintosh): a system libexpat/pyexpat ABI mismatch made
        # `from core.config import load_config` itself raise ImportError --
        # before the try/except around the load_config() *call* even ran --
        # which escaped this function uncaught and killed the whole install
        # script after the app was already built, signed and installed.
        # `None` in sys.modules is the documented way to force ImportError on
        # a specific import without touching the real module for other tests.
        app = Path(self.tmp.name) / "Broken.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "Mouser").write_bytes(b"\xcf\xfa\xed\xfe")
        with (
            mock.patch.dict(sys.modules, {"core.config": None}),
            redirect_stderr(io.StringIO()) as err,
        ):
            install_lifecycle.sync_login_startup_after_install(app)  # must not raise
        self.assertIn("Could not import login-startup support", err.getvalue())


if __name__ == "__main__":
    unittest.main()
