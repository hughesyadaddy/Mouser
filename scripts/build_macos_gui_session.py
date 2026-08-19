#!/usr/bin/env python3
"""Run the macOS build inside the console (GUI) session so it can sign.

Code signing needs the login keychain, and an SSH session is not a member of
the user's GUI session -- ``security`` reports "User interaction is not
allowed" and ``codesign`` fails with ``errSecInternalComponent``. The keychain
is not locked, so unlocking it is not the fix, and wrapping ``codesign`` in
``sudo`` / ``launchctl asuser`` does not move it into the session either.

The tempting workaround is ``MOUSER_SIGN_IDENTITY=-`` (ad-hoc). Do not: an
ad-hoc signature is regenerated on every build, so macOS treats each build as a
new app and resets its Accessibility / Input Monitoring grants, which is how
the fleet ended up re-prompting for permissions on every deploy.

Instead, hand the build to the session that already holds the keychain. When
run from a terminal that can reach the keychain this just execs the normal
build; over SSH it drives Terminal.app in the console session and waits.

Usage:
    python3 scripts/build_macos_gui_session.py [build_and_install.py args...]
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Written as the final line of the log so the poller can tell "finished" from
#: "still running" without racing a partially flushed file.
SENTINEL = "__MOUSER_BUILD_EXIT__"

POLL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 1800


def console_user() -> str | None:
    """The user owning the GUI session, or None when nobody is logged in."""
    try:
        owner = subprocess.check_output(
            ["stat", "-f", "%Su", "/dev/console"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    # Logged out consoles report root; that is not a session we can build in.
    return owner or None


def keychain_reachable() -> bool:
    """True when this session can actually use the login keychain.

    Deliberately probes the keychain rather than checking for SSH_* env vars:
    what matters is session membership, and an SSH session is only the most
    common way to end up outside it (cron and some CI runners are others).
    """
    keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    try:
        result = subprocess.run(
            ["security", "show-keychain-info", str(keychain)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def should_route_through_gui_session() -> bool:
    """Route only when we cannot sign here but the console session could."""
    if sys.platform != "darwin":
        return False
    if keychain_reachable():
        return False
    owner = console_user()
    return bool(owner) and owner == os.environ.get("USER", "")


def build_command(extra_args: list[str], log_path: str) -> str:
    """Shell line run inside the GUI session, logging to *log_path*."""
    args = " ".join(shlex.quote(a) for a in extra_args)
    inner = (
        f"cd {shlex.quote(str(ROOT))} && "
        f"python3 scripts/build_and_install.py {args}".rstrip()
    )
    return (
        f"{inner} > {shlex.quote(log_path)} 2>&1; "
        f"echo {SENTINEL}=$? >> {shlex.quote(log_path)}"
    )


def _osascript_run(command: str) -> None:
    script = f'tell application "Terminal" to do script "{command}"'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


def _wait_for_exit(log_path: str, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for line in Path(log_path).read_text().splitlines():
                if line.startswith(f"{SENTINEL}="):
                    return int(line.split("=", 1)[1])
        except (OSError, ValueError):
            pass
        time.sleep(POLL_SECONDS)
    print(f"error: build did not finish within {timeout}s; see {log_path}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    extra_args = list(sys.argv[1:] if argv is None else argv)

    if not should_route_through_gui_session():
        # Already able to sign (or not macOS): run the normal build in place.
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_and_install.py"), *extra_args]
        ).returncode

    log_path = f"/tmp/mouser-build-{os.getpid()}.log"
    print(f"Keychain is unreachable here; running the build in {console_user()}'s "
          f"GUI session so it can sign.\n  log: {log_path}")
    _osascript_run(build_command(extra_args, log_path))

    code = _wait_for_exit(log_path, DEFAULT_TIMEOUT_SECONDS)
    try:
        print(Path(log_path).read_text().rstrip())
    except OSError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
