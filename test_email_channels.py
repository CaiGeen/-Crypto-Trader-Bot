import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

import email_gate
import trader_260725

SPEC = importlib.util.spec_from_file_location(
    "patrol", os.path.join(os.path.dirname(__file__), "健康巡检.py")
)
patrol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patrol)


class EmailChannelGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "trade_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **overrides):
        env = {
            "QQ_MAIL_USER": "u@example.com",
            "QQ_MAIL_AUTH_CODE": "code",
            "QQ_MAIL_TO": "to@example.com",
            "EMAIL_ALERT_ENABLED": "true",
            "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
        }
        env.update(overrides)
        return env

    def test_patrol_email_skipped_when_disabled(self):
        env = self._env(EMAIL_ALERT_ENABLED="false")
        with mock.patch.object(
            email_gate, "should_send_email", return_value=(False, "disabled")
        ) as gate, mock.patch("smtplib.SMTP_SSL") as smtp, \
                mock.patch.object(patrol, "log") as plog:
            self.assertFalse(patrol.send_email(env, "subject", "text"))
        gate.assert_called_once()
        smtp.assert_not_called()
        self.assertTrue(plog.called, "闸门跳过也应记录，但必须记到被 patch 的 log()")

    def test_patrol_email_skipped_without_active_position(self):
        env = self._env()
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"BTCUSDT": {"b1": {"is_active": False}}}, f)
        with mock.patch.object(
            patrol, "TRADE_STATE_FILE", self.state_path
        ), mock.patch("smtplib.SMTP_SSL") as smtp, \
                mock.patch.object(patrol, "log"):
            self.assertFalse(patrol.send_email(env, "subject", "text"))
        smtp.assert_not_called()

    def test_patrol_email_sends_with_active_position(self):
        env = self._env()
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"BTCUSDT": {"b1": {"is_active": True}}}, f)

        server = mock.MagicMock()
        with mock.patch.object(patrol, "TRADE_STATE_FILE", self.state_path), \
                mock.patch("smtplib.SMTP_SSL") as smtp, \
                mock.patch.object(patrol, "log") as plog:
            smtp.return_value.__enter__.return_value = server
            self.assertTrue(patrol.send_email(env, "subject", "text"))
        smtp.assert_called_once()
        server.sendmail.assert_called_once()
        # 第十六轮复审 P3：patrol.log() 会**追加进生产 logs/patrol.log**，于是这条与
        # 真实告警一字不差的「邮件告警已发送」曾被写进生产观测日志，运维无法分辨
        # 测试噪声与真事件。patch log() 既阻断污染，又用断言保留原验证意图。
        logged = [str(a[0]) for a in plog.call_args_list if a[0]]
        self.assertTrue(any("邮件告警已发送" in m for m in logged),
                        f"发送路径必须记录成功，但应记到被 patch 的 log: {logged}")

    def test_telegram_fallback_still_runs_when_email_gated(self):
        env = self._env(EMAIL_ALERT_ENABLED="false")
        with mock.patch.object(patrol, "send_tg", return_value=False) as tg, \
                mock.patch.object(patrol, "send_email", return_value=False) as mail, \
                mock.patch.object(patrol, "read_json", return_value={}), \
                mock.patch.object(patrol, "write_json"), \
                mock.patch.object(patrol, "log"):
            patrol.alert(env, "gate_test", "title", "detail", dry_run=False)
        self.assertEqual(tg.call_count, 2)
        mail.assert_called_once()


class TraderEmailChannelGateTests(unittest.TestCase):
    def test_trader_email_skipped_before_thread_when_gate_blocks(self):
        with mock.patch.object(
            email_gate, "should_send_email", return_value=(False, "disabled")
        ) as gate, mock.patch.object(
            trader_260725.threading, "Thread"
        ) as thread:
            trader_260725.CryptoTrader._send_email_alert(
                object(), "test", subject="测试"
            )
        gate.assert_called_once()
        thread.assert_not_called()

    def test_trader_email_starts_thread_when_gate_allows(self):
        with mock.patch.object(
            email_gate, "should_send_email",
            return_value=(True, "active_positions")
        ) as gate, mock.patch.dict(
            os.environ,
            {
                "QQ_MAIL_USER": "u@example.com",
                "QQ_MAIL_AUTH_CODE": "code",
                "QQ_MAIL_TO": "to@example.com",
            },
        ), mock.patch.object(
            trader_260725.threading, "Thread"
        ) as thread:
            trader_260725.CryptoTrader._send_email_alert(
                object(), "test", subject="测试"
            )
        gate.assert_called_once()
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])

if __name__ == "__main__":
    unittest.main()