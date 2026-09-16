"""Stop and relaunch installed Mouser builds on macOS and Windows."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS_APP_NAME = "Mouser.app"
MACOS_EXECUTABLE = "Mouser"
WINDOWS_APP_DIR = "Mouser"
WINDOWS_EXECUTABLE = "Mouser.exe"
DEFAULT_MACOS_INSTALL_DIR = Path("/Applications")


def deskflow_root() -> Path:
    """Checkout of the deskflow repo that hosts the shared fleet tooling.

    Mouser borrows two pieces of fleet infrastructure from deskflow:
    ``tools/fleet-gui-exec.py`` (run a command in the console session so it can
    reach the login keychain) and ``scripts/sign-windows.ps1`` (signtool
    wrapper keyed by ``DESKFLOW_SIGN_THUMBPRINT``). ``DESKFLOW_ROOT`` overrides
    the default sibling checkout.
    """
    override = os.environ.get("DESKFLOW_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Desktop" / "deskflow"


def fleet_gui_exec_script() -> Path | None:
    """Path to deskflow's ``fleet-gui-exec.py`` when that checkout has it."""
    candidate = deskflow_root() / "tools" / "fleet-gui-exec.py"
    return candidate if candidate.is_file() else None


def windows_sign_script() -> Path | None:
    """Path to deskflow's ``sign-windows.ps1`` when that checkout has it."""
    candidate = deskflow_root() / "scripts" / "sign-windows.ps1"
    return candidate if candidate.is_file() else None


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


def _install_root_executable(root: Path) -> Path | None:
    """Image path Mouser runs from for a given install root, if it exists."""
    if sys.platform == "darwin":
        if not root.name.endswith(".app"):
            return None
        exe = _macos_bundle_executable(root)
    elif sys.platform == "win32":
        exe = root / WINDOWS_EXECUTABLE
    else:
        return None
    return exe if exe.is_file() else None


def run_ctl(verb: str, install_root: Path | None = None, **kwargs) -> int:
    """Dispatch a ``--ctl`` verb in-process (``core.single_instance``).

    Installers call the verbs directly instead of exec'ing the installed
    binary: ``stop`` runs while the old build may be half-broken, and ``start``
    must never depend on ``open``/``Start-Process`` (both re-launch races).
    """
    from core import single_instance

    if verb == "stop":
        roots = [install_root] if install_root is not None else iter_known_install_roots()
        exe_paths = [str(exe) for exe in (_install_root_executable(r) for r in roots) if exe]
        if not exe_paths:
            exe_paths = [single_instance.default_executable()]
        return single_instance.ctl_stop(exe_paths, **kwargs)
    exe = _install_root_executable(install_root) if install_root is not None else None
    exe_path = str(exe) if exe else None
    if verb == "start":
        return single_instance.ctl_start(exe_path, **kwargs)
    if verb == "status":
        return single_instance.ctl_status(exe_path)
    if verb == "assert-single":
        return single_instance.ctl_assert_single(exe_path, **kwargs)
    raise ValueError(f"unknown ctl verb: {verb}")


def stop_running_instances() -> None:
    """``ctl stop`` every known install location before replacing files.

    One graceful ``{"cmd":"quit"}`` over the raise channel (which really quits,
    bypassing the macOS quit-to-tray filter), then SIGKILL/taskkill by PID.
    """
    if sys.platform not in {"darwin", "win32"}:
        return
    code = run_ctl("stop")
    if code != 0:
        print("[!] Some Mouser processes survived ctl stop", file=sys.stderr)


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
    if sys.platform == "win32":
        # The Run key is the only launcher; drop leftovers from earlier
        # scheduled-task experiments (MouserStart/Dist/Exe/Src/Probe).
        from core.startup import remove_stale_scheduled_tasks

        remove_stale_scheduled_tasks()
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
    """``ctl start`` the freshly installed build -- exactly once, never ``open``."""
    install_root = install_root.resolve()
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError(f"launch_installed_application is unsupported on {sys.platform}")
    exe = _install_root_executable(install_root)
    if exe is None:
        raise FileNotFoundError(f"Install executable not found under: {install_root}")
    print(f"[*] Starting {exe} via ctl start")
    code = run_ctl("start", install_root)
    if code != 0:
        raise RuntimeError(f"ctl start failed with exit code {code}")
