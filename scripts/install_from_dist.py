#!/usr/bin/env python3
"""Install an existing dist/Mouser build without rebuilding."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_and_install as installer
from scripts.install_lifecycle import (
    launch_installed_application,
    restart_enabled,
    stop_running_instances,
    sync_login_startup_after_install,
)
from scripts.windows_install import (
    default_install_root,
    finalize_windows_install,
    replace_tree,
    resolve_install_scope,
)


def install_macos_from_dist() -> None:
    build_output = ROOT / "dist" / installer.MACOS_APP_NAME
    install_dir = installer.resolve_install_dir(installer.DEFAULT_MACOS_INSTALL_DIR)
    install_path = install_dir / installer.MACOS_APP_NAME

    if not build_output.is_dir():
        raise SystemExit(f"Build output not found: {build_output}")

    print(f"Installing {build_output} -> {install_path}")
    print("[*] Stopping running Mouser instances...")
    stop_running_instances()

    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", str(build_output), str(install_path)], check=True)

    if shutil.which("codesign"):
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(install_path)],
            check=False,
        )

    print(f"Installed: {install_path}")
    sync_login_startup_after_install(install_path)
    if restart_enabled():
        launch_installed_application(install_path)


def install_windows_from_dist() -> None:
    build_output = ROOT / "dist" / installer.WINDOWS_APP_DIR
    scope = resolve_install_scope()
    install_path = (
        Path(os.environ["MOUSER_INSTALL_DIR"]).expanduser()
        if os.environ.get("MOUSER_INSTALL_DIR")
        else default_install_root(scope)
    )

    print(f"Installing {build_output} -> {install_path} ({scope} scope)")
    print("[*] Stopping running Mouser instances...")
    stop_running_instances()
    replace_tree(build_output, install_path)
    shell = finalize_windows_install(install_path, scope=scope)
    print(f"Installed: {shell['install_root']}")
    print(f"Start Menu: {shell['start_menu_shortcut']}")
    sync_login_startup_after_install(shell["install_root"])
    if restart_enabled():
        launch_installed_application(shell["install_root"])


def main() -> None:
    if sys.platform == "darwin":
        install_macos_from_dist()
        return
    if sys.platform == "win32":
        install_windows_from_dist()
        return
    raise SystemExit("install_from_dist.py supports macOS and Windows only")


if __name__ == "__main__":
    main()
