# PyObjC: `CGEventTapCreate` callback trampoline leaks the returned `CGEventRef`

Status: to be filed against <https://github.com/ronaldoussoren/pyobjc>
(pyobjc-framework-Quartz). Verified against the **12.1** sdist on macOS 26
(Darwin 25.6.0), Python 3.13.2, arm64. Affects every version that ships this
trampoline (the code is unchanged for years).

## Summary

`Modules/_callbacks.m` `m_CGEventTapCallBack` (the C trampoline that
`Quartz.CGEventTapCreate` installs around a Python callback) never releases
the callback's return value. A callback that passes the event through
(`return event`) therefore leaks one Python proxy per event, and with it
the `CFRetain` the proxy holds on the `CGEventRef` and its
`CGSEventAppendix` / `HIDEvent` payload. Returning `None` does not leak
(`None` is immortal on 3.12+).

Observed in production (Mouser, a session event tap on
`kCGEventMouseMoved|OtherMouseDragged|OtherMouse*|ScrollWheel`): after 81 h
`heap` showed **3.2 M `CGEvent` + 3.2 M `CGSEventAppendix` + 9.4 M
`HIDEvent`, ~1.8 GB**, growing at ~11 objects/s, i.e. exactly one per
pass-through callback.

## The code (pyobjc-framework-Quartz 12.1, `Modules/_callbacks.m:1152-1194`)

```objc
static CGEventRef
m_CGEventTapCallBack(CGEventTapProxy proxy, CGEventType type, CGEventRef event,
                     void* _info)
{
    PyObject* info = (PyObject*)_info;

    PyGILState_STATE state = PyGILState_Ensure();

    PyObject* py_proxy;
    PyObject* py_type;
    PyObject* py_event;

    py_proxy = PyObjC_ObjCToPython(@encode(CGEventTapProxy), &proxy);
    if (py_proxy == NULL) {
        PyObjCErr_ToObjCWithGILState(&state);
    }

    py_type = PyObjC_ObjCToPython(@encode(CGEventType), &type);
    if (py_type == NULL) {
        Py_DECREF(py_proxy);
        PyObjCErr_ToObjCWithGILState(&state);
    }

    py_event = PyObjC_ObjCToPython(@encode(CGEventRef), &event);
    if (py_event == NULL) {
        Py_DECREF(py_proxy);
        Py_DECREF(py_type);
        PyObjCErr_ToObjCWithGILState(&state);
    }

    PyObject* result = PyObject_CallFunction(PyTuple_GetItem(info, 0), "NNNO", py_proxy,
                                             py_type, py_event, PyTuple_GetItem(info, 1));
    if (result == NULL) {
        PyObjCErr_ToObjCWithGILState(&state);
    }

    if (PyObjC_PythonToObjC(@encode(CGEventRef), result, &event) < 0) {
        PyObjCErr_ToObjCWithGILState(&state);
    }

    PyGILState_Release(state);

    return event;
}
```

* Lines 1181-1182: `"NNNO"` — the argument tuple **steals** `py_proxy`,
  `py_type` and `py_event`, so those three are correctly released when the
  call returns.
* Line 1181: `result` is a **new reference** returned by the call.
* Lines 1187-1193: `result` is converted with `PyObjC_PythonToObjC` and the
  function returns. **There is no `Py_DECREF(result)` on the success path.**
  The only `_Py_Dealloc` paths in the compiled `_callbacks*.so` are the
  error branches above, which matches the disassembly taken before the
  source was checked.

When the callback returns the same `py_event` it received (the normal
"pass the event through" idiom the CGEventTap API documents), `result` is
that proxy with one extra reference that is never dropped. The proxy's
`dealloc` would `CFRelease` the event; it never runs.

## Minimal reproduction

```python
import sys, time, Quartz

def cb(proxy, type_, event, refcon):
    return event            # pass-through, the documented idiom

tap = Quartz.CGEventTapCreate(
    Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
    Quartz.kCGEventTapOptionListenOnly,
    Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved), cb, None)
src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
Quartz.CGEventTapEnable(tap, True)
Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 60, False)  # move the mouse
```

Run for a minute while moving the mouse, then `heap <pid> | grep -E
'CGEvent|HIDEvent'`: the counts equal the number of callbacks delivered
and never go down. Replace `return event` with `return None`
(listen-only taps ignore the return) and the counts stay flat. Needs an
Accessibility / Input Monitoring grant for the interpreter, which is why
this is not in the automated test suite.

Refcount-level check without a tap (no permissions needed): a fresh proxy
from `Quartz.CGEventCreate(None)` has `sys.getrefcount(ev) == 2`
(local + argument). Holding it only in an attribute and returning it from
a function that mimics the trampoline's call leaves the attribute holder
at 3 instead of 2 once the argument tuple is gone; that extra reference is
`result`.

## Proposed fix

```objc
    if (PyObjC_PythonToObjC(@encode(CGEventRef), result, &event) < 0) {
        Py_DECREF(result);
        PyObjCErr_ToObjCWithGILState(&state);
    }
    Py_DECREF(result);

    PyGILState_Release(state);

    return event;
```

`event` is a borrowed `CGEventRef` from the window server for the duration
of the callback, so dropping the proxy after the conversion is safe: the
trampoline returns the raw pointer, not the proxy. (If the callback
returns a *different*, newly created event the caller already owns it via
the tap contract; the proxy's `CFRelease` on dealloc would then be wrong
for a fresh event only if the proxy was created with `CFRetain` semantics,
which `PyObjC_ObjCToPython` does for the incoming event and
`CGEventCreate*` results alike — so the documented idiom "return a new
event you created" would then need a `CFRetain`; worth confirming with the
maintainer, the pass-through case is unambiguous.)

## Downstream mitigation (Mouser)

Until this lands, `core/mouse_hook_macos.py` `MouseHook._drop_prev_passthrough`
keeps the last pass-through proxy in an attribute and, at the *next*
callback entry (after the trampoline has returned), calls
`Py_DecRef` on it **only** when `sys.getrefcount(attr) == 3`
(attribute + `getrefcount` argument + the leaked `result` reference).
Any other holder makes the count 4+ and the guard does nothing; with the
upstream fix the count is 2 and the guard does nothing. Releasing before
`return` is a use-after-free because the trampoline still dereferences
`result`. The real input path uses a C tap (`native/mac/mouser_tap.m`)
instead; the Python tap is a logged, degraded fallback.
