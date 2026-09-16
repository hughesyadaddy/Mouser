import contextlib
import importlib
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from core import app_detector


# ----------------------------------------------------------------------
# macOS fakes (no real PyObjC / AppKit is touched)
# ----------------------------------------------------------------------

class _FakeRunningApp:
    def __init__(self, bundle_id=None, exe_path=None, name=None):
        self._bundle_id = bundle_id
        self._exe_path = exe_path
        self._name = name

    def bundleIdentifier(self):
        return self._bundle_id

    def executableURL(self):
        if self._exe_path is None:
            return None
        url = MagicMock()
        url.path.return_value = self._exe_path
        return url

    def localizedName(self):
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
    def __init__(self, center, frontmost):
        self._center = center
        self.frontmost = frontmost
        self.frontmost_calls = 0

    def notificationCenter(self):
        return self._center

    def frontmostApplication(self):
        self.frontmost_calls += 1
        return self.frontmost


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


class AppDetectorMacOSTests(unittest.TestCase):
    def _darwin_module(self, center, frontmost):
        workspace = _FakeWorkspace(center, frontmost)
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

    def test_observer_registered_once_and_no_polling(self):
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(
            center, _FakeRunningApp(bundle_id="com.apple.Finder")
        )
        collector = _Collector()
        detector = module.AppDetector(collector, interval=0.01)

        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))
        self.assertEqual(collector.seen, ["com.apple.Finder"])

        # Steady state: no further frontmostApplication() reads.
        threading.Event().wait(0.15)
        self.assertEqual(workspace.frontmost_calls, 1)
        self.assertEqual(len(center.add_calls), 1)
        self.assertEqual(
            center.add_calls[0][0], "NSWorkspaceDidActivateApplicationNotification"
        )
        self.assertEqual(center.add_calls[0][1:3], (None, None))

        # A second start() while running must not register a second observer.
        detector.start()
        self.assertEqual(len(center.add_calls), 1)

    def test_notification_fires_callback_with_same_payload(self):
        center = _FakeNotificationCenter()
        module, workspace = self._darwin_module(
            center, _FakeRunningApp(bundle_id="com.apple.Finder")
        )
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        center.post(
            "NSWorkspaceDidActivateApplicationNotification",
            _FakeRunningApp(bundle_id="com.google.Chrome"),
        )
        self.assertTrue(collector.wait(2))
        # Same identifier rules as get_foreground_exe(): bundle id, else exe
        # basename, else localized name.
        center.post(
            "NSWorkspaceDidActivateApplicationNotification",
            _FakeRunningApp(exe_path="/Applications/X.app/Contents/MacOS/xbin"),
        )
        self.assertTrue(collector.wait(3))
        center.post(
            "NSWorkspaceDidActivateApplicationNotification",
            _FakeRunningApp(name="Nameless"),
        )
        self.assertTrue(collector.wait(4))
        # Re-activation of the same app is deduplicated.
        center.post(
            "NSWorkspaceDidActivateApplicationNotification",
            _FakeRunningApp(name="Nameless"),
        )
        threading.Event().wait(0.05)

        self.assertEqual(
            collector.seen,
            ["com.apple.Finder", "com.google.Chrome", "xbin", "Nameless"],
        )
        # Callbacks are delivered from the detector thread, not the poster.
        self.assertTrue(all(t == "AppDetector" for t in collector.threads))
        self.assertEqual(workspace.frontmost_calls, 1)

    def test_notification_without_app_is_ignored(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center, _FakeRunningApp(bundle_id="a.b"))
        collector = _Collector()
        detector = module.AppDetector(collector)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(collector.wait(1))

        center.post("NSWorkspaceDidActivateApplicationNotification", None)
        threading.Event().wait(0.05)
        self.assertEqual(collector.seen, ["a.b"])

    def test_stop_removes_observer(self):
        center = _FakeNotificationCenter()
        module, _ = self._darwin_module(center, _FakeRunningApp(bundle_id="a.b"))
        detector = module.AppDetector(_Collector())
        detector.start()
        token = center.add_calls[0][3]

        detector.stop()

        self.assertEqual(center.remove_calls, [token])
        self.assertFalse(detector._thread.is_alive())
        # Idempotent: a second stop() must not remove twice.
        detector.stop()
        self.assertEqual(center.remove_calls, [token])

    def test_fallback_poll_when_observer_install_raises(self):
        center = _FakeNotificationCenter(fail_add=True)
        module, workspace = self._darwin_module(
            center, _FakeRunningApp(bundle_id="a.b")
        )
        collector = _Collector()
        detector = module.AppDetector(collector, interval=0.01)

        with patch.object(module, "FALLBACK_POLL_INTERVAL", 0.02):
            with patch("builtins.print") as fake_print:
                detector.start()
            self.addCleanup(detector.stop)
            self.assertTrue(collector.wait(1))
            threading.Event().wait(0.1)

        self.assertEqual(collector.seen, ["a.b"])
        self.assertGreaterEqual(workspace.frontmost_calls, 2)
        self.assertEqual(center.remove_calls, [])
        # Logged exactly once.
        msgs = [c.args[0] for c in fake_print.call_args_list if c.args]
        self.assertEqual(
            len([m for m in msgs if "activation observer unavailable" in m]), 1
        )

    def test_fallback_uses_slow_interval_not_caller_interval(self):
        center = _FakeNotificationCenter(fail_add=True)
        module, workspace = self._darwin_module(
            center, _FakeRunningApp(bundle_id="a.b")
        )
        detector = module.AppDetector(_Collector(), interval=0.001)
        with patch("builtins.print"):
            detector.start()
        self.addCleanup(detector.stop)
        threading.Event().wait(0.1)
        # At 5 s fallback only the initial read happens within 100 ms.
        self.assertEqual(workspace.frontmost_calls, 1)


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
