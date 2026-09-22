"""MainWindowHost / GestureHudHost lifecycle (M3 QML teardown).

The live tests build the real ``Main.qml`` + ``GestureHud.qml`` on the
``offscreen`` platform with the real ``Backend`` (engine=None, config
patched). They need a ``QApplication``; when another test module already
created a bare ``QCoreApplication`` in this process (``test_backend`` does),
they are re-run in a clean subprocess instead (see ``SubprocessRunner``).
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QML_DIR = ROOT / "ui" / "qml"
MAIN_QML = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
MOUSE_PAGE_QML = (QML_DIR / "MousePage.qml").read_text(encoding="utf-8")
HOTSPOT_QML = (QML_DIR / "HotspotDot.qml").read_text(encoding="utf-8")
HUD_QML = (QML_DIR / "GestureHud.qml").read_text(encoding="utf-8")

import tests.support  # noqa: F401  - forces QT_QPA_PLATFORM=offscreen before PySide6

try:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QMetaObject, QObject, QTimer
    from PySide6.QtGui import QGuiApplication, QWindow
    from PySide6.QtQml import QQmlEngine
    from PySide6.QtQuick import QQuickImageProvider, QQuickItem
    from PySide6.QtWidgets import QApplication

    import main_qml
    from core.config import DEFAULT_CONFIG
    from ui.backend import Backend
    from ui.locale_manager import LocaleManager
except Exception:  # pragma: no cover - env without PySide6 / project deps
    main_qml = None


_LIVE_ENV = "MOUSER_TEST_MAIN_WINDOW_HOST_LIVE"


def _foreign_app_present() -> bool:
    if main_qml is None:
        return False
    app = QCoreApplication.instance()
    return app is not None and not isinstance(app, QGuiApplication)


# ── Static QML checks (no Qt needed) ─────────────────────────────


class QmlStructureTests(unittest.TestCase):
    def test_device_image_decodes_at_display_size_without_mipmaps(self):
        block = re.search(r"Image \{\s*id: mouseImg.*?\n\s*\}", MOUSE_PAGE_QML, re.S)
        self.assertIsNotNone(block, "mouseImg Image block missing")
        text = block.group(0)
        self.assertRegex(text, r"sourceSize\.width:\s*Math\.ceil\(width \* Screen\.devicePixelRatio\)")
        self.assertRegex(text, r"sourceSize\.height:\s*Math\.ceil\(height \* Screen\.devicePixelRatio\)")
        self.assertNotIn("mipmap: true", text)

    def test_hotspot_has_no_canvas(self):
        self.assertNotRegex(HOTSPOT_QML, r"\bCanvas\s*\{")
        self.assertIn("ShapePath", HOTSPOT_QML)
        self.assertIn("ShapePath.DashLine", HOTSPOT_QML)

    def test_hotspot_pulse_stops_while_window_hidden(self):
        pulse = re.search(r"SequentialAnimation on scale \{.*?running:([^\n]*)", HOTSPOT_QML, re.S)
        self.assertIsNotNone(pulse)
        self.assertIn("Window.visibility", pulse.group(1))

    def test_pages_are_loaders_without_stickiness(self):
        self.assertNotIn("|| item", MAIN_QML)
        self.assertRegex(MAIN_QML, r"Loader \{\s*id: mousePageLoader")
        self.assertIn('source: "MousePage.qml"', MAIN_QML)
        self.assertIn('source: "ScrollPage.qml"', MAIN_QML)
        self.assertNotRegex(MAIN_QML, re.compile(r"^\s*MousePage \{", re.M))

    def test_current_page_lives_on_ui_state(self):
        self.assertIn("property int currentPage: uiState.currentPage", MAIN_QML)
        self.assertIn("uiState.currentPage = page", MAIN_QML)

    def test_hud_moved_to_own_file(self):
        self.assertNotRegex(MAIN_QML, r"id:\s*gestureHud")
        self.assertRegex(HUD_QML, re.compile(r"^Window \{", re.M))
        self.assertIn("target: backend", HUD_QML)
        self.assertIn("onGestureFeedback", HUD_QML)
        self.assertNotIn("root.", HUD_QML)

    def test_action_picker_and_debug_card_are_lazy(self):
        self.assertRegex(MOUSE_PAGE_QML, r"Loader \{\s*id: pickerLoader")
        self.assertRegex(MOUSE_PAGE_QML, re.compile(r"id: pickerLoader.*?active: selectedButton !== \"\"", re.S))
        self.assertRegex(MOUSE_PAGE_QML, r"Loader \{\s*id: debugCardLoader")
        self.assertRegex(MOUSE_PAGE_QML, re.compile(r"id: debugCardLoader.*?active: backend\.debugMode", re.S))

    def test_dismiss_clears_hotspot_selection(self):
        self.assertIn("function clearSelection()", MOUSE_PAGE_QML)
        dismiss = re.search(r"function dismiss\(\) \{.*?\n    \}", MAIN_QML, re.S).group(0)
        self.assertIn("clearSelection()", dismiss)


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class UiStateCurrentPageTests(unittest.TestCase):
    def test_current_page_property_notifies_and_coerces(self):
        seen = []

        class _FakeApp:
            def font(self):
                from PySide6.QtGui import QFont
                return QFont()

            def styleHints(self):
                from PySide6.QtCore import Qt
                from types import SimpleNamespace
                return SimpleNamespace(colorScheme=lambda: Qt.ColorScheme.Light)

        state = main_qml.UiState(_FakeApp())
        state.currentPageChanged.connect(lambda: seen.append(state.currentPage))
        self.assertEqual(state.currentPage, 0)
        state.currentPage = 1
        state.currentPage = 1
        state.currentPage = "bogus"
        self.assertEqual(seen, [1, 0])


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class QuitFilterTests(unittest.TestCase):
    def test_quit_filter_tolerates_missing_window(self):
        from PySide6.QtCore import QEvent

        filt = main_qml._MacOSQuitToTrayFilter(None)
        with patch.object(main_qml, "_macos_current_quit_is_system_session_event", return_value=False):
            self.assertTrue(filt.eventFilter(None, QEvent(QEvent.Type.Quit)))

        class _Win:
            hidden = 0

            def hide(self):
                self.hidden += 1

        win = _Win()
        filt.set_window(win)
        with patch.object(main_qml, "_macos_current_quit_is_system_session_event", return_value=False):
            self.assertTrue(filt.eventFilter(None, QEvent(QEvent.Type.Quit)))
        self.assertEqual(win.hidden, 1)
        filt.set_window(None)
        with patch.object(main_qml, "_macos_current_quit_is_system_session_event", return_value=False):
            self.assertTrue(filt.eventFilter(None, QEvent(QEvent.Type.Quit)))
        self.assertEqual(win.hidden, 1)


# ── Live engine tests ────────────────────────────────────────────


def _pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    QCoreApplication.sendPostedEvents()


def _qml_refs(obj) -> list:
    """Attribute names on ``obj`` whose value still references a QML/Quick object."""
    qml_types = (QQmlEngine, QWindow, QQuickItem, QQuickImageProvider)
    found = []

    def _walk(name, value):
        if isinstance(value, qml_types):
            found.append(name)
        elif isinstance(value, dict):
            for key, item in value.items():
                _walk(f"{name}[{key!r}]", item)
        elif isinstance(value, (list, tuple, set)):
            for index, item in enumerate(value):
                _walk(f"{name}[{index}]", item)

    for name, value in vars(obj).items():
        _walk(name, value)
    return found


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class MainWindowHostLiveTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        if _foreign_app_present():
            raise unittest.SkipTest("bare QCoreApplication already present; run via SubprocessRunner")
        cls.app = QCoreApplication.instance() or QApplication(sys.argv)
        cls.app.setQuitOnLastWindowClosed(False)
        with (
            patch("ui.backend.load_config", return_value=copy.deepcopy(DEFAULT_CONFIG)),
            patch("ui.backend.save_config"),
            patch("ui.backend.supports_login_startup", return_value=False),
        ):
            cls.backend = Backend(engine=None, root_dir=str(ROOT))
        cls.ui_state = main_qml.UiState(cls.app)
        cls.locale_mgr = LocaleManager(language="en")
        cls.context = {
            "backend": cls.backend,
            "uiState": cls.ui_state,
            "lm": cls.locale_mgr,
            "appVersion": main_qml.APP_VERSION,
            "appBuildMode": main_qml.APP_BUILD_MODE,
            "appCommit": main_qml.APP_COMMIT_DISPLAY,
            "appLaunchPath": str(ROOT),
        }
        cls.hud = main_qml.GestureHudHost(
            qml_path=str(QML_DIR / "GestureHud.qml"),
            context_properties=cls.context,
        )

    def _make_host(self, **kwargs):
        kwargs.setdefault("teardown_delay_ms", 50)
        return main_qml.MainWindowHost(
            qml_path=str(QML_DIR / "Main.qml"),
            context_properties=self.context,
            image_providers={
                "appicons": lambda: main_qml.AppIconProvider(str(ROOT)),
                "systemicons": main_qml.SystemIconProvider,
            },
            launch_hidden=False,
            **kwargs,
        )

    def setUp(self):
        self.ui_state.currentPage = 0
        self.host = self._make_host()
        self._extra_hosts = []

    def tearDown(self):
        for host in [self.host, *self._extra_hosts]:
            host.teardown(force=True)
            _pump(50)
            host.deleteLater()
        _pump(20)

    # -- helpers --
    def _about_dialog(self):
        window = self.host.window()
        for child in window.findChildren(QObject):
            if child.objectName() == "aboutDialog":
                return child
        self.fail("aboutDialog not found")

    def _child(self, root, object_name):
        for child in root.findChildren(QObject):
            if child.objectName() == object_name:
                return child
        self.fail(f"{object_name} not found")

    def _mouse_page(self):
        loader = self._child(self.host.window(), "mousePageLoader")
        item = loader.property("item")
        self.assertIsNotNone(item, "MousePage not loaded")
        return item

    def _hud_pill(self):
        for child in self.hud.window().findChildren(QObject):
            if child.objectName() == "hudPill":
                return child
        self.fail("hudPill not found")

    # -- tests --
    def test_hide_then_timer_releases_engine_and_all_qml_refs(self):
        window = self.host.show()
        _pump(100)
        self.assertIsNotNone(self.host.engine())
        self.assertTrue(self.host.is_visible())
        torn = []
        self.host.windowTornDown.connect(lambda: torn.append(True))

        self.host.hide()
        self.assertTrue(self.host.teardown_pending())
        _pump(250)  # 50 ms timer fires inside

        self.assertIsNone(self.host.engine())
        self.assertIsNone(self.host.window())
        self.assertEqual(torn, [True])
        self.assertEqual(self.host.teardown_count, 1)
        self.assertEqual(_qml_refs(self.host), [])
        del window

    def test_reensure_restores_current_page_and_is_visible(self):
        window = self.host.show()
        _pump(100)
        self.assertEqual(window.property("currentPage"), 0)
        self.ui_state.currentPage = 1
        _pump(20)
        self.assertEqual(window.property("currentPage"), 1)

        self.host.hide()
        _pump(250)
        self.assertIsNone(self.host.window())

        window = self.host.show()
        _pump(100)
        self.assertIsNotNone(self.host.engine())
        self.assertTrue(self.host.is_visible())
        self.assertEqual(window.property("currentPage"), 1)
        # launchHidden only applies to the process-start engine.
        self.assertFalse(bool(window.property("launchHidden") or False))

    def test_launch_hidden_first_engine_is_hidden_and_armed(self):
        host = main_qml.MainWindowHost(
            qml_path=str(QML_DIR / "Main.qml"),
            context_properties=self.context,
            launch_hidden=True,
            teardown_delay_ms=50,
        )
        try:
            window = host.ensure()
            self.assertFalse(host.is_visible())
            self.assertTrue(host.teardown_pending(), "hidden launch must arm the timer")
            _pump(250)
            self.assertIsNone(host.engine())
            window = host.ensure()
            _pump(50)
            self.assertTrue(host.is_visible(), "re-created engine must come up visible")
        finally:
            host.teardown(force=True)
            _pump(50)
            host.deleteLater()

    def test_teardown_skipped_while_modal_open(self):
        host = self._make_host(teardown_delay_ms=50)
        self._extra_hosts.append(host)
        host.show()
        _pump(100)
        dialog = None
        for child in host.window().findChildren(QObject):
            if child.objectName() == "aboutDialog":
                dialog = child
        self.assertIsNotNone(dialog)
        QMetaObject.invokeMethod(dialog, "open")
        _pump(50)
        self.assertTrue(bool(host.window().property("shortcutsBlocked")))

        host.hide()
        _pump(250)  # the 50 ms timer fires (several times) in here
        self.assertIsNotNone(host.engine(), "modal open must block teardown")
        self.assertTrue(host.teardown_pending(), "timer must be re-armed")

        # Direct call is refused too while the dialog is up.
        self.assertFalse(host.teardown())

        # Once the dialog is closed (after its exit transition) the guard
        # lifts and the next timer fire releases the engine.
        host._timer.stop()
        QMetaObject.invokeMethod(dialog, "close")
        for _ in range(20):  # Material Dialog exit transition
            _pump(50)
            if not bool(host.window().property("shortcutsBlocked")):
                break
        self.assertFalse(bool(host.window().property("shortcutsBlocked")))
        self.assertTrue(host.teardown())
        self.assertIsNone(host.engine())

    def test_teardown_skipped_by_python_guard(self):
        blocked = {"value": True}
        host = main_qml.MainWindowHost(
            qml_path=str(QML_DIR / "Main.qml"),
            context_properties=self.context,
            teardown_delay_ms=50,
            teardown_blocked=lambda: blocked["value"],
        )
        try:
            host.show()
            _pump(100)
            host.hide()
            _pump(250)
            self.assertIsNotNone(host.engine())
            blocked["value"] = False
            _pump(250)
            self.assertIsNone(host.engine())
        finally:
            host.teardown(force=True)
            host.deleteLater()

    def test_show_before_timer_cancels_teardown(self):
        self.host.show()
        _pump(100)
        self.host.hide()
        self.assertTrue(self.host.teardown_pending())
        self.host.show()
        self.assertFalse(self.host.teardown_pending())
        _pump(250)
        self.assertIsNotNone(self.host.engine())
        self.assertEqual(self.host.teardown_count, 0)

    def test_teardown_disabled_never_arms_timer(self):
        host = self._make_host(teardown_enabled=False)
        self._extra_hosts.append(host)
        host.show()
        _pump(100)
        host.hide()
        self.assertFalse(host.teardown_pending())
        _pump(250)
        self.assertIsNotNone(host.engine(), "hide-only mode keeps the engine")
        # An explicit call still works (process shutdown path).
        self.assertTrue(host.teardown(force=True))
        self.assertIsNone(host.engine())

    def test_teardown_env_switch(self):
        with patch.dict(os.environ, {main_qml.MainWindowHost.TEARDOWN_ENV: "1"}):
            self.assertTrue(main_qml.MainWindowHost.teardown_enabled_from_env())
        with patch.dict(os.environ, {main_qml.MainWindowHost.TEARDOWN_ENV: ""}):
            self.assertFalse(main_qml.MainWindowHost.teardown_enabled_from_env())
        env = {k: v for k, v in os.environ.items() if k != main_qml.MainWindowHost.TEARDOWN_ENV}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(main_qml.MainWindowHost.teardown_enabled_from_env())

    def test_teardown_while_visible_is_refused(self):
        self.host.show()
        _pump(100)
        self.assertFalse(self.host.teardown())
        self.assertIsNotNone(self.host.engine())

    def test_hud_engine_survives_main_teardown(self):
        self.host.show()
        _pump(100)
        self.host.hide()
        _pump(250)
        self.assertIsNone(self.host.engine())

        self.assertIsNotNone(self.hud.window())
        self.backend.gestureFeedback.emit("swipe left", "fired")
        _pump(300)  # 160 ms opacity Behavior
        self.assertGreater(self._hud_pill().property("opacity"), 0.9)

    def test_action_picker_loads_only_while_button_selected(self):
        self.host.show()
        _pump(100)
        page = self._mouse_page()
        picker = self._child(page, "pickerLoader")
        self.assertIsNone(picker.property("item"))
        QMetaObject.invokeMethod(page, "selectHScroll")
        _pump(50)
        self.assertEqual(page.property("selectedButton"), "hscroll_left")
        self.assertIsNotNone(picker.property("item"))
        self.assertGreater(picker.property("item").property("implicitHeight"), 0)

        # dismiss() clears the selection before hiding (all synchronous, so
        # check before the teardown timer can fire).
        QMetaObject.invokeMethod(self.host.window(), "dismiss")
        self.assertFalse(self.host.is_visible())
        self.assertEqual(page.property("selectedButton"), "")
        self.assertIsNone(picker.property("item"))

    def test_debug_card_loads_only_in_debug_mode(self):
        self.host.show()
        _pump(100)
        page = self._mouse_page()
        card = self._child(page, "debugCardLoader")
        self.assertIsNone(card.property("item"))
        with patch("ui.backend.save_config"):
            self.backend.setDebugMode(True)
            _pump(50)
            self.assertIsNotNone(card.property("item"))
            self.assertGreater(card.property("height"), 0)
            self.backend.setDebugMode(False)
            _pump(50)
        self.assertIsNone(card.property("item"))

    def test_hud_host_release_drops_engine(self):
        hud = main_qml.GestureHudHost(
            qml_path=str(QML_DIR / "GestureHud.qml"),
            context_properties=self.context,
        )
        self.assertIsNotNone(hud.window())
        hud.release()
        self.assertIsNone(hud.engine())
        self.assertIsNone(hud.window())
        self.assertEqual(_qml_refs(hud), [])
        _pump(50)

    def test_quit_filter_rebinds_across_teardown(self):
        filt = main_qml._MacOSQuitToTrayFilter(None)
        self.host.windowCreated.connect(filt.set_window)
        self.host.windowTornDown.connect(lambda: filt.set_window(None))
        window = self.host.show()
        _pump(50)
        self.assertIs(filt._root_window, window)
        self.host.hide()
        _pump(250)
        self.assertIsNone(filt._root_window)
        del window


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class SubprocessRunner(unittest.TestCase):
    """Re-run the live tests in a fresh interpreter when this process already
    owns a bare ``QCoreApplication`` (a ``QApplication`` cannot be created
    next to it)."""

    def test_live_suite_in_clean_process(self):
        if not _foreign_app_present():
            self.skipTest("live tests ran in-process")
        if os.environ.get(_LIVE_ENV):
            self.skipTest("already inside the subprocess")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", **{_LIVE_ENV: "1"})
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "-q",
             "tests.test_main_window_host.MainWindowHostLiveTests"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(
            proc.returncode, 0,
            "live MainWindowHost tests failed in subprocess:\n" + proc.stdout[-4000:] + proc.stderr[-4000:],
        )


if __name__ == "__main__":
    unittest.main()
