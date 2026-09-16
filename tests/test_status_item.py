"""Native macOS status item + Dock icon: allocate once, update in place.

Everything AppKit/PyObjC is faked so these run on any platform without a
QApplication. The scenario mirrors the harness ``window-toggle`` row: the
main window is shown and hidden repeatedly, which flips the activation policy
and fires the status-item / Dock-icon refresh callbacks each time.
"""
import contextlib
import gc
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

try:
    import main_qml
except Exception:  # pragma: no cover - env without PySide6 / project deps
    main_qml = None


class _FakeButton:
    def __init__(self):
        self._window = object()
        self.image = None
        self.target = None
        self.action = None
        self.mask = None
        self.tooltip = None

    def window(self):
        return self._window

    def setImage_(self, image):
        self.image = image

    def setToolTip_(self, text):
        self.tooltip = text

    def setTarget_(self, target):
        self.target = target

    def setAction_(self, action):
        self.action = action

    def sendActionOn_(self, mask):
        self.mask = mask


class _FakeStatusItem:
    def __init__(self, bar, reattach_on_visible_toggle):
        self._bar = bar
        self._button = _FakeButton()
        self._reattach = reattach_on_visible_toggle
        self.visible_calls = []

    def button(self):
        return self._button

    def detach(self):
        self._button._window = None

    def setVisible_(self, flag):
        self.visible_calls.append(flag)
        if flag and self._reattach:
            self._button._window = object()


class _FakeStatusBar:
    def __init__(self, reattach_on_visible_toggle=False):
        self.created = []
        self.removed = []
        self._reattach = reattach_on_visible_toggle

    def statusItemWithLength_(self, _length):
        item = _FakeStatusItem(self, self._reattach)
        self.created.append(item)
        return item

    def removeStatusItem_(self, item):
        self.removed.append(item)
        item.detach()


class _FakeNSImage:
    instances = 0      # every NSImage (status item + Dock)
    from_data = 0      # status-item images decoded from PNG bytes

    def __init__(self):
        type(self).instances += 1
        self.template = None
        self.size = None

    def isValid(self):
        return True

    def setTemplate_(self, flag):
        self.template = flag

    def setSize_(self, size):
        self.size = size

    def isEqual_(self, other):
        return self is other


class _FakeNSImageFactory:
    def alloc(self):
        return self

    def initWithData_(self, _data):
        image = _FakeNSImage()
        _FakeNSImage.from_data += 1
        return image

    def initWithContentsOfFile_(self, _path):
        image = _FakeNSImage()
        image.size = lambda: SimpleNamespace(width=1024.0, height=1024.0)
        return image


class _FakeNSApp:
    def __init__(self, status_bar, detach_on_policy_change=False):
        self._status_bar = status_bar
        self._detach = detach_on_policy_change
        self.policies = []
        self.icon = None
        self.icon_sets = 0

    def setActivationPolicy_(self, policy):
        self.policies.append(policy)
        if self._detach:
            for item in self._status_bar.created:
                item.detach()

    def applicationIconImage(self):
        return self.icon

    def setApplicationIconImage_(self, image):
        self.icon = image
        self.icon_sets += 1


class _FakeTarget:
    instances = 0

    @classmethod
    def alloc(cls):
        return cls

    @classmethod
    def init(cls):
        cls.instances += 1
        return cls()

    def __init__(self):
        self.handlers = {}

    def setPyHandlers_(self, handlers):
        self.handlers = handlers


class _Menu:
    """Weak-referenceable stand-in for the tray QMenu."""

    def __init__(self):
        self.popups = 0

    def popup(self, _pos):
        self.popups += 1


def _fake_appkit(status_bar, nsapp):
    return SimpleNamespace(
        NSApplicationActivationPolicyRegular="regular",
        NSApplicationActivationPolicyAccessory="accessory",
        NSStatusBar=SimpleNamespace(systemStatusBar=lambda: status_bar),
        NSImage=_FakeNSImageFactory(),
        NSMakeSize=lambda w, h: (w, h),
        NSEventMaskLeftMouseDown=1,
        NSEventMaskRightMouseDown=2,
        NSEventMaskOtherMouseDown=4,
        NSApp=nsapp,
    )


@unittest.skipIf(main_qml is None, "main_qml / PySide6 not available")
class _StatusItemTestCase(unittest.TestCase):
    _global_names = (
        "_MACOS_APPKIT",
        "_MACOS_ACTIVATION_POLICY_REGULAR",
        "_MACOS_NATIVE_STATUS_ITEM",
        "_MACOS_NATIVE_STATUS_TARGET",
        "_MACOS_STATUS_ITEM_NSIMAGE",
        "_MACOS_STATUS_ITEM_PARAMS",
        "_MACOS_STATUS_ITEM_REINSTALL_GENERATION",
        "_MACOS_DOCK_ICON_NSIMAGE",
    )

    def setUp(self):
        for name in self._global_names:
            self.addCleanup(setattr, main_qml, name, getattr(main_qml, name))
        main_qml._MACOS_ACTIVATION_POLICY_REGULAR = None
        main_qml._MACOS_NATIVE_STATUS_ITEM = None
        main_qml._MACOS_NATIVE_STATUS_TARGET = None
        main_qml._MACOS_STATUS_ITEM_NSIMAGE = None
        main_qml._MACOS_STATUS_ITEM_PARAMS = None
        main_qml._MACOS_DOCK_ICON_NSIMAGE = None
        _FakeNSImage.instances = 0
        _FakeNSImage.from_data = 0
        _FakeTarget.instances = 0
        self.renders = 0
        self.timers = []

    def _render(self, *_args):
        self.renders += 1
        return SimpleNamespace(isNull=lambda: False)

    def _single_shot(self, delay, callback):
        self.timers.append((delay, callback))

    def _drain_timers(self):
        while self.timers:
            pending, self.timers = self.timers, []
            for _delay, callback in sorted(pending, key=lambda t: t[0]):
                callback()

    def _env(self, appkit):
        main_qml._MACOS_APPKIT = appkit
        stack = contextlib.ExitStack()
        for ctx in (
            patch.object(main_qml.sys, "platform", "darwin"),
            patch.object(main_qml, "_macos_appkit", return_value=appkit),
            patch.object(main_qml, "_MacOSStatusItemTarget", _FakeTarget),
            patch.object(main_qml, "_render_svg_pixmap", side_effect=self._render),
            patch.object(main_qml, "_qpixmap_to_png_bytes", return_value=b"png"),
            patch.object(main_qml.os.path, "isfile", return_value=True),
            patch.object(main_qml.QTimer, "singleShot", side_effect=self._single_shot),
            patch("builtins.print"),
        ):
            stack.enter_context(ctx)
        return stack

    def _toggle_window(self, cycles):
        for _ in range(cycles):
            main_qml._set_macos_activation_policy(regular=True)
            self._drain_timers()
            main_qml._set_macos_activation_policy(regular=False)
            self._drain_timers()


class StatusItemAllocationTests(_StatusItemTestCase):
    def test_one_item_target_and_image_across_100_show_hide_cycles(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        menu = _Menu()
        with self._env(_fake_appkit(status_bar, nsapp)):
            item = main_qml._install_native_macos_status_item(menu, lambda: None)
            self.assertIs(item, status_bar.created[0])
            self._toggle_window(100)

        self.assertEqual(len(status_bar.created), 1)
        self.assertEqual(status_bar.removed, [])
        self.assertEqual(_FakeTarget.instances, 1)
        self.assertEqual(_FakeNSImage.from_data, 1)
        self.assertEqual(self.renders, 1)
        self.assertIs(main_qml._MACOS_NATIVE_STATUS_ITEM, item)
        self.assertIs(item.button().image, main_qml._MACOS_STATUS_ITEM_NSIMAGE)
        self.assertEqual(len(nsapp.policies), 200)

    def test_detached_item_is_reattached_without_rebuilding(self):
        status_bar = _FakeStatusBar(reattach_on_visible_toggle=True)
        nsapp = _FakeNSApp(status_bar, detach_on_policy_change=True)
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_native_macos_status_item(_Menu(), lambda: None)
            self._toggle_window(50)

        self.assertEqual(len(status_bar.created), 1)
        self.assertEqual(status_bar.removed, [])
        self.assertEqual(_FakeTarget.instances, 1)
        self.assertEqual(_FakeNSImage.from_data, 1)
        self.assertGreaterEqual(len(status_bar.created[0].visible_calls), 2)

    def test_hard_detach_replaces_only_the_status_item(self):
        """When AppKit truly drops the slot and a visible toggle cannot restore
        it, only the NSStatusItem is rebuilt; the target and NSImage persist."""
        status_bar = _FakeStatusBar(reattach_on_visible_toggle=False)
        nsapp = _FakeNSApp(status_bar, detach_on_policy_change=True)
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_native_macos_status_item(_Menu(), lambda: None)
            self._toggle_window(20)

        # One item per policy flip (40) plus the initial install.
        self.assertEqual(len(status_bar.created), 41)
        self.assertEqual(len(status_bar.removed), 40)
        self.assertEqual(_FakeTarget.instances, 1)
        self.assertEqual(_FakeNSImage.from_data, 1)
        self.assertEqual(self.renders, 1)
        latest = status_bar.created[-1]
        self.assertIs(main_qml._MACOS_NATIVE_STATUS_ITEM, latest)
        self.assertIs(latest.button().target, main_qml._MACOS_NATIVE_STATUS_TARGET)
        self.assertIs(latest.button().image, main_qml._MACOS_STATUS_ITEM_NSIMAGE)

    def test_reinstall_rebinds_handlers_to_current_menu_and_callback(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        first_menu, second_menu = _Menu(), _Menu()
        calls = []
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_native_macos_status_item(first_menu, lambda: calls.append(1))
            main_qml._install_native_macos_status_item(second_menu, lambda: calls.append(2))
            target = main_qml._MACOS_NATIVE_STATUS_TARGET
            target.handlers["primary"]()
            with patch.object(main_qml, "QCursor", create=True):
                target.handlers["menu"]()

        self.assertEqual(len(status_bar.created), 1)
        self.assertEqual(calls, [2])
        self.assertEqual(first_menu.popups, 0)
        self.assertEqual(second_menu.popups, 1)
        self.assertEqual(main_qml._MACOS_STATUS_ITEM_PARAMS[0], second_menu)


class StatusItemOwnershipTests(_StatusItemTestCase):
    def test_target_handlers_do_not_keep_the_menu_alive(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        menu = _Menu()
        menu_ref = weakref.ref(menu)
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_native_macos_status_item(menu, lambda: None)
            # The retry params tuple is the only intended strong owner here.
            main_qml._MACOS_STATUS_ITEM_PARAMS = None
            del menu
            gc.collect()
            self.assertIsNone(menu_ref())
            # A click after the menu is gone must be a harmless no-op.
            main_qml._MACOS_NATIVE_STATUS_TARGET.handlers["menu"]()

    def test_teardown_clears_handlers_and_removes_item(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        with self._env(_fake_appkit(status_bar, nsapp)):
            item = main_qml._install_native_macos_status_item(_Menu(), lambda: None)
            target = main_qml._MACOS_NATIVE_STATUS_TARGET
            main_qml._teardown_native_macos_status_item()
            # Idempotent.
            main_qml._teardown_native_macos_status_item()

        self.assertEqual(status_bar.removed, [item])
        self.assertEqual(target.handlers, {})
        self.assertIsNone(item.button().target)
        self.assertIsNone(item.button().image)
        self.assertIsNone(main_qml._MACOS_NATIVE_STATUS_ITEM)
        self.assertIsNone(main_qml._MACOS_NATIVE_STATUS_TARGET)
        self.assertIsNone(main_qml._MACOS_STATUS_ITEM_NSIMAGE)
        self.assertIsNone(main_qml._MACOS_STATUS_ITEM_PARAMS)

    def test_failed_image_render_is_not_cached(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        appkit = _fake_appkit(status_bar, nsapp)
        with self._env(appkit):
            with patch.object(
                main_qml, "_render_svg_pixmap",
                return_value=SimpleNamespace(isNull=lambda: True),
            ):
                self.assertIsNone(main_qml._install_native_macos_status_item(_Menu(), lambda: None))
            self.assertIsNone(main_qml._MACOS_STATUS_ITEM_NSIMAGE)
            self.assertIsNone(main_qml._MACOS_STATUS_ITEM_PARAMS)
            self.assertIsNotNone(main_qml._install_native_macos_status_item(_Menu(), lambda: None))
        self.assertEqual(_FakeNSImage.instances, 1)


class DockIconTests(_StatusItemTestCase):
    def test_dock_icon_is_set_once_for_repeated_same_image(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        with self._env(_fake_appkit(status_bar, nsapp)):
            for _ in range(25):
                main_qml._install_macos_dock_icon()

        self.assertEqual(nsapp.icon_sets, 1)
        self.assertEqual(_FakeNSImage.instances, 1)
        self.assertIs(nsapp.icon, main_qml._MACOS_DOCK_ICON_NSIMAGE)

    def test_dock_icon_is_reapplied_when_appkit_reseeds_it(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_macos_dock_icon()
            nsapp.icon = object()  # AppKit re-seeded from the bundle
            main_qml._install_macos_dock_icon()
            main_qml._install_macos_dock_icon()

        self.assertEqual(nsapp.icon_sets, 2)
        self.assertIs(nsapp.icon, main_qml._MACOS_DOCK_ICON_NSIMAGE)

    def test_show_hide_storm_sets_dock_icon_once_per_promotion_at_most(self):
        status_bar = _FakeStatusBar()
        nsapp = _FakeNSApp(status_bar)
        with self._env(_fake_appkit(status_bar, nsapp)):
            main_qml._install_native_macos_status_item(_Menu(), lambda: None)
            self._toggle_window(100)

        # The Dock never re-seeded in this fake, so the very first promotion
        # is the only set; the 0 ms / 250 ms refresh callbacks all no-op.
        self.assertEqual(nsapp.icon_sets, 1)
        self.assertEqual(_FakeNSImage.instances, 2)  # status item + dock


if __name__ == "__main__":
    unittest.main()
