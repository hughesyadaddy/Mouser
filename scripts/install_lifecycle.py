"""Stop and relaunch installed Mouser builds on macOS and Windows."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS_APP_NAME = "Mouser.app"
MACOS_EXECUTABLE = "Mouser"
WINDOWS_APP_DIR = "Mouser"
WINDOWS_EXECUTABLE = "Mouser.exe"
DEFAULT_MACOS_INSTALL_DIR = Path("/Applications")


def restart_enabled() -> bool:
    """True unless MOUSER_RESTART is set to a falsey string."""
    value = (os.environ.get("MOUSER_RESTART") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _macos_bundle_executable(app_bundle: Path) -> Path:
    return app_bundle / "Contents" / "MacOS" / MACOS_EXECUTABLE


def iter_known_install_roots() -> list[Path]:
    """Install locations that may still be running an older build."""
    roots: list[Path] = []
    override = os.environ.get("MOUSER_INSTALL_DIR")
    if override:
        roots.append(Path(override).expanduser())

    if sys.platform == "darwin":
        roots.append(DEFAULT_MACOS_INSTALL_DIR / MACOS_APP_NAME)
        roots.append(ROOT / "dist" / MACOS_APP_NAME)
    elif sys.platform == "win32":
        from scripts.windows_install import default_install_root

        for scope in ("user", "machine"):
            try:
                roots.append(default_install_root(scope))
            except (OSError, ValueError, RuntimeError):
                pass
        roots.append(ROOT / "dist" / WINDOWS_APP_DIR)

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = os.path.normcase(str(root.resolve())) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _stop_macos_instances(install_roots: list[Path]) -> bool:
    stopped = False
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Mouser" to quit',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    time.sleep(0.75)

    for root in install_roots:
        if not root.name.endswith(".app"):
            continue
        executable = _macos_bundle_executable(root)
        if not executable.is_file():
            continue
        result = subprocess.run(
            ["pkill", "-f", str(executable)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            stopped = True
            print(f"[*] Stopped Mouser running from {root}")

    result = subprocess.run(
        ["pkill", "-x", MACOS_EXECUTABLE],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        stopped = True
        print("[*] Stopped running Mouser process(es)")
    if stopped:
        time.sleep(0.5)
    return stopped


def _stop_windows_instances(install_roots: list[Path]) -> bool:
    stopped = False
    result = subprocess.run(
        ["taskkill", "/IM", WINDOWS_EXECUTABLE, "/F", "/T"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        stopped = True
        print("[*] Stopped running Mouser.exe instance(s) before install")

    for root in install_roots:
        if not root.is_dir():
            continue
        exe = root / WINDOWS_EXECUTABLE
        if not exe.is_file():
            continue
        ps = (
            "$target = '"
            + str(exe).replace("'", "''")
            + "'; "
            "$procs = Get-CimInstance Win32_Process -Filter "
            f"\"Name = '{WINDOWS_EXECUTABLE}'\" | "
            "Where-Object { $_.ExecutablePath -and "
            "($_.ExecutablePath -eq $target -or $_.ExecutablePath -like ($target + '*')) }; "
            "foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        probe = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            stopped = True

    if stopped:
        time.sleep(1)
    return stopped


def stop_running_instances() -> None:
    """Quit/kill Mouser from every known install location before replacing files."""
    if sys.platform not in {"darwin", "win32"}:
        return
    roots = iter_known_install_roots()
    if sys.platform == "darwin":
        _stop_macos_instances(roots)
        return
    _stop_windows_instances(roots)


def installed_program_arguments(install_root: Path) -> list[str]:
    """Argv list for the installed build (used by login-startup sync)."""
    install_root = install_root.resolve()
    if sys.platform == "darwin":
        return [str(_macos_bundle_executable(install_root))]
    if sys.platform == "win32":
        return [str((install_root / WINDOWS_EXECUTABLE).resolve())]
    raise RuntimeError(
        f"installed_program_arguments is unsupported on {sys.platform}"
    )


def sync_login_startup_after_install(install_root: Path) -> None:
    """Ensure OS login startup matches config after installing a build."""
    from core.config import load_config
    from core.startup import apply_login_startup, supports_login_startup

    if not supports_login_startup():
        return
    try:
        cfg = load_config()
    except Exception as exc:
        print(
            f"[startup] Could not load config for login sync: {exc}",
            file=sys.stderr,
        )
        return
    if not cfg.get("settings", {}).get("start_at_login", False):
        return
    try:
        args = installed_program_arguments(install_root)
    except RuntimeError:
        return
    if not Path(args[0]).is_file():
        print(
            f"[startup] Skipping login sync; executable missing: {args[0]}",
            file=sys.stderr,
        )
        return
    print(f"[*] Enabling start at login -> {args[0]}")
    apply_login_startup(True, program_arguments=args)


def launch_installed_application(install_root: Path) -> None:
    """Start the freshly installed build from its install directory."""
    install_root = install_root.resolve()
    if sys.platform == "darwin":
        if not install_root.is_dir():
            raise FileNotFoundError(f"Install bundle not found: {install_root}")
        executable = _macos_bundle_executable(install_root)
        if not executable.is_file():
            raise FileNotFoundError(f"Install executable not found: {executable}")
        print(f"[*] Launching {install_root}")
        subprocess.run(["open", str(install_root)], check=True)
        return

    if sys.platform == "win32":
        exe = install_root / WINDOWS_EXECUTABLE
        if not exe.is_file():
            raise FileNotFoundError(f"Install executable not found: {exe}")
        print(f"[*] Launching {exe}")
        os.startfile(str(exe))  # noqa: S606 — intentional GUI relaunch
        return

    raise RuntimeError(f"launch_installed_application is unsupported on {sys.platform}")
