"""
Foreground application detector — watches the active window and fires
a callback when the foreground app changes.
Windows: GetForegroundWindow + QueryFullProcessImageNameW (with UWP resolution).
macOS:   NSWorkspaceDidActivateApplicationNotification (initial read via
         NSWorkspace.sharedWorkspace().frontmostApplication()).
"""

import os
import queue
import sys
import threading


# ==================================================================
# Platform-specific get_foreground_exe()
# ==================================================================

# Platforms that can push foreground changes (macOS) override this with a
# function ``install(handler) -> remove``; ``None`` means poll.
_install_activation_observer = None

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAX_PATH = 260

    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD

    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD,
        ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND

    user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
    user32.EnumChildWindows.restype = wt.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    def _get_window_title(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _path_from_pid(pid: int) -> str | None:
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return None
        try:
            buf = ctypes.create_unicode_buffer(MAX_PATH)
            size = wt.DWORD(MAX_PATH)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(hproc)
        return None

    def _resolve_uwp_child(hwnd) -> str | None:
        host_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(host_pid))
        result = [None]

        def _enum_cb(child_hwnd, _lparam):
            child_pid = wt.DWORD()
            user32.GetWindowThreadProcessId(child_hwnd, ctypes.byref(child_pid))
            if child_pid.value != host_pid.value:
                exe_path = _path_from_pid(child_pid.value)
                if exe_path and os.path.basename(exe_path).lower() != "applicationframehost.exe":
                    result[0] = exe_path
                    return False
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(_enum_cb), 0)
        return result[0]

    # Window classes that belong to genuine explorer.exe usage
    _EXPLORER_CLASSES = frozenset({
        "CabinetWClass",           # File Explorer windows
        "Shell_TrayWnd",           # Taskbar
        "Shell_SecondaryTrayWnd",  # Taskbar on secondary monitors
        "Progman",                 # Desktop
        "WorkerW",                 # Desktop worker
    })

    def _get_window_class(hwnd) -> str:
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        return cls.value

    def _find_uwp_app_global() -> str | None:
        """Enumerate all top-level windows to find a UWP app behind an overlay."""
        result = [None]

        def _enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            exe_path = _path_from_pid(pid.value)
            if exe_path and os.path.basename(exe_path).lower() == "applicationframehost.exe":
                real = _resolve_uwp_child(hwnd)
                if real:
                    result[0] = real
                    return False
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return result[0]

    def get_foreground_exe() -> str | None:
        """Return the foreground app path on Windows, or None."""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return None
        exe_path = _path_from_pid(pid.value)
        if not exe_path:
            return None
        exe_lower = os.path.basename(exe_path).lower()
        if exe_lower == "applicationframehost.exe":
            real = _resolve_uwp_child(hwnd)
            # If we can't resolve the real app (e.g. fullscreen UWP),
            # return None so the detector keeps the last known profile.
            return real
        if exe_lower == "explorer.exe":
            wc = _get_window_class(hwnd)
            if wc not in _EXPLORER_CLASSES:
                title = _get_window_title(hwnd)
                print(f"[AppDetect] FG: explorer.exe class={wc} title='{title}'")
                real = _resolve_uwp_child(hwnd)
                if real:
                    return real
                real = _find_uwp_app_global()
                return real  # None keeps last profile
        return exe_path

elif sys.platform == "darwin":
    import functools

    try:
        import objc as _objc
    except ImportError as exc:
        raise ImportError(
            "PyObjC is required on macOS. Run "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    def _autoreleased(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _objc.autorelease_pool():
                return fn(*args, **kwargs)
        return wrapper

    _ACTIVATE_NOTIFICATION = "NSWorkspaceDidActivateApplicationNotification"
    _APPLICATION_KEY = "NSWorkspaceApplicationKey"

    def _app_identifier(app) -> str | None:
        """Stable identifier for an NSRunningApplication (bundle id, exe name, or name)."""
        if app is None:
            return None
        ident = app.bundleIdentifier()
        if ident:
            return ident
        url = app.executableURL()
        if url:
            return os.path.basename(url.path())
        return app.localizedName()

    @_autoreleased
    def get_foreground_exe() -> str | None:
        """Return a stable app identifier for the frontmost app on macOS."""
        try:
            from AppKit import NSWorkspace
            return _app_identifier(NSWorkspace.sharedWorkspace().frontmostApplication())
        except Exception:
            return None

    def _install_activation_observer(handler):
        """Observe NSWorkspaceDidActivateApplicationNotification.

        ``handler(ident)`` is invoked from the notification's posting thread
        (the main run loop) with the same identifier ``get_foreground_exe``
        would return. Returns a zero-arg ``remove`` callable. Raises when the
        observer cannot be installed so the caller can fall back to polling.
        """
        from AppKit import NSWorkspace

        center = NSWorkspace.sharedWorkspace().notificationCenter()

        def _on_activate(notification):
            # Autorelease pool per delivery: the NSRunningApplication/NSURL
            # proxies created while extracting the identifier are released
            # as soon as the string has been handed off.
            with _objc.autorelease_pool():
                ident = None
                try:
                    info = notification.userInfo()
                    app = info.get(_APPLICATION_KEY) if info is not None else None
                    ident = _app_identifier(app)
                except Exception:
                    ident = None
                if ident:
                    handler(ident)

        token = center.addObserverForName_object_queue_usingBlock_(
            _ACTIVATE_NOTIFICATION, None, None, _on_activate,
        )

        def _remove():
            center.removeObserver_(token)

        return _remove

elif sys.platform == "linux":
    import subprocess as _subprocess

    _WAYLAND = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    _KDE = "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()

    def _pid_to_exe(pid: int) -> str | None:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None

    def _get_foreground_xdotool() -> str | None:
        """X11: use xdotool."""
        try:
            result = _subprocess.run(
                ["xdotool", "getactivewindow", "getwindowpid"],
                capture_output=True, text=True, timeout=1,
            )
            if result.returncode == 0 and result.stdout.strip():
                return _pid_to_exe(int(result.stdout.strip()))
        except (FileNotFoundError, ValueError, OSError, _subprocess.TimeoutExpired):
            pass
        return None

    def _get_foreground_kdotool() -> str | None:
        """KDE Wayland: use kdotool."""
        try:
            result = _subprocess.run(
                ["kdotool", "getactivewindow", "getwindowpid"],
                capture_output=True, text=True, timeout=1,
            )
            if result.returncode == 0 and result.stdout.strip():
                return _pid_to_exe(int(result.stdout.strip()))
        except (FileNotFoundError, ValueError, OSError, _subprocess.TimeoutExpired):
            pass
        return None

    def get_foreground_exe() -> str | None:
        """Return the foreground app executable path on Linux."""
        if _WAYLAND:
            if _KDE:
                exe = _get_foreground_kdotool()
                if exe:
                    return exe
                # Fall back to xdotool so XWayland apps still work when
                # kdotool is unavailable or cannot resolve the active window.
                return _get_foreground_xdotool()
            # GNOME / other Wayland compositors: not yet supported
            return None
        return _get_foreground_xdotool()

else:
    def get_foreground_exe() -> str | None:
        return None


_STOP_SENTINEL = object()

# Poll period used only when the OS cannot push activation events to us.
FALLBACK_POLL_INTERVAL = 5.0


class AppDetector:
    """
    Watches the foreground application and calls ``on_change(exe_name: str)``
    from the detector thread when it changes.

    On macOS the change is pushed by
    ``NSWorkspaceDidActivateApplicationNotification`` (one initial
    ``frontmostApplication()`` read, then no polling). If that observer cannot
    be installed the detector falls back to a slow safety poll. Other
    platforms poll every *interval* seconds as before.
    """

    def __init__(self, on_change, interval: float = 0.3):
        self._on_change = on_change
        self._interval = interval
        self._last_exe: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._events: queue.Queue = queue.Queue()
        self._remove_observer = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._events = queue.Queue()
        self._remove_observer = self._try_install_observer()
        target = self._run_observer if self._remove_observer else self._poll
        self._thread = threading.Thread(target=target, daemon=True, name="AppDetector")
        self._thread.start()

    def stop(self):
        self._stop.set()
        remove = self._remove_observer
        self._remove_observer = None
        if remove is not None:
            try:
                remove()
            except Exception as exc:
                print(f"[AppDetect] failed to remove activation observer: {exc}")
        self._events.put(_STOP_SENTINEL)
        if self._thread:
            self._thread.join(timeout=2)

    # ------------------------------------------------------------------
    def _try_install_observer(self):
        install = _install_activation_observer
        if install is None:
            return None
        try:
            return install(self._events.put)
        except Exception as exc:
            print(
                f"[AppDetect] activation observer unavailable ({exc!r}); "
                f"falling back to a {FALLBACK_POLL_INTERVAL:.0f} s safety poll"
            )
            return None

    @staticmethod
    def _read_foreground() -> str | None:
        try:
            return get_foreground_exe()
        except Exception:
            return None

    def _deliver(self, exe: str | None):
        try:
            if exe and exe != self._last_exe:
                self._last_exe = exe
                self._on_change(exe)
        except Exception:
            pass

    def _run_observer(self):
        # One initial read so the profile matches the app that was already in
        # front when we started; everything after this is event-driven.
        self._deliver(self._read_foreground())
        while not self._stop.is_set():
            try:
                item = self._events.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is _STOP_SENTINEL:
                break
            self._deliver(item)

    def _poll(self):
        interval = self._interval
        if _install_activation_observer is not None:
            # Observer platform whose observer failed to install: poll slowly.
            interval = max(interval, FALLBACK_POLL_INTERVAL)
        while not self._stop.is_set():
            self._deliver(self._read_foreground())
            self._stop.wait(interval)
