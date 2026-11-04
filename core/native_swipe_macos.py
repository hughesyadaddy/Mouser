"""Synthetic trackpad swipes for macOS (Dock swipe and navigation swipe).

Posts the same private CGEvent pair a three/four-finger trackpad swipe
produces, so Mission Control, App Exposé and Space switching animate the
way they do on a trackpad, and Safari-style back/forward pages slide.

The field layout is ported from Mac Mouse Fix
(``Helper/Core/Touch/TouchSimulator.m``, pre-macOS-27 branch) and matches
TwinDesk's ``Gesture.swift``. All field numbers are undocumented; on
macOS 27+ Apple dropped the field path (it needs an IOHIDEvent payload),
so :func:`supported` returns False there and callers fall back.

Events are posted at ``kCGHIDEventTap`` so that a KVM capture tap sitting
at the HID level (TwinDesk) can see and forward them, and they carry
Mouser's own source marker (not TwinDesk's 0x4D53).

Stuck-swipe protection: every swipe that posts a *began* frame always
posts an *ended* frame (``try/finally``), the ended frame is re-sent after
200 ms (Mac Mouse Fix's fix for the Dock ignoring an end under load), and
only one swipe runs at a time -- overlapping requests are dropped, never
queued. A whole swipe is bounded to ~0.3 s; nothing waits on user input.
"""

from __future__ import annotations

import platform
import sys
import threading
import time

#: kCGEventSourceUserData value stamped on every event posted here.
#: Distinct from Mouser's scroll/inject markers (0x4D4F5553..56) and from
#: TwinDesk's own-event marker (0x4D53), so TwinDesk forwards these.
NATIVE_SWIPE_MARKER = 0x4D4F5557

# Private CGEvent fields (see module docstring).
F_SOURCE_USER_DATA = 42
F_TYPE = 55
F_41 = 41
F_HID_SUBTYPE = 110
F_SWIPE_DIRECTION = 115
F_TYPE_SUM_A = 119
F_MOTION_A = 123
F_PROGRESS = 124
F_EXIT_SPEED_A = 129
F_EXIT_SPEED_B = 130
F_PHASE_A = 132
F_PHASE_B = 134
F_PROGRESS_F32 = 135
F_INVERTED = 136
F_TYPE_SUM_B = 139
F_MOTION_B = 165

NS_EVENT_TYPE_GESTURE = 29
NS_EVENT_TYPE_DOCK_CONTROL = 30
VAL_41 = 33231
HID_DOCK_SWIPE = 23
HID_NAVIGATION_SWIPE = 16

PHASE_BEGAN = 1
PHASE_CHANGED = 2
PHASE_ENDED = 4
PHASE_CANCELLED = 8

AXIS_HORIZONTAL = 1
AXIS_VERTICAL = 2

# Mac Mouse Fix's "weirdTypeOrSum": the float bit patterns 1 and 2
# reinterpreted as doubles, per axis.
_TYPE_SUM = {AXIS_HORIZONTAL: 1.401298464324817e-45, AXIS_VERTICAL: 2.802596928649634e-45}

SWIPE_LEFT = 1 << 2   # kIOHIDSwipeLeft
SWIPE_RIGHT = 1 << 3  # kIOHIDSwipeRight
SWIPE_NONE = 0

FRAMES = 8
FRAME_INTERVAL_S = 0.012
TOTAL_PROGRESS = 1.0
EXIT_SPEED = 3.0
END_RESEND_DELAY_S = 0.2

_lock = threading.Lock()


def _macos_major():
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def supported():
    """True when the field-based recipe can be used on this OS."""
    if sys.platform != "darwin":
        return False
    major = _macos_major()
    return 11 <= major < 27


def _quartz():
    import Quartz  # local import: only macOS has it
    return Quartz


def _new_event(q):
    e = q.CGEventCreate(None)
    if e is None:
        raise RuntimeError("CGEventCreate returned NULL")
    q.CGEventSetIntegerValueField(e, F_SOURCE_USER_DATA, NATIVE_SWIPE_MARKER)
    return e


def _post(q, event):
    q.CGEventPost(q.kCGHIDEventTap, event)


def _dock_pair(q, axis, phase, progress, exit_speed):
    e30 = _new_event(q)
    q.CGEventSetDoubleValueField(e30, F_TYPE, NS_EVENT_TYPE_DOCK_CONTROL)
    q.CGEventSetDoubleValueField(e30, F_HID_SUBTYPE, HID_DOCK_SWIPE)
    q.CGEventSetDoubleValueField(e30, F_PHASE_A, phase)
    q.CGEventSetDoubleValueField(e30, F_PHASE_B, phase)
    q.CGEventSetDoubleValueField(e30, F_PROGRESS, progress)
    q.CGEventSetIntegerValueField(e30, F_PROGRESS_F32, float32_bits(progress))
    q.CGEventSetDoubleValueField(e30, F_41, VAL_41)
    q.CGEventSetDoubleValueField(e30, F_TYPE_SUM_A, _TYPE_SUM[axis])
    q.CGEventSetDoubleValueField(e30, F_TYPE_SUM_B, _TYPE_SUM[axis])
    q.CGEventSetDoubleValueField(e30, F_MOTION_A, axis)
    q.CGEventSetDoubleValueField(e30, F_MOTION_B, axis)
    q.CGEventSetIntegerValueField(e30, F_INVERTED, 0)
    if phase in (PHASE_ENDED, PHASE_CANCELLED):
        q.CGEventSetDoubleValueField(e30, F_EXIT_SPEED_A, exit_speed)
        q.CGEventSetDoubleValueField(e30, F_EXIT_SPEED_B, exit_speed)
    e29 = _new_event(q)
    q.CGEventSetDoubleValueField(e29, F_TYPE, NS_EVENT_TYPE_GESTURE)
    q.CGEventSetDoubleValueField(e29, F_41, VAL_41)
    return e30, e29


def float32_bits(value):
    import struct
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


def frame_plan(direction, frames=FRAMES, total=TOTAL_PROGRESS):
    """Return the (phase, progress) list for one swipe. Pure; tested."""
    sign = 1.0 if direction > 0 else -1.0
    plan = [(PHASE_BEGAN, 0.0)]
    for i in range(1, frames + 1):
        plan.append((PHASE_CHANGED, sign * total * i / frames))
    plan.append((PHASE_ENDED, sign * total))
    return plan


def dock_swipe(axis, direction, *, quartz=None, sleep=time.sleep, timer=threading.Timer):
    """Post one complete Dock swipe. Returns True if it was posted.

    ``direction`` is +1 or -1 along ``axis``. The ended frame is always
    posted once a began frame went out, then re-sent after 200 ms.
    """
    if axis not in _TYPE_SUM:
        raise ValueError(f"bad axis {axis!r}")
    if quartz is None and not supported():
        return False
    if not _lock.acquire(blocking=False):
        return False  # a swipe is already in flight: drop, never queue
    q = quartz or _quartz()
    sign = 1.0 if direction > 0 else -1.0
    progress = 0.0
    began = False
    try:
        plan = frame_plan(direction)
        for phase, progress in plan[:-1]:
            pair = _dock_pair(q, axis, phase, progress, 0.0)
            for e in pair:
                _post(q, e)
            began = True
            sleep(FRAME_INTERVAL_S)
        progress = plan[-1][1]
    finally:
        if began:
            end = _dock_pair(q, axis, PHASE_ENDED, progress, sign * EXIT_SPEED)
            for e in end:
                _post(q, e)

            def _resend(events=end):
                try:
                    for e in events:
                        _post(q, e)
                except Exception:  # pragma: no cover - best effort
                    pass

            t = timer(END_RESEND_DELAY_S, _resend)
            t.daemon = True
            t.start()
        _lock.release()
    return began


def navigation_swipe(direction, *, quartz=None):
    """Post a back (-1) / forward (+1) navigation swipe (began + ended)."""
    if quartz is None and not supported():
        return False
    if not _lock.acquire(blocking=False):
        return False
    q = quartz or _quartz()
    try:
        e = _new_event(q)
        q.CGEventSetDoubleValueField(e, F_TYPE, NS_EVENT_TYPE_GESTURE)
        q.CGEventSetDoubleValueField(e, F_HID_SUBTYPE, HID_NAVIGATION_SWIPE)
        q.CGEventSetDoubleValueField(e, F_PHASE_A, PHASE_BEGAN)
        q.CGEventSetDoubleValueField(
            e, F_SWIPE_DIRECTION, SWIPE_RIGHT if direction > 0 else SWIPE_LEFT
        )
        try:
            _post(q, e)
        finally:
            q.CGEventSetDoubleValueField(e, F_SWIPE_DIRECTION, SWIPE_NONE)
            q.CGEventSetDoubleValueField(e, F_PHASE_A, PHASE_ENDED)
            _post(q, e)
    finally:
        _lock.release()
    return True
