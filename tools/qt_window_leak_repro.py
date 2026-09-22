#!/usr/bin/env python3
"""Repro: every native QQuickWindow create/destroy leaks ~11.8 MB on macOS.

Loads the trivial ``Window { Rectangle { Text {} } }`` below on a fresh
``QQmlApplicationEngine``, shows it, hides it, deletes the engine (which
destroys the window), and prints ``phys_footprint`` after each cycle.

Observed on macOS 26.6 / PySide6 6.11.0 / Apple M3 Max (2x display) with a
real cocoa window: +11.8 MB per cycle (exactly one 2120x1400 BGRA display
drawable) under ``QSG_RHI_BACKEND=metal``, ``=opengl``,
``QT_QUICK_BACKEND=software`` and ``QSG_RENDER_LOOP=basic`` alike; the same
loop on the ``offscreen`` platform is flat. ``QQuickWindow.destroy()`` on a
kept window leaks the same amount, so it is per native NSWindow/CAMetalLayer
lifecycle, not the QML engine. ``releaseResources()`` and
``setPersistentGraphics(False)`` do not help. No matching upstream report was
found on bugreports.qt.io as of 2026-09-22 (searched "QQuickWindow leak per
window macOS Metal", "CAMetalLayer drawable leak"; QTBUG-114721 is about
``nextDrawable`` blocking, not a leak).

Default (and the only mode agents may run): ``offscreen`` -- it proves the
harness itself is flat. ``--cocoa`` reproduces the leak but opens a real
window that STEALS FOCUS on the seat; it is for a human at the keyboard,
never for an automated run.

    tools/qt_window_leak_repro.py --cycles 8
    tools/qt_window_leak_repro.py --cycles 8 --cocoa     # human only
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TRIVIAL_QML = """
import QtQuick
import QtQuick.Window
Window {
    visible: true
    width: 1060; height: 700
    Rectangle { anchors.fill: parent; color: "steelblue"
        Text { anchors.centerIn: parent; text: "trivial" } }
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--settle", type=float, default=0.8, help="seconds shown / after destroy")
    ap.add_argument("--cocoa", action="store_true",
                    help="use a REAL window (steals focus; humans only, never agents)")
    ap.add_argument("--keep-window", action="store_true",
                    help="keep one window and call destroy() per cycle instead of deleting the engine")
    args = ap.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "cocoa" if args.cocoa else "offscreen"

    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from measure_qml_teardown import phys_footprint_mb

    app = QGuiApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    tmp = tempfile.mkdtemp(prefix="qt-window-leak-")
    qml = os.path.join(tmp, "Trivial.qml")
    with open(qml, "w", encoding="utf-8") as fh:
        fh.write(TRIVIAL_QML)

    def pump(seconds: float) -> None:
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()
        QCoreApplication.sendPostedEvents()
        gc.collect()

    pump(0.3)
    base = phys_footprint_mb()
    print(f"platform={os.environ['QT_QPA_PLATFORM']} base={base:.1f} MB")
    values = []
    engine = window = None
    for cycle in range(1, args.cycles + 1):
        if engine is None:
            engine = QQmlApplicationEngine()
            engine.load(QUrl.fromLocalFile(qml))
            window = engine.rootObjects()[0]
        window.show()
        pump(args.settle)
        window.hide()
        if args.keep_window:
            window.destroy()
        else:
            window = None
            engine.deleteLater()
            engine = None
        pump(args.settle)
        values.append(phys_footprint_mb())
        print(f"cycle {cycle}: {values[-1]:.1f} MB", flush=True)
    deltas = [b - a for a, b in zip(values, values[1:])]
    if deltas:
        print(f"per-cycle growth: {[round(d, 1) for d in deltas]} (mean {sum(deltas)/len(deltas):+.1f} MB)")
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
