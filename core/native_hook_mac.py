"""Contract and ctypes binding for ``libmouser_tap.dylib``
(``native/mac/mouser_tap.m``), Mouser's native CGEventTap callback.

The macOS session event tap sits in the delivery path of every mouse event,
and macOS disables a tap whose callback stalls. A Python callback has to take
the GIL, so any busy Mouser thread stalled all mouse input on the machine and
the tap was repeatedly disabled by timeout. The dylib owns the callback
instead: it runs on its own run-loop thread, decides from the packed
flags/interest/block word Python pushes, and queues only the events Mouser
acts on for a Python drain thread that waits with the GIL released.

The event codes and filter flags extend :mod:`core.native_hook_filter` (the
Windows contract) with the macOS-only decisions the Python tap made in
``MouseHook._event_tap_callback``. :func:`decide` is that decision table in
Python; the C ``tap_decide`` must agree with it and the suite checks both.

Importable anywhere: the ``ctypes.CDLL`` lookup happens inside ``load`` and
returns ``None`` for every failure, in which case the hook keeps its Python
callback.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass

from core.mouse_hook_types import MouseEvent
from core.native_hook_filter import (
    EVENT_CODES,
    EVT_HSCROLL_LEFT,
    EVT_HSCROLL_RIGHT,
    EVT_MIDDLE_DOWN,
    EVT_MIDDLE_UP,
    EVT_NONE,
    EVT_XBUTTON1_DOWN,
    EVT_XBUTTON1_UP,
    EVT_XBUTTON2_DOWN,
    EVT_XBUTTON2_UP,
    FILTER_CAPTURE,
    FILTER_DEBUG,
    FILTER_HSCROLL_INVERT,
    FILTER_INTERCEPT,
    FILTER_VSCROLL_INVERT,
    build_filter_flags,
)

ABI_VERSION = 1

DYLIB_NAME = "libmouser_tap.dylib"

# ── macOS-only filter flags (above the shared ones) ─────────────────────

#: ``MouseHook.ignore_trackpad``: continuous (trackpad / Magic Mouse) wheel
#: events are neither remapped nor inverted.
FILTER_IGNORE_TRACKPAD = 1 << 5
#: The thumb button arrives over HID++; a leaked OS btn=6 is swallowed.
FILTER_THUMB_VIA_HID = 1 << 6
#: OS btn=6 is the Sense Panel driving gesture capture (MX Master 4 fallback).
FILTER_SENSE_PANEL = 1 << 7

# ── macOS-only event codes (above the shared ones) ──────────────────────

EVT_THUMB_DOWN = 9
EVT_THUMB_UP = 10
EVT_SENSE_PANEL_DOWN = 11
EVT_SENSE_PANEL_UP = 12

TAP_EVENT_CODES = {
    **EVENT_CODES,
    MouseEvent.THUMB_BUTTON_DOWN: EVT_THUMB_DOWN,
    MouseEvent.THUMB_BUTTON_UP: EVT_THUMB_UP,
}
TAP_EVENT_NAMES = {code: name for name, code in TAP_EVENT_CODES.items()}

MARKER_MOUSER = 0x4D4F5554
MARKER_DESKFLOW = 0x44534B46
INJECTED_MARKERS = frozenset({MARKER_MOUSER, MARKER_DESKFLOW})

BTN_MIDDLE = 2
BTN_BACK = 3
BTN_FORWARD = 4
BTN_OS_EXTRA = 6

CG_EVENT_MOUSE_MOVED = 5
CG_EVENT_OTHER_MOUSE_DOWN = 25
CG_EVENT_OTHER_MOUSE_UP = 26
CG_EVENT_OTHER_MOUSE_DRAGGED = 27
CG_EVENT_SCROLL_WHEEL = 22
CG_SCROLL_PHASE_NONE = 0
CG_SCROLL_PHASE_ENDED = 4

# ── decision outcomes ───────────────────────────────────────────────────

ACT_PASS = 0
ACT_DROP = 1
ACT_INVERT_V = 1 << 1
ACT_INVERT_H = 1 << 2
ACT_QUEUE = 1 << 3


def tap_event_bit(event_type) -> int:
    code = TAP_EVENT_CODES.get(event_type, EVT_NONE)
    return 0 if code == EVT_NONE else 1 << code


def build_tap_mask(event_types) -> int:
    mask = 0
    for event_type in event_types:
        mask |= tap_event_bit(event_type)
    return mask


def compute_tap_filter(hook):
    """``(flags, interest, block)`` for the native tap, read off a hook.

    Mirrors :func:`core.native_hook_filter.compute_filter` with the macOS
    specifics of ``MouseHook._event_tap_callback``: the capture flag follows
    the Python tap (a directional capture drops motion whenever a gesture is
    held with direction detection on, physical device or not -- the tap IS
    the motion source for devices that cannot stream rawXY), and the
    trackpad / thumb / Sense Panel routing bits ride along.
    """
    mapped, blocked = hook.bindings_snapshot()
    physical = hook._physical_logitech_bound()
    intercept = hook._should_intercept_events()
    flags = build_filter_flags(
        intercept=intercept,
        vscroll_invert=(
            physical and hook.invert_vscroll and not hook.wheel_native_invert_vertical
        ),
        hscroll_invert=(
            physical and hook.invert_hscroll and not hook.wheel_native_invert_horizontal
        ),
        debug=bool(hook.debug_mode and hook._debug_callback),
        capture=bool(intercept and hook._gesture_active and hook._gesture_direction_enabled),
    )
    if hook.ignore_trackpad:
        flags |= FILTER_IGNORE_TRACKPAD
    if hook._thumb_button_via_hid:
        flags |= FILTER_THUMB_VIA_HID
    if hook._gesture_via_sense_panel:
        flags |= FILTER_SENSE_PANEL
    block_mask = build_tap_mask(blocked)
    interest_mask = build_tap_mask(mapped) | block_mask
    return flags, interest_mask, block_mask


def describe_tap_filter(flags: int, interest_mask: int, block_mask: int) -> str:
    names = [
        name
        for name, bit in (
            ("intercept", FILTER_INTERCEPT),
            ("vinvert", FILTER_VSCROLL_INVERT),
            ("hinvert", FILTER_HSCROLL_INVERT),
            ("debug", FILTER_DEBUG),
            ("capture", FILTER_CAPTURE),
            ("no-trackpad", FILTER_IGNORE_TRACKPAD),
            ("thumb-hid", FILTER_THUMB_VIA_HID),
            ("sense-panel", FILTER_SENSE_PANEL),
        )
        if flags & bit
    ]
    return (
        f"flags={'|'.join(names) or 'none'} "
        f"interest=0x{interest_mask:04X} block=0x{block_mask:04X}"
    )


# ── the decision table, in Python ───────────────────────────────────────


@dataclass(frozen=True)
class TapFields:
    """What the tap callback reads off a CGEvent before deciding."""

    event_type: int
    user_data: int = 0
    button: int = 0
    is_continuous: int = 0
    momentum_phase: int = 0
    scroll_phase: int = 0
    h_fixed: int = 0
    v_fixed: int = 0
    recent_logitech_wheel: int = 0


@dataclass(frozen=True)
class TapDecision:
    action: int = ACT_PASS
    event_code: int = EVT_NONE
    blocked: int = 0


def _scroll_targets_logitech(f: TapFields, flags: int) -> bool:
    if flags & FILTER_IGNORE_TRACKPAD and f.is_continuous:
        return False
    if f.momentum_phase:
        return False
    if f.scroll_phase not in (CG_SCROLL_PHASE_NONE, CG_SCROLL_PHASE_ENDED):
        return False
    return bool(f.recent_logitech_wheel)


def _invert_actions(f: TapFields, flags: int) -> int:
    if not flags & (FILTER_VSCROLL_INVERT | FILTER_HSCROLL_INVERT):
        return 0
    if not _scroll_targets_logitech(f, flags):
        return 0
    action = 0
    if flags & FILTER_VSCROLL_INVERT:
        action |= ACT_INVERT_V
    if flags & FILTER_HSCROLL_INVERT:
        action |= ACT_INVERT_H
    return action


def decide(flags: int, interest: int, block: int, f: TapFields) -> TapDecision:
    """The early-return set of ``MouseHook._event_tap_callback``, in order,
    with the down/up pairing left to the caller. ``tap_decide`` in
    ``mouser_tap.m`` must produce the same result for the same inputs."""
    debug = bool(flags & FILTER_DEBUG)
    t = f.event_type

    if t in (CG_EVENT_MOUSE_MOVED, CG_EVENT_OTHER_MOUSE_DRAGGED):
        return TapDecision(ACT_DROP if flags & FILTER_CAPTURE else ACT_PASS)

    if f.user_data in INJECTED_MARKERS:
        return TapDecision()

    if not flags & FILTER_INTERCEPT:
        if t == CG_EVENT_SCROLL_WHEEL:
            return TapDecision(_invert_actions(f, flags))
        return TapDecision()

    if t == CG_EVENT_SCROLL_WHEEL:
        if flags & FILTER_IGNORE_TRACKPAD and f.is_continuous:
            return TapDecision()
        action = 0
        code = EVT_NONE
        blocked = 0
        if f.h_fixed != 0:
            code = EVT_HSCROLL_RIGHT if f.h_fixed > 0 else EVT_HSCROLL_LEFT
            blocked = int(bool(block & (1 << code)))
            if interest & (1 << code) or debug:
                action |= ACT_QUEUE
            if blocked:
                return TapDecision(action | ACT_DROP, code, blocked)
        elif debug:
            action |= ACT_QUEUE
        return TapDecision(action | _invert_actions(f, flags), code, blocked)

    if t not in (CG_EVENT_OTHER_MOUSE_DOWN, CG_EVENT_OTHER_MOUSE_UP):
        return TapDecision()

    down = t == CG_EVENT_OTHER_MOUSE_DOWN
    action = ACT_QUEUE if debug else 0
    if f.button == BTN_MIDDLE:
        code = EVT_MIDDLE_DOWN if down else EVT_MIDDLE_UP
    elif f.button == BTN_BACK:
        code = EVT_XBUTTON1_DOWN if down else EVT_XBUTTON1_UP
    elif f.button == BTN_FORWARD:
        code = EVT_XBUTTON2_DOWN if down else EVT_XBUTTON2_UP
    elif f.button == BTN_OS_EXTRA:
        if flags & FILTER_SENSE_PANEL:
            code = EVT_SENSE_PANEL_DOWN if down else EVT_SENSE_PANEL_UP
            return TapDecision(action | ACT_QUEUE | ACT_DROP, code, 1)
        if flags & FILTER_THUMB_VIA_HID:
            return TapDecision(action | ACT_DROP, EVT_NONE, 1)
        code = EVT_THUMB_DOWN if down else EVT_THUMB_UP
    else:
        return TapDecision(action)

    blocked = int(bool(block & (1 << code)))
    if interest & (1 << code) or debug:
        action |= ACT_QUEUE
    if blocked:
        action |= ACT_DROP
    return TapDecision(action, code, blocked)


# ── ctypes binding ──────────────────────────────────────────────────────


class NativeTapEvent(ctypes.Structure):
    """One queued event; layout must match ``MouserTapEvent``."""

    _fields_ = [
        ("user_data", ctypes.c_int64),
        ("event_type", ctypes.c_uint32),
        ("event_code", ctypes.c_uint32),
        ("button", ctypes.c_int32),
        ("h_fixed", ctypes.c_int32),
        ("v_fixed", ctypes.c_int32),
        ("blocked", ctypes.c_uint32),
    ]


class _CTapFields(ctypes.Structure):
    _fields_ = [
        ("event_type", ctypes.c_uint32),
        ("user_data", ctypes.c_int64),
        ("button", ctypes.c_int32),
        ("is_continuous", ctypes.c_int32),
        ("momentum_phase", ctypes.c_int32),
        ("scroll_phase", ctypes.c_int32),
        ("h_fixed", ctypes.c_int32),
        ("v_fixed", ctypes.c_int32),
        ("recent_logitech_wheel", ctypes.c_int32),
    ]


class _CTapDecision(ctypes.Structure):
    _fields_ = [
        ("action", ctypes.c_uint32),
        ("event_code", ctypes.c_uint32),
        ("blocked", ctypes.c_uint32),
    ]


def candidate_paths():
    paths = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        paths.append(os.path.join(bundle_dir, DYLIB_NAME))
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths.append(os.path.join(repo_root, "native", "mac", DYLIB_NAME))
    override = os.environ.get("MOUSER_TAP_DYLIB", "").strip()
    if override:
        paths.insert(0, override)
    return paths


def _resolve_dylib_path():
    for path in candidate_paths():
        if os.path.isfile(path):
            return path
    return None


class NativeTap:
    """Typed wrapper over the dylib's exports. The dylib's state is
    process-global, so this wraps a singleton however many instances exist."""

    def __init__(self, lib, path):
        self._lib = lib
        self.path = path

    @classmethod
    def load(cls, path=None):
        """A ready tap, or ``None`` when the native path is unavailable.
        Never raises: every failure means "keep the Python callback"."""
        if sys.platform != "darwin":
            return None
        path = path or _resolve_dylib_path()
        if path is None:
            return None
        try:
            lib = ctypes.CDLL(path)
            cls._declare(lib)
            abi = lib.mouser_tap_abi_version()
            if abi != ABI_VERSION:
                print(
                    f"[MouseHook] Ignoring {path}: ABI {abi}, expected "
                    f"{ABI_VERSION} -- rebuild native/mac"
                )
                return None
            for name, size in (
                ("mouser_tap_event_size", ctypes.sizeof(NativeTapEvent)),
                ("mouser_tap_fields_size", ctypes.sizeof(_CTapFields)),
                ("mouser_tap_decision_size", ctypes.sizeof(_CTapDecision)),
            ):
                got = getattr(lib, name)()
                if got != size:
                    print(
                        f"[MouseHook] Ignoring {path}: {name} is {got} bytes, "
                        f"expected {size}"
                    )
                    return None
        except OSError as exc:
            print(f"[MouseHook] Could not load {path}: {exc}")
            return None
        except AttributeError as exc:
            print(f"[MouseHook] {path} is missing an export: {exc}")
            return None
        return cls(lib, path)

    @staticmethod
    def _declare(lib):
        for name in (
            "mouser_tap_abi_version",
            "mouser_tap_event_size",
            "mouser_tap_fields_size",
            "mouser_tap_decision_size",
            "mouser_tap_dropped",
            "mouser_tap_reenabled",
        ):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_uint32
            fn.argtypes = []
        for name in (
            "mouser_tap_start",
            "mouser_tap_stop",
            "mouser_tap_is_enabled",
            "mouser_tap_hid_monitor_open",
        ):
            fn = getattr(lib, name)
            fn.restype = ctypes.c_int
            fn.argtypes = []
        lib.mouser_tap_set_enabled.restype = None
        lib.mouser_tap_set_enabled.argtypes = [ctypes.c_int]
        lib.mouser_tap_set_filter.restype = None
        lib.mouser_tap_set_filter.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        lib.mouser_tap_next_event.restype = ctypes.c_int
        lib.mouser_tap_next_event.argtypes = [
            ctypes.POINTER(NativeTapEvent),
            ctypes.c_uint32,
        ]
        lib.mouser_tap_take_capture_delta.restype = None
        lib.mouser_tap_take_capture_delta.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.mouser_tap_decide.restype = None
        lib.mouser_tap_decide.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_CTapFields),
            ctypes.POINTER(_CTapDecision),
        ]

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> bool:
        return bool(self._lib.mouser_tap_start())

    def stop(self) -> bool:
        return bool(self._lib.mouser_tap_stop())

    def set_enabled(self, enabled: bool):
        self._lib.mouser_tap_set_enabled(ctypes.c_int(1 if enabled else 0))

    @property
    def enabled(self) -> bool:
        return bool(self._lib.mouser_tap_is_enabled())

    # ── configuration ─────────────────────────────────────────────

    def set_filter(self, flags: int, interest_mask: int, block_mask: int):
        self._lib.mouser_tap_set_filter(
            ctypes.c_uint32(flags),
            ctypes.c_uint32(interest_mask),
            ctypes.c_uint32(block_mask),
        )

    # ── event queue ───────────────────────────────────────────────

    def next_event(self, event: NativeTapEvent, timeout_ms: int) -> bool:
        """Block up to ``timeout_ms`` for a queued event; ctypes releases the
        GIL for the wait, so the drain thread holds nothing the tap needs."""
        return bool(self._lib.mouser_tap_next_event(
            ctypes.byref(event), ctypes.c_uint32(timeout_ms)
        ))

    def take_capture_delta(self):
        dx = ctypes.c_int(0)
        dy = ctypes.c_int(0)
        self._lib.mouser_tap_take_capture_delta(ctypes.byref(dx), ctypes.byref(dy))
        return dx.value, dy.value

    @property
    def dropped(self) -> int:
        return int(self._lib.mouser_tap_dropped())

    @property
    def reenabled(self) -> int:
        """Times the tap was disabled by the system and re-enabled."""
        return int(self._lib.mouser_tap_reenabled())

    @property
    def hid_monitor_open(self) -> bool:
        return bool(self._lib.mouser_tap_hid_monitor_open())

    def decide(self, flags: int, interest: int, block: int, f: TapFields) -> TapDecision:
        """Run the C decision table (tests only)."""
        c_fields = _CTapFields(
            f.event_type, f.user_data, f.button, f.is_continuous,
            f.momentum_phase, f.scroll_phase, f.h_fixed, f.v_fixed,
            f.recent_logitech_wheel,
        )
        out = _CTapDecision()
        self._lib.mouser_tap_decide(
            ctypes.c_uint32(flags), ctypes.c_uint32(interest), ctypes.c_uint32(block),
            ctypes.byref(c_fields), ctypes.byref(out),
        )
        return TapDecision(int(out.action), int(out.event_code), int(out.blocked))
