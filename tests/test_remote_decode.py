"""Tests for shared KVM decode context parsing."""

import unittest

from core.remote_decode import parse_feat_idx, parse_gesture_cid


class RemoteDecodeTests(unittest.TestCase):
    def test_parse_feat_idx_rejects_invalid(self):
        self.assertIsNone(parse_feat_idx(None))
        self.assertIsNone(parse_feat_idx({"feat_idx": 0}))
        self.assertIsNone(parse_feat_idx({"feat_idx": 999}))

    def test_parse_feat_idx_accepts_valid(self):
        self.assertEqual(parse_feat_idx({"feat_idx": 11}), 11)

    def test_parse_gesture_cid_accepts_hex_string(self):
        self.assertEqual(parse_gesture_cid({"gesture_cid": "0x01A0"}), 0x01A0)


if __name__ == "__main__":
    unittest.main()
