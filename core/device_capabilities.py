"""Pure capability-resolution layer.

This module is the single source of truth for *what a connected device can
actually do*. It exists to kill a whole bug class: behavior must never be
selected from device *identity* (catalog hints), a command *ACK*, or
*feature-index presence* alone -- each of those was historically mistaken for
proof of a real capability, which is how the original MX Master ended up
silently dropping to cursor-tracking gestures (a rawXY divert that the firmware
ACKed but never honored) and how a horizontal-invert firmware limit reverted a
working vertical invert.

Design rules enforced here:
  * Pure + total. No I/O, no threads, no HID++ requests. Every input is plain
    data, so the resolver is trivially unit-testable without hardware.
  * Runtime ground truth wins; the static catalog is only a prior.
  * Independent capabilities are modelled independently (e.g. the two wheel
    axes are separate ``Capability`` values and can never poison each other).
  * Tri-state (``None``) means "not probed yet" and defers to the catalog;
    ``True``/``False`` are definitive runtime observations.
  * Every decision records a human-readable ``note`` so resolution is auditable
    in the logs rather than silent.

The resolver consumes the same primitive data the connection path already has:
the REPROG_V4 control dicts (see ``hid_gesture._discover_reprog_controls``), a
``feature_id -> index`` map from feature discovery, and the matched catalog
spec. It produces a :class:`DeviceCapabilities` of resolved *decisions* that the
engine and platform hooks read instead of re-deriving facts ad hoc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping, Optional

from core.logi_devices import (
    DEFAULT_DPI_MAX,
    DEFAULT_DPI_MIN,
    LogiDeviceSpec,
)

# ── HID++ protocol constants (mirror core.hid_gesture; protocol-stable) ──────
FEAT_SMART_SHIFT = 0x2110
FEAT_SMART_SHIFT_ENHANCED = 0x2111
FEAT_HIRES_WHEEL_ENHANCED = 0x2121
FEAT_THUMB_WHEEL = 0x2150
FEAT_ADJUSTABLE_DPI = 0x2201

# REPROG_V4 control capability bits (key flags + mapping flags).
_KEY_FLAG_DIVERTABLE = 0x0020
_KEY_FLAG_RAW_XY = 0x0100
_KEY_FLAG_FORCE_RAW_XY = 0x0200
_MAP_FLAG_RAW_XY = 0x0010
_MAP_FLAG_FORCE_RAW_XY = 0x0040

# The MX Master 4 haptic Sense Panel gesture control. When the catalog marks a
# device sense-panel-driven but the listener diverted something *other* than
# this CID, the gesture arrives over the OS button path instead of HID++.
SENSE_PANEL_CID = 0x01A0

GestureSource = Literal["rawxy", "event_tap", "none"]
ThumbRouting = Literal["hid", "os", "none"]


def _coerce_cid(value) -> Optional[int]:
    """Normalize an int / ``"0x01A0"`` / ``None`` CID to ``int | None``,
    fail-closed (malformed -> ``None``)."""
    if value in (None, ""):
        return None
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value, default: int = 0) -> int:
    """Fail-closed int coercion for flag/index fields that may be malformed."""
    if value is None:
        return default
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Capability:
    """A single feature's resolved state.

    ``supported`` is tri-state: ``None`` = not probed (defer to catalog),
    ``True``/``False`` = definitive runtime observation. ``feature_index`` is
    the HID++ feature index when discovered, for callers that issue requests.
    Whether the firmware *honors* a write (vs merely exposing the feature) is a
    separate runtime confirmation carried by the per-axis active lease -- this
    object never conflates "feature present" with "feature works".
    """

    supported: Optional[bool] = None
    feature_index: Optional[int] = None


@dataclass(frozen=True)
class DpiCapability:
    supported: Optional[bool] = None
    feature_index: Optional[int] = None
    dpi_min: int = DEFAULT_DPI_MIN
    dpi_max: int = DEFAULT_DPI_MAX


@dataclass(frozen=True)
class DeviceCapabilities:
    """Resolved behavioral decisions for the connected device.

    Read by the engine and platform hooks instead of catalog hints / ACKs /
    feature-index checks. Built by :func:`resolve_capabilities`.
    """

    # Gesture: the source decision is the one that bit us -- rawXY only when the
    # active control truly advertises it (and, when known, the divert was
    # confirmed); otherwise the OS event-tap path (cursor-tracking + warp).
    gesture_source: GestureSource = "none"
    active_gesture_cid: Optional[int] = None
    gesture_click: bool = False
    gesture_directions: bool = False
    # Catalog declares a Sense Panel AND the listener diverted something other
    # than 0x01A0 -> gesture/thumb arrive over the OS button path (MX Master 4).
    gesture_via_sense_panel: bool = False

    # Wheel inversion, per axis, fully independent. A failure on one axis can
    # never affect the other (the historical all-or-nothing double-invert bug).
    wheel_invert_vertical: Capability = field(default_factory=Capability)
    wheel_invert_horizontal: Capability = field(default_factory=Capability)

    # Thumb button routing: "hid" when diverted over HID++, "os" when it falls
    # back to the OS button, "none" when the device has no thumb button.
    thumb_button_routing: ThumbRouting = "none"
    thumb_button_cid: Optional[int] = None

    smart_shift: Capability = field(default_factory=Capability)
    smart_shift_enhanced: Optional[bool] = None

    dpi: DpiCapability = field(default_factory=DpiCapability)

    # (subsystem, reason) provenance for each decision; logged by callers.
    notes: tuple[tuple[str, str], ...] = ()


def _control_index(reprog_controls) -> dict[int, Mapping]:
    out: dict[int, Mapping] = {}
    for control in reprog_controls or ():
        if not isinstance(control, Mapping):
            continue
        cid = _coerce_cid(control.get("cid"))
        if cid is not None:
            out[cid] = control
    return out


def _is_rawxy_capable(control: Mapping) -> bool:
    flags = _safe_int(control.get("flags", 0))
    mapping_flags = _safe_int(control.get("mapping_flags", 0))
    return bool(
        flags & _KEY_FLAG_RAW_XY
        or flags & _KEY_FLAG_FORCE_RAW_XY
        or mapping_flags & _MAP_FLAG_RAW_XY
        or mapping_flags & _MAP_FLAG_FORCE_RAW_XY
    )


def _feature_capability(
    discovered_features: Optional[Mapping[int, Optional[int]]],
    feature_id: int,
) -> Capability:
    """Resolve a feature to a tri-state Capability.

    Missing key  -> not probed (``supported=None``).
    Key -> None  -> probed, absent (``supported=False``).
    Key -> index -> present (``supported=True``, ``feature_index=index``).
    """
    if discovered_features is None or feature_id not in discovered_features:
        return Capability(supported=None, feature_index=None)
    idx = discovered_features.get(feature_id)
    if idx is None:
        return Capability(supported=False, feature_index=None)
    coerced = _coerce_cid(idx)
    if coerced is None:
        # Probed, but the index is unusable -> treat as absent (fail-closed).
        return Capability(supported=False, feature_index=None)
    return Capability(supported=True, feature_index=coerced)


def resolve_capabilities(
    catalog_spec: Optional[LogiDeviceSpec],
    reprog_controls: Optional[Iterable[Mapping]],
    discovered_features: Optional[Mapping[int, Optional[int]]],
    *,
    active_gesture_cid=None,
    gesture_rawxy_confirmed: Optional[bool] = None,
) -> DeviceCapabilities:
    """Resolve device capabilities from runtime ground truth + catalog priors.

    Pure and total: bad/partial input never raises, it just yields a
    conservative (fail-closed) result with an explanatory note.

    Args:
        catalog_spec: matched :class:`LogiDeviceSpec` (priors only) or ``None``.
        reprog_controls: REPROG_V4 control dicts (``cid``/``flags``/
            ``mapping_flags``); the runtime ground truth for gesture/thumb.
        discovered_features: ``feature_id -> index | None`` from feature
            discovery. Absent key = not probed; ``None`` value = probed & absent.
        active_gesture_cid: the CID the listener actually diverted as the
            gesture role (``None`` until a divert succeeds).
        gesture_rawxy_confirmed: runtime confirmation that the rawXY divert
            stuck. ``None`` = unknown (decide from capability flags);
            ``False`` = it didn't stick (force event_tap even if flag-capable).
    """
    notes: list[tuple[str, str]] = []
    controls = _control_index(reprog_controls)
    active_cid = _coerce_cid(active_gesture_cid)

    # ── Gesture source ──────────────────────────────────────────────────
    if active_cid is None:
        gesture_source: GestureSource = "none"
        notes.append(("gesture", "no gesture CID diverted -> source=none"))
    else:
        control = controls.get(active_cid)
        flag_rawxy = bool(control is not None and _is_rawxy_capable(control))
        if gesture_rawxy_confirmed is False:
            gesture_source = "event_tap"
            notes.append(
                ("gesture", f"0x{active_cid:04X} rawXY divert not confirmed -> event_tap")
            )
        elif flag_rawxy and gesture_rawxy_confirmed is not False:
            gesture_source = "rawxy"
            notes.append(("gesture", f"0x{active_cid:04X} advertises rawXY -> rawxy"))
        else:
            gesture_source = "event_tap"
            notes.append(
                ("gesture", f"0x{active_cid:04X} lacks rawXY capability -> event_tap")
            )

    gesture_present = active_cid is not None
    # Directions work on both feeds (rawXY firmware stream OR cursor tracking),
    # so they ride on having a diverted gesture control, not on the source.
    gesture_directions = gesture_present
    gesture_click = gesture_present

    # ── Sense-panel fallback (MX Master 4 family) ───────────────────────
    catalog_sense_panel = bool(getattr(catalog_spec, "gesture_via_sense_panel", False))
    gesture_via_sense_panel = bool(
        catalog_sense_panel and active_cid is not None and active_cid != SENSE_PANEL_CID
    )
    if catalog_sense_panel:
        active_str = f"0x{active_cid:04X}" if active_cid is not None else "none"
        state = "ON" if gesture_via_sense_panel else "off"
        notes.append(("gesture", f"sense-panel fallback {state} (active={active_str})"))

    # ── Wheel invert, per axis (independent) ────────────────────────────
    wheel_v = _feature_capability(discovered_features, FEAT_HIRES_WHEEL_ENHANCED)
    wheel_h = _feature_capability(discovered_features, FEAT_THUMB_WHEEL)
    notes.append(("wheel_v", f"hires-wheel(0x2121) supported={wheel_v.supported}"))
    notes.append(("wheel_h", f"thumbwheel(0x2150) supported={wheel_h.supported}"))

    # ── Thumb button routing ────────────────────────────────────────────
    catalog_thumb_cid = _coerce_cid(getattr(catalog_spec, "thumb_button_cid", None))
    if catalog_thumb_cid is None:
        thumb_routing: ThumbRouting = "none"
        thumb_cid = None
    elif catalog_thumb_cid in controls and catalog_thumb_cid != active_cid:
        thumb_routing = "hid"
        thumb_cid = catalog_thumb_cid
        notes.append(("thumb", f"0x{catalog_thumb_cid:04X} present + divertable -> hid"))
    elif gesture_via_sense_panel:
        thumb_routing = "os"
        thumb_cid = catalog_thumb_cid
        notes.append(("thumb", "sense-panel fallback -> thumb via OS button"))
    else:
        thumb_routing = "none"
        thumb_cid = catalog_thumb_cid
        notes.append(("thumb", f"0x{catalog_thumb_cid:04X} not divertable here -> none"))

    # ── Smart shift ─────────────────────────────────────────────────────
    ss_enhanced = _feature_capability(discovered_features, FEAT_SMART_SHIFT_ENHANCED)
    ss_basic = _feature_capability(discovered_features, FEAT_SMART_SHIFT)
    if ss_enhanced.supported:
        smart_shift = ss_enhanced
        smart_shift_enhanced: Optional[bool] = True
    elif ss_basic.supported:
        smart_shift = ss_basic
        smart_shift_enhanced = False
    elif ss_enhanced.supported is False and ss_basic.supported is False:
        smart_shift = Capability(supported=False)
        smart_shift_enhanced = None
    else:
        smart_shift = Capability(supported=None)
        smart_shift_enhanced = None
    notes.append(("smart_shift", f"supported={smart_shift.supported} enhanced={smart_shift_enhanced}"))

    # ── DPI ─────────────────────────────────────────────────────────────
    dpi_cap = _feature_capability(discovered_features, FEAT_ADJUSTABLE_DPI)
    dpi = DpiCapability(
        supported=dpi_cap.supported,
        feature_index=dpi_cap.feature_index,
        dpi_min=int(getattr(catalog_spec, "dpi_min", DEFAULT_DPI_MIN) or DEFAULT_DPI_MIN),
        dpi_max=int(getattr(catalog_spec, "dpi_max", DEFAULT_DPI_MAX) or DEFAULT_DPI_MAX),
    )

    return DeviceCapabilities(
        gesture_source=gesture_source,
        active_gesture_cid=active_cid,
        gesture_click=gesture_click,
        gesture_directions=gesture_directions,
        gesture_via_sense_panel=gesture_via_sense_panel,
        wheel_invert_vertical=wheel_v,
        wheel_invert_horizontal=wheel_h,
        thumb_button_routing=thumb_routing,
        thumb_button_cid=thumb_cid,
        smart_shift=smart_shift,
        smart_shift_enhanced=smart_shift_enhanced,
        dpi=dpi,
        notes=tuple(notes),
    )
