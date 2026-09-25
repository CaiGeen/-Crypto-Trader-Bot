#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_m1_m4_maintenance.py —— 合并维护批次 M1~M4 专项测试（2026-09-25）

背景（第七轮复审 / ChatGPT 复核 f0df163）：
  M1 SMTP 三态（失败 / 超时但在途 / 确认）+ 同进程在途去重 —— 消除
     「join 超时报未确认 → 线程后来成功 → 下一轮重试 → 同一封邮件到达两次」
  M2 消除第 3 批次起的轮询断崖（F6：<=2 → 10~15s，<=4 → 原 75~100s）
  M3 D10：修正「改 .env 即时生效无需重启」的误导文案（实为需重启）
  M4 启动配置横幅：打印**进程内真正生效**的值（A9 类验收缺口的永久解法）

运行：.venv/Scripts/python.exe -m pytest test_m1_m4_maintenance.py -q
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import email_gate  # noqa: E402
import bot_runner  # noqa: E402
import trader_260725  # noqa: E402

MAIL_ENV = {
    "QQ_MAIL_USER": "u@example.com",
    "QQ_MAIL_AUTH_CODE": "code",
    "QQ_MAIL_TO": "to@example.com",
    "EMAIL_ALERT_ENABLED": "true",
    "EMAIL_ALERT_ONLY_WITH_POSITION": "true",
}
KEY = email_gate.mail_send_key("crash", "单元测试崩溃")


class _SlowSmtp:
    """模拟慢 SMTP：login 阻塞 delay 秒（模拟实测 31s 的量级，不真的等）"""

    def __init__(self, delay=0.6, fail=False):
        self.delay = delay
        self.fail = fail
        self.sent = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *a, **k):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("SMTP 登录失败")

    def sendmail(self, *a, **k):
        self.sent += 1


def _wait_slot_free(key, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not email_gate.mail_in_flight(key):
            return True
        time.sleep(0.05)
    return False


class M1InFlightDedupTests(unittest.TestCase):
    def setUp(self):
        email_gate.reset_mail_state_for_test()

    def tearDown(self):
        email_gate.reset_mail_state_for_test()

    def test_m1_suppressed_while_previous_in_flight(self):
        """核心修复：第一封在途时，第二封**不得**启动 → 无重复投递"""
        slow = _SlowSmtp(0.6)
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL", return_value=slow) as smtp:
            ok1 = bot_runner.send_email_alert("x", subject="单元测试崩溃",
                                              event="crash", wait=False)
            ok2 = bot_runner.send_email_alert("x", subject="单元测试崩溃",
                                              event="crash", wait=False)
            self.assertTrue(ok1)     # 已提交
            self.assertFalse(ok2)    # 在途 → 抑制
            self.assertEqual(smtp.call_count, 1, "在途期间不得建立第二个 SMTP 会话")
            self.assertEqual(email_gate.mail_last_result(KEY),
                             email_gate.MAIL_RESULT_SUPPRESSED_IN_FLIGHT)
            self.assertTrue(_wait_slot_free(KEY))
            self.assertEqual(slow.sent, 1)

    def test_m1_slot_released_then_next_send_allowed(self):
        """线程真正结束后槽位释放，后续发送恢复正常（不永久禁发）"""
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL", return_value=_SlowSmtp(0.05)) as smtp:
            bot_runner.send_email_alert("x", subject="单元测试崩溃",
                                        event="crash", wait=True)
            self.assertTrue(_wait_slot_free(KEY))
            self.assertEqual(email_gate.mail_last_result(KEY),
                             email_gate.MAIL_RESULT_CONFIRMED)
            ok = bot_runner.send_email_alert("x", subject="单元测试崩溃",
                                            event="crash", wait=True)
            self.assertTrue(ok)
            self.assertEqual(smtp.call_count, 2)

    def test_m1_failure_is_distinct_from_timeout(self):
        """三态可区分：失败记 failed；超时在途记 timeout_in_flight（≠ failed）"""
        self.assertNotEqual(email_gate.MAIL_RESULT_FAILED,
                            email_gate.MAIL_RESULT_TIMEOUT_IN_FLIGHT)
        with mock.patch.dict(os.environ, MAIL_ENV), \
                mock.patch("smtplib.SMTP_SSL", return_value=_SlowSmtp(0.05, fail=True)):
            ok = bot_runner.send_email_alert("x", subject="单元测试崩溃",
                                             event="crash", wait=True)
            self.assertFalse(ok)
            self.assertEqual(email_gate.mail_last_result(KEY),
                             email_gate.MAIL_RESULT_FAILED)

    def test_m1_timeout_state_keeps_slot(self):
        """在途未结束时标 timeout_in_flight 不清槽位；且只释放自己的槽"""
        slot = object()
        self.assertTrue(email_gate.claim_mail_slot(KEY, slot))
        self.assertTrue(email_gate.mail_in_flight(KEY))
        email_gate.release_mail_slot(KEY, object())   # 非本线程：不得释放
        self.assertTrue(email_gate.mail_in_flight(KEY))
        email_gate.record_mail_result(KEY, email_gate.MAIL_RESULT_TIMEOUT_IN_FLIGHT)
        self.assertTrue(email_gate.mail_in_flight(KEY),
                        "timeout_in_flight 期间槽位必须保持占用")
        email_gate.release_mail_slot(KEY, slot)
        self.assertFalse(email_gate.mail_in_flight(KEY))


class M2PollingCliffTests(unittest.TestCase):
    """F6：轮询分级必须连续，且第 3 档起不得回到 75~100s 量级"""

    def _interval(self, n):
        fake = mock.Mock()
        fake._get_active_batch_count.return_value = n
        return trader_260725.CryptoTrader._calculate_monitoring_interval(fake)

    def test_m2_tiers_continuous_and_bounded(self):
        expected = {0: (10, 15), 1: (10, 15), 2: (10, 15),
                    3: (20, 30), 4: (20, 30), 5: (30, 40), 6: (30, 40),
                    7: (45, 60), 12: (45, 60)}
        for n, (lo, hi) in expected.items():
            for _ in range(5):
                v = self._interval(n)
                self.assertGreaterEqual(v, lo, f"批次 {n} 低于下界")
                self.assertLessEqual(v, hi, f"批次 {n} 高于上界")
        # 连续性判据：相邻档位的**上界**不得出现 >2 倍断崖
        # （旧实现 15s → 100s 为 6.7 倍，正是 F6 所指的缺陷）
        maxes = [max(self._interval(n) for _ in range(20)) for n in range(0, 13)]
        for n, (prev, cur) in enumerate(zip(maxes, maxes[1:])):
            self.assertLessEqual(cur, prev * 2.0 + 1e-6,
                                 f"批次 {n} → {n + 1} 出现断崖式跃升")
        # 第 3 档起必须显著快于旧值 75~100s
        self.assertLessEqual(max(maxes[3:]), 60.0, "第 3 档起必须快于 60s")

    def test_m2_source_no_longer_has_old_tiers(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "trader_260725.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("base_interval = 75.0", src)
        self.assertNotIn("base_interval = 120.0", src)


class M3D10WordingTests(unittest.TestCase):
    def test_m3_no_false_immediate_effect_claim(self):
        root = os.path.dirname(os.path.abspath(__file__))
        for name in ("trader_260725.py", "bot_runner.py"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("即时生效无需重启", src, f"{name} 仍含误导文案")
        with open(os.path.join(root, "trader_260725.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("需重启 watchdog/bot_runner 才对运行中进程生效", src)


class M4ConfigBannerTests(unittest.TestCase):
    def test_m4_banner_reflects_in_process_env(self):
        env = {"RISK_MAX_ACTIVE_BATCHES": "2", "MAX_LEVERAGE": "100",
               "DAILY_REPORT_EMAIL_ENABLED": "false"}
        with mock.patch.dict(os.environ, env, clear=False):
            line = bot_runner.log_effective_config()
        self.assertIn("RISK_MAX_ACTIVE_BATCHES=2", line)
        self.assertIn("DAILY_REPORT_EMAIL_ENABLED=False", line)
        self.assertIn("FATAL_EVENTS=", line)
        for key in ("RISK_MAX_ACTIVE_SYMBOLS", "EMAIL_ALERT_ENABLED",
                    "EMAIL_ALERT_ONLY_WITH_POSITION", "MAX_LEVERAGE"):
            self.assertIn(key, line)

    def test_m4_banner_shows_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            line = bot_runner.log_effective_config()
        self.assertIn("RISK_MAX_ACTIVE_BATCHES=3（默认）", line)


if __name__ == "__main__":
    unittest.main()
