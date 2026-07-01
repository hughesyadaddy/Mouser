"""macOS IOHID wheel monitor for Logitech scroll attribution.

CGEventTap does not expose which HID device produced a scroll event. This
module registers an IOHIDManager value callback on Logitech pointing devices
and records recent wheel motion so the event tap can correlate in time.

Bindings are isolated here (also used by ``hid_gesture`` IOKit paths) so
``mouse_hook_macos`` stays focused on Quartz hook logic.
"""

from __future__ import annotations

import sys
import time

from core.mouse_hook_types import LOGI_VENDOR_ID, LOGITECH_SCROLL_RECENT_S

_HID_PAGE_GENERIC_DESKTOP = 0x01
_HID_USAGE_MOUSE = 0x02
_HID_USAGE_WHEEL = 0x38
_HID_PAGE_CONSUMER = 0x0C
_HID_USAGE_AC_PAN = 0x0238

SCROLL_MONITOR_AVAILABLE = False

if sys.platform == "darwin":
    try:
        import ctypes
        from ctypes import POINTER, byref, c_int, c_long, c_uint32, c_void_p

        _cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

        _cf.CFNumberCreate.argtypes = [c_void_p, c_int, c_void_p]
        _cf.CFNumberCreate.restype = c_void_p
        _cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
        _cf.CFStringCreateWithCString.restype = c_void_p
        _cf.CFDictionaryCreate.argtypes = [
            c_void_p,
            POINTER(c_void_p),
            POINTER(c_void_p),
            c_long,
            c_void_p,
            c_void_p,
        ]
        _cf.CFDictionaryCreate.restype = c_void_p
        _cf.CFRelease.argtypes = [c_void_p]
        _cf.CFRunLoopGetCurrent.argtypes = []
        _cf.CFRunLoopGetCurrent.restype = c_void_p

        _iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_int]
        _iokit.IOHIDManagerCreate.restype = c_void_p
        _iokit.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
        _iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_int]
        _iokit.IOHIDManagerOpen.restype = c_int
        _iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
        _iokit.IOHIDManagerUnscheduleFromRunLoop.argtypes = [
            c_void_p,
            c_void_p,
            c_void_p,
        ]
        _iokit.IOHIDManagerClose.argtypes = [c_void_p, c_int]
        _IOHID_VALUE_CALLBACK = ctypes.CFUNCTYPE(
            None, c_void_p, c_int, c_void_p, c_void_p
        )
        _iokit.IOHIDManagerRegisterInputValueCallback.argtypes = [
            c_void_p,
            _IOHID_VALUE_CALLBACK,
            c_void_p,
        ]
        _iokit.IOHIDValueGetElement.argtypes = [c_void_p]
        _iokit.IOHIDValueGetElement.restype = c_void_p
        _iokit.IOHIDElementGetUsagePage.argtypes = [c_void_p]
        _iokit.IOHIDElementGetUsagePage.restype = c_uint32
        _iokit.IOHIDElementGetUsage.argtypes = [c_void_p]
        _iokit.IOHIDElementGetUsage.restype = c_uint32

        _K_CF_NUMBER_SINT32 = 3
        _K_CF_STRING_ENCODING_UTF8 = 0x08000100
        _K_CF_RUN_LOOP_DEFAULT_MODE = c_void_p.in_dll(_cf, "kCFRunLoopDefaultMode")
        SCROLL_MONITOR_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001 - optional macOS HID monitor
        print(f"[macos_iokit_scroll] IOHID monitor unavailable: {exc}")


def _release_cf(obj) -> None:
    if obj is None or not SCROLL_MONITOR_AVAILABLE:
        return
    try:
        _cf.CFRelease(obj)
    except Exception:
        pass


class LogitechScrollMonitor:
    """IOHID wheel tap: marks when a Logitech mouse wheel actually moved."""

    def __init__(self):
        self._last_wheel_monotonic = 0.0
        self._manager = None
        self._matching = None
        self._callback_ref = None

    def mark_wheel(self) -> None:
        self._last_wheel_monotonic = time.monotonic()

    def recent_wheel(self) -> bool:
        return (time.monotonic() - self._last_wheel_monotonic) < LOGITECH_SCROLL_RECENT_S

    def start(self) -> None:
        if not SCROLL_MONITOR_AVAILABLE or self._manager is not None:
            return
        keys = []
        values = []
        try:
            keys = [
                _cf.CFStringCreateWithCString(None, b"VendorID", _K_CF_STRING_ENCODING_UTF8),
                _cf.CFStringCreateWithCString(
                    None, b"PrimaryUsagePage", _K_CF_STRING_ENCODING_UTF8
                ),
                _cf.CFStringCreateWithCString(
                    None, b"PrimaryUsage", _K_CF_STRING_ENCODING_UTF8
                ),
            ]
            for number in (LOGI_VENDOR_ID, _HID_PAGE_GENERIC_DESKTOP, _HID_USAGE_MOUSE):
                slot = c_int(number)
                values.append(
                    _cf.CFNumberCreate(None, _K_CF_NUMBER_SINT32, byref(slot))
                )
            key_array = (c_void_p * len(keys))(*keys)
            val_array = (c_void_p * len(values))(*values)
            matching = _cf.CFDictionaryCreate(
                None, key_array, val_array, len(keys), None, None
            )
            manager = _iokit.IOHIDManagerCreate(None, 0)
            if manager is None:
                raise OSError("IOHIDManagerCreate failed")
            _iokit.IOHIDManagerSetDeviceMatching(manager, matching)
            if _iokit.IOHIDManagerOpen(manager, 0) != 0:
                raise OSError("IOHIDManagerOpen failed")
            loop = _cf.CFRunLoopGetCurrent()
            _iokit.IOHIDManagerScheduleWithRunLoop(
                manager, loop, _K_CF_RUN_LOOP_DEFAULT_MODE
            )

            def _on_value(context, result, sender, value):
                del context, result, sender
                if not value:
                    return
                element = _iokit.IOHIDValueGetElement(value)
                if not element:
                    return
                page = int(_iokit.IOHIDElementGetUsagePage(element))
                usage = int(_iokit.IOHIDElementGetUsage(element))
                if (page, usage) in (
                    (_HID_PAGE_GENERIC_DESKTOP, _HID_USAGE_WHEEL),
                    (_HID_PAGE_CONSUMER, _HID_USAGE_AC_PAN),
                ):
                    self.mark_wheel()

            self._callback_ref = _IOHID_VALUE_CALLBACK(_on_value)
            _iokit.IOHIDManagerRegisterInputValueCallback(
                manager, self._callback_ref, None
            )
            self._manager = manager
            self._matching = matching
        except Exception as exc:  # noqa: BLE001 - optional monitor
            print(f"[macos_iokit_scroll] monitor start failed: {exc}")
            self.stop()
        finally:
            for obj in keys + values:
                _release_cf(obj)

    def stop(self) -> None:
        manager = self._manager
        matching = self._matching
        self._manager = None
        self._matching = None
        self._callback_ref = None
        self._last_wheel_monotonic = 0.0
        if not SCROLL_MONITOR_AVAILABLE or manager is None:
            return
        try:
            loop = _cf.CFRunLoopGetCurrent()
            _iokit.IOHIDManagerUnscheduleFromRunLoop(
                manager, loop, _K_CF_RUN_LOOP_DEFAULT_MODE
            )
            _iokit.IOHIDManagerClose(manager, 0)
        except Exception:
            pass
        _release_cf(matching)
