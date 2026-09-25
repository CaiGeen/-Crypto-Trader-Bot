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
import bot_runner  # noqa: E402
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

    def test_tg_returns_none_when_unconfigured(self):
        """R3：未配置 TG 时返回 None（不可判定），与 False（失败）区分

        注：本用例首版误缩进在 `if __name__ == '__main__':` 之下 → pytest 从不收集，
        却一直被我计入「通过数量」（复审发现，见报告 §14）。
        """
        fake = mock.MagicMock()
        fake.tg_bot = None
        fake.chat_id = None
        fake.loop = None
        self.assertIsNone(trader_260725.CryptoTrader.send_tg_notification(fake, "hi"))


class BotRunnerEmailConfirmTests(unittest.TestCase):
    """第四轮复审（阻断项）：bot_runner 邮件必须能返回**实际 SMTP 结果**"""

    def test_crash_email_returns_true_on_confirmed_send(self):
        server = mock.MagicMock()
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value = server
            ok = bot_runner.send_email_alert(
                "崩溃报警", subject="测试", event="crash", wait=True)
        self.assertTrue(ok)
        server.sendmail.assert_called_once()

    def test_crash_email_returns_false_when_smtp_raises(self):
        """SMTP 抛错 → 必须 False，调用方据此不得记「已发送」"""
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL", side_effect=OSError("smtp down")):
            ok = bot_runner.send_email_alert(
                "崩溃报警", subject="测试", event="crash", wait=True)
        self.assertFalse(ok)

    def test_crash_email_false_when_gate_blocks(self):
        with mock.patch.object(email_gate, "should_send_email",
                               return_value=(False, "disabled")), \
                mock.patch("smtplib.SMTP_SSL") as smtp:
            ok = bot_runner.send_email_alert(
                "崩溃报警", subject="测试", event="crash", wait=True)
        self.assertFalse(ok)
        smtp.assert_not_called()


class _AtDatetime:
    """可设定时刻（用于循环窗口守卫用例）"""

    moment = None

    @classmethod
    def now(cls, tz=None):
        return cls.moment


class DailyReportDeliveryTests(unittest.TestCase):
    """R4 / F12 / 复审 D1 / D2b（状态机直测 + 循环窗口守卫）"""

    def _run_loop_once(self, helper_state, moment):
        """驱动一轮循环（sleep 首次即中断），返回 fake（其 _try_daily_report_once 被替换）"""
        fake = mock.MagicMock()
        fake._last_daily_report_date = None
        fake._daily_report_retry_count = 0
        fake._daily_report_retry_date = None
        fake._daily_report_done = {"tg": False, "email": False}
        fake._try_daily_report_once = mock.MagicMock(return_value=helper_state)
        _AtDatetime.moment = moment
        with mock.patch.object(trader_260725, "datetime", _AtDatetime), \
                mock.patch.object(trader_260725.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                trader_260725.CryptoTrader._daily_report_loop(fake)
        return fake

    def test_retry_window_guard(self):
        """只有 08:05/10/15/20/25/30 才尝试；08:04 与 08:31 必须不触发"""
        before = self._run_loop_once("done", _dt.datetime(2026, 9, 25, 8, 4, 0))
        before._try_daily_report_once.assert_not_called()
        after = self._run_loop_once("done", _dt.datetime(2026, 9, 25, 8, 31, 0))
        after._try_daily_report_once.assert_not_called()
        inside = self._run_loop_once("done", _dt.datetime(2026, 9, 25, 8, 5, 0))
        inside._try_daily_report_once.assert_called_once_with("2026-09-25")

    def test_loop_delegates_and_marks_nothing_on_retry(self):
        """循环只做调度：状态机返回 retry 时不得写日期"""
        fake = self._run_loop_once("retry", _dt.datetime(2026, 9, 25, 8, 5, 0))
        self.assertIsNone(fake._last_daily_report_date)
        fake._try_daily_report_once.assert_called_once_with("2026-09-25")

    # ---------------- 复审发现的缺陷回归（2026-09-25 自审 D1/D2b） ----------------
    # 注：首版三条用例把状态**预设到 fake 上再驱动无限循环** —— 无效测试：
    # 循环启动时会重置 _daily_report_retry_* / _daily_report_done（线程生命周期一次），
    # 预设被静默抹掉，用例恒定通过（其中一条已实测恒定绿）。
    # 改为直接验收抽出的状态机 _try_daily_report_once（确定性、无 sleep/时间依赖）。

    def _fake_for_helper(self, result, done=None, count=0, retry_date=None,
                         last_date=None):
        fake = mock.MagicMock()
        fake._last_daily_report_date = last_date
        fake._daily_report_retry_count = count
        fake._daily_report_retry_date = retry_date
        fake._daily_report_done = dict(done or {"tg": False, "email": False})
        fake._send_daily_report = mock.MagicMock(return_value=result)
        return fake

    def test_retry_counter_resets_on_new_day(self):
        """D1：跨日必须重置重试额度（修复前沿用前一日残留 → 次日首次失败即放弃）"""
        fake = self._fake_for_helper(result={"tg": False, "email": True},
                                     count=6, retry_date="2026-09-24")
        state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "retry")
        self.assertEqual(fake._daily_report_retry_count, 1)      # 不是 7
        self.assertIsNone(fake._last_daily_report_date)          # 未放弃
        self.assertEqual(fake._daily_report_retry_date, "2026-09-25")

    def test_confirmed_channel_not_resent_on_retry(self):
        """D2b：邮件已确认后重试只补 TG（修复前 25 分钟内会重复发 6 封日报邮件）"""
        fake = self._fake_for_helper(result={"tg": False, "email": None},
                                     done={"tg": False, "email": True},
                                     retry_date="2026-09-25")
        trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        channels = fake._send_daily_report.call_args.kwargs.get("channels")
        self.assertEqual(tuple(channels), ("tg",))

    def test_both_channels_done_marks_date_without_resend(self):
        fake = self._fake_for_helper(result={"tg": True, "email": True},
                                     done={"tg": True, "email": True},
                                     retry_date="2026-09-25")
        state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "done")
        fake._send_daily_report.assert_not_called()
        self.assertEqual(fake._last_daily_report_date, "2026-09-25")

    def test_retry_exhaustion_after_six_attempts(self):
        """反向对照：额度用尽才放弃（同日第 6 次失败 → exhausted + 标记日期）"""
        fake = self._fake_for_helper(result={"tg": False, "email": True},
                                     retry_date="2026-09-25", count=5)
        state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "exhausted")
        self.assertEqual(fake._daily_report_retry_count, 6)
        self.assertEqual(fake._last_daily_report_date, "2026-09-25")

    def test_idle_after_day_done(self):
        fake = self._fake_for_helper(result={"tg": True, "email": True},
                                     last_date="2026-09-25")
        state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "idle")
        fake._send_daily_report.assert_not_called()

    # ---------------- 第三轮复审（D5：策略关闭 ≠ 投递失败） ----------------

    def test_daily_report_email_switch_off_degrades_to_tg_only(self):
        """D5：DAILY_REPORT_EMAIL_ENABLED=false 是**策略关闭**，
        不得被当成投递失败 → 触发 6 次重试与每天 ❌『需人工确认』误报。"""
        fake = self._fake_for_helper(result={"tg": True, "email": False})
        with mock.patch.object(email_gate, "daily_report_email_enabled",
                               return_value=False):
            state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "done")
        channels = fake._send_daily_report.call_args.kwargs.get("channels")
        self.assertEqual(tuple(channels), ("tg",))
        self.assertEqual(fake._last_daily_report_date, "2026-09-25")

    def test_master_email_switch_off_degrades_to_tg_only(self):
        """D5：EMAIL_ALERT_ENABLED=false（总开关）同样按策略关闭处理"""
        fake = self._fake_for_helper(result={"tg": True, "email": False})
        with mock.patch.object(email_gate, "email_enabled", return_value=False):
            state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "done")
        channels = fake._send_daily_report.call_args.kwargs.get("channels")
        self.assertEqual(tuple(channels), ("tg",))

    def test_email_included_when_policy_enabled(self):
        """反向对照：策略允许时 email 必须在 todo 中（防止过度抑制）"""
        fake = self._fake_for_helper(result={"tg": True, "email": True})
        with mock.patch.object(email_gate, "daily_report_email_enabled",
                               return_value=True), \
                mock.patch.object(email_gate, "email_enabled", return_value=True):
            state = trader_260725.CryptoTrader._try_daily_report_once(fake, "2026-09-25")
        self.assertEqual(state, "done")
        channels = fake._send_daily_report.call_args.kwargs.get("channels")
        self.assertEqual(set(channels), {"tg", "email"})



class DailyReportStatePersistenceTests(unittest.TestCase):
    """D6：日报投递状态跨进程持久化（窗口内崩溃重启不重复发送）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ".daily_report.state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_roundtrip(self):
        trader_260725._save_daily_report_state(
            {"date": "2026-09-25", "retry_date": "2026-09-25",
             "retry_count": 2, "done": {"tg": True, "email": False}}, self.path)
        st = trader_260725._load_daily_report_state(self.path)
        self.assertEqual(st["date"], "2026-09-25")
        self.assertEqual(st["done"], {"tg": True, "email": False})
        self.assertEqual(st["retry_count"], 2)

    def test_corrupt_state_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(trader_260725._load_daily_report_state(self.path), {})

    def test_missing_state_returns_empty(self):
        self.assertEqual(
            trader_260725._load_daily_report_state(
                os.path.join(self.tmp.name, "nope.json")), {})

    def test_restart_within_window_does_not_resend(self):
        """核心场景：08:05 已发 → 08:10 崩溃重启 → 恢复状态后必须 idle，不得重发"""
        trader_260725._save_daily_report_state(
            {"date": "2026-09-25", "retry_date": "2026-09-25",
             "retry_count": 0, "done": {"tg": True, "email": True}}, self.path)
        ct = trader_260725.CryptoTrader
        fake = mock.MagicMock()
        fake._daily_report_state_file = self.path
        fake._last_daily_report_date = None          # 进程重启：内存状态清零
        fake._daily_report_retry_count = 0
        fake._daily_report_retry_date = None
        fake._daily_report_done = {"tg": False, "email": False}
        fake._send_daily_report = mock.MagicMock()
        # MagicMock 属性会自动生成 mock，必须显式绑定真实实现
        fake._load_daily_report_state_into = lambda: ct._load_daily_report_state_into(fake)
        fake._try_daily_report_once = lambda today: ct._try_daily_report_once(fake, today)

        fake._load_daily_report_state_into()                      # 启动恢复
        state = fake._try_daily_report_once("2026-09-25")
        self.assertEqual(state, "idle")
        fake._send_daily_report.assert_not_called()               # 不重复发送

    def test_persist_after_attempt_writes_all_fields(self):
        ct = trader_260725.CryptoTrader
        fake = mock.MagicMock()
        fake._daily_report_state_file = self.path
        fake._last_daily_report_date = "2026-09-25"
        fake._daily_report_retry_date = "2026-09-25"
        fake._daily_report_retry_count = 3
        fake._daily_report_done = {"tg": True, "email": False}
        ct._persist_daily_report_state(fake)
        st = trader_260725._load_daily_report_state(self.path)
        self.assertEqual(st["retry_count"], 3)
        self.assertEqual(st["done"], {"tg": True, "email": False})
        self.assertEqual(st["date"], "2026-09-25")


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
