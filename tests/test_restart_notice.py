"""Watchdog restart notice reaches the tray, not a hidden window."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch
import tests.support  # noqa: E402,F401  (offscreen Qt, tmp MOUSER_LOG_DIR before main_qml import)

try:
    import main_qml
except Exception:  # pragma: no cover - env without PySide6 / project deps
    main_qml = None


class _FakeTray:
    def __init__(self):
        self.messages = []
        self.tooltip = "Mouser"

    def showMessage(self, title, body, icon, ms):
        self.messages.append((title, body, icon, ms))

    def setToolTip(self, text):
        self.tooltip = text


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _FakeButton:
    def __init__(self):
        self.tooltip = "Mouser"

    def setToolTip_(self, text):
        self.tooltip = text


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class RestartNoticeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(main_qml, "_MACOS_NATIVE_STATUS_ITEM", None))
        self.enterContext(patch.object(main_qml, "_STATUS_TOOLTIP", "Mouser"))

    def test_notice_is_a_tray_warning_and_a_sticky_tooltip(self):
        tray = _FakeTray()
        backend = SimpleNamespace(restartRequired=_FakeSignal())
        main_qml._connect_restart_notice(backend, tray)

        backend.restartRequired.emit("mem footprint_mb=1600 > 1500")

        self.assertEqual(len(tray.messages), 1)
        title, body, icon, ms = tray.messages[0]
        self.assertEqual(title, "Mouser needs a restart")
        self.assertEqual(body, "mem footprint_mb=1600 > 1500")
        self.assertEqual(icon, main_qml.QSystemTrayIcon.MessageIcon.Warning)
        self.assertGreaterEqual(ms, 10_000)
        self.assertEqual(tray.tooltip, "Mouser needs a restart: mem footprint_mb=1600 > 1500")
        self.assertEqual(main_qml._STATUS_TOOLTIP, tray.tooltip)

    def test_native_status_item_gets_the_tooltip_too(self):
        button = _FakeButton()
        item = SimpleNamespace(button=lambda: button)
        with patch.object(main_qml, "_MACOS_NATIVE_STATUS_ITEM", item):
            main_qml._show_restart_notice(_FakeTray(), "mem growth/h=25.0 over 3.0h")
        self.assertEqual(button.tooltip, "Mouser needs a restart: mem growth/h=25.0 over 3.0h")

    def test_tooltip_failure_does_not_lose_the_notification(self):
        class _BrokenTray(_FakeTray):
            def setToolTip(self, text):
                raise RuntimeError("no tray")

        tray = _BrokenTray()
        with patch("builtins.print"):
            main_qml._show_restart_notice(tray, "mem footprint_mb=1600 > 1500")
        self.assertEqual(len(tray.messages), 1)

    def test_backend_exposes_the_signal_the_notice_listens_to(self):
        try:
            from ui.backend import Backend
        except Exception:  # pragma: no cover
            self.skipTest("ui.backend not importable")
        self.assertTrue(hasattr(Backend, "restartRequired"))


if __name__ == "__main__":
    unittest.main()
