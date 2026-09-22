import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        # The hint names the *required* interpreter, not the running one.
        self.assertIn("python3.13 -m venv", text)
        self.assertNotIn("python3.12", text)
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

    def test_windows_build_verifies_before_cleanup_and_pip(self):
        """A wrong interpreter must fail with the old install untouched."""
        with mock.patch.object(windows_install, "resolve_install_scope", return_value="user"), \
                mock.patch.object(installer, "resolve_python", return_value=(self.python, "test")), \
                mock.patch.object(installer, "verify_python_provenance",
                                  side_effect=SystemExit(1)), \
                mock.patch.object(windows_install, "cleanup_all_windows_installs") as cleanup, \
                mock.patch.object(installer, "run_command") as run_command, \
                mock.patch.dict(os.environ, {"MOUSER_INSTALL_DIR": str(self.root / "i")}), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                installer.build_and_install_windows()
        cleanup.assert_not_called()
        run_command.assert_not_called()

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
            with mock.patch.object(installer, "resolve_macos_sign_identity", return_value="SIGN"), \
                    mock.patch.object(installer, "verify_macos_bundle_signatures"):
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


SIGNED_INFO = """Executable={path}
Identifier=io.github.hughesyadaddy.mouser
Format=Mach-O thin (arm64)
CodeDirectory v=20500 size=1234 flags=0x10000(runtime) hashes=30+7 location=embedded
Signature size=4795
Authority=Apple Development: Alex Hughes (J5KPG8ZR5C)
Authority=Apple Worldwide Developer Relations Certification Authority
Authority=Apple Root CA
TeamIdentifier=J5KPG8ZR5C
"""

UNHARDENED_INFO = SIGNED_INFO.replace("flags=0x10000(runtime)", "flags=0x0(none)")

ADHOC_INFO = """Executable={path}
Identifier=Mouser
CodeDirectory v=20400 size=1234 flags=0x2(adhoc) hashes=30+7 location=embedded
Signature=adhoc
TeamIdentifier=not set
"""

OTHER_TEAM_INFO = SIGNED_INFO.replace("TeamIdentifier=J5KPG8ZR5C", "TeamIdentifier=ZZZZ999999")


def make_fake_bundle(root: Path) -> Path:
    """dist/Mouser.app with the PyInstaller layout: MacOS binary, framework
    binary, dylib, .so extension (executable) plus Resources/Headers noise."""
    app = root / "dist" / installer.MACOS_APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "Mouser"
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    exe.chmod(0o755)
    fw = app / "Contents" / "Frameworks"
    (fw / "QtCore.framework" / "Versions" / "A" / "Resources").mkdir(parents=True)
    (fw / "QtCore.framework" / "Versions" / "A" / "Headers").mkdir(parents=True)
    qt = fw / "QtCore.framework" / "Versions" / "A" / "QtCore"
    qt.write_bytes(b"\xcf\xfa\xed\xfe")
    qt.chmod(0o755)
    (fw / "QtCore.framework" / "Versions" / "A" / "Resources" / "Info.plist").write_text("plist")
    (fw / "QtCore.framework" / "Versions" / "A" / "Headers" / "q.h").write_text("h")
    (fw / "libcrypto.3.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    so = fw / "_ssl.cpython-313-darwin.so"
    so.write_bytes(b"\xcf\xfa\xed\xfe")
    so.chmod(0o755)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / "icon.icns").write_text("icon")
    return app


class BundleSignatureWalkTests(unittest.TestCase):
    """verify_macos_bundle_signatures walks EVERY Mach-O of the built bundle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = make_fake_bundle(self.root)
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("MOUSER_EXPECT_TEAM", None)

    def _walk(self, info_for):
        with mock.patch.object(installer, "codesign_info", side_effect=lambda p: info_for(p).format(path=p)), \
                redirect_stdout(io.StringIO()) as out:
            counts = installer.verify_macos_bundle_signatures(self.app)
        return counts, out.getvalue().splitlines()

    def _walk_fails(self, info_for):
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()) as err:
            self._walk(info_for)
        self.assertNotEqual(ctx.exception.code, 0)
        return err.getvalue()

    def test_enumerates_macos_frameworks_dylibs_and_so_but_not_resources(self):
        rel = sorted(str(p.relative_to(self.app)) for p in installer.bundle_machos(self.app))
        self.assertEqual(
            rel,
            [
                "Contents/Frameworks/QtCore.framework/Versions/A/QtCore",
                "Contents/Frameworks/_ssl.cpython-313-darwin.so",
                "Contents/Frameworks/libcrypto.3.dylib",
                "Contents/MacOS/Mouser",
            ],
        )

    def test_all_signed_hardened_passes_with_summary(self):
        counts, msgs = self._walk(lambda p: SIGNED_INFO)
        self.assertEqual(counts, {"total": 4, "apple": 4, "adhoc": 0, "hardened": 4})
        self.assertIn("sign: total=4 apple=4 adhoc=0 hardened=4", msgs)

    def test_adhoc_macho_anywhere_fails(self):
        err = self._walk_fails(lambda p: ADHOC_INFO if p.name == "libcrypto.3.dylib" else SIGNED_INFO)
        self.assertIn("Contents/Frameworks/libcrypto.3.dylib: Signature=adhoc", err)

    def test_unhardened_first_party_binary_fails(self):
        err = self._walk_fails(lambda p: UNHARDENED_INFO if p.name == "Mouser" else SIGNED_INFO)
        self.assertIn("Contents/MacOS/Mouser: not signed with the hardened runtime", err)

    def test_unhardened_framework_is_accepted(self):
        counts, _ = self._walk(lambda p: UNHARDENED_INFO if p.name == "QtCore" else SIGNED_INFO)
        self.assertEqual(counts["hardened"], 3)
        self.assertEqual(counts["apple"], 4)

    def test_other_team_fails_and_env_overrides_expected_team(self):
        err = self._walk_fails(lambda p: OTHER_TEAM_INFO if p.name == "QtCore" else SIGNED_INFO)
        self.assertIn("TeamIdentifier=ZZZZ999999 != expected J5KPG8ZR5C", err)
        os.environ["MOUSER_EXPECT_TEAM"] = "ZZZZ999999"
        err = self._walk_fails(lambda p: OTHER_TEAM_INFO if p.name == "QtCore" else SIGNED_INFO)
        self.assertIn("Contents/MacOS/Mouser: TeamIdentifier=J5KPG8ZR5C != expected ZZZZ999999", err)

    def test_codesign_failure_on_one_binary_fails(self):
        def info(p):
            if p.name == "Mouser":
                raise RuntimeError("codesign -dvvv failed for x: code object is not signed at all")
            return SIGNED_INFO

        err = self._walk_fails(info)
        self.assertIn("code object is not signed at all", err)

    def test_empty_bundle_fails(self):
        shutil_rm = __import__("shutil").rmtree
        shutil_rm(self.app / "Contents" / "MacOS")
        shutil_rm(self.app / "Contents" / "Frameworks")
        err = self._walk_fails(lambda p: SIGNED_INFO)
        self.assertIn("No Mach-O found", err)

    def test_build_and_install_gates_dist_before_stopping_or_installing(self):
        """The walk runs on dist/ after the build and before ctl stop / ditto."""
        calls = []
        install_dir = self.root / "Applications"
        install_dir.mkdir()

        def fake_run(args, **kwargs):
            calls.append(" ".join(str(a) for a in args))

        with mock.patch.object(installer, "resolve_macos_sign_identity", return_value="A" * 40), \
                mock.patch.object(installer, "resolve_python", return_value=(Path("/py"), "test")), \
                mock.patch.object(installer, "verify_python_provenance"), \
                mock.patch.object(installer, "run_command", side_effect=fake_run), \
                mock.patch.object(installer, "stop_running_instances", side_effect=lambda: calls.append("stop")), \
                mock.patch.object(installer, "sync_login_startup_after_install"), \
                mock.patch.object(installer, "restart_enabled", return_value=False), \
                mock.patch.object(installer, "ROOT", self.root), \
                mock.patch.object(installer.shutil, "which", return_value="/usr/bin/codesign"), \
                mock.patch.object(installer, "codesign_info", side_effect=lambda p: ADHOC_INFO.format(path=p)), \
                mock.patch.dict(os.environ, {"MOUSER_INSTALL_DIR": str(install_dir)}), \
                mock.patch("builtins.print"), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.build_and_install_macos()
        self.assertTrue(any("build_macos_app.sh" in c for c in calls))
        self.assertTrue(any(c.startswith("codesign --verify --deep --strict") and "dist" in c for c in calls))
        self.assertNotIn("stop", calls)
        self.assertFalse(any(c.startswith("ditto") for c in calls))
        self.assertFalse((install_dir / installer.MACOS_APP_NAME).exists())

    def test_build_script_signs_with_hardened_runtime_and_pyinstaller_entitlements(self):
        script = (ROOT / "build_macos_app.sh").read_text(encoding="utf-8")
        sign_call = script[script.index("sign_with_identity()"):]
        self.assertIn("--options runtime", sign_call)
        self.assertIn('--entitlements "$ENTITLEMENTS"', sign_call)
        nested = script[script.index("sign_nested_code()"):script.index("verify_bundle()")]
        self.assertIn("--options runtime", nested)
        ent = (ROOT / "build_resources" / "Mouser.entitlements").read_text(encoding="utf-8")
        self.assertIn("com.apple.security.cs.allow-unsigned-executable-memory", ent)
        self.assertIn("com.apple.security.cs.allow-jit", ent)


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
