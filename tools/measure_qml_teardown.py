#!/usr/bin/env python3
"""Measure the settings-window engine lifecycle footprint (M3 QML teardown).

Builds the real ``Main.qml`` + ``GestureHud.qml`` on ``MainWindowHost`` /
``GestureHudHost`` with the real ``Backend`` (engine=None, config patched to
defaults) and prints ``phys_footprint`` after each lifecycle step:

    baseline -> hud -> shown -> hidden -> torn down -> re-shown -> torn down

No tray, no hooks, no installed app, and ALWAYS the ``offscreen`` platform
(never a cocoa window: it steals focus on the seat). Offscreen renders in
software, so absolute numbers are lower than the real Metal path; they are
valid for relative before/after comparisons only -- see
docs/memory-qml-teardown.md for the one-off cocoa reference numbers.
``--vmmap`` also diffs ``vmmap --summary`` of this pid between "shown" and
"torn down" (macOS only).

    tools/measure_qml_teardown.py --cycles 3 --vmmap
    tools/measure_qml_teardown.py --qml-dir /path/to/old/qml   # A/B
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import gc
import os
import re
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any PySide6 import


def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--settle", type=float, default=1.5, help="seconds to pump events per step")
    ap.add_argument("--vmmap", action="store_true", help="diff vmmap --summary shown vs torn down")
    ap.add_argument("--cycles", type=int, default=2, help="show/hide/teardown cycles")
    ap.add_argument("--qml-dir", default=os.path.join(ROOT, "ui", "qml"),
                    help="directory holding Main.qml / GestureHud.qml (default: the checkout)")
    ap.add_argument("--no-teardown", action="store_true",
                    help="control run: hide only, never release the engine")
    ap.add_argument("--no-page-switch", action="store_true",
                    help="control run: stay on page 0 (no MousePage/ScrollPage Loader churn)")
    ap.add_argument("--vmmap-all", action="store_true",
                    help="print the largest vmmap region deltas after every step (slow)")
    return ap.parse_args()


class _RusageInfoV4(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups",
            "ri_interrupt_wkups", "ri_pageins", "ri_wired_size",
            "ri_resident_size", "ri_phys_footprint",
        )
    ] + [(f"_pad{i}", ctypes.c_uint64) for i in range(27)]


def phys_footprint_mb() -> float:
    """``ri_phys_footprint`` in MB (what Activity Monitor calls Memory)."""
    if sys.platform != "darwin":
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        except Exception:
            return float("nan")
    assert ctypes.sizeof(_RusageInfoV4) == 16 + 35 * 8
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    fn = libproc.proc_pid_rusage
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    buf = _RusageInfoV4()
    if fn(os.getpid(), 4, ctypes.byref(buf)) != 0:  # RUSAGE_INFO_V4
        return float("nan")
    return buf.ri_phys_footprint / (1024 * 1024)


def _vmmap_summary() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        return subprocess.run(
            ["vmmap", "--summary", str(os.getpid())],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return f"(vmmap failed: {exc})"


_REGION_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9 _()/.'-]*?)\s{2,}(?P<virt>[\d.]+[KMG]?)\s+(?P<res>[\d.]+[KMG]?)\s+(?P<dirty>[\d.]+[KMG]?)")


def _to_mb(token: str) -> float:
    mult = {"K": 1 / 1024, "M": 1.0, "G": 1024.0}
    if token[-1] in mult:
        return float(token[:-1]) * mult[token[-1]]
    return float(token) / (1024 * 1024)


def _vmmap_regions(summary: str) -> dict[str, tuple[float, float]]:
    """{region name: (resident MB, dirty MB)} from a --summary dump."""
    out = {}
    for line in summary.splitlines():
        m = _REGION_RE.match(line)
        if not m or m.group("name").startswith("REGION TYPE"):
            continue
        try:
            out[m.group("name").strip()] = (_to_mb(m.group("res")), _to_mb(m.group("dirty")))
        except ValueError:
            continue
    return out


def main() -> int:
    args = _parse_args()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never a real window

    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    import main_qml
    from core.config import DEFAULT_CONFIG
    from ui.backend import Backend
    from ui.locale_manager import LocaleManager

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    def pump(seconds: float) -> None:
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()
        app.sendPostedEvents()
        gc.collect()

    rows: list[tuple[str, float]] = []

    last_regions: dict = {}

    def mark(label: str) -> float:
        value = phys_footprint_mb()
        rows.append((label, value))
        print(f"[measure] {label:<28} phys_footprint = {value:8.1f} MB", flush=True)
        if args.vmmap_all:
            nonlocal last_regions
            regions = _vmmap_regions(_vmmap_summary())
            deltas = sorted(
                ((regions.get(n, (0, 0))[0] - last_regions.get(n, (0, 0))[0], n)
                 for n in set(regions) | set(last_regions)),
                key=lambda t: -abs(t[0]),
            )
            shown = [f"{n} {d:+.1f}" for d, n in deltas[:8] if abs(d) >= 1.0]
            print(f"[vmmap]   resident deltas: {', '.join(shown) or 'none >= 1 MB'}", flush=True)
            last_regions = regions
        return value

    with (
        patch("ui.backend.load_config", return_value=copy.deepcopy(DEFAULT_CONFIG)),
        patch("ui.backend.save_config"),
        patch("ui.backend.supports_login_startup", return_value=False),
    ):
        backend = Backend(engine=None, root_dir=ROOT)
    ui_state = main_qml.UiState(app)
    locale_mgr = LocaleManager(language="en")
    context = {
        "backend": backend,
        "uiState": ui_state,
        "lm": locale_mgr,
        "appVersion": main_qml.APP_VERSION,
        "appBuildMode": main_qml.APP_BUILD_MODE,
        "appCommit": main_qml.APP_COMMIT_DISPLAY,
        "appLaunchPath": ROOT,
    }
    pump(0.3)
    mark("baseline (app+backend)")

    hud = main_qml.GestureHudHost(
        qml_path=os.path.join(args.qml_dir, "GestureHud.qml"),
        context_properties=context, parent=app,
    )
    pump(0.3)
    mark("hud engine")

    host = main_qml.MainWindowHost(
        qml_path=os.path.join(args.qml_dir, "Main.qml"),
        context_properties=context,
        image_providers={
            "appicons": lambda: main_qml.AppIconProvider(ROOT),
            "systemicons": main_qml.SystemIconProvider,
        },
        launch_hidden=False,
        parent=app,
    )

    shown_summary = torn_summary = ""
    for cycle in range(1, args.cycles + 1):
        host.show()
        pump(args.settle)
        if not args.no_page_switch:
            # Visit the second page too so both Loaders have been exercised.
            ui_state.currentPage = 1
            pump(args.settle / 2)
            ui_state.currentPage = 0
            pump(args.settle / 2)
        mark(f"cycle {cycle}: shown")
        pump(args.settle)
        mark(f"cycle {cycle}: shown+settled")
        if args.vmmap and cycle == 1:
            shown_summary = _vmmap_summary()

        # Fire the HUD once so its window has been mapped at least once.
        backend.gestureFeedback.emit("measure", "fired")
        pump(0.3)

        if not args.no_page_switch:
            ui_state.currentPage = 1
        host.hide()
        pump(args.settle)
        mark(f"cycle {cycle}: hidden")
        assert host.teardown_pending(), "timer should be armed after hide"
        if args.no_teardown:
            continue

        released = host.teardown()  # what the 30 s timer would do
        assert released, "teardown skipped"
        pump(args.settle)
        mark(f"cycle {cycle}: torn down")
        if args.vmmap and cycle == 1:
            torn_summary = _vmmap_summary()

    host.show()
    pump(args.settle)
    win = host.window()
    expected_page = 0 if args.no_page_switch else 1
    assert win is not None and win.property("currentPage") == expected_page, "currentPage not restored"
    mark("re-shown (page restored)")
    host.hide()
    host.teardown()
    pump(args.settle)
    mark("final torn down")

    assert hud.window() is not None
    backend.gestureFeedback.emit("still alive", "fired")
    pump(0.2)
    hud_pill_opacity = None
    for child in hud.window().findChildren(object):
        try:
            if child.property("radius") == 24:
                hud_pill_opacity = child.property("opacity")
                break
        except Exception:
            continue
    print(f"[measure] hud pill opacity after flash: {hud_pill_opacity}")

    print()
    print("| step | phys_footprint MB |")
    print("|---|---:|")
    for label, value in rows:
        print(f"| {label} | {value:.1f} |")

    if args.vmmap and shown_summary and torn_summary:
        before = _vmmap_regions(shown_summary)
        after = _vmmap_regions(torn_summary)
        print()
        print("| vmmap region | shown res/dirty MB | torn down res/dirty MB |")
        print("|---|---:|---:|")
        for name in sorted(set(before) | set(after), key=lambda n: -(before.get(n, (0, 0))[0])):
            b = before.get(name, (0.0, 0.0))
            a = after.get(name, (0.0, 0.0))
            if max(b[0], a[0]) < 4:
                continue
            print(f"| {name} | {b[0]:.1f} / {b[1]:.1f} | {a[0]:.1f} / {a[1]:.1f} |")
        cg_lines = [line for line in shown_summary.splitlines() if "CG image" in line]
        print()
        print("CG image (shown):", *cg_lines, sep="\n  ")
        cg_lines = [line for line in torn_summary.splitlines() if "CG image" in line]
        print("CG image (torn down):", *cg_lines, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
