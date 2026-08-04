"""Contract shared by the Python hook and the native WH_MOUSE_LL filter.

Windows calls a ``WH_MOUSE_LL`` procedure inside Mouser's process and blocks
the entire system input pipeline until it returns. A Python procedure must
take the GIL to do that, so whenever another Mouser thread is mid-work the
hook waits -- and every application's input waits with it. Measured on
tiny11: ``SendInput`` 76-187ms with Mouser running against 1.3-1.6ms without.

The fix is that the hook procedure is native (``native/win/mouser_hook.c``)
and decides from the two masks below, which Python pushes down whenever they
change. Python is entered only for events Mouser actually acts on, and never
from the procedure itself -- the native side queues them and a Python drain
thread picks them up off the input path.

This module is deliberately platform-neutral: it holds the numbering both
sides agree on, so the mask arithmetic is testable anywhere. The ctypes
binding lives in :mod:`core.native_hook_win` (Windows only).
"""

from core.mouse_hook_types import MouseEvent

# Bumped whenever the struct layout or export signatures change. The loader
# refuses a DLL that disagrees and falls back to the Python procedure, so a
# stale binary next to a new build can never be silently half-compatible.
ABI_VERSION = 1

# ── filter flags (first argument of set_filter) ────────────────────────

#: ``_should_intercept_events()`` -- a Logitech is bound and KVM focus is local.
FILTER_INTERCEPT = 1 << 0
#: The OS-layer vertical scroll-invert fallback is armed.
FILTER_VSCROLL_INVERT = 1 << 1
#: The OS-layer horizontal scroll-invert fallback is armed.
FILTER_HSCROLL_INVERT = 1 << 2
#: Mirror every non-move event to the queue so debug logging stays complete.
#: Off, the native side queues only events in the interest mask.
FILTER_DEBUG = 1 << 3

# ── event codes (``event_code`` on a queued event) ─────────────────────

EVT_NONE = 0
EVT_XBUTTON1_DOWN = 1
EVT_XBUTTON1_UP = 2
EVT_XBUTTON2_DOWN = 3
EVT_XBUTTON2_UP = 4
EVT_MIDDLE_DOWN = 5
EVT_MIDDLE_UP = 6
EVT_HSCROLL_LEFT = 7
EVT_HSCROLL_RIGHT = 8

#: Every event the native procedure can recognise, in code order.
EVENT_CODES = {
    MouseEvent.XBUTTON1_DOWN: EVT_XBUTTON1_DOWN,
    MouseEvent.XBUTTON1_UP: EVT_XBUTTON1_UP,
    MouseEvent.XBUTTON2_DOWN: EVT_XBUTTON2_DOWN,
    MouseEvent.XBUTTON2_UP: EVT_XBUTTON2_UP,
    MouseEvent.MIDDLE_DOWN: EVT_MIDDLE_DOWN,
    MouseEvent.MIDDLE_UP: EVT_MIDDLE_UP,
    MouseEvent.HSCROLL_LEFT: EVT_HSCROLL_LEFT,
    MouseEvent.HSCROLL_RIGHT: EVT_HSCROLL_RIGHT,
}

EVENT_NAMES = {code: name for name, code in EVENT_CODES.items()}

#: Horizontal wheel is the only queued event carrying a delta.
HSCROLL_EVENT_CODES = frozenset((EVT_HSCROLL_LEFT, EVT_HSCROLL_RIGHT))


def event_bit(event_type) -> int:
    """The mask bit for ``event_type``, or 0 when the native side cannot
    recognise it (gestures, HID++ buttons -- those never touch the hook)."""
    code = EVENT_CODES.get(event_type, EVT_NONE)
    return 0 if code == EVT_NONE else 1 << code


def build_mask(event_types) -> int:
    """OR the bits of every recognised event in ``event_types``."""
    mask = 0
    for event_type in event_types:
        mask |= event_bit(event_type)
    return mask


def build_filter_flags(
    *,
    intercept: bool,
    vscroll_invert: bool,
    hscroll_invert: bool,
    debug: bool,
) -> int:
    """Pack the four native decisions into the flags word."""
    flags = 0
    if intercept:
        flags |= FILTER_INTERCEPT
    if vscroll_invert:
        flags |= FILTER_VSCROLL_INVERT
    if hscroll_invert:
        flags |= FILTER_HSCROLL_INVERT
    if debug:
        flags |= FILTER_DEBUG
    return flags


def compute_filter(hook):
    """The three words the native procedure decides from, read off a hook.

    ``interest`` is what it queues for Python; ``block`` is what it swallows.
    Anything in neither mask -- every mouse move, every wheel event with no
    invert armed and no horizontal remap -- goes straight to the next hook
    without Python being involved at all.

    The scroll-invert flags mirror :meth:`_apply_vscroll_invert_fallback` and
    its horizontal twin, minus the per-event Logitech attribution the native
    side does for itself. They stay armed while KVM focus is remote, because
    scroll inversion is host-local: Deskflow forwards scroll through
    untouched and must not need to know about the setting.
    """
    physical = hook._physical_logitech_bound()
    flags = build_filter_flags(
        intercept=hook._should_intercept_events(),
        vscroll_invert=(
            physical
            and hook.invert_vscroll
            and not hook.wheel_native_invert_vertical
        ),
        hscroll_invert=(
            physical
            and hook.invert_hscroll
            and not hook.wheel_native_invert_horizontal
        ),
        debug=bool(hook.debug_mode and hook._debug_callback),
    )
    block_mask = build_mask(hook._blocked_events)
    interest_mask = build_mask(hook._callbacks) | block_mask
    return flags, interest_mask, block_mask


def describe_filter(flags: int, interest_mask: int, block_mask: int) -> str:
    """One-line human summary, for the status/debug log on every push."""
    names = [
        name
        for name, bit in (
            ("intercept", FILTER_INTERCEPT),
            ("vinvert", FILTER_VSCROLL_INVERT),
            ("hinvert", FILTER_HSCROLL_INVERT),
            ("debug", FILTER_DEBUG),
        )
        if flags & bit
    ]
    return (
        f"flags={'|'.join(names) or 'none'} "
        f"interest=0x{interest_mask:02X} block=0x{block_mask:02X}"
    )
