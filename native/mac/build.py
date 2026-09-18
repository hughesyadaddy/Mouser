"""Build ``libmouser_tap.dylib`` -- Mouser's native CGEventTap callback.

Run from the repository root::

    python3 native/mac/build.py

The result lands next to this file, which is where :mod:`core.native_hook_mac`
looks for it in a source checkout and what ``Mouser-mac.spec`` bundles.

Nothing here is required to run Mouser: without the dylib the macOS hook
falls back to its Python tap callback on a dedicated run-loop thread.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.abspath(os.path.dirname(__file__))
SOURCE = os.path.join(HERE, "mouser_tap.m")
DYLIB_NAME = "libmouser_tap.dylib"
OUTPUT = os.path.join(HERE, DYLIB_NAME)

FRAMEWORKS = ("CoreGraphics", "CoreFoundation", "IOKit", "Foundation")


def compile_command(output: str, *, compiler: str | None = None) -> list[str] | None:
    compiler = compiler or shutil.which("clang")
    if compiler is None:
        return None
    command = [
        compiler,
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-fobjc-arc",
        "-shared",
        "-o",
        output,
        SOURCE,
    ]
    for framework in FRAMEWORKS:
        command += ["-framework", framework]
    return command


def is_stale(output: str = OUTPUT) -> bool:
    """True when the dylib is missing or older than its source."""
    if not os.path.isfile(output):
        return True
    return os.path.getmtime(output) < os.path.getmtime(SOURCE)


def build(output: str = OUTPUT) -> int:
    if not os.path.isfile(SOURCE):
        print(f"[build] missing source: {SOURCE}")
        return 1
    command = compile_command(output)
    if command is None:
        print("[build] clang not found; install the Xcode command line tools")
        return 1
    print(f"[build] {' '.join(command)}")
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode != 0:
        print(f"[build] compiler failed with exit code {result.returncode}")
        return result.returncode
    if not os.path.isfile(output):
        print(f"[build] compiler reported success but {output} is missing")
        return 1
    print(f"[build] wrote {output} ({os.path.getsize(output)} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=OUTPUT)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("[build] libmouser_tap.dylib targets macOS; run this on the Mac.")
        return 1
    return build(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
