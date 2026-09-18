"""Cross-platform login startup helpers for Windows, macOS, and Linux."""

# This module is imported by scripts/build_and_install.py, which runs under
# whatever `python3` is first on PATH -- on a stock macOS that is the Xcode
# Command Line Tools' 3.9, which cannot parse `str | None` at import time.
# Deferring annotation evaluation keeps the installer working there; the
# sibling installer modules already do the same.
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

# Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Mouser"

# macOS
MACOS_LAUNCH_AGENT_LABEL = "io.github.hughesyadaddy.mouser"
MACOS_PLIST_NAME = f"{MACOS_LAUNCH_AGENT_LABEL}.plist"
MACOS_BUNDLE_ID = MACOS_LAUNCH_AGENT_LABEL
MACOS_LAUNCHD_THROTTLE_SECONDS = 5

# Windows: scheduled tasks left behind by earlier launch experiments.  The
# HKCU Run value is the only sanctioned launcher; these are removed on install.
WINDOWS_STALE_SCHEDULED_TASKS = (
    "MouserStart",
    "MouserDist",
    "MouserExe",
    "MouserSrc",
    "MouserProbe",
)

# Linux
LINUX_APP_ID = "io.github.tombadash.mouser"
LINUX_DESKTOP_ENTRY_NAME = "io.github.tombadash.mouser.desktop"
LINUX_DESKTOP_TEMPLATE_NAME = f"{LINUX_DESKTOP_ENTRY_NAME}.in"
LINUX_AUTOSTART_DELAY_SECONDS = 15
LINUX_ICON_NAME = LINUX_APP_ID
LINUX_ICON_FILENAME = f"{LINUX_ICON_NAME}.png"
LINUX_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
APP_DISPLAY_NAME = "Mouser"


def supports_login_startup():
    return sys.platform in ("win32", "darwin", "linux")


def _quote_arg(s: str) -> str:
    if not s:
        return '""'
    if " " in s or "\t" in s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


def build_run_command() -> str:
    """Windows: command line stored in the HKCU Run value."""
    exe = os.path.abspath(sys.executable)
    exe_q = _quote_arg(exe)
    if getattr(sys, "frozen", False):
        return exe_q
    script = os.path.abspath(sys.argv[0])
    return f"{exe_q} {_quote_arg(script)}"


def _program_arguments():
    """Argv list for macOS LaunchAgent ProgramArguments."""
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return [exe]
    return [exe, os.path.abspath(sys.argv[0])]


def _runtime_root_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    script_path = os.path.abspath(sys.argv[0]) if sys.argv else os.path.abspath(__file__)
    return os.path.dirname(script_path)


def _source_checkout_python() -> str | None:
    root_dir = _runtime_root_dir()
    if sys.platform == "win32":
        candidate = os.path.join(root_dir, ".venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(root_dir, ".venv", "bin", "python")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def _desktop_exec_parts(*, force_show: bool = False):
    if getattr(sys, "frozen", False):
        args = [os.path.abspath(sys.executable)]
        if force_show:
            args.append("--show-window")
        return args
    script_path = os.path.abspath(sys.argv[0]) if sys.argv else os.path.abspath(__file__)
    exe = _source_checkout_python() or os.path.abspath(sys.executable)
    args = [exe, script_path]
    if force_show:
        args.append("--show-window")
    return args


def _desktop_exec_arg(arg: str) -> str:
    if not arg:
        return '""'
    if all(ch not in arg for ch in ' \t\n"\\`$'):
        return arg
    escaped = (
        arg.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def _desktop_exec_string(args: list[str]) -> str:
    return " ".join(_desktop_exec_arg(arg) for arg in args)


def _repo_root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _linux_template_path() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            candidates.append(os.path.join(bundle_root, "linux", LINUX_DESKTOP_TEMPLATE_NAME))
        candidates.append(
            os.path.join(_runtime_root_dir(), "linux", LINUX_DESKTOP_TEMPLATE_NAME)
        )
    candidates.append(
        os.path.join(
            _repo_root_dir(),
            "packaging",
            "linux",
            LINUX_DESKTOP_TEMPLATE_NAME,
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _linux_desktop_path() -> str:
    return os.path.expanduser(
        os.path.join("~", ".local", "share", "applications", LINUX_DESKTOP_ENTRY_NAME)
    )


def _linux_autostart_path() -> str:
    return os.path.expanduser(
        os.path.join("~", ".config", "autostart", LINUX_DESKTOP_ENTRY_NAME)
    )


def _linux_icon_path() -> str:
    runtime_icon = os.path.join(_runtime_root_dir(), "images", "logo_icon.png")
    if os.path.isfile(runtime_icon):
        return runtime_icon
    return os.path.join(_repo_root_dir(), "images", "logo_icon.png")


def _linux_icon_theme_source_root() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            candidates.append(os.path.join(bundle_root, "linux", "icons", "hicolor"))
        candidates.append(
            os.path.join(_runtime_root_dir(), "linux", "icons", "hicolor")
        )
    candidates.append(
        os.path.join(_repo_root_dir(), "packaging", "linux", "icons", "hicolor")
    )
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ""


def _linux_user_icon_theme_root() -> str:
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    data_home = (
        os.path.expanduser(xdg_data_home)
        if xdg_data_home
        else os.path.expanduser(os.path.join("~", ".local", "share"))
    )
    return os.path.join(data_home, "icons", "hicolor")


def _linux_icon_source_path(source_root: str, size: int) -> str:
    return os.path.join(
        source_root,
        f"{size}x{size}",
        "apps",
        LINUX_ICON_FILENAME,
    )


def _linux_icon_destination_path(destination_root: str, size: int) -> str:
    return os.path.join(
        destination_root,
        f"{size}x{size}",
        "apps",
        LINUX_ICON_FILENAME,
    )


def _refresh_linux_icon_theme_cache(destination_root: str) -> None:
    tool = shutil.which("gtk-update-icon-cache")
    if not tool:
        return
    try:
        subprocess.run(
            [tool, "-qtf", destination_root],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _sync_linux_icon_theme() -> bool:
    source_root = _linux_icon_theme_source_root()
    if not source_root:
        return False
    sources = [
        _linux_icon_source_path(source_root, size)
        for size in LINUX_ICON_SIZES
    ]
    if not all(os.path.isfile(source) for source in sources):
        return False
    destination_root = _linux_user_icon_theme_root()
    try:
        for size, source in zip(LINUX_ICON_SIZES, sources):
            destination = _linux_icon_destination_path(destination_root, size)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copyfile(source, destination)
            try:
                os.chmod(destination, 0o644)
            except OSError:
                pass
    except OSError as exc:
        print(f"[startup] failed to sync Linux icon theme: {exc}")
        return False
    _refresh_linux_icon_theme_cache(destination_root)
    return True


def _linux_icon_name_or_path() -> str:
    if _sync_linux_icon_theme():
        return LINUX_ICON_NAME
    return _linux_icon_path()


def sync_linux_icon_theme() -> bool:
    """Best-effort sync of Mouser's hicolor icons into the user's icon theme."""
    return _sync_linux_icon_theme()


def linux_runtime_icon_path(preferred_size: int = 256) -> str:
    """Return the best Linux runtime icon asset for window/tray surfaces."""
    source_root = _linux_icon_theme_source_root()
    if source_root:
        sizes = sorted(LINUX_ICON_SIZES, key=lambda size: abs(size - preferred_size))
        for size in sizes:
            candidate = _linux_icon_source_path(source_root, size)
            if os.path.isfile(candidate):
                return candidate
    return _linux_icon_path()


def _linux_source_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    if sys.argv:
        return os.path.abspath(sys.argv[0])
    return os.path.join(_repo_root_dir(), "main_qml.py")


def _linux_template_text() -> str:
    with open(_linux_template_path(), "r", encoding="utf-8") as fh:
        return fh.read()


def _render_linux_desktop_entry(*, autostart: bool) -> str:
    autostart_lines = ""
    if autostart:
        autostart_lines = "\n".join(
            [
                "X-GNOME-Autostart-enabled=true",
                f"X-GNOME-Autostart-Delay={LINUX_AUTOSTART_DELAY_SECONDS}",
                "Hidden=false",
            ]
        )
    entry = _linux_template_text()
    exec_parts = _desktop_exec_parts(force_show=not autostart)
    replacements = {
        "@APP_NAME@": APP_DISPLAY_NAME,
        "@EXEC@": _desktop_exec_string(exec_parts),
        "@TRY_EXEC@": exec_parts[0],
        "@WORKDIR@": _runtime_root_dir(),
        "@ICON@": _linux_icon_name_or_path(),
        "@SOURCE_PATH@": _linux_source_path(),
        "@AUTOSTART_LINES@": autostart_lines,
    }
    for placeholder, value in replacements.items():
        entry = entry.replace(placeholder, value)
    return entry.rstrip() + "\n"


def _write_linux_desktop_entry(path: str, *, autostart: bool) -> None:
    entry = _render_linux_desktop_entry(autostart=autostart)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(entry)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def ensure_linux_launcher() -> str:
    launcher_path = _linux_desktop_path()
    _write_linux_desktop_entry(launcher_path, autostart=False)
    return launcher_path


def _get_winreg():
    import winreg

    return winreg


def _apply_windows(enabled: bool, *, run_command: str | None = None) -> None:
    if sys.platform != "win32":
        return
    winreg = _get_winreg()
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        if enabled:
            winreg.SetValueEx(
                key,
                RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                run_command if run_command is not None else build_run_command(),
            )
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def _macos_plist_path() -> str:
    return os.path.expanduser(
        os.path.join("~/Library/LaunchAgents", MACOS_PLIST_NAME)
    )


def _macos_log_dir() -> str:
    return os.path.expanduser(os.path.join("~", "Library", "Logs", "Mouser"))


def macos_launch_agent_payload(program_arguments: list[str]) -> dict:
    """LaunchAgent plist contents.

    ``KeepAlive/SuccessfulExit=false`` makes launchd relaunch Mouser after a
    crash or a kill but *not* after a clean quit (tray Quit, ``--ctl stop``),
    so installers can stop it without a resurrection race.  ``ProcessType``
    ``Interactive`` keeps it out of the background-QoS band, and
    ``AssociatedBundleIdentifiers`` attributes it correctly in Login Items.
    """
    log_dir = _macos_log_dir()
    return {
        "Label": MACOS_LAUNCH_AGENT_LABEL,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "AssociatedBundleIdentifiers": [MACOS_BUNDLE_ID],
        "StandardOutPath": os.path.join(log_dir, "launchd.out.log"),
        "StandardErrorPath": os.path.join(log_dir, "launchd.err.log"),
        "ThrottleInterval": MACOS_LAUNCHD_THROTTLE_SECONDS,
    }


def remove_stale_scheduled_tasks(
    task_names: tuple[str, ...] = WINDOWS_STALE_SCHEDULED_TASKS,
) -> list[str]:
    """One-time Windows cleanup: unregister leftover Mouser scheduled tasks.

    Returns the task names that were handed to ``Unregister-ScheduledTask``.
    No-op on other platforms.
    """
    if sys.platform != "win32":
        return []
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in task_names)
    script = (
        f"foreach ($n in @({names})) {{ "
        "if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { "
        "Unregister-ScheduledTask -TaskName $n -Confirm:$false; "
        "Write-Output $n } }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[startup] scheduled-task cleanup skipped: {exc}", file=sys.stderr)
        return []
    removed = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    for name in removed:
        print(f"[startup] removed stale scheduled task {name}")
    return removed


def _launchctl_run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
    )


def _launchctl_failure_message(action: str, result: subprocess.CompletedProcess) -> str:
    details = (result.stderr or result.stdout or "").strip()
    if details:
        return f"launchctl {action} failed: {details}"
    return f"launchctl {action} failed with exit code {result.returncode}"


def _atomic_write_file(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    prefix = f".{os.path.basename(path)}."
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _remove_file_if_present(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _restore_macos_plist(
    plist_path: str,
    previous_plist: bytes | None,
    domain: str,
) -> None:
    if previous_plist is None:
        _remove_file_if_present(plist_path)
        return
    _atomic_write_file(plist_path, previous_plist)
    result = _launchctl_run(["launchctl", "bootstrap", domain, plist_path])
    if result.returncode != 0:
        raise RuntimeError(_launchctl_failure_message("restore bootstrap", result))


def _restore_macos_plist_then_raise(
    plist_path: str,
    previous_plist: bytes | None,
    domain: str,
    exc: Exception,
) -> None:
    try:
        _restore_macos_plist(plist_path, previous_plist, domain)
    except Exception as restore_exc:
        raise RuntimeError(
            f"{exc}; additionally failed to restore the previous launch agent: {restore_exc}"
        ) from exc
    if isinstance(exc, RuntimeError):
        raise exc
    raise RuntimeError(f"failed to update launch agent: {exc}") from exc


_LAUNCHD_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)


def _launchd_agent_pid(domain: str) -> int | None:
    """PID launchd reports for the agent, 0 when loaded but not running,
    None when the agent is not loaded at all."""
    result = _launchctl_run(
        ["launchctl", "print", f"{domain}/{MACOS_LAUNCH_AGENT_LABEL}"]
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    match = _LAUNCHD_PID_RE.search(stdout)
    return int(match.group(1)) if match else 0


def _apply_macos(enabled: bool, *, program_arguments: list[str] | None = None) -> None:
    if sys.platform != "darwin":
        return
    plist_path = _macos_plist_path()
    launch_agents_dir = os.path.dirname(plist_path)
    uid = os.getuid()
    domain = f"gui/{uid}"

    if enabled:
        os.makedirs(launch_agents_dir, exist_ok=True)
        plist_existed = os.path.isfile(plist_path)
        previous_plist = None
        if plist_existed:
            try:
                with open(plist_path, "rb") as f:
                    previous_plist = f.read()
            except OSError as exc:
                raise RuntimeError(
                    f"failed to preserve existing launch agent: {exc}"
                ) from exc
        payload = macos_launch_agent_payload(
            program_arguments or _program_arguments()
        )
        try:
            os.makedirs(_macos_log_dir(), exist_ok=True)
        except OSError:
            pass
        new_plist = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        # This runs on every launch. Bootout + bootstrap here used to spawn
        # a second Mouser (RunAtLoad) that lost the instance lock, raised
        # the window and exited -- the "self-sync" window pop. Converge
        # instead: identical plist means only `enable`; a changed plist is
        # written in place when launchd already owns this very process,
        # and only re-bootstrapped when it does not.
        if previous_plist == new_plist:
            _launchctl_run(
                ["launchctl", "enable", f"{domain}/{MACOS_LAUNCH_AGENT_LABEL}"]
            )
            return
        if plist_existed and _launchd_agent_pid(domain) == os.getpid():
            _atomic_write_file(plist_path, new_plist)
            _launchctl_run(
                ["launchctl", "enable", f"{domain}/{MACOS_LAUNCH_AGENT_LABEL}"]
            )
            return
        if plist_existed:
            _launchctl_run(["launchctl", "bootout", domain, plist_path])
        try:
            _atomic_write_file(plist_path, new_plist)
            result = _launchctl_run(["launchctl", "bootstrap", domain, plist_path])
            if result.returncode != 0:
                raise RuntimeError(_launchctl_failure_message("bootstrap", result))
        except Exception as exc:
            _restore_macos_plist_then_raise(plist_path, previous_plist, domain, exc)
    else:
        if os.path.isfile(plist_path):
            _launchctl_run(["launchctl", "bootout", domain, plist_path])
            try:
                os.remove(plist_path)
            except OSError:
                pass
        else:
            _launchctl_run(
                ["launchctl", "bootout", domain, MACOS_LAUNCH_AGENT_LABEL]
            )


def _apply_linux(enabled: bool) -> None:
    if sys.platform != "linux":
        return
    autostart_path = _linux_autostart_path()
    if enabled:
        ensure_linux_launcher()
        _write_linux_desktop_entry(autostart_path, autostart=True)
        return
    try:
        os.remove(autostart_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[startup] failed to remove Linux autostart entry: {exc}", file=sys.stderr)
    try:
        ensure_linux_launcher()
    except Exception as exc:
        print(f"[startup] failed to refresh Linux launcher on disable: {exc}", file=sys.stderr)


def apply_login_startup(
    enabled: bool,
    *,
    program_arguments: list[str] | None = None,
) -> None:
    if not supports_login_startup():
        return
    if sys.platform == "win32":
        run_command = None
        if program_arguments:
            run_command = _quote_arg(program_arguments[0])
        _apply_windows(enabled, run_command=run_command)
    elif sys.platform == "darwin":
        _apply_macos(enabled, program_arguments=program_arguments)
    elif sys.platform == "linux":
        _apply_linux(enabled)


def sync_from_config(enabled: bool) -> None:
    """Ensure OS login startup matches config."""
    apply_login_startup(enabled)
