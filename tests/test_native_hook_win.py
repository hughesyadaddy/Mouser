"""Loading the native WH_MOUSE_LL procedure -- and refusing to.

Every rejection path here matters more than the happy one: when the DLL is
missing, stale, or built against a different struct layout, the loader must
return None so the Windows hook keeps its Python procedure. A hook that is
merely slow still remaps buttons and still fires gestures; a half-bound DLL
would corrupt both.
"""

import ctypes
import os
import re
import sys
import unittest
from unittest.mock import patch

from core import native_hook_win
from core.mouse_hook_types import MouseEvent
from core.native_hook_filter import (
    ABI_VERSION,
    EVT_HSCROLL_LEFT,
    EVT_NONE,
    EVT_XBUTTON1_DOWN,
)
from core.native_hook_win import DLL_NAME, NativeHookEvent, NativeHookFilter

EXPORTS = (
    "mouser_hook_abi_version",
    "mouser_hook_event_size",
    "mouser_hook_install",
    "mouser_hook_uninstall",
    "mouser_hook_installed",
    "mouser_hook_set_filter",
    "mouser_hook_set_inject_target",
    "mouser_hook_mark_logitech_wheel",
    "mouser_hook_next_event",
    "mouser_hook_take_pending_vscroll",
    "mouser_hook_take_pending_hscroll",
    "mouser_hook_dropped",
)


class _FakeExport:
    def __init__(self, result=0):
        self.restype = None
        self.argtypes = None
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeLib:
    """Stands in for a loaded DLL: every export assignable and callable."""

    def __init__(self, *, omit=(), results=None):
        results = results or {}
        self.path = None
        for name in EXPORTS:
            if name in omit:
                continue
            setattr(self, name, _FakeExport(results.get(name, 0)))

    def __getattr__(self, name):  # only reached for omitted exports
        raise AttributeError(name)


def _default_results():
    return {
        "mouser_hook_abi_version": ABI_VERSION,
        "mouser_hook_event_size": ctypes.sizeof(NativeHookEvent),
    }


class ExportAgreementTests(unittest.TestCase):
    """The binding and the DLL must name exactly the same functions.

    A name only present on one side surfaces as an AttributeError at load
    time, which the loader turns into "no native filter" -- the fast path
    would silently never engage. Catch it here instead.
    """

    def test_binding_declares_every_c_export(self):
        c_source = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "native",
            "win",
            "mouser_hook.c",
        )
        with open(c_source, encoding="utf-8") as handle:
            source = handle.read()
        exported = set(
            re.findall(r"^EXPORT\s+[\w ]+?\s*\*?\s*(mouser_hook_\w+)\s*\(",
                       source, re.MULTILINE)
        )
        self.assertTrue(exported, "no exports found in mouser_hook.c")

        declared = set()

        class _Recorder:
            def __getattr__(self, name):
                declared.add(name)
                return _FakeExport()

        NativeHookFilter._declare(_Recorder())

        self.assertEqual(exported, declared)

    def test_the_test_fixture_lists_the_same_exports(self):
        declared = set()

        class _Recorder:
            def __getattr__(self, name):
                declared.add(name)
                return _FakeExport()

        NativeHookFilter._declare(_Recorder())
        self.assertEqual(set(EXPORTS), declared)


class CandidatePathTests(unittest.TestCase):
    def test_source_checkout_path_is_always_offered(self):
        paths = native_hook_win.candidate_paths()
        expected = os.path.join("native", "win", DLL_NAME)
        self.assertTrue(
            any(path.endswith(expected) for path in paths),
            f"no source-tree candidate in {paths}",
        )

    def test_bundle_dir_wins_over_the_source_tree(self):
        with patch.object(sys, "_MEIPASS", os.sep + "bundle", create=True):
            paths = native_hook_win.candidate_paths()
        self.assertEqual(paths[0], os.path.join(os.sep + "bundle", DLL_NAME))

    def test_env_override_takes_precedence(self):
        override = os.path.join(os.sep + "custom", "hook.dll")
        with patch.dict(os.environ, {"MOUSER_HOOK_DLL": override}):
            paths = native_hook_win.candidate_paths()
        self.assertEqual(paths[0], override)

    def test_blank_env_override_is_ignored(self):
        with patch.dict(os.environ, {"MOUSER_HOOK_DLL": "   "}):
            paths = native_hook_win.candidate_paths()
        self.assertTrue(all(path.strip() for path in paths))


class LoadTests(unittest.TestCase):
    """``load`` returns None for every failure and never raises -- the caller
    treats None as 'keep the Python procedure'."""

    def setUp(self):
        self.dll = os.path.join(self.enter_tmp_dir(), DLL_NAME)
        with open(self.dll, "wb") as handle:
            handle.write(b"not really a dll")

    def enter_tmp_dir(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        return tmp

    def _load(self, lib, dll=None):
        with patch.object(
            native_hook_win, "_resolve_dll_path", return_value=dll or self.dll
        ), patch.object(ctypes, "WinDLL", lambda path: lib, create=True):
            return NativeHookFilter.load()

    def test_missing_dll_returns_none(self):
        with patch.object(native_hook_win, "_resolve_dll_path", return_value=None):
            self.assertIsNone(NativeHookFilter.load())

    def test_successful_load_declares_every_signature(self):
        lib = _FakeLib(results=_default_results())
        native = self._load(lib)

        self.assertIsNotNone(native)
        self.assertEqual(native.path, self.dll)
        for name in EXPORTS:
            with self.subTest(export=name):
                export = getattr(lib, name)
                self.assertIsNotNone(
                    export.argtypes, f"{name} left argtypes implicit"
                )

    def test_abi_mismatch_is_refused(self):
        results = _default_results()
        results["mouser_hook_abi_version"] = ABI_VERSION + 1
        self.assertIsNone(self._load(_FakeLib(results=results)))

    def test_struct_size_mismatch_is_refused(self):
        results = _default_results()
        results["mouser_hook_event_size"] = ctypes.sizeof(NativeHookEvent) + 8
        self.assertIsNone(self._load(_FakeLib(results=results)))

    def test_missing_export_is_refused(self):
        lib = _FakeLib(omit=("mouser_hook_dropped",), results=_default_results())
        self.assertIsNone(self._load(lib))

    def test_os_error_from_the_loader_is_refused(self):
        def boom(_path):
            raise OSError("%1 is not a valid Win32 application")

        with patch.object(
            native_hook_win, "_resolve_dll_path", return_value=self.dll
        ), patch.object(ctypes, "WinDLL", boom, create=True):
            self.assertIsNone(NativeHookFilter.load())

    def test_no_windll_on_this_platform_returns_none(self):
        """Importable everywhere; loadable only on Windows."""
        with patch.object(
            native_hook_win, "_resolve_dll_path", return_value=self.dll
        ), patch.object(ctypes, "WinDLL", None, create=True):
            self.assertIsNone(NativeHookFilter.load())


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.lib = _FakeLib(results=_default_results())
        self.native = NativeHookFilter(self.lib, "fake.dll")

    def test_install_and_uninstall_report_the_c_result(self):
        self.lib.mouser_hook_install.result = 1
        self.lib.mouser_hook_uninstall.result = 0
        self.assertTrue(self.native.install())
        self.assertFalse(self.native.uninstall())

    def test_installed_reflects_the_dll(self):
        self.lib.mouser_hook_installed.result = 1
        self.assertTrue(self.native.installed)

    def test_set_filter_passes_all_three_words(self):
        self.native.set_filter(0b1011, 0x2A, 0x08)
        (flags, interest, block), = self.lib.mouser_hook_set_filter.calls
        self.assertEqual(
            [flags.value, interest.value, block.value], [0b1011, 0x2A, 0x08]
        )

    def test_set_inject_target_survives_a_null_window(self):
        """Teardown hands over hwnd 0; it must not raise on the way out."""
        self.native.set_inject_target(0, 0x8001, 0x8002)
        (hwnd, vmsg, hmsg), = self.lib.mouser_hook_set_inject_target.calls
        self.assertIsNone(hwnd.value)
        self.assertEqual([vmsg.value, hmsg.value], [0x8001, 0x8002])

    def test_set_inject_target_forwards_the_window_handle(self):
        self.native.set_inject_target(0x1234, 0x8001, 0x8002)
        (hwnd, _v, _h), = self.lib.mouser_hook_set_inject_target.calls
        self.assertEqual(hwnd.value, 0x1234)

    def test_pending_scroll_deltas_are_signed(self):
        self.lib.mouser_hook_take_pending_vscroll.result = -120
        self.lib.mouser_hook_take_pending_hscroll.result = 240
        self.assertEqual(self.native.take_pending_vscroll(), -120)
        self.assertEqual(self.native.take_pending_hscroll(), 240)

    def test_next_event_reports_whether_one_arrived(self):
        event = NativeHookEvent()
        self.lib.mouser_hook_next_event.result = 0
        self.assertFalse(self.native.next_event(event, 50))
        self.lib.mouser_hook_next_event.result = 1
        self.assertTrue(self.native.next_event(event, 50))

    def test_dropped_counts_ring_overflow(self):
        self.lib.mouser_hook_dropped.result = 7
        self.assertEqual(self.native.dropped, 7)

    def test_mark_logitech_wheel_reaches_the_dll(self):
        self.native.mark_logitech_wheel()
        self.assertEqual(len(self.lib.mouser_hook_mark_logitech_wheel.calls), 1)


class NativeHookEventTests(unittest.TestCase):
    def test_struct_has_no_padding(self):
        """The C side leads with the 64-bit member for exactly this reason;
        any padding would shift every field the drain thread reads."""
        self.assertEqual(ctypes.sizeof(NativeHookEvent), 32)

    def test_event_code_maps_to_a_mouse_event(self):
        event = NativeHookEvent()
        event.event_code = EVT_XBUTTON1_DOWN
        self.assertEqual(event.event_type, MouseEvent.XBUTTON1_DOWN)
        event.event_code = EVT_HSCROLL_LEFT
        self.assertEqual(event.event_type, MouseEvent.HSCROLL_LEFT)

    def test_debug_mirror_has_no_event_type(self):
        event = NativeHookEvent()
        event.event_code = EVT_NONE
        self.assertIsNone(event.event_type)

    def test_unknown_code_is_none_rather_than_a_crash(self):
        event = NativeHookEvent()
        event.event_code = 99
        self.assertIsNone(event.event_type)


if __name__ == "__main__":
    unittest.main()
