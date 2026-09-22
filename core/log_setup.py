"""
log_setup.py — Redirect all print() output to a rotating log file.

Call setup_logging() once, early in main_qml.py, before Qt and core imports.
"""
import collections
import io
import json
import logging
import logging.handlers
import os
import sys
import threading

DEFAULT_LOG_LEVEL = "INFO"

#: After the first occurrence, a repeated Qt/QML message is logged again
#: only every this many repeats (with its running count).
QT_MESSAGE_REPEAT_EVERY = 100
#: Distinct dedupe keys kept; the oldest is evicted past this.
QT_MESSAGE_MAX_KEYS = 1000
#: Without a source location the key is this prefix of the message text.
QT_MESSAGE_KEY_CHARS = 120

# QtMsgType -> logging level, by enum name (PySide6 exposes an IntEnum)
# and by raw value (Qt: Debug=0, Warning=1, Critical=2, Fatal=3, Info=4).
_QT_LEVEL_BY_NAME = {
    "QtDebugMsg": logging.DEBUG,
    "QtInfoMsg": logging.INFO,
    "QtWarningMsg": logging.WARNING,
    "QtCriticalMsg": logging.ERROR,
    "QtFatalMsg": logging.CRITICAL,
}
_QT_LEVEL_BY_VALUE = {
    0: logging.DEBUG, 1: logging.WARNING, 2: logging.ERROR,
    3: logging.CRITICAL, 4: logging.INFO,
}


def _get_log_dir() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Logs", "Mouser")
    elif sys.platform == "linux":
        xdg_state = os.environ.get(
            "XDG_STATE_HOME",
            os.path.join(os.path.expanduser("~"), ".local", "state"),
        )
        return os.path.join(xdg_state, "Mouser", "logs")
    else:  # Windows
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(appdata, "Mouser", "logs")


def _configured_log_level() -> int:
    """``settings.log_level`` from config.json (``MOUSER_LOG_LEVEL`` overrides),
    default INFO. Read raw so this stays usable before core.config loads."""
    name = os.environ.get("MOUSER_LOG_LEVEL", "")
    if not name:
        try:
            from core.config import CONFIG_FILE

            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                name = (json.load(f).get("settings") or {}).get("log_level", "")
        except Exception:  # noqa: BLE001 - missing/corrupt config = default level
            name = ""
    level = logging.getLevelName(str(name or DEFAULT_LOG_LEVEL).upper())
    if not isinstance(level, int):
        return logging.INFO
    # print() lines are INFO records; anything quieter would blank the log.
    return min(level, logging.INFO)


def debug_enabled() -> bool:
    """Gate for per-event prints (one per button/wheel report)."""
    return logging.getLogger().isEnabledFor(logging.DEBUG)


def log_debug(msg: str) -> None:
    logging.getLogger().debug(msg)


class _StreamToLogger:
    """Forward writes to a Logger. Thread-safe via threading.local buffer."""

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self._logger = logger
        self._level = level
        self._local = threading.local()

    def write(self, msg: str) -> int:
        if not hasattr(self._local, "buf"):
            self._local.buf = ""
        self._local.buf += msg
        while "\n" in self._local.buf:
            line, self._local.buf = self._local.buf.split("\n", 1)
            if line:
                self._logger.log(self._level, line)
        return len(msg)

    def flush(self) -> None:
        if hasattr(self._local, "buf") and self._local.buf:
            self._logger.log(self._level, self._local.buf)
            self._local.buf = ""

    def fileno(self):
        raise io.UnsupportedOperation("fileno")

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def isatty(self):
        return False


def _qt_level(mode) -> int:
    name = getattr(mode, "name", None)
    if name in _QT_LEVEL_BY_NAME:
        return _QT_LEVEL_BY_NAME[name]
    try:
        return _QT_LEVEL_BY_VALUE.get(int(mode), logging.WARNING)
    except (TypeError, ValueError):
        return logging.WARNING


class QtMessageBridge:
    """``qInstallMessageHandler`` target: routes Qt/QML messages into the
    Python logger with per-message dedupe.

    Without this, QML warnings (binding loops, ``TypeError: Cannot read
    property ... of null`` on teardown, etc.) go to stderr -> launchd's
    unrotated err.log, and a warning that fires per frame writes without
    bound. Each distinct message text is logged on its first occurrence
    and then every ``repeat_every``-th occurrence with its running count.
    """

    def __init__(self, logger: logging.Logger | None = None,
                 repeat_every: int = QT_MESSAGE_REPEAT_EVERY,
                 max_keys: int = QT_MESSAGE_MAX_KEYS):
        self._logger = logger or logging.getLogger("qt")
        self._repeat_every = max(1, int(repeat_every))
        self._max_keys = max(1, int(max_keys))
        # Bounded: keyed by (file, line) when Qt gives a source location,
        # else by a prefix of the text (messages that embed a changing
        # value would otherwise grow the dict without limit).
        self._counts: "collections.OrderedDict[tuple, int]" = collections.OrderedDict()
        self._lock = threading.Lock()

    @property
    def counts(self) -> dict:
        with self._lock:
            return dict(self._counts)

    @staticmethod
    def _key(context, text):
        file = getattr(context, "file", None)
        if file:
            return (str(file), int(getattr(context, "line", 0) or 0))
        return (text[:QT_MESSAGE_KEY_CHARS],)

    def __call__(self, mode, context, message) -> None:
        try:
            text = str(message)
            key = self._key(context, text)
            with self._lock:
                count = self._counts.pop(key, 0) + 1
                self._counts[key] = count           # most recent last
                while len(self._counts) > self._max_keys:
                    self._counts.popitem(last=False)
            if count != 1 and count % self._repeat_every != 0:
                return
            where = ""
            file = getattr(context, "file", None)
            if file:
                where = f" ({file}:{getattr(context, 'line', 0)})"
            suffix = f" [repeated x{count}]" if count > 1 else ""
            self._logger.log(_qt_level(mode), f"[Qt] {text}{where}{suffix}")
        except Exception:  # noqa: BLE001 - never raise into Qt
            pass


_QT_BRIDGE: QtMessageBridge | None = None


def install_qt_message_handler(install=None, logger=None):
    """Install a :class:`QtMessageBridge` via ``qInstallMessageHandler``.

    ``install`` overrides the installer (tests pass a fake); by default
    PySide6 is imported here -- lazily, never at module import, so the
    non-Qt entry points that use this module stay Qt-free. Returns the
    bridge, or None when PySide6 is unavailable.
    """
    global _QT_BRIDGE
    if install is None:
        try:
            from PySide6.QtCore import qInstallMessageHandler as install
        except Exception:  # noqa: BLE001 - ImportError or a broken Qt install
            return None
    bridge = QtMessageBridge(logger=logger)
    try:
        install(bridge)
    except Exception:  # noqa: BLE001 - never fail startup over the bridge
        return None
    _QT_BRIDGE = bridge
    return bridge


def setup_logging(qt_messages: bool = True) -> str:
    """
    Configure rotating file log and redirect stdout to it.
    Returns the log file path. Idempotent (safe to call multiple times).

    ``qt_messages`` also routes Qt/QML warnings into the same log (see
    :class:`QtMessageBridge`); PySide6 is imported lazily for that and its
    absence is tolerated.

    Only sys.stdout is redirected (all app output uses print()). sys.stderr
    is left untouched to avoid a recursion: logging handler errors call
    handleError() which writes to sys.stderr — redirecting it through the
    logger would create an infinite loop.
    """
    root = logging.getLogger()
    if root.handlers:
        return ""  # already configured

    log_dir = _get_log_dir()
    fmt = logging.Formatter(fmt="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    log_path = ""
    try:
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        log_path = os.path.join(log_dir, "mouser.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,              # 25 MB total ceiling
            encoding="utf-8",
            delay=False,                # create file immediately on startup
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        log_path = ""
        # Fall back to console-only — app must not crash due to logging failure
        print(f"[Logging] Cannot create log dir {log_dir}: {exc}", file=sys.__stderr__)

    # Terminal output: only when NOT running as a frozen bundle.
    # getattr(sys, "frozen", False) is set by PyInstaller (same pattern used
    # in main_qml.py for ROOT path resolution). When frozen with console=False,
    # sys.stdout is /dev/null, so we skip the StreamHandler.
    if not getattr(sys, "frozen", False):
        console_handler = logging.StreamHandler(sys.__stdout__)
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    root.setLevel(_configured_log_level())

    # Redirect stdout — must come AFTER StreamHandler setup above.
    # StreamHandler uses sys.__stdout__ (original), not sys.stdout, so
    # redirecting sys.stdout here does not create a circular loop.
    sys.stdout = _StreamToLogger(root, logging.INFO)

    if qt_messages:
        install_qt_message_handler()

    return log_path
