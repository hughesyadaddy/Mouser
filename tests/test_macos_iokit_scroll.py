"""Lifecycle tests for ``core.macos_iokit_scroll.LogitechScrollMonitor``.

Real IOKit is never touched: the module-level ``_cf``/``_iokit`` bindings are
replaced with a fake that hands out integer handles and records every
create/open/close/release so leaks show up as unbalanced counts.
"""

from __future__ import annotations

import ctypes
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from core import macos_iokit_scroll as mod

K_NOT_PERMITTED = 0xE00002E2


class FakeIOKit:
    """Minimal CF + IOKit stand-in with reference accounting."""

    def __init__(self, *, open_result=0, create_ok=True):
        self.open_result = open_result
        self.create_ok = create_ok
        self._next = 0x1000
        self.live = set()  # handles that have been created and not released
        self.managers = []
        self.opened = []
        self.closed = []
        self.released = []
        self.scheduled = []
        self.unscheduled = []
        self.input_matching = []
        self.callbacks = []
        self.cf = self._make_cf()
        self.iokit = self._make_iokit()

    # -- handles -----------------------------------------------------------
    def _alloc(self):
        self._next += 8
        self.live.add(self._next)
        return self._next

    # -- CoreFoundation ----------------------------------------------------
    def _make_cf(self):
        fake = self

        class CF:
            def CFStringCreateWithCString(self, _alloc, _text, _enc):
                return fake._alloc()

            def CFNumberCreate(self, _alloc, _type, _ptr):
                return fake._alloc()

            def CFDictionaryCreate(self, _alloc, _keys, _vals, _n, _kcb, _vcb):
                return fake._alloc()

            def CFArrayCreate(self, _alloc, _vals, _n, _cb):
                return fake._alloc()

            def CFRelease(self, obj):
                assert obj in fake.live, f"double/invalid CFRelease of {obj:#x}"
                fake.live.discard(obj)
                fake.released.append(obj)

            def CFRunLoopGetCurrent(self):
                return 0xBEEF

        return CF()

    # -- IOKit -------------------------------------------------------------
    def _make_iokit(self):
        fake = self

        class IOKit:
            def IOHIDManagerCreate(self, _alloc, _opts):
                if not fake.create_ok:
                    return None
                handle = fake._alloc()
                fake.managers.append(handle)
                return handle

            def IOHIDManagerSetDeviceMatching(self, _mgr, _dict):
                pass

            def IOHIDManagerSetInputValueMatchingMultiple(self, mgr, array):
                fake.input_matching.append((mgr, array))

            def IOHIDManagerOpen(self, mgr, _opts):
                fake.opened.append(mgr)
                # Real binding declares restype c_int, so 0xE00002E2 comes
                # back negative; reproduce that.
                return ctypes.c_int(fake.open_result).value

            def IOHIDManagerScheduleWithRunLoop(self, mgr, _loop, _mode):
                fake.scheduled.append(mgr)

            def IOHIDManagerUnscheduleFromRunLoop(self, mgr, _loop, _mode):
                fake.unscheduled.append(mgr)

            def IOHIDManagerClose(self, mgr, _opts):
                fake.closed.append(mgr)

            def IOHIDManagerRegisterInputValueCallback(self, mgr, cb, _ctx):
                fake.callbacks.append((mgr, cb))

            def IOHIDValueGetElement(self, value):
                return value

            def IOHIDElementGetUsagePage(self, element):
                return element[0]

            def IOHIDElementGetUsage(self, element):
                return element[1]

        return IOKit()


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _patched(fake: FakeIOKit):
    stack = ExitStack()
    stack.enter_context(patch.object(mod, "SCROLL_MONITOR_AVAILABLE", True))
    stack.enter_context(patch.object(mod, "_cf", fake.cf, create=True))
    stack.enter_context(patch.object(mod, "_iokit", fake.iokit, create=True))
    stack.enter_context(patch.object(mod, "_K_CF_RUN_LOOP_DEFAULT_MODE", 0, create=True))
    stack.enter_context(patch.object(mod, "_K_CF_NUMBER_SINT32", 3, create=True))
    stack.enter_context(patch.object(mod, "_K_CF_STRING_ENCODING_UTF8", 0, create=True))
    stack.enter_context(patch.object(mod, "_IOHID_VALUE_CALLBACK", lambda fn: fn, create=True))
    stack.enter_context(patch.object(mod, "c_void_p", ctypes.c_void_p, create=True))
    stack.enter_context(patch.object(mod, "c_int", ctypes.c_int, create=True))
    stack.enter_context(patch.object(mod, "byref", ctypes.byref, create=True))
    return stack


class LogitechScrollMonitorLifecycleTests(unittest.TestCase):
    def test_success_path_creates_one_manager_and_keeps_it(self):
        fake = FakeIOKit()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            self.assertTrue(monitor.running)
            self.assertIsNone(monitor.last_error)
            self.assertFalse(monitor.permission_denied)
            self.assertEqual(len(fake.managers), 1)
            self.assertEqual(fake.opened, fake.managers)
            self.assertEqual(fake.scheduled, fake.managers)
            self.assertEqual(len(fake.callbacks), 1)
            self.assertEqual(fake.closed, [])
            # Alive: manager, device-matching dict + 6 key/value refs, and
            # the element-matching array + 2 dicts + 8 key/value refs (NULL
            # CF callbacks -> containers do not retain their contents).
            self.assertIn(fake.managers[0], fake.live)
            self.assertEqual(len(fake.live), 1 + (1 + 6) + (1 + 2 + 8))

            # Element matching was narrowed to the two scroll elements.
            self.assertEqual(len(fake.input_matching), 1)
            self.assertEqual(fake.input_matching[0][0], fake.managers[0])

            monitor.stop()
            self.assertFalse(monitor.running)
            self.assertEqual(fake.unscheduled, fake.managers)
            self.assertEqual(fake.closed, fake.managers)
            self.assertEqual(fake.live, set(), "stop() must release everything")

    def test_start_twice_creates_one_manager(self):
        fake = FakeIOKit()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            for _ in range(50):
                monitor.start()
            self.assertEqual(len(fake.managers), 1)
            self.assertEqual(len(fake.opened), 1)
            monitor.stop()
            self.assertEqual(fake.live, set())

    def test_open_failure_releases_everything_created(self):
        fake = FakeIOKit(open_result=0xE00002BD)  # kIOReturnError, not permission
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            self.assertFalse(monitor.running)
            self.assertIsInstance(monitor.last_error, OSError)
            self.assertFalse(monitor.permission_denied)
            self.assertEqual(len(fake.managers), 1)
            # Never opened -> never closed / unscheduled, but CFReleased.
            self.assertEqual(fake.closed, [])
            self.assertEqual(fake.unscheduled, [])
            self.assertIn(fake.managers[0], fake.released)
            self.assertEqual(fake.live, set(), f"leaked handles: {fake.live}")
            self.assertIsNone(monitor._manager)
            self.assertIsNone(monitor._matching)
            self.assertEqual(monitor._matching_refs, [])

            # Non-permission failure: next tick retries (one more manager).
            monitor.start()
            self.assertEqual(len(fake.managers), 2)
            self.assertEqual(fake.live, set())

    def test_create_failure_releases_matching_dict(self):
        fake = FakeIOKit(create_ok=False)
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            self.assertFalse(monitor.running)
            self.assertEqual(fake.managers, [])
            self.assertEqual(fake.live, set())

    def test_failure_after_open_closes_and_releases(self):
        fake = FakeIOKit()

        def boom(*_a, **_k):
            raise RuntimeError("register failed")

        fake.iokit.IOHIDManagerRegisterInputValueCallback = boom
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            self.assertFalse(monitor.running)
            self.assertEqual(fake.opened, fake.managers)
            self.assertEqual(fake.scheduled, fake.managers)
            self.assertEqual(fake.unscheduled, fake.managers)
            self.assertEqual(fake.closed, fake.managers)
            self.assertEqual(fake.live, set())

    def test_permission_denied_is_negative_cached(self):
        fake = FakeIOKit(open_result=K_NOT_PERMITTED)
        clock = _Clock()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor(retry_after_s=60.0, clock=clock)
            monitor.start()
            self.assertTrue(monitor.permission_denied)
            self.assertIsInstance(monitor.last_error, PermissionError)
            self.assertEqual(len(fake.managers), 1)
            self.assertEqual(fake.live, set())

            # Scroll ticks within the TTL: no new manager, no open attempt.
            for _ in range(1000):
                clock.now += 0.01
                monitor.start()
            self.assertEqual(len(fake.managers), 1)
            self.assertEqual(len(fake.opened), 1)
            self.assertTrue(monitor.permission_denied)

            # TTL expiry: exactly one retry.
            clock.now += 60.0
            self.assertFalse(monitor.permission_denied)
            monitor.start()
            self.assertEqual(len(fake.managers), 2)
            self.assertTrue(monitor.permission_denied)
            self.assertEqual(fake.live, set())

    def test_permission_grant_after_ttl_succeeds(self):
        fake = FakeIOKit(open_result=K_NOT_PERMITTED)
        clock = _Clock()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor(retry_after_s=60.0, clock=clock)
            monitor.start()
            self.assertTrue(monitor.permission_denied)
            fake.open_result = 0
            clock.now += 61.0
            monitor.start()
            self.assertTrue(monitor.running)
            self.assertIsNone(monitor.last_error)
            self.assertFalse(monitor.permission_denied)
            monitor.stop()
            self.assertEqual(fake.live, set())

    def test_stop_after_failure_and_double_stop_are_safe(self):
        fake = FakeIOKit(open_result=K_NOT_PERMITTED)
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            monitor.stop()
            monitor.stop()
            self.assertEqual(fake.closed, [])
            self.assertEqual(len(fake.released), len(set(fake.released)))
            self.assertEqual(fake.live, set())
            # Cache survives stop(): still no re-create.
            monitor.start()
            self.assertEqual(len(fake.managers), 1)

    def test_stop_without_start_is_noop(self):
        fake = FakeIOKit()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.stop()
            self.assertEqual(fake.released, [])

    def test_callback_marks_only_scroll_elements(self):
        fake = FakeIOKit()
        with _patched(fake):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            _mgr, callback = fake.callbacks[0]
            callback(None, 0, None, (0x01, 0x30))  # X axis
            self.assertFalse(monitor.recent_wheel())
            callback(None, 0, None, (0x01, 0x38))  # Wheel
            self.assertTrue(monitor.recent_wheel())
            monitor.stop()
            self.assertFalse(monitor.recent_wheel())
            monitor.start()
            _mgr, callback = fake.callbacks[-1]
            callback(None, 0, None, (0x0C, 0x0238))  # AC Pan
            self.assertTrue(monitor.recent_wheel())
            monitor.stop()

    def test_unavailable_monitor_is_inert(self):
        with patch.object(mod, "SCROLL_MONITOR_AVAILABLE", False):
            monitor = mod.LogitechScrollMonitor()
            monitor.start()
            monitor.stop()
            self.assertFalse(monitor.running)
            self.assertFalse(monitor.permission_denied)


if __name__ == "__main__":
    unittest.main()
