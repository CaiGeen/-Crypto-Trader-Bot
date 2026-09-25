import json
import os
import tempfile
import unittest
from unittest import mock

import email_gate


class EmailGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "trade_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, data):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_default_keeps_legacy_behavior(self):
        allowed, reason = email_gate.should_send_email(
            env={}, state_path=os.path.join(self.tmp.name, "missing.json")
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "position_gate_disabled")

    def test_manual_switch_blocks_even_with_active_position(self):
        self._write_state({"BTCUSDT": {"b1": {"is_active": True}}})
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "false",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "disabled")

    def test_position_gate_allows_active_batch(self):
        self._write_state({"BTCUSDT": {"b1": {"is_active": True}}})
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "true",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "active_positions")

    def test_position_gate_blocks_no_active_batch(self):
        self._write_state({"BTCUSDT": {"b1": {"is_active": False}}})
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "true",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "no_active_positions")

    def test_position_gate_blocks_unreadable_state(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "1",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "yes",
            },
            state_path=self.state_path,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "state_unreadable")

    def test_manual_switch_ignores_unreadable_state(self):
        allowed, reason = email_gate.should_send_email(
            env={"EMAIL_ALERT_ENABLED": "true"},
            state_path=os.path.join(self.tmp.name, "missing.json"),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "position_gate_disabled")


if __name__ == "__main__":
    unittest.main()