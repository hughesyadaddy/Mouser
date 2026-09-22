"""Native hook build helpers.

Windows (native/win/build.py): the DLL is optional, so this script failing
must never fail a Mouser build -- it has to report the failure and return
non-zero without raising, and it has to find whichever of the two supported
toolchains is present.

macOS (scripts/native_tap_build.py, used by Mouser-mac.spec): the dylib is
*not* optional -- a packaged app without it silently runs the Python
CGEventTap callback, which leaks one CGEvent per pass-through event through
PyObjC's trampoline. A build that cannot produce the dylib must abort.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
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

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from scripts import native_tap_build  # noqa: E402


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
        # macOS ships a `gcc` that is clang targeting Darwin; it has no
        # windows.h and cannot build this.
        probe = subprocess.run(
            [compiler, "-dumpmachine"], capture_output=True, text=True, check=False
        )
        if "mingw" not in probe.stdout and "w64" not in probe.stdout:
            self.skipTest(f"{compiler} targets {probe.stdout.strip() or 'unknown'}, not Windows")

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


class NativeTapBundlingTests(unittest.TestCase):
    """Mouser-mac.spec must refuse to package without libmouser_tap.dylib."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.build_py, self.dylib, self.source = native_tap_build.native_tap_paths(
            self.root
        )
        os.makedirs(os.path.dirname(self.build_py))
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("// source\n")
        with open(self.build_py, "w", encoding="utf-8") as fh:
            fh.write("# build\n")
        self.runs = []
        self.logs = []

    def _run_writing_dylib(self, args, **kwargs):
        self.runs.append((args, kwargs))
        with open(self.dylib, "wb") as fh:
            fh.write(b"dylib")

    def _run_writing_nothing(self, args, **kwargs):
        self.runs.append((args, kwargs))

    def _run_failing(self, args, **kwargs):
        self.runs.append((args, kwargs))
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(1, args)

    def test_missing_dylib_is_built_with_check_true_and_bundled(self):
        result = native_tap_build.native_tap_binaries(
            self.root, run=self._run_writing_dylib, python="py", log=self.logs.append
        )
        self.assertEqual(result, [(self.dylib, ".")])
        self.assertEqual(len(self.runs), 1)
        args, kwargs = self.runs[0]
        self.assertEqual(args, ["py", self.build_py])
        self.assertEqual(kwargs.get("cwd"), self.root)
        self.assertIs(kwargs.get("check"), True)
        self.assertTrue(any("bundling native tap" in m for m in self.logs))

    def test_fresh_dylib_is_not_rebuilt(self):
        with open(self.dylib, "wb") as fh:
            fh.write(b"dylib")
        os.utime(self.source, (1_000_000, 1_000_000))
        os.utime(self.dylib, (2_000_000, 2_000_000))
        result = native_tap_build.native_tap_binaries(
            self.root, run=self._run_writing_nothing, log=self.logs.append
        )
        self.assertEqual(result, [(self.dylib, ".")])
        self.assertEqual(self.runs, [])

    def test_stale_dylib_is_rebuilt(self):
        with open(self.dylib, "wb") as fh:
            fh.write(b"old")
        os.utime(self.dylib, (1_000_000, 1_000_000))
        os.utime(self.source, (2_000_000, 2_000_000))
        native_tap_build.native_tap_binaries(
            self.root, run=self._run_writing_dylib, log=self.logs.append
        )
        self.assertEqual(len(self.runs), 1)

    def test_compiler_failure_aborts_the_build(self):
        """check=True: a clang error propagates instead of shipping the
        Python tap with a warning."""
        with self.assertRaises(subprocess.CalledProcessError):
            native_tap_build.native_tap_binaries(
                self.root, run=self._run_failing, log=self.logs.append
            )
        self.assertEqual(self.logs, [])

    def test_missing_dylib_after_build_is_a_systemexit_not_a_warning(self):
        with self.assertRaises(SystemExit) as ctx:
            native_tap_build.native_tap_binaries(
                self.root, run=self._run_writing_nothing, log=self.logs.append
            )
        message = str(ctx.exception)
        self.assertIn(self.dylib, message)
        self.assertIn("Python", message)
        self.assertIn("leak", message)
        self.assertEqual(self.logs, [])

    def test_missing_build_script_is_a_systemexit(self):
        os.remove(self.build_py)
        with self.assertRaises(SystemExit):
            native_tap_build.native_tap_binaries(
                self.root, run=self._run_writing_nothing, log=self.logs.append
            )
        self.assertEqual(self.runs, [])

    def test_spec_helper_delegates_to_the_script(self):
        """Mouser-mac.spec must call the shared helper (no private copy with
        check=False can creep back in)."""
        with open(os.path.join(REPO_ROOT, "Mouser-mac.spec"), encoding="utf-8") as fh:
            spec = fh.read()
        start = spec.index("def _native_tap_binaries():")
        end = spec.index("a = Analysis(")
        helper = spec[start:end]
        self.assertIn("from scripts.native_tap_build import native_tap_binaries", helper)
        self.assertNotIn("check=False", helper)
        self.assertNotIn("subprocess.run", helper)


if __name__ == "__main__":
    unittest.main()
