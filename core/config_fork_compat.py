"""Read configs written by the hughesyadaddy fork (schema "v12").

The fork numbered its own schema steps 10-12 independently of upstream,
whose v10 (MX Master 4 schema) and v11 (scroll force) mean different
things. A fork config therefore reports ``version: 12`` while never having
run upstream's v10/v11 steps, and upstream's ``_migrate`` would skip them.

:func:`migrate_fork_lineage` runs once per config, before ``_migrate``:

* applies upstream's v10/v11 additions with ``setdefault`` only -- nothing
  the user saved is overwritten;
* maps the fork's gesture model onto upstream's: in the fork the gesture
  control (Sense Panel on the MX Master 4, thumb Gesture button elsewhere)
  swiped whenever ``gesture_<dir>`` was set and its tap used ``gesture``;
  upstream only swipes when the button's action is the ``gesture_swipe``
  sentinel, with the tap in ``<btn>_tap``, and the MX Master 4 Sense Panel
  is the ``actions_ring`` key;
* keeps every other key, fork-only ones included (they are ignored);
* renumbers to upstream's v11 and records ``settings.config_lineage`` so it
  never runs again and later upstream steps (v12+) still apply.
"""

from __future__ import annotations

FORK_SCHEMA_VERSION = 12
UPSTREAM_SCHEMA_VERSION = 11
LINEAGE_KEY = "config_lineage"
_DIRS = ("left", "right", "up", "down")
GESTURE_SWIPE_ACTION = "gesture_swipe"


def is_fork_lineage(cfg) -> bool:
    if not isinstance(cfg, dict):
        return False
    settings = cfg.get("settings") or {}
    if isinstance(settings, dict) and LINEAGE_KEY in settings:
        return False
    try:
        version = int(cfg.get("version", 1))
    except (TypeError, ValueError):
        return False
    return version >= FORK_SCHEMA_VERSION


def _profile_gestures(mappings, default_slots):
    tap = mappings.get("gesture", "none")
    if not isinstance(tap, str):
        tap = "none"
    dirs = {d: mappings.get(f"gesture_{d}", "none") for d in _DIRS}
    has_swipes = any(isinstance(v, str) and v != "none" for v in dirs.values())
    if has_swipes and tap != GESTURE_SWIPE_ACTION:
        mappings["gesture"] = GESTURE_SWIPE_ACTION
        mappings.setdefault("gesture_tap", tap)
    # MX Master 4: the fork's gesture control was the Sense Panel.
    ring = mappings.get("actions_ring")
    if ring in (None, "none") and has_swipes:
        mappings["actions_ring"] = GESTURE_SWIPE_ACTION
        mappings.setdefault("actions_ring_tap", tap)
    elif ring is None:
        mappings["actions_ring"] = tap
    for d, v in dirs.items():
        key = f"actions_ring_{d}"
        if mappings.get(key, "none") == "none":
            mappings[key] = v
    mappings.setdefault("thumb_button", "none")
    mappings.setdefault("actions_ring_slots", default_slots())
    mappings.setdefault("gesture_tap", "none")
    mappings.setdefault("actions_ring_tap", "none")


def migrate_fork_lineage(cfg, default_slots, wheel_divert_default="auto"):
    """Normalize a fork config in place; return it. No-op for others."""
    if not is_fork_lineage(cfg):
        return cfg
    fork_version = cfg.get("version")
    settings = cfg.setdefault("settings", {})
    # upstream v10 (setdefault only)
    for key, value in (
        ("gesture_commit_window_ms", 400), ("gesture_settle_ms", 90),
        ("gesture_cross_ratio", 0.5), ("wheel_divert", wheel_divert_default),
        ("haptic_level", 2), ("haptic_enabled", True), ("action_haptic", []),
        ("button_haptic", []), ("haptic_dedup", True),
        ("force_sensitivity", None), ("actions_ring_hold_ms", 250),
    ):
        settings.setdefault(key, value)
    if settings.get("hscroll_threshold") == 1:
        settings["hscroll_threshold"] = 0.1  # upstream v10 unit change
    # upstream v11
    settings.setdefault("scroll_force", 50)
    for pdata in (cfg.get("profiles") or {}).values():
        if not isinstance(pdata, dict):
            continue
        mappings = pdata.setdefault("mappings", {})
        if isinstance(mappings, dict):
            _profile_gestures(mappings, default_slots)
        pdata.setdefault("button_haptic", {})
    settings[LINEAGE_KEY] = {
        "source": "hughesyadaddy-fork",
        "fork_version": fork_version,
        "normalized_to": UPSTREAM_SCHEMA_VERSION,
    }
    cfg["version"] = UPSTREAM_SCHEMA_VERSION
    print(f"[Config] fork config v{fork_version} normalized to upstream v{UPSTREAM_SCHEMA_VERSION}")
    return cfg
