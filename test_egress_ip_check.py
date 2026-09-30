#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_egress_ip_check.py —— D 出口 IP 白名单核验（健康巡检.py）离线验收测试

背景（2026-09-30 实盘事件）：
  代理 FlClash 在多个出口节点间轮换 → 新出口不在币安 API key 白名单 →
  签名请求 -2015 → 生产代码单次即进入盲区安全模式（需人工 /auth_reset）。
  既有 `_check_ip_periodically` 只在有活跃批次的监控循环里跑，空仓期无人巡检。

D 的契约（ChatGPT 授权实现）：
  1. 已登记且未过 TTL 的出口 IP → 零币安签名 API 调用（仍查一次出口 IP 以比对，
     该查询走 ipify、非鉴权、不消耗交易所 weight）；
  2. 新出口 + 探活通过        → 登记进 .egress_ip.state.json，**不告警**（新 IP 合法是常态）；
  3. 新出口 + 币安明确回 -2015 → 报 issue `egress_ip_rejected`（交由 alert 走 30min 去重）；
  4. 取不到出口 IP / 网络失败  → 只记日志，**绝不告警**（代理抖动不能变成噪音）；
  5. 探活必须是**只读** GET /fapi/v1/balance，带签名与 X-MBX-APIKEY，绝无下单路径；
  6. 任何自身异常按设计约束④ 不得抛出；
  7. 状态文件损坏当空表；条目数封顶，防无限增长。

运行：.venv\\Scripts\\python.exe test_egress_ip_check.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SPEC = importlib.util.spec_from_file_location(
    "patrol_egress", os.path.join(HERE, "健康巡检.py"))
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)

ENV = {"BINANCE_API_KEY": "test-key", "BINANCE_SECRET": "test-secret",
       "BINANCE_PROXY": "http://127.0.0.1:7890"}


class _HTTP:
    """可编排的 _egress_http_get 替身：按 URL 分派，记录全部调用。"""

    def __init__(self, ip="203.0.113.7", ip_ok=True,
                 probe_status=200, probe_body='[{"balance":"1"}]',
                 probe_raises=None, ip_raises=None):
        self.ip, self.ip_ok = ip, ip_ok
        self.probe_status, self.probe_body = probe_status, probe_body
        self.probe_raises, self.ip_raises = probe_raises, ip_raises
        self.calls = []

    def __call__(self, url, proxy_url="", timeout=10, headers=None):
        self.calls.append({"url": url, "proxy": proxy_url,
                           "timeout": timeout, "headers": dict(headers or {})})
        if "ipify" in url or "ifconfig" in url:
            if self.ip_raises:
                raise self.ip_raises
            if not self.ip_ok:
                raise OSError("probe: DNS failure")
            return 200, json.dumps({"ip": self.ip})
        if self.probe_raises:
            raise self.probe_raises
        return self.probe_status, self.probe_body

    @property
    def probe_calls(self):
        return [c for c in self.calls if "/fapi/v1/balance" in c["url"]]

    @property
    def ip_calls(self):
        return [c for c in self.calls if "ipify" in c["url"] or "ifconfig" in c["url"]]


class EgressBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, ".egress_ip.state.json")
        # 隔离：状态文件、日志都指向临时目录；日志静音
        self._orig = (p.EGRESS_STATE_FILE, p.LOG_FILE, p.LOG_DIR,
                      p.EGRESS_CHECK_ENABLED, p.EGRESS_VERIFY_TTL_SECONDS, p.log)
        p.EGRESS_STATE_FILE = self.state_file
        p.LOG_DIR = self.tmp.name
        p.LOG_FILE = os.path.join(self.tmp.name, "patrol.log")
        p.EGRESS_CHECK_ENABLED = True
        p.EGRESS_VERIFY_TTL_SECONDS = 21600
        self.logs = []
        p.log = lambda msg: self.logs.append(str(msg))
        self.addCleanup(self._restore)

    def _restore(self):
        (p.EGRESS_STATE_FILE, p.LOG_FILE, p.LOG_DIR, p.EGRESS_CHECK_ENABLED,
         p.EGRESS_VERIFY_TTL_SECONDS, p.log) = self._orig

    def use_http(self, fake):
        p._egress_http_get = fake
        self.addCleanup(lambda: setattr(p, "_egress_http_get", _REAL_HTTP))

    def run_check_e(self):
        summary = []
        issues = p.check_egress_ip(dict(ENV), ENV["BINANCE_PROXY"], summary)
        return issues, summary

    def read_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


_REAL_HTTP = p._egress_http_get


class CachedIpTests(EgressBase):
    def test_registered_ip_makes_zero_signed_api_calls(self):
        """契约1：已登记且未过期 → 零币安签名 API 调用、无告警。

        注意：仍必须查一次出口 IP（ipify，非币安、非鉴权），否则无从判断"是否已登记"。
        这里断言的是**不消耗币安 weight、不触发鉴权**。
        """
        now = time.time()
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"203.0.113.7": now - 60}, f)
        fake = _HTTP()
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [],
                         "已登记 IP 不应发起任何币安签名探活")
        self.assertEqual(len(fake.ip_calls), 1, "只查一次出口 IP")
        self.assertTrue(any("203.0.113.7" in s for s in summary), summary)

    def test_expired_entry_reprobes(self):
        """契约1 反面：过 TTL 必须复检（防白名单后来被移除）。"""
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"203.0.113.7": time.time() - (21600 + 5)}, f)
        fake = _HTTP(probe_status=200)
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(len(fake.probe_calls), 1, "过期必须重新探活")
        # 复检通过后时间戳被刷新
        self.assertAlmostEqual(self.read_state()["203.0.113.7"], time.time(), delta=5)


class NewIpTests(EgressBase):
    def test_new_ip_probe_ok_registers_without_alert(self):
        """契约2：新出口 + 探活通过 → 登记、不告警。"""
        fake = _HTTP(ip="198.51.100.23", probe_status=200)
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(len(fake.ip_calls), 1)
        self.assertEqual(len(fake.probe_calls), 1)
        self.assertIn("198.51.100.23", self.read_state())
        self.assertTrue(any("探活通过" in s for s in summary), summary)

    def test_probe_is_readonly_signed_balance_get(self):
        """契约5：探活必须是只读 balance GET + 签名 + API key 头，绝无下单路径。"""
        fake = _HTTP()
        self.use_http(fake)
        self.run_check_e()

        call = fake.probe_calls[0]
        self.assertTrue(call["url"].startswith("https://fapi.binance.com/fapi/v1/balance?"),
                        call["url"])
        self.assertIn("signature=", call["url"])
        self.assertIn("timestamp=", call["url"])
        for banned in ("order", "leverage", "position", "listenKey"):
            self.assertNotIn(banned, call["url"].lower(), "探活 URL 不得含写操作路径")
        self.assertEqual(call["headers"].get("X-MBX-APIKEY"), "test-key")
        self.assertEqual(call["proxy"], ENV["BINANCE_PROXY"], "探活必须经代理（与 bot 同路）")

    def test_rejected_ip_raises_issue_and_not_registered(self):
        """契约3：币安明确回 -2015 → 报 egress_ip_rejected，且不得记为已核验。"""
        fake = _HTTP(ip="192.0.2.55", probe_status=400,
                     probe_body='{"code":-2015,"msg":"Invalid API-key, IP, or '
                                'permissions for action, request ip: 192.0.2.55"}')
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(len(issues), 1, issues)
        key, title, detail = issues[0]
        self.assertEqual(key, "egress_ip_rejected")
        self.assertIn("192.0.2.55", title)
        self.assertIn("-2015", detail)
        self.assertIn("auth_reset", detail, "处置说明必须指向人工恢复方式")
        self.assertIsNone(self.read_state(), "被拒出口不得被登记成已核验")
        self.assertEqual(summary, [])

    def test_non_auth_http_error_is_silent(self):
        """契约4：网关/限频类错误 ≠ 不在白名单 → 不告警，只记日志。"""
        fake = _HTTP(probe_status=503, probe_body="<html>Service Unavailable</html>")
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state())
        self.assertTrue(any("未完成" in m for m in self.logs), self.logs)

    def test_ip_lookup_failure_is_silent(self):
        """契约4：取不到出口 IP → 不告警、不探活。"""
        fake = _HTTP(ip_raises=OSError("proxy refused"))
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [], "取不到 IP 时不应探活")
        self.assertTrue(any("出口 IP 获取失败" in m for m in self.logs), self.logs)

    def test_probe_network_failure_is_silent(self):
        """契约4：探活网络失败 → 不告警（不能把网络抖动当白名单问题）。"""
        fake = _HTTP(probe_raises=OSError("connection reset"))
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state())


class RobustnessTests(EgressBase):
    def test_disabled_check_makes_no_calls(self):
        p.EGRESS_CHECK_ENABLED = False
        fake = _HTTP()
        self.use_http(fake)
        self.assertEqual(self.run_check_e()[0], [])
        self.assertEqual(fake.calls, [])

    def test_missing_credentials_skip_probe(self):
        """无 key/secret → 跳过探活，不告警。"""
        fake = _HTTP()
        self.use_http(fake)
        env = dict(ENV)
        env.pop("BINANCE_API_KEY")
        issues = p.check_egress_ip(env, ENV["BINANCE_PROXY"], [])
        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [])
        self.assertTrue(any("跳过" in m for m in self.logs), self.logs)

    def test_corrupt_state_file_treated_as_empty(self):
        """契约7：状态文件损坏 → 当空表，不抛出、按新 IP 走探活。"""
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write("{not-json!!")
        fake = _HTTP()
        self.use_http(fake)
        issues, _ = self.run_check_e()
        self.assertEqual(issues, [])
        self.assertEqual(len(fake.probe_calls), 1)
        self.assertIn("203.0.113.7", self.read_state())

    def test_state_file_capped(self):
        """契约7：条目数封顶，防无限增长。"""
        ips = {f"10.0.0.{i}": time.time() - i * 60 for i in range(1, 60)}
        p._egress_state_save(ips)
        saved = self.read_state()
        self.assertLessEqual(len(saved), p.EGRESS_STATE_MAX_ENTRIES)
        # 保留的应是最新的
        self.assertIn("10.0.0.1", saved)
        self.assertNotIn("10.0.0.59", saved)

    def test_self_exception_never_propagates(self):
        """契约6：任何自身异常按设计约束④ 不得抛出。"""
        def boom(*a, **k):
            raise RuntimeError("injected self-failure")
        p.get_egress_ip = boom
        self.addCleanup(lambda: setattr(p, "get_egress_ip", _REAL_GET_IP))
        issues = p.check_egress_ip(dict(ENV), ENV["BINANCE_PROXY"], [])
        self.assertEqual(issues, [])
        self.assertTrue(any("按设计不抛出" in m for m in self.logs), self.logs)


_REAL_GET_IP = p.get_egress_ip


class RunCheckIntegrationTests(unittest.TestCase):
    """端到端：rejected 出口必须能穿过 run_check 到达 alert。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self._orig = {name: getattr(p, name) for name in (
            "HEARTBEAT_FILE", "TRADE_STATE_FILE", "HEALTH_DIR", "ALERT_STATE_FILE",
            "LOG_FILE", "LOG_DIR", "EGRESS_STATE_FILE", "EGRESS_CHECK_ENABLED",
            "EGRESS_VERIFY_TTL_SECONDS", "log", "alert", "get_egress_ip",
            "probe_auth_for_ip", "_egress_http_get")}
        now = time.time()
        hb = os.path.join(d, ".heartbeat.json")
        with open(hb, "w", encoding="utf-8") as f:
            json.dump({"ts": now, "ts_str": "test", "watchdog_pid": 1, "bot_pid": 2,
                       "bot_alive": True, "stopped": False, "restarts": 0}, f)
        with open(os.path.join(d, "trade_state.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        health = os.path.join(d, ".bot_health")
        os.makedirs(health, exist_ok=True)
        with open(os.path.join(health, "control.json"), "w", encoding="utf-8") as f:
            json.dump({"instance_id": "i-test", "ts": now}, f)

        p.HEARTBEAT_FILE = hb
        p.TRADE_STATE_FILE = os.path.join(d, "trade_state.json")
        p.HEALTH_DIR = health
        p.ALERT_STATE_FILE = os.path.join(d, ".patrol_alert.state.json")
        p.LOG_DIR = d
        p.LOG_FILE = os.path.join(d, "patrol.log")
        p.EGRESS_STATE_FILE = os.path.join(d, ".egress_ip.state.json")
        p.EGRESS_CHECK_ENABLED = True
        p.EGRESS_VERIFY_TTL_SECONDS = 21600
        p.log = lambda msg: None
        p._egress_http_get = _HTTP()   # 兜底（正常路径会被下面的桩短路）
        self.addCleanup(self._restore)

    def _restore(self):
        for name, val in self._orig.items():
            setattr(p, name, val)

    def test_rejected_egress_reaches_alert(self):
        p.get_egress_ip = lambda env, proxy_url="": "192.0.2.77"
        p.probe_auth_for_ip = lambda env, proxy_url="": (
            "rejected", 'HTTP 400: {"code":-2015,"msg":"Invalid API-key, IP..."}')
        captured = []
        p.alert = lambda env, key, title, detail, dry: captured.append((key, title, detail))

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0, "巡检自身必须 exit 0")
        self.assertEqual([k for k, _, _ in captured], ["egress_ip_rejected"], captured)
        self.assertIn("192.0.2.77", captured[0][1])

    def test_accepted_egress_stays_silent(self):
        p.get_egress_ip = lambda env, proxy_url="": "203.0.113.7"
        p.probe_auth_for_ip = lambda env, proxy_url="": ("ok", "HTTP 200")
        captured = []
        p.alert = lambda env, key, title, detail, dry: captured.append((key, title, detail))

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual(captured, [], "合法出口 IP 不得打扰运维")
        with open(p.EGRESS_STATE_FILE, encoding="utf-8") as f:
            self.assertIn("203.0.113.7", json.load(f))


if __name__ == "__main__":
    unittest.main(verbosity=2)
