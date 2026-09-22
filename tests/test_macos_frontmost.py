"""core.macos_frontmost: pid -> bundle id resolution and its LRU.

The ctypes framework calls are not exercised here (they need macOS and, for
AX, an Accessibility grant); ``_proc_pidpath`` is patched so the resolver
and cache run on any platform against a temporary ``.app`` fixture.
"""

import os
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import macos_frontmost as fm


class BundleIdResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        self.resolves = 0
        real = fm._resolve_bundle_id

        def counted(path):
            self.resolves += 1
            return real(path)

        self.enterContext(patch.object(fm, "_proc_pidpath", self.paths.get))
        self.enterContext(patch.object(fm, "_resolve_bundle_id", counted))
        fm.clear_cache()
        self.addCleanup(fm.clear_cache)

    def _bundle(self, name, bundle_id, *, nested_in=None):
        base = nested_in or self.tmp.name
        app = os.path.join(base, f"{name}.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, fh)
        exe = os.path.join(app, "Contents", "MacOS", name)
        with open(exe, "wb") as fh:
            fh.write(b"bin")
        return app, exe

    def test_bundle_identifier_from_nearest_app(self):
        _, exe = self._bundle("Finder", "com.apple.Finder")
        self.paths[10] = exe
        self.assertEqual(fm.bundle_id_for_pid(10), "com.apple.Finder")

    def test_nested_helper_app_uses_its_own_bundle(self):
        outer, _ = self._bundle("Outer", "com.example.outer")
        _, helper_exe = self._bundle(
            "Helper", "com.example.outer.helper",
            nested_in=os.path.join(outer, "Contents", "Frameworks"),
        )
        self.paths[11] = helper_exe
        self.assertEqual(fm.bundle_id_for_pid(11), "com.example.outer.helper")

    def test_ios_on_mac_wrapper_layout_resolves_the_inner_bundle(self):
        """iPhone/iPad apps on Apple Silicon: Foo.app/Wrapper/Bar.app/Info.plist
        (no Contents/). NSRunningApplication reported the inner id."""
        inner = os.path.join(self.tmp.name, "USCG Exam Prep.app", "Wrapper", "USCG.app")
        os.makedirs(inner)
        with open(os.path.join(inner, "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.codanyon.uscgexamprep"}, fh)
        exe = os.path.join(inner, "USCG")
        open(exe, "wb").close()
        self.paths[10] = exe
        self.assertEqual(fm.bundle_id_for_pid(10), "com.codanyon.uscgexamprep")

    def test_xpc_service_and_app_extension_keep_their_own_ids(self):
        outer, _ = self._bundle("Outer", "com.example.outer")
        for suffix, ident in ((".xpc", "com.example.outer.xpc"), (".appex", "com.example.outer.ext")):
            bundle = os.path.join(outer, "Contents", "PlugIns", f"Helper{suffix}")
            os.makedirs(os.path.join(bundle, "Contents", "MacOS"))
            with open(os.path.join(bundle, "Contents", "Info.plist"), "wb") as fh:
                plistlib.dump({"CFBundleIdentifier": ident}, fh)
            exe = os.path.join(bundle, "Contents", "MacOS", "Helper")
            open(exe, "wb").close()
            self.paths[20] = exe
            self.assertEqual(fm.bundle_id_for_pid(20), ident)

    def test_has_pid_tracks_eviction(self):
        _, exe = self._bundle("A", "com.example.a")
        self.paths[10] = exe
        self.assertFalse(fm.has_pid(10))
        fm.bundle_id_for_pid(10)
        self.assertTrue(fm.has_pid(10))
        fm.evict(10)
        self.assertFalse(fm.has_pid(10))

    def test_bare_executable_falls_back_to_basename(self):
        exe = os.path.join(self.tmp.name, "python3.13")
        with open(exe, "wb") as fh:
            fh.write(b"bin")
        self.paths[12] = exe
        self.assertEqual(fm.bundle_id_for_pid(12), "python3.13")

    def test_app_without_identifier_falls_back_to_basename(self):
        app = os.path.join(self.tmp.name, "Broken.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
            fh.write(b"not a plist")
        exe = os.path.join(app, "Contents", "MacOS", "brokenbin")
        with open(exe, "wb") as fh:
            fh.write(b"bin")
        self.paths[13] = exe
        self.assertEqual(fm.bundle_id_for_pid(13), "brokenbin")

    def test_unknown_pid_is_none(self):
        self.assertIsNone(fm.bundle_id_for_pid(4242))
        self.assertEqual(fm.cache_size(), 0)

    def test_repeat_lookups_hit_the_cache(self):
        _, exe = self._bundle("Finder", "com.apple.Finder")
        self.paths[10] = exe
        for _ in range(1_000):
            self.assertEqual(fm.bundle_id_for_pid(10), "com.apple.Finder")
        self.assertEqual(self.resolves, 1)
        self.assertEqual(fm.cache_size(), 1)

    def test_pid_reuse_with_a_different_binary_misses(self):
        _, a = self._bundle("A", "com.example.a")
        _, b = self._bundle("B", "com.example.b")
        self.paths[10] = a
        self.assertEqual(fm.bundle_id_for_pid(10), "com.example.a")
        self.paths[10] = b
        self.assertEqual(fm.bundle_id_for_pid(10), "com.example.b")
        self.assertEqual(self.resolves, 2)

    def test_updated_binary_mtime_misses(self):
        _, exe = self._bundle("A", "com.example.a")
        self.paths[10] = exe
        fm.bundle_id_for_pid(10)
        os.utime(exe, (1_000_000, 1_000_000))
        fm.bundle_id_for_pid(10)
        self.assertEqual(self.resolves, 2)

    def test_evict_forgets_only_that_pid(self):
        _, a = self._bundle("A", "com.example.a")
        _, b = self._bundle("B", "com.example.b")
        self.paths[10], self.paths[11] = a, b
        fm.bundle_id_for_pid(10)
        fm.bundle_id_for_pid(11)
        fm.evict(10)
        self.assertEqual(fm.cache_size(), 1)
        fm.bundle_id_for_pid(11)
        self.assertEqual(self.resolves, 2)
        fm.bundle_id_for_pid(10)
        self.assertEqual(self.resolves, 3)
        fm.evict(999)  # unknown pid is a no-op
        self.assertEqual(fm.cache_size(), 2)

    def test_lru_is_bounded_and_drops_the_oldest(self):
        exes = {}
        for i in range(fm.LRU_SIZE + 10):
            _, exes[i] = self._bundle(f"App{i}", f"com.example.app{i}")
            self.paths[1000 + i] = exes[i]
            fm.bundle_id_for_pid(1000 + i)
        self.assertEqual(fm.cache_size(), fm.LRU_SIZE)
        resolves = self.resolves
        # The newest entries are still cached...
        fm.bundle_id_for_pid(1000 + fm.LRU_SIZE + 9)
        self.assertEqual(self.resolves, resolves)
        # ...the oldest was evicted.
        fm.bundle_id_for_pid(1000)
        self.assertEqual(self.resolves, resolves + 1)

    def test_recently_used_entry_survives_eviction(self):
        for i in range(fm.LRU_SIZE):
            _, exe = self._bundle(f"App{i}", f"com.example.app{i}")
            self.paths[1000 + i] = exe
            fm.bundle_id_for_pid(1000 + i)
        fm.bundle_id_for_pid(1000)  # touch the oldest
        _, exe = self._bundle("New", "com.example.new")
        self.paths[2000] = exe
        fm.bundle_id_for_pid(2000)  # evicts 1001, not 1000
        resolves = self.resolves
        fm.bundle_id_for_pid(1000)
        self.assertEqual(self.resolves, resolves)
        fm.bundle_id_for_pid(1001)
        self.assertEqual(self.resolves, resolves + 1)


class PlatformGuardTests(unittest.TestCase):
    def test_focused_pid_is_none_off_macos(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(fm, "_libs", None), patch.object(fm, "_libs_failed", False):
            self.assertIsNone(fm.focused_pid())
            self.assertIsNone(fm._proc_pidpath(1))

    def test_framework_load_failure_is_remembered_and_logged_once(self):
        with patch.object(sys, "platform", "darwin"), \
                patch.object(fm, "_libs", None), patch.object(fm, "_libs_failed", False), \
                patch.object(fm, "_Libs", side_effect=OSError("no framework")), \
                patch("builtins.print") as fake_print:
            self.assertIsNone(fm.focused_pid())
            self.assertIsNone(fm.focused_pid())
        self.assertEqual(fake_print.call_count, 1)


@unittest.skipUnless(sys.platform == "darwin", "needs the macOS frameworks")
class LiveMacOSTests(unittest.TestCase):
    """Real ctypes calls. AX needs an Accessibility grant for this
    interpreter, so only the CGWindowList fallback and libproc are asserted;
    the AX path must simply not raise."""

    @unittest.skipUnless(
        os.path.isdir("/Applications/USCG Exam Prep.app"), "iOS-on-Mac app not installed"
    )
    def test_installed_ios_app_resolves_to_its_bundle_id(self):
        with patch.object(fm, "_proc_pidpath", lambda pid: (
            "/Applications/USCG Exam Prep.app/Wrapper/USCG.app/USCG"
        )):
            self.assertEqual(fm.bundle_id_for_pid(77), "com.codanyon.uscgexamprep")
        fm.evict(77)

    def test_focused_pid_and_self_resolution(self):
        libs = fm._get_libs()
        self.assertIsNotNone(libs)
        fm._ax_focused_pid(libs)  # None without the grant; must not raise
        pid = fm.focused_pid()
        self.assertIsNotNone(pid)
        self.assertGreater(pid, 0)
        self.assertEqual(
            fm._proc_pidpath(os.getpid()), os.path.realpath(sys.executable)
        )
        self.assertIsInstance(fm.bundle_id_for_pid(os.getpid()), str)
        fm.evict(os.getpid())

    def test_repeated_lookups_retain_nothing(self):
        for _ in range(500):
            self.assertIsNotNone(fm.focused_pid())


if __name__ == "__main__":
    unittest.main()
