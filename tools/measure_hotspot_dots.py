#!/usr/bin/env python3
"""Footprint of N real ``HotspotDot.qml`` instances (audit R6 harness).

Instantiates ``ui/qml/HotspotDot.qml`` N times over a fake 460x360 mouse
image inside a 740x420 page, forces a render pass, and prints
``phys_footprint``. Run once with N=0 for the harness floor and subtract.
Always offscreen (software renderer, DPR 1): the old page-sized ``Canvas``
bitmap is 4x larger at DPR 2 on a real display, so treat the numbers as a
relative A/B, not the on-seat cost.

    tools/measure_hotspot_dots.py 0 6 12
    tools/measure_hotspot_dots.py --qml-dir /path/to/old/ui/qml 6   # A/B

Only ``HotspotDot.qml`` and ``Theme.js`` are read from ``--qml-dir``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never a real window

HARNESS_QML = """
import QtQuick

Item {
    id: mousePage
    width: 740; height: 420
    property string selectedButton: ""
    function selectButton(k) { selectedButton = k }
    function selectHScroll() { selectedButton = "hscroll_left" }

    Item {
        id: mouseImg
        width: 460; height: 360
        anchors.centerIn: parent
        property real paintedWidth: 460
        property real paintedHeight: 360
        property real offX: 0
        property real offY: 0
    }

    Repeater {
        model: harnessCount
        delegate: HotspotDot {
            required property int index
            anchors.fill: mousePage
            imgItem: mouseImg
            normX: 0.15 + 0.06 * index; normY: 0.3 + 0.03 * (index % 4)
            buttonKey: "b" + index
            label: "Button " + index; sublabel: "Do Nothing"
            labelSide: index % 2 ? "left" : "right"
        }
    }
}
"""


def phys_footprint_mb() -> float:
    from measure_qml_teardown import phys_footprint_mb as _fp  # same helper
    return _fp()


def measure(qml_dir: str, count: int, settle_ms: int) -> float:
    """Spawn a fresh interpreter per N so counts do not contaminate each other."""
    import subprocess

    out = subprocess.run(
        [sys.executable, __file__, "--_child", "--qml-dir", qml_dir,
         "--settle", str(settle_ms), str(count)],
        capture_output=True, text=True, timeout=120, check=False,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
    )
    for line in out.stdout.splitlines():
        if line.startswith("RESULT "):
            return float(line.split()[1])
    raise RuntimeError(f"child failed for n={count}:\n{out.stdout}\n{out.stderr}")


def _child(qml_dir: str, count: int, settle_ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QObject, Property, QTimer, QUrl, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuick import QQuickView

    class UiState(QObject):
        @Property(bool, constant=True)
        def darkMode(self):
            return True

        @Property(str, constant=True)
        def fontFamily(self):
            return "Helvetica"

    class Lm(QObject):
        @Property("QVariantMap", constant=True)
        def strings(self):
            return {}

        @Slot(str, result=str)
        def trButton(self, s):
            return s

        @Slot(str, result=str)
        def trAction(self, s):
            return s

    tmp = tempfile.mkdtemp(prefix="hotspot-harness-")
    for name in ("HotspotDot.qml", "Theme.js"):
        shutil.copy(os.path.join(qml_dir, name), tmp)
    harness = os.path.join(tmp, "Harness.qml")
    with open(harness, "w", encoding="utf-8") as fh:
        fh.write(HARNESS_QML)

    app = QGuiApplication(sys.argv[:1])
    view = QQuickView()
    ui, lm = UiState(), Lm()
    ctx = view.rootContext()
    ctx.setContextProperty("uiState", ui)
    ctx.setContextProperty("lm", lm)
    ctx.setContextProperty("harnessCount", count)
    view.setSource(QUrl.fromLocalFile(harness))
    if view.status() != QQuickView.Status.Ready:
        print("ERRORS", [e.toString() for e in view.errors()])
        sys.exit(2)
    view.resize(740, 420)
    view.show()

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    pump(200)
    view.grabWindow()  # full render pass so every item (and any Canvas) paints
    pump(settle_ms)
    view.grabWindow()
    pump(100)
    print(f"RESULT {phys_footprint_mb():.1f} n={count} dpr={view.devicePixelRatio()}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    os._exit(0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("counts", nargs="*", type=int, default=[0, 6, 12])
    ap.add_argument("--qml-dir", default=os.path.join(ROOT, "ui", "qml"))
    ap.add_argument("--settle", type=int, default=300, help="ms after the first render pass")
    ap.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if args._child:
        _child(args.qml_dir, args.counts[0], args.settle)
        return 0
    counts = args.counts or [0, 6, 12]
    floor = None
    print(f"HotspotDot from {args.qml_dir} (offscreen, DPR 1)")
    print("| hotspots | phys_footprint MB | over N=0 | per hotspot |")
    print("|---:|---:|---:|---:|")
    for n in counts:
        value = measure(args.qml_dir, n, args.settle)
        if floor is None:
            floor = value if n == 0 else measure(args.qml_dir, 0, args.settle)
        over = value - floor
        per = over / n if n else 0.0
        print(f"| {n} | {value:.1f} | {over:+.1f} | {per:+.2f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
