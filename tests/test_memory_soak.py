"""Memory soak: the three per-event paths must not grow the heap.

Each case warms a path up (``WARMUP`` iterations, so lazy caches and the
bounded dispatch queue reach steady state), then drives ``N`` more and
asserts that ``gc.collect()`` leaves at most ``MAX_OBJECT_DELTA`` new
tracked objects and ``tracemalloc`` at most ``MAX_BYTES_DELTA`` more live
bytes. The paths are the ones that leaked in 2026-09 (see
``docs/memory-guards.md``):

* ``MouseHook._event_tap_callback`` -- the CGEventTap entry (Leak A);
* ``AppDetector`` activations and idle ticks (Leak B);
* ``BaseMouseHook._dispatch`` with the engine's handlers registered.

Everything runs on Linux CI against fakes. The last case is macOS-only:
it drives real ``CGEvent`` proxies through the deferred-release guard
while emulating PyObjC's leaking trampoline with ``Py_IncRef`` and
proves the guard reclaims exactly the leaked reference (retain count back
to baseline, no over-release when the trampoline is fixed or another
holder exists).
"""

import contextlib
import copy
import ctypes
import gc
import importlib.util
import os
import sys
import threading
import tracemalloc
import types
import unittest
from unittest.mock import patch

from core.config import DEFAULT_CONFIG
from core.mouse_hook_base import BaseMouseHook
from core.mouse_hook_types import MouseEvent
from tests import test_app_detector as _ad
from tests import test_mouse_hook_macos as _hook
from tests.support.fake_mouse_hook import FakeMouseHook

WARMUP = 1_000
N = 10_000
MAX_OBJECT_DELTA = 100
MAX_BYTES_DELTA = 256 * 1024


class _SoakCase(unittest.TestCase):
    """``measure(step)`` runs ``step`` WARMUP + N times and returns the
    (objects, bytes) growth of the N measured iterations."""

    def measure(self, step, *, settle=lambda: None):
        for _ in range(WARMUP):
            step()
        settle()
        gc.collect()
        tracemalloc.start()
        try:
            gc.collect()
            objects_before = len(gc.get_objects())
            bytes_before = tracemalloc.get_traced_memory()[0]
            for _ in range(N):
                step()
            settle()
            gc.collect()
            objects_after = len(gc.get_objects())
            bytes_after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        return objects_after - objects_before, bytes_after - bytes_before

    def assert_flat(self, step, *, settle=lambda: None):
        objects, nbytes = self.measure(step, settle=settle)
        self.assertLessEqual(
            objects, MAX_OBJECT_DELTA, f"{objects} new gc objects after {N} iterations"
        )
        self.assertLessEqual(
            nbytes, MAX_BYTES_DELTA, f"{nbytes} new live bytes after {N} iterations"
        )


# ----------------------------------------------------------------------
# (a) CGEventTap callback, mocked Quartz
# ----------------------------------------------------------------------

class _QuietQuartz:
    """Quartz stand-in whose functions record nothing (a MagicMock's
    call_args_list would itself be the leak under test)."""

    def __init__(self, fields):
        self.fields = fields
        self.kCGEventMouseMoved = _hook._MOVED
        self.kCGEventOtherMouseDown = _hook._OTHER_DOWN
        self.kCGEventOtherMouseUp = _hook._OTHER_UP
        self.kCGEventOtherMouseDragged = _hook._OTHER_DRAGGED
        self.kCGEventScrollWheel = _hook._SCROLL
        self.kCGMouseEventButtonNumber = _hook._F_BUTTON
        self.kCGEventSourceUserData = _hook._F_USER_DATA
        self.kCGMouseEventDeltaX = _hook._F_DX
        self.kCGMouseEventDeltaY = _hook._F_DY
        self.kCGScrollWheelEventFixedPtDeltaAxis2 = _hook._F_H_FIXED
        self.kCGScrollWheelEventFixedPtDeltaAxis1 = _hook._F_V_FIXED
        self.kCGEventTapDisabledByTimeout = 0xFFFFFFFE
        self.kCGEventTapDisabledByUserInput = 0xFFFFFFFF
        self.calls = 0

    def CGEventGetIntegerValueField(self, _event, field):
        self.calls += 1
        return self.fields.get(field, 0)

    def CGEventGetLocation(self, _event):
        self.calls += 1
        return (10.0, 20.0)

    def CGEventGetDoubleValueField(self, _event, _field):
        self.calls += 1
        return 0.0

    def CGEventTapIsEnabled(self, _tap):
        return True

    def __getattr__(self, name):
        # Any other Quartz call is a no-op that records nothing.
        def _noop(*_args, **_kwargs):
            return None

        return _noop


class TapCallbackSoak(_hook._MacOSHookCase, _SoakCase):
    def setUp(self):
        _hook._MacOSHookCase.setUp(self)
        self.quartz = _QuietQuartz(self.fields)
        self.module.Quartz = self.quartz
        # The harness imports the hook with objc.autorelease_pool as a
        # MagicMock when nothing loaded the real module first (always the
        # case on Linux); its recorded calls would read as 10 objects per
        # event. Use a pool that records nothing.
        self.enterContext(
            patch.object(
                self.module, "objc", types.SimpleNamespace(autorelease_pool=contextlib.nullcontext)
            )
        )

    def test_10k_mixed_events_do_not_grow_the_heap(self):
        hook = self._hook()
        # Blocked xbutton1: the OtherMouseDown/Up pair is swallowed (returns
        # None) and lands on the bounded dispatch queue; moves and wheel
        # ticks pass through the guard's bookkeeping (_prev_passthrough).
        # The fake event is a plain object, so the Py_DecRef branch is NOT
        # taken here; DeferredReleaseGuardSoak below covers it on macOS.
        hook.block(MouseEvent.XBUTTON1_DOWN)
        self.fields[_hook._F_BUTTON] = 3
        cg_event = object()
        sequence = (
            _hook._MOVED, _hook._MOVED, _hook._SCROLL, _hook._OTHER_DOWN,
            _hook._MOVED, _hook._OTHER_DRAGGED, _hook._OTHER_UP, _hook._MOVED,
        )
        results = {None: 0, "pass": 0}
        cursor = [0]

        def step():
            event_type = sequence[cursor[0] % len(sequence)]
            cursor[0] += 1
            result = hook._event_tap_callback(None, event_type, cg_event, None)
            results[None if result is None else "pass"] += 1

        drained = [0]

        def settle():
            # Stand in for the dispatch thread: consume what the swallowed
            # events queued (bounded at 512, so a live seat holds at most
            # that many).
            q = hook._dispatch_queue
            while not q.empty():
                q.get_nowait()
                drained[0] += 1

        self.assert_flat(step, settle=settle)
        self.assertGreater(drained[0], 0)
        self.assertGreater(results[None], 0, "no event was swallowed")
        self.assertGreater(results["pass"], results[None], "no event passed through")
        self.assertIsNotNone(hook._prev_passthrough)
        self.assertGreater(self.quartz.calls, 0)


# ----------------------------------------------------------------------
# (b) AppDetector activations + idle ticks, fake AppKit
# ----------------------------------------------------------------------

class _Counter:
    def __init__(self):
        self.count = 0
        self.last = None

    def __call__(self, exe):
        self.count += 1
        self.last = exe


class AppDetectorSoak(_SoakCase):
    setUp = _ad.AppDetectorMacOSTests.setUp
    _app = _ad.AppDetectorMacOSTests._app
    _darwin_module = _ad.AppDetectorMacOSTests._darwin_module

    def _wait_for(self, predicate, timeout=10.0):
        deadline = threading.Event()
        for _ in range(int(timeout / 0.005)):
            if predicate():
                return True
            deadline.wait(0.005)
        return predicate()

    def test_10k_activations_and_idle_ticks_do_not_grow_the_heap(self):
        center = _ad._FakeNotificationCenter()
        module, workspace = self._darwin_module(center)
        apps = [self._app(100 + i, f"app{i}", f"com.example.app{i}") for i in range(5)]
        self.focused[0] = 100
        counter = _Counter()
        detector = module.AppDetector(counter)
        detector.start()
        self.addCleanup(detector.stop)
        self.assertTrue(self._wait_for(lambda: counter.count >= 1))
        cursor = [0]
        expected = [counter.count]

        def step():
            i = cursor[0]
            cursor[0] += 1
            # Every activation switches pid, so every one is a delivery.
            center.post(_ad._ACTIVATE, apps[i % 5])
            expected[0] += 1
            # Idle ticks compare the AX pid with the current one; keep
            # them on the pid the last activation will settle on so they
            # never deliver (a real idle seat).
            self.focused[0] = 100 + (i % 5)
            detector._idle_check()

        def settle():
            self.assertTrue(
                self._wait_for(lambda: counter.count >= expected[0] and detector._events.empty()),
                f"delivered {counter.count} of {expected[0]}",
            )
            # The observer thread has popped the last item; give it the
            # few microseconds it needs to return to its queue wait.
            threading.Event().wait(0.05)

        self.assert_flat(step, settle=settle)
        self.assertEqual(workspace.frontmost_calls, 0)
        self.assertEqual(_ad._FakeRunningApp.ls_calls, 0)
        self.assertLessEqual(self.frontmost_module.cache_size(), 5)


# ----------------------------------------------------------------------
# (c) Engine handlers through BaseMouseHook._dispatch
# ----------------------------------------------------------------------

class _DispatchHook(FakeMouseHook):
    """Engine-compatible fake that keeps the real binding tables and the
    real ``_dispatch`` so the engine's handlers are what runs."""

    def __init__(self):
        super().__init__()
        self._callbacks = {}
        self._blocked_events = set()

    def register(self, event_type, callback):
        self._callbacks.setdefault(event_type, []).append(callback)

    def block(self, event_type):
        self._blocked_events.add(event_type)

    def reset_bindings(self):
        self._callbacks.clear()
        self._blocked_events.clear()

    _dispatch = BaseMouseHook._dispatch
    _emit_debug = BaseMouseHook._emit_debug
    _emit_gesture_event = BaseMouseHook._emit_gesture_event


class EngineDispatchSoak(_SoakCase):
    def _make_engine(self):
        from core.engine import Engine
        from tests.test_engine import _FakeAppDetector

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        with (
            patch("core.engine.MouseHook", _DispatchHook),
            patch("core.engine.AppDetector", _FakeAppDetector),
            patch("core.engine.load_config", return_value=cfg),
            patch("core.deskflow_integration.resolve_integration", return_value=None),
        ):
            return Engine()

    def test_10k_events_through_dispatch_do_not_grow_the_heap(self):
        engine = self._make_engine()
        hook = engine.hook
        self.assertIsInstance(hook, _DispatchHook)
        # Default profile: xbutton1/2 -> alt_tab (keyboard action), the
        # horizontal wheel -> browser back/forward (cooldown handler);
        # middle and the swipe are unmapped (the "no mapped action" and
        # gesture-unmapped branches).
        self.assertIn(MouseEvent.XBUTTON1_DOWN, hook._callbacks)
        self.assertIn(MouseEvent.HSCROLL_LEFT, hook._callbacks)
        actions = [0]

        def execute_action(_action_id):
            actions[0] += 1

        self.enterContext(patch("core.engine.execute_action", execute_action))
        sequence = (
            (MouseEvent.XBUTTON1_DOWN, None),
            (MouseEvent.HSCROLL_LEFT, 1),
            (MouseEvent.MIDDLE_DOWN, None),
            (MouseEvent.XBUTTON2_DOWN, None),
            (MouseEvent.HSCROLL_RIGHT, {"value": 1}),
            (MouseEvent.GESTURE_SWIPE_LEFT, None),
        )
        cursor = [0]

        def step():
            event_type, raw = sequence[cursor[0] % len(sequence)]
            cursor[0] += 1
            hook._dispatch(MouseEvent(event_type, raw))

        self.assert_flat(step)
        self.assertGreater(actions[0], N // 3)


# ----------------------------------------------------------------------
# (d) macOS only: real CGEvent proxies through the deferred-release guard
# ----------------------------------------------------------------------

def _real_quartz_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import objc  # noqa: F401
        import Quartz  # noqa: F401
    except Exception:  # noqa: BLE001 - PyObjC missing is a skip, not a failure
        return False
    return True


def _load_real_hook_module():
    """Import core/mouse_hook_macos.py against the real objc/Quartz under a
    private name, so the mocked copy other tests install in
    ``sys.modules['core.mouse_hook_macos']`` is neither reused nor replaced."""
    import objc
    import Quartz

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "mouse_hook_macos.py")
    spec = importlib.util.spec_from_file_location("_soak_real_mouse_hook_macos", path)
    module = importlib.util.module_from_spec(spec)
    # The module registers itself via sys.modules[__name__] at import time.
    sys.modules[spec.name] = module
    try:
        with patch.dict(sys.modules, {"objc": objc, "Quartz": Quartz}):
            spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@unittest.skipUnless(_real_quartz_available(), "needs macOS with PyObjC Quartz")
class DeferredReleaseGuardSoak(unittest.TestCase):
    """Emulates ``m_CGEventTapCallBack`` around ``_event_tap_callback``.

    Per event: build the args tuple (which owns the proxy, as the "N"
    format steals it), call the callback, ``Py_IncRef`` the result when
    emulating the leaking trampoline, drop everything, and at the next
    entry check that the guard released exactly the leaked reference: the
    raw CGEvent's retain count falls back to our own +1.
    """

    @classmethod
    def setUpClass(cls):
        import objc
        import Quartz

        cls.objc = objc
        cls.Quartz = Quartz
        cls.mh = _load_real_hook_module()
        cls.addClassCleanup(sys.modules.pop, cls.mh.__name__, None)
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRetain.argtypes = [ctypes.c_void_p]
        cf.CFRetain.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFGetRetainCount.argtypes = [ctypes.c_void_p]
        cf.CFGetRetainCount.restype = ctypes.c_long
        cls.cf = cf
        py_incref = ctypes.pythonapi.Py_IncRef
        py_incref.argtypes = [ctypes.py_object]
        py_incref.restype = None
        cls.py_incref = py_incref
        py_decref = ctypes.pythonapi.Py_DecRef
        py_decref.argtypes = [ctypes.py_object]
        py_decref.restype = None
        cls.py_decref = py_decref

    def _hook(self):
        hook = self.mh.MouseHook.__new__(self.mh.MouseHook)
        hook._prev_passthrough = None
        hook._running = True
        hook._tap_callback_body = lambda _proxy, _type, event, _refcon: event
        return hook

    def _run(self, mode, n=N):
        """mode: 'leak' (PyObjC 12.1), 'fixed' (upstream DECREF added),
        'extra' (leak plus another Python holder -> guard must skip)."""
        cf, Quartz, objc = self.cf, self.Quartz, self.objc
        hook = self._hook()
        extra_holder = []
        prev = None  # (raw CGEventRef, expect the proxy to be freed)
        reclaimed = skipped = 0
        for i in range(n):
            ev = Quartz.CGEventCreate(None)
            self.assertTrue(self.mh._is_bridge_proxy(ev), type(ev))
            ptr = objc.pyobjc_id(ev)
            cf.CFRetain(ptr)  # our own hold, so the raw event outlives the proxy
            args = (None, 5, ev, None)
            del ev
            self.assertEqual(sys.getrefcount(args[2]), 2)  # tuple + getrefcount arg
            result = hook._event_tap_callback(*args)
            self.assertIs(result, args[2])
            if mode in ("leak", "extra"):
                self.py_incref(result)  # the reference the trampoline never drops
            if mode == "extra":
                extra_holder.append(result)
            del result
            del args
            # Proxy still alive (attr [+ leak] [+ extra]): CF count = proxy + ours.
            self.assertEqual(cf.CFGetRetainCount(ptr), 2)
            if prev is not None:
                pptr, expect_freed = prev
                count = cf.CFGetRetainCount(pptr)
                if expect_freed:
                    self.assertEqual(count, 1, f"event {i - 1}: proxy not reclaimed")
                    reclaimed += 1
                else:
                    self.assertEqual(count, 2, f"event {i - 1}: freed under another holder")
                    skipped += 1
                cf.CFRelease(pptr)
            prev = (ptr, mode != "extra")
        # stop() path: the last pass-through is reclaimed once the tap is down.
        hook._drop_prev_passthrough()
        pptr, expect_freed = prev
        self.assertEqual(cf.CFGetRetainCount(pptr), 1 if expect_freed else 2)
        cf.CFRelease(pptr)
        for obj in extra_holder:
            self.py_decref(obj)  # give back the emulated leak on the held ones
        extra_holder.clear()
        gc.collect()
        return reclaimed, skipped

    def test_leaking_trampoline_every_event_is_reclaimed(self):
        reclaimed, skipped = self._run("leak")
        self.assertEqual((reclaimed, skipped), (N - 1, 0))

    def test_fixed_trampoline_guard_is_a_no_op_and_never_over_releases(self):
        reclaimed, skipped = self._run("fixed")
        self.assertEqual((reclaimed, skipped), (N - 1, 0))

    def test_another_holder_disables_the_guard_for_that_event(self):
        reclaimed, skipped = self._run("extra")
        self.assertEqual((reclaimed, skipped), (0, N - 1))

    def test_refcount_of_the_proxy_is_stable_across_events(self):
        """The guard leaves exactly one Python reference (the attribute)
        on the current pass-through, whatever happened to the previous."""
        hook = self._hook()
        Quartz = self.Quartz
        counts = set()
        for _ in range(WARMUP):
            args = (None, 5, Quartz.CGEventCreate(None), None)
            result = hook._event_tap_callback(*args)
            self.py_incref(result)
            del result, args
            counts.add(sys.getrefcount(hook._prev_passthrough))
        hook._drop_prev_passthrough()
        # attr + leaked + getrefcount's argument
        self.assertEqual(counts, {self.mh._LEAKED_PASSTHROUGH_REFCOUNT})
        self.assertIsNone(hook._prev_passthrough)


if __name__ == "__main__":
    unittest.main()
