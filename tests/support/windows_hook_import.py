"""Import ``core.mouse_hook_windows`` from a test on any platform.

The module binds Win32 entry points at import time (``from ctypes import
windll``), so importing it anywhere but Windows raises. Off Windows it is
imported here against a stub: only module-level declarations touch ``windll``,
and they bind it into the module's own globals, so the stub is removed again
as soon as the import completes.

Every test module that needs the Windows hook must call this for itself.
Leaning on another module having already imported it makes a test's pass/skip
status depend on what else pytest happened to collect -- which is exactly how
the ``byref()`` misuse in ``WindowsScrollAttributionTests`` sat unnoticed:
those tests skipped in isolation and only ran once an unrelated file had
imported the module first.
"""

import ctypes
import importlib


class _Win32Stub:
    def __init__(self):
        self.restype = None
        self.argtypes = None
        self.result = 0

    def __call__(self, *args):
        return self.result


class _Win32Lib:
    def __getattr__(self, name):
        stub = _Win32Stub()
        setattr(self, name, stub)
        return stub


class _WinDllStub:
    def __getattr__(self, name):
        lib = _Win32Lib()
        setattr(self, name, lib)
        return lib


def import_windows_hook():
    """Return ``core.mouse_hook_windows``, importing it if it is not loaded."""
    had_windll = hasattr(ctypes, "windll")
    if not had_windll:
        ctypes.windll = _WinDllStub()
    try:
        return importlib.import_module("core.mouse_hook_windows")
    finally:
        if not had_windll:
            del ctypes.windll
