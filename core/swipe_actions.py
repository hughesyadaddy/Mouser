"""Opt-in "native swipe" actions layered on top of key_simulator.

Adds ``swipe_*`` action ids that perform a synthetic trackpad swipe on
macOS (see :mod:`core.native_swipe_macos`) and otherwise run the
equivalent built-in action, so a mapping to a swipe action never does
less than the action it replaced.

The native path is gated by ``settings.native_swipes_enabled`` (default
False): until it is switched on, every ``swipe_*`` action behaves exactly
like its built-in equivalent. This module is the only place that knows
about swipes; key_simulator and config each get a one-line hook.
"""

from __future__ import annotations

import sys

SETTING_ENABLED = "native_swipes_enabled"
SETTING_MIGRATION = "native_swipe_migration"
MIGRATION_VERSION = 1

# action id -> (kind, axis, direction, built-in fallback action, label)
# Direction signs follow Mac Mouse Fix; UNVERIFIED on hardware.
SWIPE_ACTIONS = {
    "swipe_mission_control": ("dock", 2, -1, "mission_control", "Mission Control (Swipe)"),
    "swipe_app_expose": ("dock", 2, +1, "app_expose", "App Exposé (Swipe)"),
    "swipe_space_left": ("dock", 1, -1, "space_left", "Previous Desktop (Swipe)"),
    "swipe_space_right": ("dock", 1, +1, "space_right", "Next Desktop (Swipe)"),
    "swipe_back": ("nav", 0, -1, "browser_back", "Back (Swipe)"),
    "swipe_forward": ("nav", 0, +1, "browser_forward", "Forward (Swipe)"),
}

#: Built-in action -> swipe equivalent, used by the settings migration.
BUILTIN_TO_SWIPE = {spec[3]: aid for aid, spec in SWIPE_ACTIONS.items()}

_state = {"enabled": False}


def configure(cfg):
    """Read the enable flag from a loaded config dict."""
    settings = (cfg or {}).get("settings", {}) or {}
    _state["enabled"] = bool(settings.get(SETTING_ENABLED, False))


def native_enabled():
    return _state["enabled"]


def action_entries(actions, platform_name=None):
    """ACTIONS rows for the swipe ids, borrowing keys from the fallback.

    Offered on macOS only, and only where the fallback exists. Other
    platforms still *execute* swipe ids (via the fallback) so a config
    copied from a Mac never loses an action.
    """
    rows = {}
    if (platform_name or sys.platform) != "darwin":
        return rows
    for aid, (_kind, _axis, _dir, fallback, label) in SWIPE_ACTIONS.items():
        base = actions.get(fallback)
        if base is None:
            continue
        row = dict(base)
        row["label"] = label
        row["category"] = base.get("category", "Navigation")
        rows[aid] = row
    return rows


def _post_native(spec):
    from core import native_swipe_macos as ns
    kind, axis, direction = spec[0], spec[1], spec[2]
    if kind == "dock":
        return ns.dock_swipe(axis, direction)
    return ns.navigation_swipe(direction)


def run(action_id, execute_builtin, *, post_native=None):
    """Run a swipe action; return False if ``action_id`` is not one."""
    spec = SWIPE_ACTIONS.get(action_id)
    if spec is None:
        return False
    if sys.platform == "darwin" and native_enabled():
        try:
            if (post_native or _post_native)(spec):
                return True
        except Exception as exc:  # never lose the action
            print(f"[Swipe] native swipe failed for {action_id}: {exc}")
    execute_builtin(spec[3])
    return True


def wrap_execute_action(execute_action):
    """Return an execute_action that understands swipe ids."""
    def execute(action_id):
        if run(action_id, execute_action):
            return None
        return execute_action(action_id)
    execute.__wrapped__ = execute_action
    execute.__name__ = getattr(execute_action, "__name__", "execute_action")
    return execute


def _is_gesture_slot(key):
    return key.startswith("gesture_")


def migrate_to_swipes(cfg, platform_name=None):
    """Map gesture slots that use a built-in Dock/Spaces/back-forward action
    to the swipe equivalent. In-memory, idempotent, non-destructive: the
    original value of every changed slot is kept under
    ``settings.native_swipe_migration.original`` so it can be restored.
    macOS only. Returns the list of (profile, slot, old, new) changes.
    """
    platform_name = platform_name or sys.platform
    if platform_name != "darwin" or not isinstance(cfg, dict):
        return []
    settings = cfg.setdefault("settings", {})
    record = settings.get(SETTING_MIGRATION)
    if isinstance(record, dict) and record.get("version", 0) >= MIGRATION_VERSION:
        return []
    changes = []
    original = {}
    for pname, profile in (cfg.get("profiles") or {}).items():
        mappings = (profile or {}).get("mappings")
        if not isinstance(mappings, dict):
            continue
        for slot, action in list(mappings.items()):
            if not _is_gesture_slot(slot) or not isinstance(action, str):
                continue
            new = BUILTIN_TO_SWIPE.get(action)
            if new is None:
                continue
            mappings[slot] = new
            original.setdefault(pname, {})[slot] = action
            changes.append((pname, slot, action, new))
    settings[SETTING_MIGRATION] = {"version": MIGRATION_VERSION, "original": original}
    return changes


def restore_from_migration(cfg):
    """Undo :func:`migrate_to_swipes` using the recorded originals."""
    settings = (cfg or {}).get("settings", {})
    record = settings.get(SETTING_MIGRATION) or {}
    for pname, slots in (record.get("original") or {}).items():
        mappings = cfg.get("profiles", {}).get(pname, {}).get("mappings", {})
        for slot, action in slots.items():
            if mappings.get(slot) in SWIPE_ACTIONS:
                mappings[slot] = action
    settings.pop(SETTING_MIGRATION, None)
    return cfg
