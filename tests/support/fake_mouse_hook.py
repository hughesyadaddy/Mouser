"""Minimal MouseHook stand-in for engine and SmartShift tests."""

from core.mouse_hook_types import is_physical_device_source


class FakeMouseHook:
    """Engine-compatible hook fake with wheel-invert and KVM policy hooks."""

    def __init__(self):
        self.invert_vscroll = False
        self.invert_hscroll = False
        self.debug_mode = False
        self.connected_device = None
        self.device_connected = False
        self._hid_gesture = None
        self.start_called = False
        self.stop_called = False
        self.divert_mode_shift = False
        self.divert_dpi_switch = False
        self.ignore_trackpad = True
        self.wheel_native_invert_vertical = False
        self.wheel_native_invert_horizontal = False
        self.wheel_divert_active = False
        self.hid_runtime_state = None
        self._remote_forwarder = None

    def _physical_logitech_bound(self) -> bool:
        device = self.connected_device
        if device is None:
            return False
        return is_physical_device_source(getattr(device, "source", None))

    def set_debug_callback(self, cb):
        self._debug_callback = cb

    def set_gesture_callback(self, cb):
        self._gesture_callback = cb

    def set_status_callback(self, cb):
        self._status_callback = cb

    def set_connection_change_callback(self, cb):
        self._connection_change_callback = cb

    def configure_gestures(self, **kwargs):
        self._gesture_config = kwargs

    def configure_wheel_multipliers(self, vertical, horizontal):
        return None

    def block(self, event_type):
        pass

    def register(self, event_type, callback):
        pass

    def reset_bindings(self):
        pass

    def sync_hid_extra_diverts(self):
        pass

    def set_remote_forwarder(self, forwarder):
        self._remote_forwarder = forwarder

    def gesture_decode_context(self):
        return None

    def set_dpi(self, dpi):
        return None

    def set_ui_passthrough(self, enabled):
        return None

    def start(self):
        self.start_called = True

    def stop(self):
        self.stop_called = True

    def dump_device_info(self):
        return None
