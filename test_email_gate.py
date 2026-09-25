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

    # ---------------- R0 事件维度（2026-09-25，F10） ----------------

    def test_fatal_event_bypasses_no_position(self):
        """F10：空仓时致命事件（crash/health/critical）仍必须放行邮件"""
        self._write_state({"BTCUSDT": {"b1": {"is_active": False}}})
        for ev in ("crash", "health", "critical", "auth_blocked", "startup_breaker"):
            allowed, reason = email_gate.should_send_email(
                env={
                    "EMAIL_ALERT_ENABLED": "true",
                    "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
                },
                state_path=self.state_path,
                event=ev,
            )
            self.assertTrue(allowed, f"event={ev} 应豁免持仓闸门")
            self.assertEqual(reason, "fatal_event", f"event={ev}")

    def test_fatal_event_bypasses_unreadable_state(self):
        """状态文件损坏时致命事件同样放行（无持仓 ≠ 无风险，状态坏 ≠ 无风险）"""
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "true",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
            event="health",
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "fatal_event")

    def test_master_switch_still_blocks_fatal_event(self):
        """总开关优先级最高：EMAIL_ALERT_ENABLED=false 时致命事件也不发（保留人工总闸）"""
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "false",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
            event="crash",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "disabled")

    def test_daily_report_independent_of_position_gate(self):
        """F12：日报由独立开关裁决，空仓也要留痕"""
        self._write_state({"BTCUSDT": {"b1": {"is_active": False}}})
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "true",
                "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
            },
            state_path=self.state_path,
            event="daily_report",
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "daily_report")

    def test_daily_report_switch_off(self):
        self._write_state({"BTCUSDT": {"b1": {"is_active": True}}})
        allowed, reason = email_gate.should_send_email(
            env={
                "EMAIL_ALERT_ENABLED": "true",
                "DAILY_REPORT_EMAIL_ENABLED": "false",
            },
            state_path=self.state_path,
            event="daily_report",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily_report_disabled")

    def test_generic_event_keeps_position_gate(self):
        """回归：普通事件（generic）行为不变，仍受持仓闸门约束"""
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

    # ---------------- 第三轮复审（D7：event 拼写错误必须可读） ----------------

    def test_unknown_event_is_gated_and_warns(self):
        """D7：event 拼错（如 htalth）不能只返回模糊的 no_active_positions，
        必须在日志里可读，否则致命事件会被静默降级为持仓闸门且难以定位。"""
        import contextlib
        import io

        self._write_state({"BTCUSDT": {"b1": {"is_active": False}}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            allowed, reason = email_gate.should_send_email(
                env={
                    "EMAIL_ALERT_ENABLED": "true",
                    "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
                },
                state_path=self.state_path,
                event="htalth",   # 故意拼错
            )
        self.assertFalse(allowed)
        self.assertEqual(reason, "no_active_positions")
        self.assertIn("未知 event", buf.getvalue())

    def test_known_fatal_event_no_warning(self):
        """反向对照：已知致命事件不产生未知告警（避免日志噪音）"""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            allowed, _ = email_gate.should_send_email(
                env={}, state_path=self.state_path, event="crash")
        self.assertTrue(allowed)
        self.assertNotIn("未知 event", buf.getvalue())


if __name__ == "__main__":
    unittest.main()