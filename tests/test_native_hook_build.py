"""Compiler selection for the native hook filter (native/win/build.py).

The DLL is optional, so this script failing must never fail a Mouser build --
it has to report the failure and return non-zero without raising, and it has
to find whichever of the two supported toolchains is present.
"""

import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_PY = os.path.join(REPO_ROOT, "native", "win", "build.py")


def _load_build_module():
    spec = importlib.util.spec_from_file_location("native_win_build", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_module = _load_build_module()


class CompilerSelectionTests(unittest.TestCase):
    def test_msvc_wins_when_cl_is_on_path(self):
        with patch.object(build_module.shutil, "which", lambda name: (
            "/msvc/cl.exe" if name == "cl" else "/mingw/gcc"
        )):
            command = build_module._msvc_command("out.dll")
        self.assertIsNotNone(command)
        self.assertIn("/LD", command)
        self.assertIn("user32.lib", command)

    def test_msvc_is_skipped_when_cl_is_absent(self):
        with patch.object(build_module.shutil, "which", lambda name: None):
            self.assertIsNone(build_module._msvc_command("out.dll"))

    def test_mingw_falls_back_through_the_candidate_names(self):
        """The cross-compiler is named differently on a Windows mingw install
        and on a cross-build host, so both spellings have to be tried."""
        with patch.object(build_module.shutil, "which", lambda name: (
            "/usr/bin/gcc" if name == "gcc" else None
        )):
            command = build_module._mingw_command("out.dll")
        self.assertIsNotNone(command)
        self.assertEqual(command[0], "/usr/bin/gcc")
        self.assertIn("-shared", command)
        self.assertIn("-luser32", command)

    def test_mingw_prefers_the_explicit_cross_name(self):
        with patch.object(build_module.shutil, "which", lambda name: f"/bin/{name}"):
            command = build_module._mingw_command("out.dll")
        self.assertEqual(command[0], "/bin/x86_64-w64-mingw32-gcc")

    def test_no_compiler_at_all(self):
        with patch.object(build_module.shutil, "which", lambda name: None):
            self.assertIsNone(build_module._mingw_command("out.dll"))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.output = os.path.join(REPO_ROOT, "native", "win", "unit-test.dll")

    def test_missing_compiler_reports_and_fails(self):
        with patch.object(build_module.shutil, "which", lambda name: None):
            self.assertEqual(build_module.build(self.output), 1)

    def test_missing_source_reports_and_fails(self):
        with patch.object(build_module.os.path, "isfile", lambda path: False):
            self.assertEqual(build_module.build(self.output), 1)

    def test_a_failing_compiler_propagates_its_exit_code(self):
        with patch.object(build_module.shutil, "which", lambda name: "/bin/gcc"), \
                patch.object(
                    build_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=2),
                ):
            self.assertEqual(build_module.build(self.output), 2)

    def test_a_silent_compiler_that_wrote_nothing_still_fails(self):
        """Exit code 0 with no DLL would otherwise be packaged as success and
        ship an app that silently runs the slow Python procedure."""
        with patch.object(build_module.shutil, "which", lambda name: "/bin/gcc"), \
                patch.object(
                    build_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ), patch.object(
                    build_module.os.path,
                    "isfile",
                    lambda path: path == build_module.SOURCE,
                ):
            self.assertEqual(build_module.build(self.output), 1)

    def test_success_reports_zero(self):
        with patch.object(build_module.shutil, "which", lambda name: "/bin/gcc"), \
                patch.object(
                    build_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ), patch.object(build_module.os.path, "isfile", lambda path: True), \
                patch.object(build_module.os.path, "getsize", lambda path: 1024):
            self.assertEqual(build_module.build(self.output), 0)


class RealCompileTests(unittest.TestCase):
    """When a cross-compiler happens to be installed, actually compile it.

    The C cannot be *run* off Windows, so this is the only automated check
    that the procedure's source is even well-formed -- including the
    compile-time assertion that MouserHookEvent is laid out as Python reads it.
    """

    def test_the_source_compiles_when_a_toolchain_is_available(self):
        import shutil
        import subprocess
        import tempfile

        compiler = None
        for name in build_module.MINGW_CANDIDATES:
            compiler = shutil.which(name)
            if compiler:
                break
        if not compiler:
            self.skipTest("no mingw-w64 cross-compiler on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "mouser_hook_x64.dll")
            result = subprocess.run(
                [compiler, "-O2", "-Wall", "-Wextra", "-Werror", "-shared",
                 "-o", output, build_module.SOURCE, "-luser32", "-lkernel32"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(
            result.returncode, 0, f"native hook did not compile:\n{result.stderr}"
        )
        self.assertEqual(result.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
