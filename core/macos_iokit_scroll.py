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
thousand wheel ticks do not each build and leak a manager; every other
failure is negative-cached for the much shorter ``FAILURE_RETRY_S`` so the
consumer's retry-while-not-running does not become a retry per wheel tick.

The matching dictionaries and array are created with the ``kCFType*``
callbacks. IOHIDDeviceClass looks the element-matching keys up with
``CFDictionaryGetValue(matching, CFSTR("UsagePage"))``; with NULL key
callbacks that lookup is pointer equality against *our* CFString and never
matches, so the filter would silently match nothing. Typed containers also
retain their contents, so each key/value is released right after insertion.
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
#: Seconds to wait before retrying ``start()`` after any other failure.
FAILURE_RETRY_S = 5.0

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
        # Exported structs, not pointers: take their addresses.
        _K_CF_TYPE_DICT_KEY_CALLBACKS = ctypes.addressof(
            ctypes.c_char.in_dll(_cf, "kCFTypeDictionaryKeyCallBacks")
        )
        _K_CF_TYPE_DICT_VALUE_CALLBACKS = ctypes.addressof(
            ctypes.c_char.in_dll(_cf, "kCFTypeDictionaryValueCallBacks")
        )
        _K_CF_TYPE_ARRAY_CALLBACKS = ctypes.addressof(
            ctypes.c_char.in_dll(_cf, "kCFTypeArrayCallBacks")
        )
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

    The dictionary uses ``kCFTypeDictionary{Key,Value}CallBacks`` so IOKit's
    ``CFDictionaryGetValue(matching, CFSTR(...))`` lookups match by string
    equality, and so the dictionary retains its contents. Ownership of every
    key/value passed in is consumed here: they are released right after
    insertion (or on failure), so the caller keeps only the dictionary.
    """
    keys = [k for k, _ in pairs]
    values = [v for _, v in pairs]
    try:
        if not all(keys) or not all(values):
            raise OSError("CFString/CFNumber creation failed")
        key_array = (c_void_p * len(keys))(*keys)
        val_array = (c_void_p * len(values))(*values)
        dictionary = _cf.CFDictionaryCreate(
            None,
            key_array,
            val_array,
            len(keys),
            _K_CF_TYPE_DICT_KEY_CALLBACKS,
            _K_CF_TYPE_DICT_VALUE_CALLBACKS,
        )
        if not dictionary:
            raise OSError("CFDictionaryCreate failed")
        return dictionary
    finally:
        for obj in keys + values:
            _release_cf(obj)


def _cfarray(items):
    """Create a retaining CFArray from CF objects, consuming the caller's
    reference to each item (released after insertion or on failure)."""
    try:
        buf = (c_void_p * len(items))(*items)
        array = _cf.CFArrayCreate(None, buf, len(items), _K_CF_TYPE_ARRAY_CALLBACKS)
        if not array:
            raise OSError("CFArrayCreate failed")
        return array
    finally:
        for obj in items:
            _release_cf(obj)


class LogitechScrollMonitor:
    """IOHID wheel tap: marks when a Logitech mouse wheel actually moved."""

    def __init__(
        self,
        *,
        retry_after_s: float = PERMISSION_RETRY_S,
        failure_retry_s: float = FAILURE_RETRY_S,
        clock=time.monotonic,
    ):
        self._last_wheel_monotonic = 0.0
        self._manager = None
        self._matching = None
        self._element_matching = None
        self._callback_ref = None
        self._opened = False
        self._scheduled = False
        # The run loop start() scheduled on; stop() may run on another thread.
        self._run_loop = None
        self._retry_after_s = float(retry_after_s)
        self._failure_retry_s = float(failure_retry_s)
        self._clock = clock
        self._last_error: Exception | None = None
        self._permission_denied_until: float | None = None
        self._failed_until: float | None = None

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

    @property
    def retry_after(self) -> float:
        """Seconds until ``start()`` will attempt IOKit again (0.0 = now).

        Non-zero while either negative cache (permission: 60 s, any other
        failure: 5 s) is live; ``start()`` is a cheap no-op in that window.
        """
        now = self._clock()
        until = max(
            (t for t in (self._permission_denied_until, self._failed_until) if t is not None),
            default=None,
        )
        if until is None:
            return 0.0
        remaining = until - now
        if remaining <= 0.0:
            self._permission_denied_until = None
            self._failed_until = None
            return 0.0
        return remaining

    def mark_wheel(self) -> None:
        self._last_wheel_monotonic = time.monotonic()

    def recent_wheel(self) -> bool:
        return (time.monotonic() - self._last_wheel_monotonic) < LOGITECH_SCROLL_RECENT_S

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not SCROLL_MONITOR_AVAILABLE or self._manager is not None:
            return
        if self.retry_after > 0.0:
            return
        try:
            # Device matching: Logitech pointing devices only.
            self._matching = _cfdict(
                [
                    (_cfstring("VendorID"), _cfnumber(LOGI_VENDOR_ID)),
                    (_cfstring("PrimaryUsagePage"), _cfnumber(_HID_PAGE_GENERIC_DESKTOP)),
                    (_cfstring("PrimaryUsage"), _cfnumber(_HID_USAGE_MOUSE)),
                ]
            )

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
            self._run_loop = loop
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
            self._failed_until = None
        except Exception as exc:  # noqa: BLE001 - optional monitor
            self._last_error = exc
            if not isinstance(exc, PermissionError):
                self._failed_until = self._clock() + self._failure_retry_s
            print(f"[macos_iokit_scroll] monitor start failed: {exc}")
            self._teardown()

    def _apply_element_matching(self, manager) -> None:
        """Restrict input-value callbacks to wheel / AC Pan elements.

        Each dictionary retains its key/value CFObjects and the array retains
        the dictionaries (``kCFType*`` callbacks), so the only reference kept
        past this call is the array itself, parked in ``_element_matching``
        and released by ``_teardown``.
        """
        dicts = []
        try:
            for page, usage in _SCROLL_ELEMENTS:
                dicts.append(
                    _cfdict(
                        [
                            (_cfstring("UsagePage"), _cfnumber(page)),
                            (_cfstring("Usage"), _cfnumber(usage)),
                        ]
                    )
                )
        except Exception:
            for obj in dicts:
                _release_cf(obj)
            raise
        self._element_matching = _cfarray(dicts)
        _iokit.IOHIDManagerSetInputValueMatchingMultiple(manager, self._element_matching)

    def _teardown(self) -> None:
        """Release exactly what ``start()`` created, in reverse order."""
        manager = self._manager
        matching = self._matching
        element_matching = self._element_matching
        opened = self._opened
        scheduled = self._scheduled
        loop = self._run_loop
        self._run_loop = None
        self._manager = None
        self._matching = None
        self._element_matching = None
        self._callback_ref = None
        self._opened = False
        self._scheduled = False
        if not SCROLL_MONITOR_AVAILABLE:
            return
        if manager is not None:
            try:
                if scheduled:
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
        _release_cf(element_matching)
        _release_cf(matching)

    def stop(self) -> None:
        self._last_wheel_monotonic = 0.0
        self._teardown()
