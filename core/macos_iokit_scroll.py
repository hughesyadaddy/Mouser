"""macOS IOHID wheel monitor for Logitech scroll attribution.

CGEventTap does not expose which HID device produced a scroll event. This
module registers an IOHIDManager value callback on Logitech pointing devices
and records recent wheel motion so the event tap can correlate in time.

Bindings are isolated here (also used by ``hid_gesture`` IOKit paths) so
``mouse_hook_macos`` stays focused on Quartz hook logic.

Lifecycle notes
---------------
``start()`` is called from the event tap on *every* scroll-wheel event, so it
must be cheap and idempotent. Every IOKit object is assigned to ``self`` the
moment it is created so that a failure at any later step releases exactly
what was created (``IOHIDManagerClose`` only if the open succeeded, a
``CFRelease`` for the manager and the matching dictionary). A failed
``IOHIDManagerOpen`` that reports a permission error (Input Monitoring
denied) is negative-cached for ``PERMISSION_RETRY_S`` so the next few
thousand wheel ticks do not each build and leak a manager.
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

# Input-value matching: only these elements reach ``_on_value``. Without this
# IOHIDManager delivers every axis/button element of every matched device.
_SCROLL_ELEMENTS = (
    (_HID_PAGE_GENERIC_DESKTOP, _HID_USAGE_WHEEL),
    (_HID_PAGE_CONSUMER, _HID_USAGE_AC_PAN),
)

# IOReturn codes IOHIDManagerOpen reports when Input Monitoring is denied.
_K_IO_RETURN_NOT_PERMITTED = 0xE00002E2
_K_IO_RETURN_NOT_PRIVILEGED = 0xE00002C1
_PERMISSION_ERRORS = frozenset({_K_IO_RETURN_NOT_PERMITTED, _K_IO_RETURN_NOT_PRIVILEGED})

#: Seconds to wait before retrying IOHIDManagerOpen after a permission error.
PERMISSION_RETRY_S = 60.0

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
        _cf.CFArrayCreate.argtypes = [c_void_p, POINTER(c_void_p), c_long, c_void_p]
        _cf.CFArrayCreate.restype = c_void_p
        _cf.CFRelease.argtypes = [c_void_p]
        _cf.CFRunLoopGetCurrent.argtypes = []
        _cf.CFRunLoopGetCurrent.restype = c_void_p

        _iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_int]
        _iokit.IOHIDManagerCreate.restype = c_void_p
        _iokit.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
        _iokit.IOHIDManagerSetInputValueMatchingMultiple.argtypes = [c_void_p, c_void_p]
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


def _cfstring(text: str):
    return _cf.CFStringCreateWithCString(
        None, text.encode("utf-8"), _K_CF_STRING_ENCODING_UTF8
    )


def _cfnumber(number: int):
    slot = c_int(number)
    return _cf.CFNumberCreate(None, _K_CF_NUMBER_SINT32, byref(slot))


def _cfdict(pairs):
    """Create a CFDictionary from ``[(key_cf, value_cf), ...]``.

    The dictionary is created with NULL callbacks (matches ``hid_gesture``),
    so it does *not* retain its keys/values; the caller owns and must keep
    them alive for as long as the dictionary lives. Returns
    ``(dictionary, refs)`` where ``refs`` are the owned key/value objects.
    """
    keys = [k for k, _ in pairs]
    values = [v for _, v in pairs]
    key_array = (c_void_p * len(keys))(*keys)
    val_array = (c_void_p * len(values))(*values)
    dictionary = _cf.CFDictionaryCreate(
        None, key_array, val_array, len(keys), None, None
    )
    return dictionary, keys + values


class LogitechScrollMonitor:
    """IOHID wheel tap: marks when a Logitech mouse wheel actually moved."""

    def __init__(self, *, retry_after_s: float = PERMISSION_RETRY_S, clock=time.monotonic):
        self._last_wheel_monotonic = 0.0
        self._manager = None
        self._matching = None
        self._matching_refs: list = []
        self._callback_ref = None
        self._opened = False
        self._scheduled = False
        self._retry_after_s = float(retry_after_s)
        self._clock = clock
        self._last_error: Exception | None = None
        self._permission_denied_until: float | None = None

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._manager is not None and self._opened

    @property
    def last_error(self) -> Exception | None:
        """Exception from the most recent failed ``start()`` (``None`` after success)."""
        return self._last_error

    @property
    def permission_denied(self) -> bool:
        """True while a permission failure is negative-cached (``start()`` is a no-op)."""
        until = self._permission_denied_until
        if until is None:
            return False
        if self._clock() < until:
            return True
        self._permission_denied_until = None
        return False

    def mark_wheel(self) -> None:
        self._last_wheel_monotonic = time.monotonic()

    def recent_wheel(self) -> bool:
        return (time.monotonic() - self._last_wheel_monotonic) < LOGITECH_SCROLL_RECENT_S

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not SCROLL_MONITOR_AVAILABLE or self._manager is not None:
            return
        if self.permission_denied:
            return
        try:
            # Device matching: Logitech pointing devices only.
            self._matching, self._matching_refs = _cfdict(
                [
                    (_cfstring("VendorID"), _cfnumber(LOGI_VENDOR_ID)),
                    (_cfstring("PrimaryUsagePage"), _cfnumber(_HID_PAGE_GENERIC_DESKTOP)),
                    (_cfstring("PrimaryUsage"), _cfnumber(_HID_USAGE_MOUSE)),
                ]
            )
            if not self._matching:
                raise OSError("CFDictionaryCreate failed")

            manager = _iokit.IOHIDManagerCreate(None, 0)
            if not manager:
                raise OSError("IOHIDManagerCreate failed")
            # Assign *before* anything else can fail so cleanup can see it.
            self._manager = manager
            _iokit.IOHIDManagerSetDeviceMatching(manager, self._matching)
            self._apply_element_matching(manager)

            res = int(_iokit.IOHIDManagerOpen(manager, 0)) & 0xFFFFFFFF
            if res != 0:
                if res in _PERMISSION_ERRORS:
                    self._permission_denied_until = self._clock() + self._retry_after_s
                    raise PermissionError(
                        f"IOHIDManagerOpen failed: 0x{res:08X} (Input Monitoring "
                        f"denied; retry in {self._retry_after_s:.0f}s)"
                    )
                raise OSError(f"IOHIDManagerOpen failed: 0x{res:08X}")
            self._opened = True

            loop = _cf.CFRunLoopGetCurrent()
            _iokit.IOHIDManagerScheduleWithRunLoop(
                manager, loop, _K_CF_RUN_LOOP_DEFAULT_MODE
            )
            self._scheduled = True

            def _on_value(context, result, sender, value):
                del context, result, sender
                if not value:
                    return
                element = _iokit.IOHIDValueGetElement(value)
                if not element:
                    return
                page = int(_iokit.IOHIDElementGetUsagePage(element))
                usage = int(_iokit.IOHIDElementGetUsage(element))
                if (page, usage) in _SCROLL_ELEMENTS:
                    self.mark_wheel()

            self._callback_ref = _IOHID_VALUE_CALLBACK(_on_value)
            _iokit.IOHIDManagerRegisterInputValueCallback(
                manager, self._callback_ref, None
            )
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - optional monitor
            self._last_error = exc
            print(f"[macos_iokit_scroll] monitor start failed: {exc}")
            self._teardown()

    def _apply_element_matching(self, manager) -> None:
        """Restrict input-value callbacks to wheel / AC Pan elements.

        The CFArray and CFDictionaries are created with NULL callbacks (same
        convention as the device-matching dictionary), so they do not retain
        their contents; the manager walks the array lazily when the callback
        is registered, so every object here must stay alive until teardown.
        They are parked in ``_matching_refs`` and released by ``_teardown``.
        """
        dicts = []
        for page, usage in _SCROLL_ELEMENTS:
            dictionary, owned = _cfdict(
                [
                    (_cfstring("UsagePage"), _cfnumber(page)),
                    (_cfstring("Usage"), _cfnumber(usage)),
                ]
            )
            self._matching_refs.extend(owned)
            if not dictionary:
                raise OSError("CFDictionaryCreate failed")
            self._matching_refs.append(dictionary)
            dicts.append(dictionary)
        dict_array = (c_void_p * len(dicts))(*dicts)
        array = _cf.CFArrayCreate(None, dict_array, len(dicts), None)
        if not array:
            raise OSError("CFArrayCreate failed")
        self._matching_refs.append(array)
        _iokit.IOHIDManagerSetInputValueMatchingMultiple(manager, array)

    def _teardown(self) -> None:
        """Release exactly what ``start()`` created, in reverse order."""
        manager = self._manager
        matching = self._matching
        refs = self._matching_refs
        opened = self._opened
        scheduled = self._scheduled
        self._manager = None
        self._matching = None
        self._matching_refs = []
        self._callback_ref = None
        self._opened = False
        self._scheduled = False
        if not SCROLL_MONITOR_AVAILABLE:
            return
        if manager is not None:
            try:
                if scheduled:
                    loop = _cf.CFRunLoopGetCurrent()
                    _iokit.IOHIDManagerUnscheduleFromRunLoop(
                        manager, loop, _K_CF_RUN_LOOP_DEFAULT_MODE
                    )
                if opened:
                    # Close releases the IOKit user-client (Mach ports); a
                    # bare CFRelease does not.
                    _iokit.IOHIDManagerClose(manager, 0)
            except Exception:
                pass
            _release_cf(manager)
        _release_cf(matching)
        for obj in refs:
            _release_cf(obj)

    def stop(self) -> None:
        self._last_wheel_monotonic = 0.0
        self._teardown()
