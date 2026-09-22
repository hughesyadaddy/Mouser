import contextlib
import importlib
import os
import plistlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from core import app_detector


# ----------------------------------------------------------------------
# macOS fakes (no real PyObjC / AppKit is touched)
# ----------------------------------------------------------------------

class _FakeRunningApp:
    """NSRunningApplication stand-in. Only processIdentifier() may be read;
    every LaunchServices-backed accessor counts a call (must stay 0)."""

    ls_calls = 0

    def __init__(self, pid, bundle_id=None, exe_path=None, name=None):
        self._pid = pid
        self._bundle_id = bundle_id
        self._exe_path = exe_path
        self._name = name

    def processIdentifier(self):
        return self._pid

    def bundleIdentifier(self):
        _FakeRunningApp.ls_calls += 1
        return self._bundle_id

    def executableURL(self):
        _FakeRunningApp.ls_calls += 1
        if self._exe_path is None:
            return None
        url = MagicMock()
        url.path.return_value = self._exe_path
        return url

    def localizedName(self):
        _FakeRunningApp.ls_calls += 1
        return self._name


class _FakeNotificationCenter:
    """Records observers and lets a test post a fake activation."""

    def __init__(self, fail_add=False):
        self.fail_add = fail_add
        self.add_calls = []
        self.remove_calls = []
        self._blocks = {}
        self._next = 1

    def addObserverForName_object_queue_usingBlock_(self, name, obj, q, block):
        if self.fail_add:
            raise RuntimeError("no notification center")
        token = f"token-{self._next}"
        self._next += 1
        self.add_calls.append((name, obj, q, token))
        self._blocks[token] = (name, block)
        return token

    def removeObserver_(self, token):
        self.remove_calls.append(token)
        self._blocks.pop(token, None)

    def post(self, name, app):
        note = MagicMock()
        note.userInfo.return_value = {"NSWorkspaceApplicationKey": app}
        for n, block in list(self._blocks.values()):
            if n == name:
                block(note)


class _FakeWorkspace:
    def __init__(self, center):
        self._center = center
        self.frontmost_calls = 0

    def notificationCenter(self):
        return self._center

    def frontmostApplication(self):
        self.frontmost_calls += 1
        return None


def _fake_objc_module():
    mod = types.ModuleType("objc")
    mod.autorelease_pool = contextlib.nullcontext
    return mod


class _Collector:
    """Records callback payloads and the thread each one arrived on."""

    def __init__(self):
        self.seen = []
        self.threads = []
        self._tick = threading.Event()

    def __call__(self, exe):
        self.seen.append(exe)
        self.threads.append(threading.current_thread().name)

    def wait(self, count, timeout=2.0):
        for _ in range(int(timeout / 0.01)):
            if len(self.seen) >= count:
                return True
            self._tick.wait(0.01)
        return len(self.seen) >= count


_ACTIVATE = "NSWorkspaceDidActivateApplicationNotification"
_TERMINATE = "NSWorkspaceDidTerminateApplicationNotification"


class AppDetectorMacOSTests(unittest.TestCase):
    """macOS detector: pid in, identifier out, zero LaunchServices calls."""

    def setUp(self):
        _FakeRunningApp.ls_calls = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # pid -> executable path, standing in for proc_pidpath.
        self.paths = {}
        self.focused = [None]  # what focused_pid() returns
        self.focused_calls = 0
        self.resolve_calls = 0
        self.frontmost_module = importlib.import_module("core.macos_frontmost")
        self.frontmost_module.clear_cache()
        self.addCleanup(self.frontmost_module.clear_cache)
        real_resolve = self.frontmost_module._resolve_bundle_id

        def counted_resolve(path):
            self.resolve_calls += 1
            return real_resolve(path)

        def focused_pid():
            self.focused_calls += 1
            return self.focused[0]

        self.enterContext(patch.object(self.frontmost_module, "focused_pid", focused_pid))
        self.enterContext(patch.object(self.frontmost_module, "_proc_pidpath", self.paths.get))
        self.enterContext(patch.object(self.frontmost_module, "_resolve_bundle_id", counted_resolve))

    def _app(self, pid, name, bundle_id=None):
        """Register a fake process: a .app bundle when bundle_id is given,
        else a bare executable."""
        if bundle_id:
            app = os.path.join(self.tmp.name, f"{name}.app")
            os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
                plistlib.dump({"CFBundleIdentifier": bundle_id}, fh)
            path = os.path.join(app, "Contents", "MacOS", name)
        else:
            path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(b"bin")
        self.paths[pid] = path
        return _FakeRunningApp(pid, bundle_id=bundle_id, exe_path=path, name=name)

    def _darwin_module(self, center):
        workspace = _FakeWorkspace(center)
        appkit = types.ModuleType("AppKit")
        appkit.NSWorkspace = MagicMock()
        appkit.NSWorkspace.sharedWorkspace.return_value = workspace
        self.enterContext(patch.object(sys, "platform", "darwin"))
        self.enterContext(
            patch.dict(sys.modules, {"objc": _fake_objc_module(), "AppKit": appkit})
        )
        importlib.reload(app_detector)
        self.addCleanup(importlib.reload, app_detector)
        return app_detector, workspace

    def test_observers_registered_once_and_no_polling(self):
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        self._app(100, "Finder", "com.apple.Finder")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector, interval=0.01)

        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))
        self.assertEqual(collector.seen, ["com.apple.Finder"])

        # Steady state: one AX read at start, no LaunchServices at all.
        threading.Event().wait(0.15)
        self.assertEqual(self.focused_calls, 1)
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)
        names = [c[0] for c in center.add_calls]
        self.assertEqual(names, [_ACTIVATE, _TERMINATE])
        self.assertEqual(center.add_calls[0][1:3], (None, None))

        # A second start() while running must not register again.
        detector.start()
        self.assertEqual(len(center.add_calls), 2)

    def test_notification_pid_resolves_to_bundle_id_or_basename(self):
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        self._app(100, "Finder", "com.apple.Finder")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        center.post(_ACTIVATE, self._app(200, "Chrome", "com.google.Chrome"))
        self.assertTrue(collector.wait(2))
        # No bundle: executable basename.
        center.post(_ACTIVATE, self._app(300, "xbin"))
        self.assertTrue(collector.wait(3))
        # Re-activation of the same pid is deduplicated before resolution.
        resolves = self.resolve_calls
        center.post(_ACTIVATE, _FakeRunningApp(300))
        threading.Event().wait(0.05)

        self.assertEqual(collector.seen, ["com.apple.Finder", "com.google.Chrome", "xbin"])
        self.assertEqual(self.resolve_calls, resolves)
        self.assertTrue(all(t == "AppDetector" for t in collector.threads))
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)

    def test_notification_without_app_or_pid_is_ignored(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        self._app(100, "a", "a.b")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        center.post(_ACTIVATE, None)
        center.post(_ACTIVATE, _FakeRunningApp(0))
        center.post(_ACTIVATE, _FakeRunningApp(999))  # unknown pid: no path
        threading.Event().wait(0.05)
        self.assertEqual(collector.seen, ["a.b"])

    def test_terminate_notification_evicts_the_cache(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        app = self._app(100, "a", "a.b")
        self.focused[0] = 100
        detector = module.AppDetector(_Collector())
        detector.start()
        self.addCleanup(detector.stop)
        threading.Event().wait(0.05)
        self.assertEqual(self.frontmost_module.cache_size(), 1)

        center.post(_TERMINATE, app)
        self.assertEqual(self.frontmost_module.cache_size(), 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)

    def test_terminate_clears_last_pid_so_a_reused_pid_is_delivered(self):
        """A terminates on pid 500, B launches and gets pid 500: B's first
        activation must not be deduplicated against A."""
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        app_a = self._app(500, "A", "com.a")
        self.focused[0] = 500
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        center.post(_TERMINATE, app_a)
        self.assertIsNone(detector._last_pid)
        self.assertEqual(self.frontmost_module.cache_size(), 0)
        center.post(_ACTIVATE, self._app(500, "B", "com.b"))
        self.assertTrue(collector.wait(2))
        self.assertEqual(collector.seen, ["com.a", "com.b"])
        self.assertEqual(_FakeRunningApp.ls_calls, 0)

    def test_evicted_pid_is_re_resolved_even_without_the_terminate_path(self):
        """Reviewer reproducer: eviction alone (no detector-side notification)
        must also defeat the dedupe."""
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        seen = []
        detector = module.AppDetector(seen.append)
        with patch.object(module, "_identifier_for_pid",
                          side_effect=lambda pid: {500: "com.a", 501: "com.b"}.get(pid)):
            detector._deliver(500)
            self.frontmost_module.evict(500)
            with patch.object(module, "_identifier_for_pid", return_value="com.b"):
                detector._deliver(500)
        self.assertEqual(seen, ["com.a", "com.b"])

    def test_same_pid_still_cached_is_deduplicated(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        self._app(500, "A", "com.a")
        seen = []
        detector = module.AppDetector(seen.append)
        detector._deliver(500)
        resolves = self.resolve_calls
        for _ in range(100):
            detector._deliver(500)
        self.assertEqual(seen, ["com.a"])
        self.assertEqual(self.resolve_calls, resolves)

    def test_run_observer_idle_branch_fires_the_ax_watchdog(self):
        """Drive the real _run_observer loop through idle >= FALLBACK_POLL_INTERVAL."""
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        self._app(100, "a", "a.b")
        self._app(200, "b", "b.c")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector)
        with patch.object(module, "IDLE_TICK_S", 0.005), \
                patch.object(module, "FALLBACK_POLL_INTERVAL", 0.02):
            detector.start()
            self.addCleanup(detector.stop)
            self.assertTrue(collector.wait(1))
            threading.Event().wait(0.2)
            # Several watchdog reads happened, nothing was resolved again.
            self.assertGreaterEqual(self.focused_calls, 4)
            self.assertEqual(self.resolve_calls, 1)
            # A switch the observer never reported is picked up by the watchdog.
            self.focused[0] = 200
            self.assertTrue(collector.wait(2))
        self.assertEqual(collector.seen, ["a.b", "b.c"])
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)

    def test_stop_removes_both_observers(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center)
        detector = module.AppDetector(_Collector())
        detector.start()
        tokens = [c[3] for c in center.add_calls]

        detector.stop()

        self.assertEqual(sorted(center.remove_calls), sorted(tokens))
        self.assertFalse(detector._thread.is_alive())
        # Idempotent: a second stop() must not remove twice.
        detector.stop()
        self.assertEqual(len(center.remove_calls), 2)

    def test_idle_watchdog_is_an_ax_pid_compare(self):
        """The 30 s watchdog only re-reads the pid; it resolves nothing
        unless the observer missed a switch."""
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        self._app(100, "a", "a.b")
        self._app(200, "b", "b.c")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector._idle_check()
        self.assertEqual(collector.seen, ["a.b"])
        resolves = self.resolve_calls
        for _ in range(50):
            detector._idle_check()
        self.assertEqual(self.resolve_calls, resolves)
        # A missed switch is caught by the watchdog.
        self.focused[0] = 200
        detector._idle_check()
        self.assertEqual(collector.seen, ["a.b", "b.c"])
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)
        self.assertEqual(module.FALLBACK_POLL_INTERVAL, 30.0)

    def test_soak_no_launchservices_calls_and_bounded_resolution(self):
        """10 000 idle ticks + 1 000 activations over 5 pids: zero
        bundleIdentifier()/frontmostApplication() calls, <= 5 resolutions."""
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        apps = [self._app(100 + i, f"app{i}", f"com.example.app{i}") for i in range(5)]
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        for i in range(1_000):
            center.post(_ACTIVATE, apps[i % 5])
        for i in range(10_000):
            self.focused[0] = 100 + (i % 5)
            detector._idle_check()
        # Drain the activation queue.
        for _ in range(200):
            if detector._events.empty():
                break
            threading.Event().wait(0.01)
        threading.Event().wait(0.05)

        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_FakeRunningApp.ls_calls, 0)
        self.assertLessEqual(self.resolve_calls, 5)
        self.assertLessEqual(self.frontmost_module.cache_size(), 5)
        self.assertEqual(set(collector.seen), {f"com.example.app{i}" for i in range(5)})

    def test_fallback_poll_when_observer_install_raises(self):
        center = _FakeNotificationCenter(fail_add=True)
        module, workspace = self._darwin_module(center)
        self._app(100, "a", "a.b")
        self.focused[0] = 100
        collector = _Collector()
        detector = module.AppDetector(collector, interval=0.01)

        with patch.object(module, "FALLBACK_POLL_INTERVAL", 0.02):
            with patch("builtins.print") as fake_print:
                detector.start()
            self.addCleanup(detector.stop)
            self.assertTrue(collector.wait(1))
            threading.Event().wait(0.1)

        self.assertEqual(collector.seen, ["a.b"])
        self.assertGreaterEqual(self.focused_calls, 2)
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(center.remove_calls, [])
        # Logged exactly once.
        msgs = [c.args[0] for c in fake_print.call_args_list if c.args]
        self.assertEqual(
            len([m for m in msgs if "activation observer unavailable" in m]), 1
        )

    def test_fallback_uses_slow_interval_not_caller_interval(self):
        center = _FakeNotificationCenter(fail_add=True)
        module, _ = self._darwin_module(center)
        self._app(100, "a", "a.b")
        self.focused[0] = 100
        detector = module.AppDetector(_Collector(), interval=0.001)
        with patch("builtins.print"):
            detector.start()
        self.addCleanup(detector.stop)
        threading.Event().wait(0.1)
        # At the 30 s fallback only the initial read happens within 100 ms.
        self.assertEqual(self.focused_calls, 1)

    def test_get_foreground_exe_uses_ax_pid_and_resolver(self):
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        self._app(100, "Finder", "com.apple.Finder")
        self.focused[0] = 100
        self.assertEqual(module.get_foreground_exe(), "com.apple.Finder")
        self.focused[0] = None
        self.assertIsNone(module.get_foreground_exe())
        self.assertEqual(workspace.frontmost_calls, 0)


class AppDetectorPollingPlatformTests(unittest.TestCase):
    def test_non_observer_platform_still_polls_at_interval(self):
        with patch.object(sys, "platform", "linux"):
            importlib.reload(app_detector)
        self.addCleanup(importlib.reload, app_detector)
        self.assertIsNone(app_detector._install_activation_observer)

        values = iter(["/a", "/a", "/b"])
        collector = _Collector()
        with patch.object(
            app_detector, "get_foreground_exe",
            side_effect=lambda: next(values, "/b"),
        ) as fg:
            detector = app_detector.AppDetector(collector, interval=0.01)
            detector.start()
            self.addCleanup(detector.stop)
            self.assertTrue(collector.wait(2))
            self.assertEqual(collector.seen, ["/a", "/b"])
            self.assertGreaterEqual(fg.call_count, 3)


class AppDetectorLinuxTests(unittest.TestCase):
    def _reload_for_linux(self, session_type: str, desktop: str):
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(
                os.environ,
                {
                    "XDG_SESSION_TYPE": session_type,
                    "XDG_CURRENT_DESKTOP": desktop,
                },
                clear=False,
            ),
        ):
            importlib.reload(app_detector)
        self.addCleanup(importlib.reload, app_detector)
        return app_detector

    def test_kde_wayland_prefers_kdotool(self):
        module = self._reload_for_linux("wayland", "KDE")

        with (
            patch.object(module, "_get_foreground_kdotool", return_value="/tmp/kde-app"),
            patch.object(module, "_get_foreground_xdotool", return_value="/tmp/x11-app") as xdotool,
        ):
            self.assertEqual(module.get_foreground_exe(), "/tmp/kde-app")
            xdotool.assert_not_called()

    def test_kde_wayland_falls_back_to_xdotool(self):
        module = self._reload_for_linux("wayland", "KDE")

        with (
            patch.object(module, "_get_foreground_kdotool", return_value=None),
            patch.object(module, "_get_foreground_xdotool", return_value="/tmp/xwayland-app") as xdotool,
        ):
            self.assertEqual(module.get_foreground_exe(), "/tmp/xwayland-app")
            xdotool.assert_called_once_with()

    def test_non_kde_wayland_returns_none(self):
        module = self._reload_for_linux("wayland", "GNOME")

        with patch.object(module, "_get_foreground_xdotool", return_value="/tmp/x11-app") as xdotool:
            self.assertIsNone(module.get_foreground_exe())
            xdotool.assert_not_called()

    def test_x11_uses_xdotool(self):
        module = self._reload_for_linux("x11", "KDE")

        with patch.object(module, "_get_foreground_xdotool", return_value="/tmp/x11-app") as xdotool:
            self.assertEqual(module.get_foreground_exe(), "/tmp/x11-app")
            xdotool.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
