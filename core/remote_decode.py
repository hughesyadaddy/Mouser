"""Validate host-published HID++ decode context for KVM relay paths."""


def parse_feat_idx(decode: object) -> int | None:
    """Return feat_idx when ``decode`` is a valid REPROG context dict."""
    if not isinstance(decode, dict):
        return None
    feat_idx = decode.get("feat_idx")
    if not isinstance(feat_idx, int) or not 0 < feat_idx <= 0xFF:
        return None
    return int(feat_idx)


def parse_gesture_cid(decode: object) -> int | None:
    """Parse optional gesture CID from decode context."""
    if not isinstance(decode, dict):
        return None
    gesture_cid = decode.get("gesture_cid")
    if gesture_cid is None:
        return None
    try:
        return (
            int(gesture_cid, 0)
            if isinstance(gesture_cid, str)
            else int(gesture_cid)
        )
    except (TypeError, ValueError):
        return None
