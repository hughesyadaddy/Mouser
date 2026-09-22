import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_and_install as installer
from scripts import windows_install


def normalize_path(path: Path) -> str:
    return os.path.normpath(str(path)).replace("\\", "/")


def _probe_output(*, minor=(3, 13), version="3.13.2", pyobjc="12.1", pyside="6.11.0"):
    import json

    return json.dumps(
        {
            "python_version": version,
            "minor": list(minor),
            "machine": "arm64",
            "pyinstaller": "6.20.0",
            "packages": {"PySide6": pyside, "pyobjc-core": pyobjc},
        }
    )


class PythonProvenanceTests(unittest.TestCase):
    """verify_python_provenance refuses a drifted interpreter or ABI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".python-version").write_text("3.13\n", encoding="utf-8")
        (self.root / "requirements.lock").write_text(
            "# frozen\npyobjc-core==12.1\nPySide6==6.11.0\nshiboken6==6.11.0\n",
            encoding="utf-8",
        )
        self.enterContext(mock.patch.object(installer, "ROOT", self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("MOUSER_SKIP_PROVENANCE", None)
        self.python = Path("/venv/bin/python")

    def _verify(self, output, *, platform="darwin"):
        with mock.patch.object(installer.sys, "platform", platform), mock.patch(
            "scripts.build_and_install.subprocess.check_output", return_value=output
        ) as probe, mock.patch("builtins.print") as fake_print:
            installer.verify_python_provenance(self.python, "test")
        return probe, [c.args[0] for c in fake_print.call_args_list if c.args]

    def test_lock_and_python_version_are_parsed(self):
        self.assertEqual(installer.required_python_minor(), (3, 13))
        self.assertEqual(
            installer.locked_versions(),
            {"pyobjc-core": "12.1", "pyside6": "6.11.0", "shiboken6": "6.11.0"},
        )

    def test_matching_environment_passes_and_logs(self):
        probe, msgs = self._verify(_probe_output())
        self.assertIn(str(self.python), probe.call_args.args[0][0])
        self.assertIn("PySide6", probe.call_args.args[0])
        self.assertIn("pyobjc-core", probe.call_args.args[0])
        self.assertTrue(any("Python version: 3.13.2" in m for m in msgs))
        self.assertTrue(any("pyobjc-core version: 12.1" in m for m in msgs))

    def test_wrong_python_minor_fails(self):
        with self.assertRaises(SystemExit) as ctx:
            self._verify(_probe_output(minor=(3, 12), version="3.12.8"))
        self.assertNotEqual(ctx.exception.code, 0)

    def test_wrong_pyobjc_core_fails_on_macos(self):
        with self.assertRaises(SystemExit):
            self._verify(_probe_output(pyobjc="11.1"))

    def test_missing_pyobjc_core_fails_on_macos(self):
        with self.assertRaises(SystemExit):
            self._verify(_probe_output(pyobjc=None))

    def test_wrong_pyside_fails_everywhere(self):
        with self.assertRaises(SystemExit):
            self._verify(_probe_output(pyside="6.12.0"), platform="win32")

    def test_pyobjc_is_not_probed_off_macos(self):
        probe, _ = self._verify(_probe_output(pyobjc=None), platform="win32")
        self.assertNotIn("pyobjc-core", probe.call_args.args[0])

    def test_failure_message_names_every_problem_and_the_lock(self):
        failures = []

        def fake_fail(message, *, code=1):
            failures.append(message)
            raise SystemExit(code)

        with mock.patch.object(installer, "fail", side_effect=fake_fail), \
                self.assertRaises(SystemExit):
            self._verify(_probe_output(minor=(3, 12), version="3.12.8", pyside="6.9.0"))
        text = "\n".join(failures)
        self.assertIn("3.12.8", text)
        self.assertIn("PySide6 6.9.0", text)
        self.assertIn("requirements.lock", text)

    def test_skip_override_only_applies_off_macos(self):
        os.environ["MOUSER_SKIP_PROVENANCE"] = "1"
        probe, msgs = self._verify(_probe_output(minor=(3, 12)), platform="linux")
        probe.assert_not_called()
        self.assertTrue(any("skipped" in m for m in msgs))
        with self.assertRaises(SystemExit):
            _, msgs = self._verify(_probe_output(minor=(3, 12)), platform="darwin")

    def test_skip_override_is_reported_as_ignored_on_macos(self):
        os.environ["MOUSER_SKIP_PROVENANCE"] = "1"
        _, msgs = self._verify(_probe_output(), platform="darwin")
        self.assertTrue(any("ignored on macOS" in m for m in msgs))

    def test_macos_build_verifies_before_running_the_build_script(self):
        calls = []
        with mock.patch.object(installer, "resolve_macos_sign_identity", return_value="A" * 40), \
                mock.patch.object(installer, "resolve_install_dir", return_value=self.root), \
                mock.patch.object(installer, "resolve_python", return_value=(self.python, "test")), \
                mock.patch.object(installer, "verify_python_provenance",
                                  side_effect=lambda *a: calls.append("verify")), \
                mock.patch.object(installer, "run_command",
                                  side_effect=lambda *a, **k: calls.append("build")), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):  # dist/Mouser.app never appears
                installer.build_and_install_macos()
        self.assertEqual(calls, ["verify", "build"])

    def test_real_repo_pins_are_consistent(self):
        """The committed .python-version and requirements.lock agree with the
        pinned lines in requirements.txt (pyobjc 12.1)."""
        with mock.patch.object(installer, "ROOT", ROOT):
            self.assertEqual(installer.required_python_minor(), (3, 13))
            locked = installer.locked_versions()
        self.assertEqual(locked["pyobjc-core"], "12.1")
        self.assertEqual(locked["pyobjc-framework-quartz"], "12.1")
        self.assertEqual(locked["pyobjc-framework-cocoa"], "12.1")
        self.assertEqual(locked["pyside6"], "6.11.0")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pyobjc-framework-Quartz==12.1", requirements)
        self.assertIn("pyobjc-framework-Cocoa==12.1", requirements)
        self.assertIn('python-version: "3.13"', (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


class BuildAndInstallTests(unittest.TestCase):
    def test_resolve_macos_sign_identity_requires_team_or_identity(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                installer.resolve_macos_sign_identity()

    def test_load_env_local_sets_defaults_without_overriding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env.local"
            env_file.write_text(
                'export MOUSER_INSTALL_DIR="/tmp/custom"\n'
                'MOUSER_TEAM_ID="TEAM123"\n',
                encoding="utf-8",
            )
            with mock.patch.object(installer, "ROOT", root):
                with mock.patch.dict(
                    os.environ,
                    {"MOUSER_INSTALL_DIR": "/existing"},
                    clear=False,
                ):
                    installer.load_env_local()
                    self.assertEqual(os.environ["MOUSER_INSTALL_DIR"], "/existing")
                    self.assertEqual(os.environ["MOUSER_TEAM_ID"], "TEAM123")

    def test_resolve_install_dir_honors_override(self):
        with mock.patch.dict(os.environ, {"MOUSER_INSTALL_DIR": "~/Apps/Mouser"}, clear=False):
            resolved = installer.resolve_install_dir(Path("/Applications"))
        self.assertEqual(resolved, Path("~/Apps/Mouser").expanduser())

    def test_python_from_env_dir_prefers_windows_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp)
            scripts = env_dir / "Scripts"
            scripts.mkdir()
            python_exe = scripts / "python.exe"
            python_exe.write_text("stub", encoding="utf-8")
            self.assertEqual(installer.python_from_env_dir(env_dir), python_exe)

    def test_python_from_env_dir_falls_back_to_unix_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp)
            bin_dir = env_dir / "bin"
            bin_dir.mkdir()
            python3 = bin_dir / "python3"
            python3.write_text("#!/bin/sh\n", encoding="utf-8")
            python3.chmod(python3.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(installer.python_from_env_dir(env_dir), python3)

    def test_resolve_python_prefers_mouser_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "python3"
            custom.write_text("#!/bin/sh\n", encoding="utf-8")
            custom.chmod(custom.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"MOUSER_PYTHON": str(custom)}, clear=False):
                resolved, source = installer.resolve_python()
            self.assertEqual(resolved, custom)
            self.assertEqual(source, "MOUSER_PYTHON")

    def test_default_macos_install_dir(self):
        self.assertEqual(
            installer.DEFAULT_MACOS_INSTALL_DIR,
            Path("/Applications"),
        )

    def test_build_and_install_macos_stops_before_install(self):
        # MOUSER_INSTALL_DIR must be set to a tmp path: build_and_install_macos()
        # resolves the real DEFAULT_MACOS_INSTALL_DIR (/Applications) and
        # shutil.rmtree()s whatever's already there when it isn't overridden.
        # Without this, this test deletes the real, installed /Applications/Mouser.app.
        with mock.patch.object(installer, "stop_running_instances") as stop, \
                mock.patch.object(installer, "resolve_python", return_value=(Path("/py"), "test")), \
                mock.patch.object(installer, "verify_python_provenance"):
            with mock.patch.object(installer, "resolve_macos_sign_identity", return_value="SIGN"):
                with mock.patch.object(installer, "run_command"):
                    with mock.patch.object(installer, "restart_enabled", return_value=False):
                        with tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            dist = root / "dist" / installer.MACOS_APP_NAME
                            dist.mkdir(parents=True)
                            install_dir = root / "Applications"
                            install_dir.mkdir()
                            with mock.patch.object(installer, "ROOT", root):
                                with mock.patch.dict(
                                    os.environ, {"MOUSER_INSTALL_DIR": str(install_dir)}, clear=False
                                ):
                                    installer.build_and_install_macos()
            stop.assert_called_once()

    def test_main_rejects_unsupported_platform(self):
        with mock.patch.object(installer.sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as ctx:
                installer.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_build_and_install_windows_verifies_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist" / installer.WINDOWS_APP_DIR
            dist.mkdir(parents=True)
            (dist / "Mouser.exe").write_text("exe", encoding="utf-8")
            (dist / "_internal").mkdir()

            install_root = root / "install"
            python = root / "python"
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)

            def fake_run_command(args, **kwargs):
                if "PyInstaller" in [str(arg) for arg in args]:
                    dist.mkdir(parents=True, exist_ok=True)
                    (dist / "Mouser.exe").write_text("exe", encoding="utf-8")
                    (dist / "_internal").mkdir(exist_ok=True)

            def fake_run(args, **kwargs):
                if args[:3] == [str(python), "-c", "import hid; print('[*] hidapi:', hid.__file__)"]:
                    return mock.Mock(returncode=0)
                raise AssertionError(f"unexpected subprocess.run: {args}")

            def fake_finalize(install_root, *, scope=None):
                script = install_root / windows_install.UNINSTALL_SCRIPT_NAME
                script.write_text("@echo off\r\n", encoding="utf-8")
                return {
                    "install_root": install_root,
                    "start_menu_shortcut": install_root / "Mouser.lnk",
                    "uninstall_script": script,
                }

            with mock.patch.object(installer, "ROOT", root):
                with mock.patch.dict(
                    os.environ,
                    {"MOUSER_INSTALL_DIR": str(install_root), "MOUSER_INSTALL_SCOPE": "user"},
                    clear=False,
                ):
                    with mock.patch.object(
                        installer,
                        "resolve_python",
                        return_value=(python, "test"),
                    ):
                        with mock.patch.object(installer, "require_pyinstaller"):
                            with mock.patch.object(installer, "verify_python_provenance"):
                                with mock.patch.object(
                                    installer,
                                    "run_command",
                                    side_effect=fake_run_command,
                                ):
                                    with mock.patch(
                                        "scripts.build_and_install.subprocess.run",
                                        side_effect=fake_run,
                                    ):
                                        with mock.patch.object(
                                            windows_install,
                                            "cleanup_all_windows_installs",
                                        ):
                                            with mock.patch.object(
                                                windows_install,
                                                "stop_running_mouser_instances",
                                            ):
                                                with mock.patch.object(
                                                    windows_install,
                                                    "finalize_windows_install",
                                                    side_effect=fake_finalize,
                                                ):
                                                    with mock.patch.object(
                                                        installer,
                                                        "restart_enabled",
                                                        return_value=False,
                                                    ):
                                                        installer.build_and_install_windows()

            self.assertTrue((install_root / "Mouser.exe").is_file())
            self.assertTrue((install_root / "_internal").is_dir())
            self.assertTrue((install_root / windows_install.UNINSTALL_SCRIPT_NAME).is_file())


class WindowsInstallTests(unittest.TestCase):
    def test_default_install_root_uses_program_files_for_machine_scope(self):
        with mock.patch.dict(
            os.environ,
            {"ProgramFiles": r"C:\Program Files", "MOUSER_INSTALL_SCOPE": "machine"},
            clear=False,
        ):
            self.assertEqual(
                normalize_path(windows_install.default_install_root()),
                normalize_path(Path(r"C:\Program Files\Mouser")),
            )

    def test_default_install_root_uses_local_programs_for_user_scope(self):
        with mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": r"C:\Users\example\AppData\Local",
                "MOUSER_INSTALL_SCOPE": "user",
            },
            clear=False,
        ):
            self.assertEqual(
                normalize_path(windows_install.default_install_root()),
                normalize_path(Path(r"C:\Users\example\AppData\Local\Programs\Mouser")),
            )

    def test_resolve_install_scope_falls_back_when_program_files_not_writable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(windows_install, "_can_install_machine", return_value=False):
                self.assertEqual(windows_install.resolve_install_scope(), "user")


if __name__ == "__main__":
    unittest.main()
