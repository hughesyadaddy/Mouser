"""Process-level single-instance lock and the ``--ctl`` lifecycle verbs.

Everything in this module is Qt-free on purpose: it runs before PySide6 is
imported so that a second launch (installer bootstrap + ``open``, launchd
RunAtLoad + a user double-click, Deskflow's ``Start-Process``) never gets far
enough to build a second tray icon.

Two mechanisms:

* **The lock** (:func:`acquire`) -- an OS-level exclusive handle that lives for
  the whole process: ``fcntl.flock`` on ``mouser.lock`` in the config dir on
  macOS/Linux, a named ``Local\\`` mutex on Windows.  Losing the race is
  detected synchronously, so the old "probe the socket for 500 ms and then
  unconditionally unlink it" window no longer exists.
* **The raise channel** -- the ``QLocalServer`` the winner listens on.  It is
  raise-only: a loser (or ``--ctl``) connects, sends a tiny message, and exits.
  The client side here is raw ``socket`` / named-pipe I/O so it works without
  a ``QCoreApplication``.

``--ctl status|stop|start|restart|assert-single`` is dispatched from
``main_qml.py`` before any Qt import and implemented by :func:`ctl_main`.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from typing import Callable, Iterable

APP_BUNDLE_ID = "io.github.hughesyadaddy.mouser"
LOCK_FILENAME = "mouser.lock"
SOCKET_FILENAME = "mouser.sock"
WINDOWS_MUTEX_NAME = "Local\\MouserSingleInstance"
WINDOWS_START_TASK_NAME = "MouserCtlStart"

RAISE_MSG_SHOW = b"show"
RAISE_MSG_QUIT = json.dumps({"cmd": "quit"}).encode("utf-8")

GRACEFUL_STOP_TIMEOUT_S = 15.0
KILL_WAIT_TIMEOUT_S = 5.0
ASSERT_SINGLE_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.25

EXIT_NONE = 0
EXIT_ONE = 1
EXIT_MANY = 2


def _log(msg: str) -> None:
    print(f"[Mouser] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def state_dir() -> str:
    """Per-user directory holding the lock and the raise socket.

    Mirrors ``core.config.CONFIG_DIR`` without importing it (that module pulls
    in the app catalog, which is too heavy for a pre-Qt hot path).
    """
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "Mouser"
        )
    if sys.platform == "linux":
        return os.path.join(
            os.environ.get(
                "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
            ),
            "Mouser",
        )
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Mouser")


def lock_path() -> str:
    return os.path.join(state_dir(), LOCK_FILENAME)


def _server_name_digest() -> str:
    raw = f"{getpass.getuser()}\0{sys.platform}"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"mouser_instance_{digest}"


def server_address() -> str:
    """Name handed to ``QLocalServer.listen`` / ``QLocalSocket.connectToServer``.

    Unix: an absolute socket path under :func:`state_dir` so the address is
    identical no matter how the process was launched (launchd, terminal,
    ``open`` -- each of which can see a different ``$TMPDIR``).  Falls back to
    ``/tmp`` when the path would overflow ``sun_path`` (104 bytes on macOS).
    Windows: a per-user pipe name.
    """
    if sys.platform == "win32":
        return _server_name_digest()
    candidate = os.path.join(state_dir(), SOCKET_FILENAME)
    if len(candidate.encode("utf-8")) > 100:
        candidate = f"/tmp/{_server_name_digest()}.sock"
    return candidate


def _windows_pipe_path(name: str) -> str:
    return f"\\\\.\\pipe\\{name}"


# ─────────────────────────────────────────────────────────────────────────────
# Lock
# ─────────────────────────────────────────────────────────────────────────────


class Lock:
    """Handle for the held single-instance lock.  Keep a reference for life."""

    def __init__(self, path: str, handle) -> None:
        self.path = path
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(handle)
            return
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(handle)
        except OSError:
            pass

    @property
    def held(self) -> bool:
        return self._handle is not None


def _acquire_posix(path: str) -> Lock | None:
    import fcntl

    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass
    return Lock(path, fd)


def _acquire_windows(name: str) -> Lock | None:
    import ctypes
    from ctypes import wintypes

    # use_last_error=True snapshots GetLastError right after the FFI call, so
    # ERROR_ALREADY_EXISTS cannot be clobbered by interpreter housekeeping.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        return None
    ERROR_ALREADY_EXISTS = 183
    if error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return Lock(name, handle)


def acquire(path: str | None = None) -> Lock | None:
    """Try to become *the* Mouser process.  Returns ``None`` if another holds it."""
    if sys.platform == "win32":
        return _acquire_windows(WINDOWS_MUTEX_NAME if path is None else path)
    return _acquire_posix(path or lock_path())


# ─────────────────────────────────────────────────────────────────────────────
# Windows console-session gate
# ─────────────────────────────────────────────────────────────────────────────


def windows_session_ids(kernel32=None, pid: int | None = None) -> tuple[int, int]:
    """Return ``(session_of_this_process, active_console_session)``."""
    import ctypes
    from ctypes import wintypes

    if kernel32 is None:
        kernel32 = ctypes.windll.kernel32
    session = wintypes.DWORD(0)
    kernel32.ProcessIdToSessionId(
        wintypes.DWORD(os.getpid() if pid is None else pid), ctypes.byref(session)
    )
    console = int(kernel32.WTSGetActiveConsoleSessionId())
    return int(session.value), console


def is_interactive_console_session(kernel32=None) -> bool:
    """True when this process runs in the physical console session.

    A Mouser started from an SSH session (session 0 / a service session) can
    never show a tray icon or hook the desktop, but it *would* win the lock and
    block the real one -- so it must refuse to run.
    """
    if sys.platform != "win32":
        return True
    try:
        session, console = windows_session_ids(kernel32)
    except Exception as exc:  # pragma: no cover - ctypes surface
        _log(f"session check failed ({exc}); assuming interactive")
        return True
    if console == 0xFFFFFFFF:  # no console session attached (logon screen)
        return False
    return session == console


# ─────────────────────────────────────────────────────────────────────────────
# Raise channel (client side, Qt-free)
# ─────────────────────────────────────────────────────────────────────────────


def send_raise_message(payload: bytes, timeout: float = 2.0, address: str | None = None) -> bool:
    """Deliver ``payload`` to the running instance's QLocalServer.  True on success."""
    address = address or server_address()
    if sys.platform == "win32":
        return _send_windows_pipe(_windows_pipe_path(address), payload, timeout)
    return _send_unix_socket(address, payload, timeout)


def _send_unix_socket(path: str, payload: bytes, timeout: float) -> bool:
    import socket

    if not os.path.exists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(payload)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _send_windows_pipe(path: str, payload: bytes, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(path, "r+b", buffering=0) as pipe:
                pipe.write(payload)
                pipe.flush()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            # ERROR_PIPE_BUSY: the server is between accepts.  Retry briefly.
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def notify_running_instance(timeout: float = 2.0) -> bool:
    return send_raise_message(RAISE_MSG_SHOW, timeout)


def request_quit(timeout: float = 2.0) -> bool:
    return send_raise_message(RAISE_MSG_QUIT, timeout)


def parse_raise_message(data: bytes) -> str:
    """Classify a raise-channel payload as ``"quit"`` or ``"show"``."""
    text = (data or b"").strip()
    if not text:
        return "show"
    try:
        obj = json.loads(text.decode("utf-8", errors="replace"))
    except ValueError:
        return "show"
    if isinstance(obj, dict) and obj.get("cmd") == "quit":
        return "quit"
    return "show"


# ─────────────────────────────────────────────────────────────────────────────
# Startup gate used by main_qml.main()
# ─────────────────────────────────────────────────────────────────────────────


def acquire_or_exit(exit_fn: Callable[[int], None] = os._exit) -> Lock | None:
    """Take the lock or hand off to the running instance and terminate.

    Called at the top of ``main()`` before ``QApplication``.  Returns the held
    :class:`Lock` on success.  On Windows a process outside the active console
    session is refused outright (log + exit 0) -- see
    :func:`is_interactive_console_session`.
    """
    if not is_interactive_console_session():
        _log("refusing to start outside the active console session (exit 0)")
        exit_fn(0)
        return None
    lock = acquire()
    if lock is not None:
        return lock
    raised = notify_running_instance(timeout=2.0)
    _log(
        "another instance holds the lock; "
        + ("raised it and exiting" if raised else "raise ping failed; exiting")
    )
    exit_fn(0)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Process discovery (by image path)
# ─────────────────────────────────────────────────────────────────────────────


def default_executable() -> str:
    """Image path the ctl verbs act on.

    Frozen: this very executable.  Source checkout: the installed build
    (``MOUSER_INSTALL_DIR`` overrides the default location).
    """
    override = os.environ.get("MOUSER_CTL_EXE", "").strip()
    if override:
        return override
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    install_dir = os.environ.get("MOUSER_INSTALL_DIR", "").strip()
    if sys.platform == "darwin":
        root = os.path.expanduser(install_dir) if install_dir else "/Applications"
        return os.path.join(root, "Mouser.app", "Contents", "MacOS", "Mouser")
    if sys.platform == "win32":
        if install_dir:
            root = os.path.expanduser(install_dir)
        else:
            root = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"), "Mouser"
            )
        return os.path.join(root, "Mouser.exe")
    return os.path.abspath(sys.executable)


def parse_ps_output(text: str, exe_path: str, own_pid: int | None = None) -> list[tuple[int, int]]:
    """Parse ``ps -axo pid=,etime=,command=`` into ``[(pid, elapsed_s), ...]``
    for rows whose command starts with ``exe_path``.  Newest first.

    ``etime`` is ``[[dd-]hh:]mm:ss`` (Darwin ``ps`` has no ``etimes``)."""
    exe_norm = os.path.normcase(exe_path)
    rows: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            elapsed = parse_etime(parts[1])
        except ValueError:
            continue
        if own_pid is not None and pid == own_pid:
            continue
        command = parts[2]
        cmd_norm = os.path.normcase(command)
        if cmd_norm == exe_norm or cmd_norm.startswith(exe_norm + " "):
            rows.append((pid, elapsed))
    rows.sort(key=lambda item: item[1])
    return rows


def parse_etime(text: str) -> int:
    """``[[dd-]hh:]mm:ss`` (or a bare integer) -> seconds."""
    if text.isdigit():
        return int(text)
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    fields = [int(f) for f in text.split(":")]
    if len(fields) == 2:
        hours, (minutes, seconds) = 0, fields
    elif len(fields) == 3:
        hours, minutes, seconds = fields
    else:
        raise ValueError(text)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _windows_path_key(path: str) -> str:
    """Case-insensitive, separator-normalised key (Windows paths, any host)."""
    return os.path.normpath(path).replace("/", "\\").lower()


def parse_cim_output(text: str, exe_path: str, own_pid: int | None = None) -> list[tuple[int, int]]:
    """Parse the JSON emitted by :func:`_windows_list_command`."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    exe_norm = _windows_path_key(exe_path)
    rows: list[tuple[int, int]] = []
    for item in data or []:
        try:
            pid = int(item.get("ProcessId"))
            elapsed = int(item.get("Elapsed", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if own_pid is not None and pid == own_pid:
            continue
        path = item.get("ExecutablePath") or ""
        if _windows_path_key(path) == exe_norm:
            rows.append((pid, elapsed))
    rows.sort(key=lambda item: item[1])
    return rows


def _windows_list_command() -> list[str]:
    ps = (
        "$now = Get-Date; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'Mouser.exe'\" | "
        "ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; "
        "ExecutablePath = $_.ExecutablePath; "
        "Elapsed = [int]($now - $_.CreationDate).TotalSeconds } } | "
        "ConvertTo-Json -Compress"
    )
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]


def list_instances(exe_path: str | None = None) -> list[tuple[int, int]]:
    """``[(pid, elapsed_seconds), ...]`` for live processes running ``exe_path``."""
    exe_path = exe_path or default_executable()
    own = os.getpid()
    if sys.platform == "win32":
        result = subprocess.run(
            _windows_list_command(), capture_output=True, text=True, check=False
        )
        return parse_cim_output(result.stdout, exe_path, own)
    result = subprocess.run(
        ["ps", "-axo", "pid=,etime=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_ps_output(result.stdout, exe_path, own)


def list_instance_pids(exe_path: str | None = None) -> list[int]:
    return [pid for pid, _ in list_instances(exe_path)]


def kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        _log(f"cannot kill pid {pid}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# macOS launchd / Windows scheduled-task helpers
# ─────────────────────────────────────────────────────────────────────────────


def _launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _macos_service_target() -> str:
    return f"gui/{os.getuid()}/{APP_BUNDLE_ID}"


def _macos_plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{APP_BUNDLE_ID}.plist")


def macos_agent_loaded() -> bool:
    return _launchctl(["print", _macos_service_target()]).returncode == 0


def _windows_console_user() -> str:
    ps = "(Get-CimInstance Win32_ComputerSystem).UserName"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip()


def windows_start_task_script(exe_path: str, user: str, task_name: str = WINDOWS_START_TASK_NAME) -> str:
    """PowerShell that runs ``exe_path`` once in the console user's interactive session."""
    exe = exe_path.replace("'", "''")
    user_q = user.replace("'", "''")
    workdir = os.path.dirname(exe_path).replace("'", "''")
    return (
        f"$action = New-ScheduledTaskAction -Execute '{exe}' -WorkingDirectory '{workdir}'; "
        f"$principal = New-ScheduledTaskPrincipal -UserId '{user_q}' -LogonType Interactive; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $action "
        "-Principal $principal -Settings $settings -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{task_name}'; "
        "Start-Sleep -Seconds 2; "
        f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false"
    )


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Verbs
# ─────────────────────────────────────────────────────────────────────────────


def _wait_until_gone(
    exe_paths: Iterable[str],
    timeout: float,
    *,
    list_pids: Callable[[str], list[int]],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> list[int]:
    """Poll until no listed executable has a live process, or ``timeout``."""
    deadline = monotonic() + timeout
    while True:
        remaining = [pid for path in exe_paths for pid in list_pids(path)]
        if not remaining or monotonic() >= deadline:
            return remaining
        sleep(POLL_INTERVAL_S)


def ctl_status(exe_path: str | None = None) -> int:
    exe_path = exe_path or default_executable()
    rows = list_instances(exe_path)
    for pid, elapsed in rows:
        print(f"{pid}\t{elapsed}s\t{exe_path}")
    if not rows:
        print(f"not running: {exe_path}")
        return EXIT_NONE
    return EXIT_ONE if len(rows) == 1 else EXIT_MANY


def ctl_stop(
    exe_paths: Iterable[str] | None = None,
    *,
    request_quit_fn: Callable[[], bool] = request_quit,
    list_pids: Callable[[str], list[int]] = list_instance_pids,
    kill: Callable[[int], None] = kill_pid,
    agent_loaded: Callable[[], bool] | None = None,
    bootout: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    graceful_timeout: float = GRACEFUL_STOP_TIMEOUT_S,
    kill_timeout: float = KILL_WAIT_TIMEOUT_S,
) -> int:
    """Graceful quit over the raise channel, then SIGKILL/taskkill by PID.

    Returns 0 once nothing matching ``exe_paths`` is alive, 1 otherwise.
    """
    paths = list(exe_paths) if exe_paths else [default_executable()]
    if agent_loaded is None:
        agent_loaded = macos_agent_loaded if sys.platform == "darwin" else (lambda: False)
    if bootout is None:
        bootout = (lambda: _launchctl(["bootout", _macos_service_target()])) if sys.platform == "darwin" else (lambda: None)

    alive = [pid for path in paths for pid in list_pids(path)]
    if not alive:
        _log("stop: nothing running")
        return 0
    if request_quit_fn():
        _log(f"stop: quit requested, waiting up to {graceful_timeout:.0f}s")
        alive = _wait_until_gone(paths, graceful_timeout, list_pids=list_pids, sleep=sleep, monotonic=monotonic)
    else:
        _log("stop: raise channel unreachable; escalating")
    if alive:
        # A launchd-managed agent with KeepAlive would resurrect a killed
        # process; unload it first so the kill sticks.  ``ctl start`` re-loads.
        try:
            if agent_loaded():
                _log("stop: booting out launch agent before kill")
                bootout()
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"stop: bootout failed: {exc}")
        for pid in alive:
            _log(f"stop: killing pid {pid}")
            kill(pid)
        alive = _wait_until_gone(paths, kill_timeout, list_pids=list_pids, sleep=sleep, monotonic=monotonic)
    if alive:
        _log(f"stop: still alive after kill: {alive}")
        return 1
    _log("stop: done")
    return 0


def _log_paths() -> tuple[str, str]:
    if sys.platform == "darwin":
        log_dir = os.path.join(os.path.expanduser("~"), "Library", "Logs", "Mouser")
    else:
        log_dir = os.path.join(state_dir(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(log_dir, "launchd.out.log"), os.path.join(log_dir, "launchd.err.log")


def _spawn_detached(exe_path: str) -> None:
    out_path, err_path = _log_paths()
    out = open(out_path, "ab")
    err = open(err_path, "ab")
    try:
        subprocess.Popen(
            [exe_path],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=os.path.dirname(exe_path) or None,
            close_fds=True,
        )
    finally:
        out.close()
        err.close()


def ctl_start(
    exe_path: str | None = None,
    *,
    launchctl: Callable[[list[str]], subprocess.CompletedProcess] = _launchctl,
    agent_loaded: Callable[[], bool] | None = None,
    plist_exists: Callable[[], bool] | None = None,
    spawn: Callable[[str], None] = _spawn_detached,
    run_powershell: Callable[[str], subprocess.CompletedProcess] = _run_powershell,
    console_user: Callable[[], str] | None = None,
) -> int:
    """Start exactly one Mouser without ``open`` / ``Start-Process``.

    macOS: ``launchctl kickstart -k`` when the agent is loaded, else
    ``launchctl bootstrap`` of the plist (RunAtLoad starts it), else a
    detached direct spawn.  Windows: a one-shot interactive scheduled task
    for the console user, so a session-0 caller (SSH) still gets a visible,
    hook-capable Mouser.
    """
    exe_path = exe_path or default_executable()
    if not os.path.isfile(exe_path):
        _log(f"start: executable not found: {exe_path}")
        return 1
    if sys.platform == "darwin":
        agent_loaded = agent_loaded or macos_agent_loaded
        plist_exists = plist_exists or (lambda: os.path.isfile(_macos_plist_path()))
        target = _macos_service_target()
        if agent_loaded():
            result = launchctl(["kickstart", "-k", target])
            if result.returncode == 0:
                _log(f"start: launchctl kickstart -k {target}")
                return 0
            _log(f"start: kickstart failed: {(result.stderr or result.stdout).strip()}")
        elif plist_exists():
            result = launchctl(["bootstrap", f"gui/{os.getuid()}", _macos_plist_path()])
            if result.returncode == 0:
                _log("start: launchctl bootstrap (RunAtLoad)")
                return 0
            _log(f"start: bootstrap failed: {(result.stderr or result.stdout).strip()}")
        _log(f"start: spawning {exe_path} directly (no launch agent)")
        spawn(exe_path)
        return 0
    if sys.platform == "win32":
        console_user = console_user or _windows_console_user
        user = console_user()
        if not user:
            _log("start: no interactive console user; refusing to start in session 0")
            return 1
        result = run_powershell(windows_start_task_script(exe_path, user))
        if result.returncode != 0:
            _log(f"start: scheduled task failed: {(result.stderr or result.stdout).strip()}")
            return 1
        _log(f"start: launched via one-shot scheduled task as {user}")
        return 0
    _log(f"start: spawning {exe_path} directly")
    spawn(exe_path)
    return 0


def ctl_assert_single(
    exe_path: str | None = None,
    *,
    list_instances_fn: Callable[[str], list[tuple[int, int]]] = list_instances,
    kill: Callable[[int], None] = kill_pid,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout: float = ASSERT_SINGLE_TIMEOUT_S,
) -> int:
    """Kill every instance except the newest.  Non-zero if >1 remain after ``timeout``."""
    exe_path = exe_path or default_executable()
    rows = list_instances_fn(exe_path)  # newest first
    if len(rows) <= 1:
        _log(f"assert-single: {len(rows)} instance(s); nothing to do")
        return 0
    keep = rows[0][0]
    for pid, _ in rows[1:]:
        _log(f"assert-single: killing extra pid {pid} (keeping {keep})")
        kill(pid)
    deadline = monotonic() + timeout
    while True:
        rows = list_instances_fn(exe_path)
        if len(rows) <= 1:
            _log("assert-single: ok")
            return 0
        if monotonic() >= deadline:
            _log(f"assert-single: still {len(rows)} instances: {[p for p, _ in rows]}")
            return 1
        sleep(POLL_INTERVAL_S)


CTL_VERBS = ("status", "stop", "start", "restart", "assert-single")


def ctl_main(argv: list[str]) -> int:
    """Entry point for ``Mouser --ctl <verb> [--exe PATH]``."""
    args = list(argv)
    exe_path: str | None = None
    verb: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--exe":
            if i + 1 >= len(args):
                print("--exe requires a path", file=sys.stderr)
                return 64
            exe_path = args[i + 1]
            i += 2
            continue
        if arg.startswith("--exe="):
            exe_path = arg.split("=", 1)[1]
            i += 1
            continue
        if verb is None:
            verb = arg
            i += 1
            continue
        print(f"unexpected argument: {arg}", file=sys.stderr)
        return 64
    if verb not in CTL_VERBS:
        print(f"usage: --ctl {{{'|'.join(CTL_VERBS)}}} [--exe PATH]", file=sys.stderr)
        return 64
    exe_path = exe_path or default_executable()
    if verb == "status":
        return ctl_status(exe_path)
    if verb == "stop":
        return ctl_stop([exe_path])
    if verb == "start":
        return ctl_start(exe_path)
    if verb == "restart":
        code = ctl_stop([exe_path])
        if code != 0:
            return code
        return ctl_start(exe_path)
    return ctl_assert_single(exe_path)
