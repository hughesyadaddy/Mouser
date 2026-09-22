"""LaunchServices-free frontmost-app lookup for macOS (ctypes only).

Leak B (plan: Established findings): on macOS 26 every
``NSRunningApplication`` query (``frontmostApplication()``,
``bundleIdentifier()``) opens a GamePolicy ``NSXPCConnection`` that is never
invalidated -- ~1.9 GB over 81 h at the old 300 ms poll. This module never
touches AppKit or PyObjC:

* :func:`focused_pid` -- ``AXUIElementCreateSystemWide`` ->
  ``kAXFocusedApplicationAttribute`` -> ``AXUIElementGetPid``; falls back to
  the first on-screen window of ``CGWindowListCopyWindowInfo``. Every CF
  object is released before returning.
* :func:`bundle_id_for_pid` -- ``proc_pidpath`` -> nearest ``*.app`` ->
  ``Contents/Info.plist`` ``CFBundleIdentifier`` (plistlib), else the
  executable basename. 64-entry LRU keyed ``(pid, path, mtime)``;
  :func:`evict` drops a pid when it terminates.

Importable everywhere (tests run on Linux); the frameworks are loaded
lazily on first use and any failure yields ``None``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import sys
import threading
from collections import OrderedDict

LRU_SIZE = 64

_AX_FOCUSED_APPLICATION = b"AXFocusedApplication"
_CG_WINDOW_OWNER_PID = b"kCGWindowOwnerPID"
_CG_WINDOW_LAYER = b"kCGWindowLayer"
_kCFStringEncodingUTF8 = 0x08000100
_kCFNumberSInt32Type = 3
_kCGWindowListOptionOnScreenOnly = 1 << 0
_kCGWindowListExcludeDesktopElements = 1 << 4
_kCGNullWindowID = 0
_kAXErrorSuccess = 0
_PROC_PIDPATHINFO_MAXSIZE = 4 * 1024

_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_AS_PATH = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


class _Libs:
    """Framework handles and signatures, resolved once."""

    def __init__(self):
        self.cf = ctypes.CDLL(_CF_PATH)
        self.ax = ctypes.CDLL(_AS_PATH)
        self.cg = ctypes.CDLL(_CG_PATH)
        self.system = ctypes.CDLL(ctypes.util.find_library("System") or "libSystem.dylib")

        cf, ax, cg, system = self.cf, self.ax, self.cg, self.system
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool

        ax.AXUIElementCreateSystemWide.argtypes = []
        ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        ax.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        ax.AXUIElementGetPid.restype = ctypes.c_int

        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p

        system.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        system.proc_pidpath.restype = ctypes.c_int

        # Constant CFStrings, created once and kept for the process lifetime.
        self.ax_focused_app = cf.CFStringCreateWithCString(
            None, _AX_FOCUSED_APPLICATION, _kCFStringEncodingUTF8
        )
        self.cg_owner_pid = cf.CFStringCreateWithCString(
            None, _CG_WINDOW_OWNER_PID, _kCFStringEncodingUTF8
        )
        self.cg_layer = cf.CFStringCreateWithCString(
            None, _CG_WINDOW_LAYER, _kCFStringEncodingUTF8
        )


_libs: _Libs | None = None
_libs_failed = False
_libs_lock = threading.Lock()


def _get_libs() -> _Libs | None:
    global _libs, _libs_failed
    if _libs is not None:
        return _libs
    if _libs_failed or sys.platform != "darwin":
        return None
    with _libs_lock:
        if _libs is None and not _libs_failed:
            try:
                _libs = _Libs()
            except (OSError, AttributeError) as exc:
                _libs_failed = True
                print(f"[AppDetect] macOS frontmost lookup unavailable: {exc}")
    return _libs


# ----------------------------------------------------------------------
# Focused pid
# ----------------------------------------------------------------------

def _ax_focused_pid(libs: _Libs) -> int | None:
    """pid of the AX focused application, or None on any AXError."""
    system_wide = libs.ax.AXUIElementCreateSystemWide()
    if not system_wide:
        return None
    try:
        value = ctypes.c_void_p()
        err = libs.ax.AXUIElementCopyAttributeValue(
            system_wide, libs.ax_focused_app, ctypes.byref(value)
        )
        if err != _kAXErrorSuccess or not value.value:
            return None
        try:
            pid = ctypes.c_int(0)
            if libs.ax.AXUIElementGetPid(value, ctypes.byref(pid)) != _kAXErrorSuccess:
                return None
            return pid.value if pid.value > 0 else None
        finally:
            libs.cf.CFRelease(value)
    finally:
        libs.cf.CFRelease(system_wide)


def _window_list_pid(libs: _Libs) -> int | None:
    """Owner pid of the first on-screen normal-level window (layer 0), else of
    the first on-screen window at all. CGWindowListCopyWindowInfo orders
    front to back."""
    windows = libs.cg.CGWindowListCopyWindowInfo(
        _kCGWindowListOptionOnScreenOnly | _kCGWindowListExcludeDesktopElements,
        _kCGNullWindowID,
    )
    if not windows:
        return None
    try:
        count = libs.cf.CFArrayGetCount(windows)
        first_any = None
        for index in range(count):
            entry = libs.cf.CFArrayGetValueAtIndex(windows, index)
            if not entry:
                continue
            pid = _dict_int(libs, entry, libs.cg_owner_pid)
            if pid is None or pid <= 0:
                continue
            if first_any is None:
                first_any = pid
            layer = _dict_int(libs, entry, libs.cg_layer)
            if layer == 0:
                return pid
        return first_any
    finally:
        libs.cf.CFRelease(windows)


def _dict_int(libs: _Libs, dictionary, key) -> int | None:
    number = libs.cf.CFDictionaryGetValue(dictionary, key)  # borrowed
    if not number:
        return None
    out = ctypes.c_int32(0)
    if not libs.cf.CFNumberGetValue(number, _kCFNumberSInt32Type, ctypes.byref(out)):
        return None
    return out.value


def focused_pid() -> int | None:
    """pid of the frontmost application, or None when it cannot be read.

    AX first (needs the Accessibility grant Mouser already has for its event
    tap); CGWindowList when AX is disabled or errors. No LaunchServices, no
    NSRunningApplication, nothing retained past the call.
    """
    libs = _get_libs()
    if libs is None:
        return None
    try:
        pid = _ax_focused_pid(libs)
    except Exception as exc:  # noqa: BLE001 - ctypes boundary
        print(f"[AppDetect] AX focused-app lookup failed: {exc!r}")
        pid = None
    if pid is not None:
        return pid
    try:
        return _window_list_pid(libs)
    except Exception as exc:  # noqa: BLE001 - ctypes boundary
        print(f"[AppDetect] window-list lookup failed: {exc!r}")
        return None


# ----------------------------------------------------------------------
# pid -> bundle id
# ----------------------------------------------------------------------

_lru: OrderedDict[tuple[int, str, float], str] = OrderedDict()
_lru_lock = threading.Lock()


def _proc_pidpath(pid: int) -> str | None:
    """Executable path for *pid* via libproc, or None."""
    libs = _get_libs()
    if libs is None:
        return None
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    length = libs.system.proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE)
    if length <= 0:
        return None
    return buf.value[:length].decode("utf-8", "surrogateescape")


def _resolve_bundle_id(path: str) -> str:
    """``CFBundleIdentifier`` of the nearest enclosing ``*.app``, else the
    executable basename."""
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        if parent.endswith(".app"):
            plist = os.path.join(parent, "Contents", "Info.plist")
            try:
                with open(plist, "rb") as fh:
                    ident = plistlib.load(fh).get("CFBundleIdentifier")
            except (OSError, ValueError, plistlib.InvalidFileException):
                ident = None
            if ident:
                return str(ident)
            break
        parent = os.path.dirname(parent)
    return os.path.basename(path)


def bundle_id_for_pid(pid: int) -> str | None:
    """Stable identifier for *pid*: bundle id, else executable basename.

    Cached in a 64-entry LRU keyed ``(pid, path, mtime)`` so a pid reuse or
    an updated binary never returns a stale identifier.
    """
    path = _proc_pidpath(pid)
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    key = (pid, path, mtime)
    with _lru_lock:
        ident = _lru.get(key)
        if ident is not None:
            _lru.move_to_end(key)
            return ident
    ident = _resolve_bundle_id(path)
    with _lru_lock:
        _lru[key] = ident
        _lru.move_to_end(key)
        while len(_lru) > LRU_SIZE:
            _lru.popitem(last=False)
    return ident


def evict(pid: int) -> None:
    """Forget every cached entry for *pid* (call on app termination)."""
    with _lru_lock:
        for key in [k for k in _lru if k[0] == pid]:
            del _lru[key]


def cache_size() -> int:
    with _lru_lock:
        return len(_lru)


def clear_cache() -> None:
    with _lru_lock:
        _lru.clear()
