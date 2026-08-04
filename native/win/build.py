"""Build ``mouser_hook.dll`` -- Mouser's native WH_MOUSE_LL procedure.

Run from the repository root::

    python native/win/build.py

Uses MSVC (``cl``) when it is on PATH, otherwise mingw-w64 (``gcc`` /
``x86_64-w64-mingw32-gcc``). The result lands next to this file as
``mouser_hook_x64.dll``, which is where :mod:`core.native_hook_win` looks for
it in a source checkout and what ``Mouser.spec`` bundles into a build.

Nothing here is required to run Mouser: without the DLL the Windows hook
falls back to its Python procedure, which works exactly as before -- just
with the input latency this DLL exists to remove.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.abspath(os.path.dirname(__file__))
SOURCE = os.path.join(HERE, "mouser_hook.c")
DLL_NAME = "mouser_hook_x64.dll"
OUTPUT = os.path.join(HERE, DLL_NAME)

MINGW_CANDIDATES = ("x86_64-w64-mingw32-gcc", "gcc")


def _msvc_command(output: str) -> list[str] | None:
    compiler = shutil.which("cl")
    if compiler is None:
        return None
    return [
        compiler,
        "/nologo",
        "/O2",
        "/W4",
        "/LD",  # DLL
        "/GS-",  # no stack cookies: the hook procedure is a hot path
        SOURCE,
        f"/Fe:{output}",
        f"/Fo:{os.path.join(HERE, 'mouser_hook.obj')}",
        "/link",
        "user32.lib",
        "kernel32.lib",
    ]


def _mingw_command(output: str) -> list[str] | None:
    for name in MINGW_CANDIDATES:
        compiler = shutil.which(name)
        if compiler is not None:
            return [
                compiler,
                "-O2",
                "-Wall",
                "-Wextra",
                "-shared",
                "-o",
                output,
                SOURCE,
                "-luser32",
                "-lkernel32",
                "-static-libgcc",
            ]
    return None


def build(output: str = OUTPUT) -> int:
    if not os.path.isfile(SOURCE):
        print(f"[build] missing source: {SOURCE}")
        return 1

    command = _msvc_command(output) or _mingw_command(output)
    if command is None:
        print(
            "[build] no compiler found. Install either the MSVC build tools "
            "(run this from a 'x64 Native Tools Command Prompt' so cl is on "
            "PATH) or mingw-w64."
        )
        return 1

    print(f"[build] {' '.join(command)}")
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode != 0:
        print(f"[build] compiler failed with exit code {result.returncode}")
        return result.returncode

    if not os.path.isfile(output):
        print(f"[build] compiler reported success but {output} is missing")
        return 1

    size = os.path.getsize(output)
    print(f"[build] wrote {output} ({size} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=OUTPUT,
        help=f"where to write the DLL (default: {OUTPUT})",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print(
            "[build] mouser_hook.dll targets Windows; run this on the Windows "
            "machine (or with a mingw-w64 cross-compiler on PATH)."
        )
    return build(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
