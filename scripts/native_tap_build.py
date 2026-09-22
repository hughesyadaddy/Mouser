"""Locate (building when stale) ``native/mac/libmouser_tap.dylib`` for the
packaged app.

Used by ``Mouser-mac.spec``. The native CGEventTap callback is not optional
for a shipped build: without it the app falls back to the Python tap, whose
PyObjC trampoline leaks one CGEvent per pass-through event
(``docs/upstream/pyobjc-cgeventtap-leak.md``). So a compiler failure or a
missing dylib after the build is a build failure, never a warning.
"""

from __future__ import annotations

import os
import subprocess
import sys

DYLIB_NAME = "libmouser_tap.dylib"


def native_tap_paths(root: str) -> tuple[str, str, str]:
    """``(build.py, dylib, source)`` under ``root``."""
    native_dir = os.path.join(root, "native", "mac")
    return (
        os.path.join(native_dir, "build.py"),
        os.path.join(native_dir, DYLIB_NAME),
        os.path.join(native_dir, "mouser_tap.m"),
    )


def is_stale(dylib: str, source: str) -> bool:
    if not os.path.isfile(dylib):
        return True
    return os.path.isfile(source) and os.path.getmtime(dylib) < os.path.getmtime(source)


def native_tap_binaries(
    root: str,
    *,
    run=subprocess.run,
    python: str = sys.executable,
    log=print,
) -> list[tuple[str, str]]:
    """PyInstaller ``binaries`` entry for the native tap.

    Rebuilds the dylib when it is missing or older than its source. Raises
    ``SystemExit`` when the build fails (``check=True``) or when no dylib
    exists afterwards, so a packaged app can never silently ship the
    leaking Python tap.
    """
    build_py, dylib, source = native_tap_paths(root)
    if is_stale(dylib, source):
        if not os.path.isfile(build_py):
            raise SystemExit(
                f"[Mouser] {dylib} is missing and {build_py} does not exist; "
                "cannot package a build without the native event tap."
            )
        # check=True: a clang failure aborts the build here instead of
        # surfacing later as a memory leak on the seat.
        run([python, build_py], cwd=root, check=True)
    if not os.path.isfile(dylib):
        raise SystemExit(
            f"[Mouser] {dylib} is still missing after running {build_py}. "
            "Refusing to package: the app would fall back to the Python "
            "CGEventTap callback, which leaks one CGEvent per pass-through "
            "event (PyObjC _callbacks.m m_CGEventTapCallBack; see "
            "docs/upstream/pyobjc-cgeventtap-leak.md). Run "
            "`python3 native/mac/build.py` and fix the compiler output."
        )
    log(f"[Mouser] bundling native tap: {dylib}")
    return [(dylib, ".")]
