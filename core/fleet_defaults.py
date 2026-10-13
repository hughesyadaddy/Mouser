"""Fleet-baked settings: identical on every seat, never drift.

These values are authoritative. On every load, each baked key takes the
baked value unless the user changed it in the UI on this seat (recorded in
settings["user_overrides"] at save time: a saved value that differs from
the baked one). Reinstalls, updates, tests and device changes therefore
cannot move a seat off these settings; only an explicit UI change can.
Source: the hackintosh seat's settings, 2026-10-12 13:03.
"""

BAKED_SETTINGS = {
    "start_minimized": True,
    "start_at_login": True,
    "hscroll_threshold": 1,
    "invert_hscroll": True,
    "invert_vscroll": True,
    "dpi": 1000,
    "smart_shift_mode": "ratchet",
    "smart_shift_enabled": True,
    "smart_shift_threshold": 25,
    "gesture_threshold": 100,
    "ignore_trackpad": True,
}

BAKED_DEFAULT_MAPPINGS = {
    "middle": "none",
    "gesture": "none",
    "gesture_left": "space_left",
    "gesture_right": "space_right",
    "gesture_up": "mission_control",
    "gesture_down": "app_expose",
    "xbutton1": "mouse_back_click",
    "xbutton2": "mouse_forward_click",
    "thumb_button": "none",
    "hscroll_left": "none",
    "hscroll_right": "none",
    "mode_shift": "switch_scroll_mode",
}

OVERRIDES_KEY = "user_overrides"
