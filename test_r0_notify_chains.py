#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_r0_notify_chains.py —— R0 三条故障通知链专项测试（2026-09-25）

背景（交叉审查 D / ChatGPT 复审 be1d44a）：
  F10 巡检邮件此刻即被持仓闸门挡住（健康巡检每次重读磁盘 .env）
  F11 崩溃「邮件兜底」只在 TG 成功后触发；bot 启动熔断时队列无消费进程
  F12 日报把「尝试发送」当「已送达」，两路皆失败当日不再重试

场景：R1 critical 豁免持仓闸门 / R2 wait=True 回传真值 / R3 TG 未配置返回 None
      R4 日报双渠道确认口径 / R5 watchdog.mark_fatal 落心跳
      R6 巡检读 fatal_alert / R7 巡检邮件 event="health" / R8 空仓仍可发

运行：.venv/Scripts/python.exe -m pytest test_r0_notify_chains.py -q
"""
import datetime as _dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import email_gate  # noqa: E402
import trader_260725  # noqa: E402
import watchdog as wd  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "patrol", os.path.join(os.path.dirname(__file__), "健康巡检.py")
)
patrol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patrol)

MAIL_ENV = {
    "QQ_MAIL_USER": "u@example.com",
    "QQ_MAIL_AUTH_CODE": "code",
    "QQ_MAIL_TO": "to@example.com",
    "EMAIL_ALERT_ENABLED": "true",
    "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
}


class _StopLoop(BaseException):
    """中断日报无限循环（继承 BaseException，避免被循环内 except Exception 吞掉）"""


class _FakeDatetime:
    """固定在 08:05 北京时间"""

    @staticmethod
    def now(tz=None):
        return _dt.datetime(2026, 9, 25, 8, 5, 0)


class TraderEmailEventTests(unittest.TestCase):
    """R1 / R2 / R3"""

    def test_critical_email_bypasses_position_gate(self):
        """R1：空仓 + 闸门开启 → critical 邮件仍走 SMTP"""
        tmp = tempfile.TemporaryDirectory()
        state = os.path.join(tmp.name, "trade_state.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"BTCUSDT": {"b1": {"is_active": False}}}, f)
        server = mock.MagicMock()
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch.object(email_gate, "DEFAULT_STATE_FILE", state), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value = server
            ok = trader_260725.CryptoTrader._send_email_alert(
                object(), "资金安全", subject="测试", event="critical", wait=True)
        self.assertTrue(ok)
        server.sendmail.assert_called_once()
        tmp.cleanup()

    def test_generic_email_still_gated(self):
        """回归：generic 事件在无持仓时仍被闸门拦（用户显式偏好不被绕过）"""
        tmp = tempfile.TemporaryDirectory()
        state = os.path.join(tmp.name, "trade_state.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"BTCUSDT": {"b1": {"is_active": False}}}, f)
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch.object(email_gate, "DEFAULT_STATE_FILE", state), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            ok = trader_260725.CryptoTrader._send_email_alert(
                object(), "普通", subject="测试", event="generic", wait=True)
        self.assertFalse(ok)
        smtp.assert_not_called()
        tmp.cleanup()

    def test_wait_true_returns_false_on_smtp_failure(self):
        """R2：SMTP 失败时 wait=True 必须回传 False（不能「提交即算送达」）"""
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL", side_effect=OSError("smtp down")):
            ok = trader_260725.CryptoTrader._send_email_alert(
                object(), "日报", subject="测试", event="daily_report", wait=True)
        self.assertFalse(ok)


class DailyReportDeliveryTests(unittest.TestCase):
    """R4 / F12"""

    def _run_loop(self, tg_ok, mail_ok):
        fake = mock.MagicMock()
        fake._send_daily_report = mock.MagicMock(
            return_value={"tg": tg_ok, "email": mail_ok})
        with mock.patch.object(trader_260725, "datetime", _FakeDatetime), \
                mock.patch.object(trader_260725.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                trader_260725.CryptoTrader._daily_report_loop(fake)
        return fake

    def test_no_date_mark_when_channels_unconfirmed(self):
        """F12：TG 或邮件未确认 → 不写当日日期（否则整日不再重试）"""
        fake = self._run_loop(tg_ok=False, mail_ok=True)
        self.assertIsNone(fake._last_daily_report_date)
        self.assertEqual(fake._daily_report_retry_count, 1)
        fake._send_daily_report.assert_called_once()

    def test_date_marked_only_when_both_confirmed(self):
        fake = self._run_loop(tg_ok=True, mail_ok=True)
        self.assertEqual(fake._last_daily_report_date, "2026-09-25")
        self.assertEqual(fake._daily_report_retry_count, 0)

    def test_mail_only_failure_blocks_mark(self):
        """邮件单独失败也不算送达（双渠道确认口径）"""
        fake = self._run_loop(tg_ok=True, mail_ok=False)
        self.assertIsNone(fake._last_daily_report_date)


class WatchdogFatalAlertTests(unittest.TestCase):
    """R5 / R6 / R7 / R8 —— F11 的 bot 未启动场景出口"""

    def test_mark_fatal_writes_heartbeat(self):
        tmp = tempfile.TemporaryDirectory()
        hb = os.path.join(tmp.name, ".heartbeat.json")
        with mock.patch.object(wd, "HEARTBEAT_FILE", hb):
            wd.mark_fatal("启动熔断：连续 5 次启动失败")
        with open(hb, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("fatal_alert", data)
        self.assertIn("启动熔断", data["fatal_alert"]["reason"])
        self.assertIn("ts_str", data["fatal_alert"])
        tmp.cleanup()

    def test_patrol_reports_watchdog_fatal(self):
        tmp = tempfile.TemporaryDirectory()
        hb = os.path.join(tmp.name, ".heartbeat.json")
        state = os.path.join(tmp.name, "trade_state.json")
        with open(hb, "w", encoding="utf-8") as f:
            json.dump({
                "ts": 1e12, "ts_str": "2026-09-25 08:00:00",
                "watchdog_pid": 1, "bot_pid": None, "bot_alive": False,
                "stopped": False,
                "fatal_alert": {"reason": "启动熔断：已停止自动重启",
                                "ts": 1e12, "ts_str": "2026-09-25 08:00:00"},
            }, f)
        with open(state, "w", encoding="utf-8") as f:
            json.dump({}, f)
        captured = []
        with mock.patch.object(patrol, "HEARTBEAT_FILE", hb), \
                mock.patch.object(patrol, "TRADE_STATE_FILE", state), \
                mock.patch.object(patrol, "HEALTH_DIR", os.path.join(tmp.name, "health")), \
                mock.patch.object(patrol, "alert",
                                  side_effect=lambda env, key, t, d, dry: captured.append(key)), \
                mock.patch.object(patrol, "log"):
            patrol.run_check({}, dry_run=False)
        self.assertIn("watchdog_fatal", captured)
        tmp.cleanup()

    def test_patrol_alert_email_uses_health_event(self):
        captured = {}

        def _fake_email(*a, **k):
            captured.update({"args": a, "kwargs": k})
            return True

        with mock.patch.object(patrol, "send_tg", return_value=False), \
                mock.patch.object(patrol, "send_email", side_effect=_fake_email), \
                mock.patch.object(patrol, "read_json", return_value={}), \
                mock.patch.object(patrol, "write_json"), \
                mock.patch.object(patrol, "log"):
            patrol.alert({}, "stale_heartbeat", "t", "d", dry_run=False)
        self.assertEqual(captured.get("kwargs", {}).get("event"), "health")

    def test_patrol_health_email_sends_without_position(self):
        """R8：无持仓 + 闸门开启 → 巡检邮件仍发送（修复前的静默吞掉场景）"""
        tmp = tempfile.TemporaryDirectory()
        state = os.path.join(tmp.name, "trade_state.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"BTCUSDT": {"b1": {"is_active": False}}}, f)
        server = mock.MagicMock()
        with mock.patch.object(patrol, "TRADE_STATE_FILE", state), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value = server
            ok = patrol.send_email(dict(MAIL_ENV), "巡检异常", "text", event="health")
        self.assertTrue(ok)
        server.sendmail.assert_called_once()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


    def test_tg_returns_none_when_unconfigured(self):
        """R3：未配置 TG 时返回 None（不可判定），与 False（失败）区分"""
        fake = mock.MagicMock()
        fake.tg_bot = None
        fake.chat_id = None
        fake.loop = None
        self.assertIsNone(trader_260725.CryptoTrader.send_tg_notification(fake, "hi"))
