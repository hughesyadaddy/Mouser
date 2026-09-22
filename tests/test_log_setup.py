import json
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import log_setup


class QtMessageBridgeTests(unittest.TestCase):
    """M5 audit R6: Qt/QML warnings are routed into the rotating Python log
    with per-message dedupe (first occurrence, then every 100th)."""

    def setUp(self):
        self.logger = logging.getLogger("test.qt.bridge")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.records = []

        class _Capture(logging.Handler):
            def emit(handler, record):
                self.records.append(record)

        self.handler = _Capture()
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_install_uses_fake_installer_without_importing_pyside(self):
        installed = []
        with patch.dict(sys.modules, {"PySide6": None, "PySide6.QtCore": None}):
            bridge = log_setup.install_qt_message_handler(
                install=installed.append, logger=self.logger)
        self.assertIsNotNone(bridge)
        self.assertEqual(installed, [bridge])

    def test_missing_pyside_is_tolerated(self):
        with patch.dict(sys.modules, {"PySide6": None, "PySide6.QtCore": None}):
            self.assertIsNone(log_setup.install_qt_message_handler(logger=self.logger))

    def test_module_import_does_not_import_pyside(self):
        import subprocess

        code = "import sys; import core.log_setup; print('PySide6' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_first_then_every_100th_with_count(self):
        bridge = log_setup.QtMessageBridge(logger=self.logger)
        ctx = type("Ctx", (), {"file": "qrc:/ui/qml/MousePage.qml", "line": 42})()
        for _ in range(250):
            bridge(1, ctx, "Binding loop detected for property \"width\"")
        self.assertEqual([r.levelno for r in self.records], [logging.WARNING] * 3)
        msgs = [r.getMessage() for r in self.records]
        self.assertIn("Binding loop detected", msgs[0])
        self.assertIn("MousePage.qml:42", msgs[0])
        self.assertNotIn("repeated", msgs[0])
        self.assertIn("[repeated x100]", msgs[1])
        self.assertIn("[repeated x200]", msgs[2])
        self.assertEqual(bridge.counts["Binding loop detected for property \"width\""], 250)

    def test_distinct_messages_dedupe_independently(self):
        bridge = log_setup.QtMessageBridge(logger=self.logger)
        for _ in range(5):
            bridge(1, None, "a")
            bridge(2, None, "b")
        self.assertEqual([r.getMessage() for r in self.records], ["[Qt] a", "[Qt] b"])
        self.assertEqual([r.levelno for r in self.records], [logging.WARNING, logging.ERROR])

    def test_levels_by_enum_name_and_value(self):
        class Mode:
            def __init__(self, name):
                self.name = name

        self.assertEqual(log_setup._qt_level(Mode("QtDebugMsg")), logging.DEBUG)
        self.assertEqual(log_setup._qt_level(Mode("QtInfoMsg")), logging.INFO)
        self.assertEqual(log_setup._qt_level(Mode("QtCriticalMsg")), logging.ERROR)
        self.assertEqual(log_setup._qt_level(Mode("QtFatalMsg")), logging.CRITICAL)
        self.assertEqual(log_setup._qt_level(4), logging.INFO)
        self.assertEqual(log_setup._qt_level(0), logging.DEBUG)
        self.assertEqual(log_setup._qt_level("junk"), logging.WARNING)

    def test_handler_never_raises_into_qt(self):
        bad = Mock(side_effect=RuntimeError("boom"))
        bridge = log_setup.QtMessageBridge(logger=Mock(log=bad))
        bridge(1, None, "x")   # must not raise

    def test_setup_logging_installs_bridge_when_requested(self):
        installed = []
        with (
            patch.object(log_setup, "_get_log_dir", return_value=tempfile.mkdtemp()),
            patch.object(log_setup, "install_qt_message_handler",
                         side_effect=lambda *a, **k: installed.append(1)),
        ):
            saved = (sys.stdout, logging.root.handlers[:], logging.root.level)
            logging.root.handlers.clear()
            try:
                log_setup.setup_logging()
                self.assertEqual(installed, [1])
                logging.root.handlers.clear()
                log_setup.setup_logging(qt_messages=False)
                self.assertEqual(installed, [1])
            finally:
                sys.stdout = saved[0]
                for h in logging.root.handlers:
                    h.close()
                logging.root.handlers[:] = saved[1]
                logging.root.setLevel(saved[2])


class GetLogDirTests(unittest.TestCase):
    def test_darwin_returns_library_logs_mouser(self):
        with patch.object(sys, "platform", "darwin"):
            result = log_setup._get_log_dir()
        self.assertTrue(result.endswith(os.path.join("Library", "Logs", "Mouser")))

    def test_linux_uses_xdg_state_home(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_STATE_HOME": "/custom/state"}, clear=False),
        ):
            result = log_setup._get_log_dir()
        self.assertEqual(result, os.path.join("/custom/state", "Mouser", "logs"))

    def test_linux_defaults_to_dot_local_state(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, env, clear=True),
        ):
            result = log_setup._get_log_dir()
        expected = os.path.join(
            os.path.expanduser("~"), ".local", "state", "Mouser", "logs"
        )
        self.assertEqual(result, expected)

    def test_windows_uses_appdata(self):
        fake_appdata = os.path.join("C:", "Users", "test", "AppData", "Roaming")
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"APPDATA": fake_appdata}, clear=False),
        ):
            result = log_setup._get_log_dir()
        self.assertEqual(result, os.path.join(fake_appdata, "Mouser", "logs"))


class _LoggingCase(unittest.TestCase):
    def setUp(self):
        self._orig_stdout = sys.stdout
        self._orig_handlers = logging.root.handlers[:]
        self._orig_level = logging.root.level
        logging.root.handlers.clear()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = self._tmp_dir.name

    def tearDown(self):
        # Restore stdout first so handler.close() can safely write errors to stderr
        sys.stdout = self._orig_stdout
        # Close handlers BEFORE temp dir cleanup to release file locks (Windows)
        for h in logging.root.handlers[:]:
            h.close()
        logging.root.handlers.clear()
        logging.root.handlers.extend(self._orig_handlers)
        logging.root.setLevel(self._orig_level)
        self._tmp_dir.cleanup()


class SetupLoggingTests(_LoggingCase):
    def test_creates_log_file_on_startup(self):
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            path = log_setup.setup_logging()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(path, os.path.join(self.tmp, "mouser.log"))

    def test_returns_empty_string_when_already_configured(self):
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
            result = log_setup.setup_logging()
        self.assertEqual(result, "")

    def test_redirects_stdout_to_stream_to_logger(self):
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
        self.assertIsInstance(sys.stdout, log_setup._StreamToLogger)

    def test_stderr_not_redirected(self):
        orig_stderr = sys.stderr
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
        self.assertIs(sys.stderr, orig_stderr)

    def test_print_output_written_to_log_file(self):
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
        print("[Test] hello from print")
        for h in logging.root.handlers:
            h.flush()
        log_path = os.path.join(self.tmp, "mouser.log")
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("[Test] hello from print", content)

    def test_log_entries_include_timestamp(self):
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
        print("[Test] timestamped line")
        for h in logging.root.handlers:
            h.flush()
        log_path = os.path.join(self.tmp, "mouser.log")
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        # Timestamp format: "YYYY-MM-DD HH:MM:SS"
        import re
        self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_graceful_fallback_on_oserror(self):
        with patch.object(log_setup, "_get_log_dir", return_value="/nonexistent/xyz"):
            with patch("core.log_setup.os.makedirs", side_effect=OSError("denied")):
                path = log_setup.setup_logging()  # must not raise
        self.assertEqual(path, "")

    def test_no_console_handler_when_frozen(self):
        with (
            patch.object(log_setup, "_get_log_dir", return_value=self.tmp),
            patch.object(sys, "frozen", True, create=True),
        ):
            log_setup.setup_logging()
        handler_types = [type(h) for h in logging.root.handlers]
        self.assertNotIn(logging.StreamHandler, handler_types)

    def test_rotating_handler_configured_with_correct_size(self):
        import logging.handlers
        with patch.object(log_setup, "_get_log_dir", return_value=self.tmp):
            log_setup.setup_logging()
        rotating = next(
            h for h in logging.root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        self.assertEqual(rotating.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(rotating.backupCount, 5)


class LogLevelTests(_LoggingCase):
    def _setup(self, *, config=None, env=None):
        env_vars = {"MOUSER_LOG_LEVEL": env} if env else {}
        with tempfile.TemporaryDirectory() as cfg_dir:
            cfg_path = os.path.join(cfg_dir, "config.json")
            if config is not None:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(config, f)
            with (
                patch.object(log_setup, "_get_log_dir", return_value=self.tmp),
                patch("core.config.CONFIG_FILE", cfg_path),
                patch.dict(os.environ, env_vars, clear=False),
            ):
                if not env:
                    os.environ.pop("MOUSER_LOG_LEVEL", None)
                log_setup.setup_logging()

    def test_default_level_is_info_and_debug_is_gated(self):
        self._setup(config={"settings": {}})
        self.assertEqual(logging.root.level, logging.INFO)
        self.assertFalse(log_setup.debug_enabled())

    def test_missing_config_defaults_to_info(self):
        self._setup(config=None)
        self.assertEqual(logging.root.level, logging.INFO)

    def test_config_log_level_debug_enables_per_event_lines(self):
        self._setup(config={"settings": {"log_level": "debug"}})
        self.assertEqual(logging.root.level, logging.DEBUG)
        self.assertTrue(log_setup.debug_enabled())

    def test_env_overrides_config(self):
        self._setup(config={"settings": {"log_level": "INFO"}}, env="DEBUG")
        self.assertEqual(logging.root.level, logging.DEBUG)

    def test_levels_above_info_are_clamped_so_prints_still_log(self):
        self._setup(config={"settings": {"log_level": "ERROR"}})
        self.assertEqual(logging.root.level, logging.INFO)

    def test_unknown_level_name_falls_back_to_info(self):
        self._setup(config={"settings": {"log_level": "LOUD"}})
        self.assertEqual(logging.root.level, logging.INFO)

    def test_prints_still_reach_the_file_at_info(self):
        self._setup(config={"settings": {}})
        print("[Test] info-level print")
        log_setup.log_debug("[Test] debug-only line")
        for h in logging.root.handlers:
            h.flush()
        with open(os.path.join(self.tmp, "mouser.log"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("[Test] info-level print", content)
        self.assertNotIn("[Test] debug-only line", content)


class StreamToLoggerTests(unittest.TestCase):
    def _make_stream(self, level=logging.INFO):
        logger = logging.getLogger(f"test_stream_{id(self)}")
        logger.handlers.clear()
        logger.propagate = False
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger.addHandler(Capture())
        logger.setLevel(logging.DEBUG)
        return log_setup._StreamToLogger(logger, level), records

    def test_complete_line_is_logged_immediately(self):
        stream, records = self._make_stream()
        stream.write("[Engine] DPI changed\n")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].getMessage(), "[Engine] DPI changed")

    def test_partial_line_is_buffered_until_newline(self):
        stream, records = self._make_stream()
        stream.write("[Engine]")
        self.assertEqual(len(records), 0)
        stream.write(" DPI changed\n")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].getMessage(), "[Engine] DPI changed")

    def test_multiple_lines_in_single_write(self):
        stream, records = self._make_stream()
        stream.write("line one\nline two\nline three\n")
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].getMessage(), "line one")
        self.assertEqual(records[1].getMessage(), "line two")
        self.assertEqual(records[2].getMessage(), "line three")

    def test_flush_emits_partial_buffer(self):
        stream, records = self._make_stream()
        stream.write("no newline")
        self.assertEqual(len(records), 0)
        stream.flush()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].getMessage(), "no newline")

    def test_blank_lines_not_logged(self):
        stream, records = self._make_stream()
        stream.write("\n\n\n")
        self.assertEqual(len(records), 0)

    def test_write_returns_length_of_message(self):
        stream, _ = self._make_stream()
        msg = "hello\n"
        result = stream.write(msg)
        self.assertEqual(result, len(msg))

    def test_isatty_returns_false(self):
        stream, _ = self._make_stream()
        self.assertFalse(stream.isatty())

    def test_encoding_is_utf8(self):
        stream, _ = self._make_stream()
        self.assertEqual(stream.encoding, "utf-8")


if __name__ == "__main__":
    unittest.main()
