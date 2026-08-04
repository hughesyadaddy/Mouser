"""ctypes binding for ``mouser_hook.dll`` (see ``native/win/mouser_hook.c``).

The DLL owns Mouser's ``WH_MOUSE_LL`` procedure so that no system mouse event
ever waits on the Python GIL. This module finds it, checks it agrees with
:mod:`core.native_hook_filter` on the ABI, and wraps the handful of exports
the Windows hook uses.

:func:`NativeHookFilter.load` returns ``None`` for every failure -- missing
DLL, wrong architecture, ABI mismatch, struct-size disagreement. The caller
then keeps its Python procedure, which works exactly as it did before. That
fallback is the reason this can ship without a build step becoming mandatory.

Importable on any platform (the Windows-only ``ctypes.WinDLL`` lookup happens
inside ``load``) so the path resolution and event decoding stay testable.
"""

from __future__ import annotations

import ctypes
import os
import sys

from core.native_hook_filter import ABI_VERSION, EVENT_NAMES, EVT_NONE

DLL_NAME = "mouser_hook_x64.dll"


class NativeHookEvent(ctypes.Structure):
    """One queued event. Layout must match ``MouserHookEvent`` in the C source
    -- the 64-bit member leads so neither compiler inserts padding."""

    _fields_ = [
        ("extra_info", ctypes.c_uint64),
        ("message", ctypes.c_uint32),
        ("mouse_data", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("event_code", ctypes.c_uint32),
        ("blocked", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]

    @property
    def event_type(self):
        """The ``MouseEvent`` name, or None for a debug-only mirror."""
        if self.event_code == EVT_NONE:
            return None
        return EVENT_NAMES.get(self.event_code)


def candidate_paths():
    """Where the DLL may live, most specific first.

    A PyInstaller build unpacks it into ``sys._MEIPASS``; a source checkout
    keeps it next to the C source it was built from.
    """
    paths = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        paths.append(os.path.join(bundle_dir, DLL_NAME))
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths.append(os.path.join(repo_root, "native", "win", DLL_NAME))
    override = os.environ.get("MOUSER_HOOK_DLL", "").strip()
    if override:
        paths.insert(0, override)
    return paths


def _resolve_dll_path():
    for path in candidate_paths():
        if os.path.isfile(path):
            return path
    return None


class NativeHookFilter:
    """Thin, typed wrapper over the DLL's exports.

    The DLL's state -- the hook, its thread, the filter, the event ring -- is
    process-global, so this wraps a singleton however many instances exist.
    Mouser runs one MouseHook per process, which is what makes that fine.
    """

    def __init__(self, lib, path):
        self._lib = lib
        self.path = path

    # ── loading ───────────────────────────────────────────────────

    @classmethod
    def load(cls):
        """Return a ready filter, or None when the native path is unavailable.

        Never raises: every failure here means "keep using the Python
        procedure", and a hook that works slowly beats no hook at all.
        """
        path = _resolve_dll_path()
        if path is None:
            return None
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return None
        try:
            lib = loader(path)
            cls._declare(lib)
            abi = lib.mouser_hook_abi_version()
            if abi != ABI_VERSION:
                print(
                    f"[MouseHook] Ignoring {path}: ABI {abi}, "
                    f"expected {ABI_VERSION} -- rebuild native/win"
                )
                return None
            size = lib.mouser_hook_event_size()
            if size != ctypes.sizeof(NativeHookEvent):
                print(
                    f"[MouseHook] Ignoring {path}: event struct is {size} "
                    f"bytes, expected {ctypes.sizeof(NativeHookEvent)}"
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
        """Pin every signature. Left implicit, ctypes would default to int
        returns and truncate the HWND we hand to set_inject_target."""
        lib.mouser_hook_abi_version.restype = ctypes.c_uint32
        lib.mouser_hook_abi_version.argtypes = []
        lib.mouser_hook_event_size.restype = ctypes.c_uint32
        lib.mouser_hook_event_size.argtypes = []
        lib.mouser_hook_install.restype = ctypes.c_int
        lib.mouser_hook_install.argtypes = []
        lib.mouser_hook_uninstall.restype = ctypes.c_int
        lib.mouser_hook_uninstall.argtypes = []
        lib.mouser_hook_set_filter.restype = None
        lib.mouser_hook_set_filter.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        lib.mouser_hook_set_inject_target.restype = None
        lib.mouser_hook_set_inject_target.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        lib.mouser_hook_mark_logitech_wheel.restype = None
        lib.mouser_hook_mark_logitech_wheel.argtypes = []
        lib.mouser_hook_next_event.restype = ctypes.c_int
        lib.mouser_hook_next_event.argtypes = [
            ctypes.POINTER(NativeHookEvent),
            ctypes.c_uint32,
        ]
        lib.mouser_hook_take_capture_delta.restype = None
        lib.mouser_hook_take_capture_delta.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.mouser_hook_take_pending_vscroll.restype = ctypes.c_int
        lib.mouser_hook_take_pending_vscroll.argtypes = []
        lib.mouser_hook_take_pending_hscroll.restype = ctypes.c_int
        lib.mouser_hook_take_pending_hscroll.argtypes = []
        lib.mouser_hook_dropped.restype = ctypes.c_uint32
        lib.mouser_hook_dropped.argtypes = []

    # ── lifecycle ─────────────────────────────────────────────────

    def install(self) -> bool:
        """Install the hook on the DLL's own thread. Callable from anywhere."""
        return bool(self._lib.mouser_hook_install())

    def uninstall(self) -> bool:
        return bool(self._lib.mouser_hook_uninstall())

    # ── configuration ─────────────────────────────────────────────

    def set_filter(self, flags: int, interest_mask: int, block_mask: int):
        self._lib.mouser_hook_set_filter(
            ctypes.c_uint32(flags),
            ctypes.c_uint32(interest_mask),
            ctypes.c_uint32(block_mask),
        )

    def set_inject_target(self, hwnd, vscroll_msg: int, hscroll_msg: int):
        """Point scroll-invert injection at the Raw Input window. The native
        side only accumulates the delta and posts; the injection itself stays
        in Python, off the hook path."""
        self._lib.mouser_hook_set_inject_target(
            ctypes.c_void_p(int(hwnd) if hwnd else 0),
            ctypes.c_uint32(vscroll_msg),
            ctypes.c_uint32(hscroll_msg),
        )

    def mark_logitech_wheel(self):
        """Record that WM_INPUT just saw a Logitech wheel report -- the native
        procedure's whole basis for attributing a wheel message to the mouse
        the invert toggle applies to."""
        self._lib.mouser_hook_mark_logitech_wheel()

    # ── event queue ───────────────────────────────────────────────

    def next_event(self, event: NativeHookEvent, timeout_ms: int) -> bool:
        """Block up to ``timeout_ms`` for a queued event.

        ctypes releases the GIL around the call, so the waiting drain thread
        holds nothing the hook could ever need.
        """
        return bool(self._lib.mouser_hook_next_event(
            ctypes.byref(event), ctypes.c_uint32(timeout_ms)
        ))

    def take_capture_delta(self):
        """``(dx, dy)`` accumulated since the last call, and reset to zero.

        Screen-pixel deltas, diffed from successive ``MSLLHOOKSTRUCT.pt``
        values by the procedure. Drained once per gesture, off the input path.
        """
        dx = ctypes.c_int(0)
        dy = ctypes.c_int(0)
        self._lib.mouser_hook_take_capture_delta(ctypes.byref(dx), ctypes.byref(dy))
        return dx.value, dy.value

    def take_pending_vscroll(self) -> int:
        return int(self._lib.mouser_hook_take_pending_vscroll())

    def take_pending_hscroll(self) -> int:
        return int(self._lib.mouser_hook_take_pending_hscroll())

    @property
    def dropped(self) -> int:
        """Events the ring had no room for. Non-zero means the drain thread
        fell behind -- the hook itself never blocks for it."""
        return int(self._lib.mouser_hook_dropped())
