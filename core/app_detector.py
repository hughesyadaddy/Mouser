"""
Foreground application detector — watches the active window and fires
a callback when the foreground app changes.
Windows: GetForegroundWindow + QueryFullProcessImageNameW (with UWP resolution).
macOS:   NSWorkspaceDidActivateApplicationNotification carries the pid;
         pid -> identifier goes through core.macos_frontmost (ctypes AX /
         libproc / Info.plist), never NSRunningApplication.bundleIdentifier()
         or NSWorkspace.frontmostApplication() -- each of those opens a
         GamePolicy NSXPCConnection on macOS 26 that is never invalidated
         (Leak B, ~1.9 GB / 81 h at the old 300 ms poll).
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

    from core import macos_frontmost as _frontmost

    def _autoreleased(fn):
        """Run ``fn`` inside an NSAutoreleasePool.

        The detector thread is a plain Python thread with no pool of its
        own; the AX / libproc / plist reads behind _deliver and _idle_check
        produce autoreleased temporaries that would otherwise accumulate
        for the process lifetime (defense in depth: the frontmost helpers
        pool internally today, but a future PyObjC call here must not
        depend on that)."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _objc.autorelease_pool():
                return fn(*args, **kwargs)
        return wrapper

    _ACTIVATE_NOTIFICATION = "NSWorkspaceDidActivateApplicationNotification"
    _TERMINATE_NOTIFICATION = "NSWorkspaceDidTerminateApplicationNotification"
    _APPLICATION_KEY = "NSWorkspaceApplicationKey"

    def _identifier_for_pid(pid: int) -> str | None:
        """Bundle id (else executable basename) for *pid*; LRU-cached."""
        return _frontmost.bundle_id_for_pid(pid)

    def _pid_cached(pid: int) -> bool:
        return _frontmost.has_pid(pid)

    def _evict_pid(pid: int) -> None:
        _frontmost.evict(pid)

    def _read_foreground_pid() -> int | None:
        """Frontmost pid via AX / CGWindowList -- no LaunchServices."""
        return _frontmost.focused_pid()

    def get_foreground_exe() -> str | None:
        """Return a stable app identifier for the frontmost app on macOS."""
        try:
            pid = _read_foreground_pid()
            if pid is None:
                return None
            return _identifier_for_pid(pid)
        except Exception:
            return None

    def _notification_pid(notification) -> int | None:
        """Only ``processIdentifier()`` is read from the NSRunningApplication:
        it is a plain int already on the object, no XPC round trip."""
        info = notification.userInfo()
        app = info.get(_APPLICATION_KEY) if info is not None else None
        if app is None:
            return None
        pid = int(app.processIdentifier())
        return pid if pid > 0 else None

    def _install_activation_observer(handler, on_terminate=None):
        """Observe NSWorkspaceDidActivateApplicationNotification.

        ``handler(pid)`` is invoked from the notification's posting thread
        (the main run loop) with the activated app's pid; the detector
        thread resolves it. A second observer on
        NSWorkspaceDidTerminateApplicationNotification calls
        ``on_terminate(pid)`` (default: evict the pid from the identifier
        cache). Returns a zero-arg ``remove`` callable. Raises when the
        observers cannot be installed so the caller can fall back to
        polling.
        """
        if on_terminate is None:
            on_terminate = _evict_pid
        from AppKit import NSWorkspace

        center = NSWorkspace.sharedWorkspace().notificationCenter()

        def _on_activate(notification):
            # Autorelease pool per delivery: the userInfo dictionary proxy is
            # released as soon as the pid has been handed off.
            with _objc.autorelease_pool():
                try:
                    pid = _notification_pid(notification)
                except Exception:
                    pid = None
                if pid is not None:
                    handler(pid)

        def _on_terminate(notification):
            with _objc.autorelease_pool():
                try:
                    pid = _notification_pid(notification)
                except Exception:
                    pid = None
                if pid is not None:
                    on_terminate(pid)

        activate_token = center.addObserverForName_object_queue_usingBlock_(
            _ACTIVATE_NOTIFICATION, None, None, _on_activate,
        )
        try:
            terminate_token = center.addObserverForName_object_queue_usingBlock_(
                _TERMINATE_NOTIFICATION, None, None, _on_terminate,
            )
        except Exception:
            center.removeObserver_(activate_token)
            raise

        def _remove():
            center.removeObserver_(activate_token)
            center.removeObserver_(terminate_token)

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

# Poll period used only when the OS cannot push activation events to us, and
# the idle watchdog period on macOS (an AX pid compare, no LaunchServices).
FALLBACK_POLL_INTERVAL = 30.0

if sys.platform != "darwin":
    _identifier_for_pid = None
    _read_foreground_pid = None
    _pid_cached = None
    _evict_pid = None

    def _autoreleased(fn):
        """No-op off macOS (see the darwin branch)."""
        return fn

#: Queue wait per loop turn in _run_observer; the idle watchdog fires after
#: FALLBACK_POLL_INTERVAL of these without an event. Patched down in tests.
IDLE_TICK_S = 1.0


class AppDetector:
    """
    Watches the foreground application and calls ``on_change(exe_name: str)``
    from the detector thread when it changes.

    On macOS the change is pushed by
    ``NSWorkspaceDidActivateApplicationNotification`` as a pid; an idle
    watchdog re-reads the frontmost pid through AX every
    ``FALLBACK_POLL_INTERVAL`` and both paths funnel through ``_deliver``.
    If the observer cannot be installed the detector falls back to a slow
    safety poll. Other platforms poll every *interval* seconds as before.
    """

    def __init__(self, on_change, interval: float = 0.3):
        self._on_change = on_change
        self._interval = interval
        self._last_exe: str | None = None
        self._last_pid: int | None = None
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
            return install(self._events.put, self._on_terminated)
        except Exception as exc:
            print(
                f"[AppDetect] activation observer unavailable ({exc!r}); "
                f"falling back to a {FALLBACK_POLL_INTERVAL:.0f} s safety poll"
            )
            return None

    @staticmethod
    def _read_foreground():
        """Current foreground item: a pid on macOS, an exe path elsewhere."""
        try:
            if _read_foreground_pid is not None:
                return _read_foreground_pid()
            return get_foreground_exe()
        except Exception:
            return None

    def _on_terminated(self, pid: int):
        """Terminate notification: forget the pid everywhere. The kernel may
        hand the same pid to the next launched app, so the dedupe in
        _deliver must not swallow that app's first activation."""
        if _evict_pid is not None:
            _evict_pid(pid)
        if pid == self._last_pid:
            self._last_pid = None

    @_autoreleased
    def _deliver(self, item):
        """Funnel for every source (activation pid, idle AX pid, poll exe).

        A pid equal to the last delivered one is dropped before any
        resolution, so repeated activations of the same app cost nothing --
        but only while its cache entry still exists: an evicted pid (app
        terminated) is always re-resolved in case the pid was reused.
        """
        try:
            if item is None:
                return
            if isinstance(item, int):
                if item == self._last_pid and (
                    _pid_cached is None or _pid_cached(item)
                ):
                    return
                exe = _identifier_for_pid(item) if _identifier_for_pid else None
                if not exe:
                    return
                self._last_pid = item
            else:
                exe = item
            if exe and exe != self._last_exe:
                self._last_exe = exe
                self._on_change(exe)
        except Exception:
            pass

    @_autoreleased
    def _idle_check(self):
        """Watchdog tick: AX pid compare only; resolves nothing unless the
        pid actually changed (the observer missed a switch)."""
        self._deliver(self._read_foreground())

    def _run_observer(self):
        # One initial read so the profile matches the app that was already in
        # front when we started; everything after this is event-driven.
        self._idle_check()
        # Watchdog: if the observer installed but never fires (starved run
        # loop, coalesced switches, background apps), compare the AX pid
        # once every FALLBACK_POLL_INTERVAL of idle.
        idle = 0.0
        while not self._stop.is_set():
            try:
                item = self._events.get(timeout=IDLE_TICK_S)
            except queue.Empty:
                idle += IDLE_TICK_S
                if idle >= FALLBACK_POLL_INTERVAL:
                    idle = 0.0
                    self._idle_check()
                continue
            idle = 0.0
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
