#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_egress_ip_check.py —— D 出口 IP 白名单核验（健康巡检.py）离线验收测试

背景（2026-09-30 实盘事件）：
  代理 FlClash 在多出口节点间轮换 → 新出口不在币安 API key 白名单 →
  签名请求 -2015 → 生产代码单次即进入盲区安全模式（需人工 /auth_reset）。
  既有 `_check_ip_periodically` 只在有活跃批次的监控循环里跑，空仓期无人巡检。

契约（2026-10-01 复审后修订）：
  1. 已登记且未过 TTL → 零币安签名 API 调用（仍查一次出口 IP 以比对）；
  2. 新出口 + 探活通过 → 登记 verified、清 rejected，**不告警**；
  3. 币安明确回 -2015/-2014 → issue `egress_ip_rejected:{ip}`；
     **必须覆盖真实 urllib 路径**：币安 4xx 会抛 HTTPError，不捕获就永远不告警（P0）；
     判定按响应体**结构化 code**，不搜原始子串——余额含 -20150.5 不得误报（第三轮 P1）；
  4. 取不到出口 IP / 网络失败 / 非鉴权 HTTP 错 / -1021 / **2xx 非 balance 数组体**
     → 只记日志，**绝不告警、绝不登记**；
  5. 探活是**只读** GET /fapi/v1/balance，带签名与 X-MBX-APIKEY，绝无下单路径；
  6. 任何自身异常按设计约束④ 不得抛出（含 .env 数值解析）；
  7. 状态文件损坏当空表；verified/rejected 各自封顶；兼容首版扁平格式；
  8. 被拒后进入冷却期：不重复发失败签名请求，但**仍出 issue**（P1 防限流稀释信号）；
  9. 停机(stopped)期间巡检照常核验出口 IP，但守护链类问题仍静默（P1b）。

运行：.venv\\Scripts\\python.exe test_egress_ip_check.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SPEC = importlib.util.spec_from_file_location(
    "patrol_egress", os.path.join(HERE, "健康巡检.py"))
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)

ENV = {"BINANCE_API_KEY": "test-key", "BINANCE_SECRET": "test-secret",
       "BINANCE_PROXY": "http://127.0.0.1:7890"}

_REAL_HTTP = p._egress_http_get
_REAL_GET_IP = p.get_egress_ip


class _HTTP:
    """可编排的 _egress_http_get 替身：按 URL 分派，记录全部调用。"""

    def __init__(self, ip="203.0.113.7", ip_ok=True, ip_body=None,
                 probe_status=200, probe_body='[{"balance":"1"}]',
                 probe_raises=None, ip_raises=None):
        self.ip, self.ip_ok, self.ip_body = ip, ip_ok, ip_body
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
            if self.ip_body is not None:
                return 200, self.ip_body
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
        self._orig = (p.EGRESS_STATE_FILE, p.ALERT_STATE_FILE, p.LOG_FILE, p.LOG_DIR,
                      p.EGRESS_CHECK_ENABLED, p.EGRESS_VERIFY_TTL_SECONDS,
                      p.EGRESS_REJECT_COOLDOWN_SECONDS, p.log)
        p.EGRESS_STATE_FILE = self.state_file
        p.ALERT_STATE_FILE = os.path.join(self.tmp.name, ".patrol_alert.state.json")
        p.LOG_DIR = self.tmp.name
        p.LOG_FILE = os.path.join(self.tmp.name, "patrol.log")
        p.EGRESS_CHECK_ENABLED = True
        p.EGRESS_VERIFY_TTL_SECONDS = 21600
        p.EGRESS_REJECT_COOLDOWN_SECONDS = 3600
        self.logs = []
        p.log = lambda msg: self.logs.append(str(msg))
        self.addCleanup(self._restore)

    def _restore(self):
        (p.EGRESS_STATE_FILE, p.ALERT_STATE_FILE, p.LOG_FILE, p.LOG_DIR,
         p.EGRESS_CHECK_ENABLED, p.EGRESS_VERIFY_TTL_SECONDS,
         p.EGRESS_REJECT_COOLDOWN_SECONDS, p.log) = self._orig

    def use_http(self, fake):
        p._egress_http_get = fake
        self.addCleanup(lambda: setattr(p, "_egress_http_get", _REAL_HTTP))

    def run_check_e(self, env=None):
        summary = []
        issues = p.check_egress_ip(dict(env or ENV), ENV["BINANCE_PROXY"], summary)
        return issues, summary

    def read_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def write_verified(self, mapping):
        p._egress_state_save({"verified": mapping, "rejected": {}})

    def write_rejected(self, mapping):
        p._egress_state_save({"verified": {}, "rejected": mapping})

    def has_log(self, needle):
        return any(needle in m for m in self.logs)


# ==================== 契约1：已登记（缓存） ====================
class CachedIpTests(EgressBase):
    def test_registered_ip_makes_zero_signed_api_calls(self):
        """契约1：已登记且未过期 → 零币安签名调用、无告警。

        仍必须查一次出口 IP（ipify，非鉴权），否则无从判断"是否已登记"。
        """
        self.write_verified({"203.0.113.7": time.time() - 60})
        fake = _HTTP()
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [], "已登记 IP 不应发起任何币安签名探活")
        self.assertEqual(len(fake.ip_calls), 1, "只查一次出口 IP")
        self.assertTrue(any("203.0.113.7" in s for s in summary), summary)

    def test_expired_entry_reprobes(self):
        """契约1 反面：过 TTL 必须复检（防白名单后来被移除）。"""
        self.write_verified({"203.0.113.7": time.time() - (21600 + 5)})
        fake = _HTTP(probe_status=200)
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(len(fake.probe_calls), 1, "过期必须重新探活")
        self.assertAlmostEqual(self.read_state()["verified"]["203.0.113.7"],
                               time.time(), delta=5)

    def test_legacy_flat_state_still_readable(self):
        """契约7：D 首版扁平格式 {ip: ts} 仍可读，不当成新 IP 重复探活。"""
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"203.0.113.7": time.time() - 60}, f)
        fake = _HTTP()
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [], "旧格式已核验 IP 不应重复探活")


# ==================== 契约2/3/4/5：新出口 ====================
class NewIpTests(EgressBase):
    def test_new_ip_probe_ok_registers_without_alert(self):
        """契约2：新出口 + 探活通过 → 登记、不告警。"""
        fake = _HTTP(ip="198.51.100.23", probe_status=200)
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(len(fake.probe_calls), 1)
        self.assertIn("198.51.100.23", self.read_state()["verified"])
        self.assertTrue(any("探活通过" in s for s in summary), summary)

    def test_probe_is_readonly_signed_balance_get(self):
        """契约5：只读 balance GET + 签名 + API key 头 + 60s recvWindow，绝无下单路径。"""
        fake = _HTTP()
        self.use_http(fake)
        self.run_check_e()

        call = fake.probe_calls[0]
        self.assertTrue(
            call["url"].startswith("https://fapi.binance.com/fapi/v1/balance?"),
            call["url"])
        self.assertIn("signature=", call["url"])
        self.assertIn("timestamp=", call["url"])
        self.assertIn("recvWindow=60000", call["url"], "P2：需容忍 60s 内时钟偏移")
        for banned in ("order", "leverage", "position", "listenKey"):
            self.assertNotIn(banned, call["url"].lower(), "探活 URL 不得含写操作路径")
        self.assertEqual(call["headers"].get("X-MBX-APIKEY"), "test-key")
        self.assertEqual(call["proxy"], ENV["BINANCE_PROXY"], "探活必须经代理（与 bot 同路）")

    def test_rejected_ip_raises_issue_and_not_registered_verified(self):
        """契约3：-2015 → issue `egress_ip_rejected:{ip}`，记 rejected、不入 verified。"""
        fake = _HTTP(ip="192.0.2.55", probe_status=400,
                     probe_body='{"code":-2015,"msg":"Invalid API-key, IP, or '
                                'permissions for action, request ip: 192.0.2.55"}')
        self.use_http(fake)

        issues, summary = self.run_check_e()

        self.assertEqual(len(issues), 1, issues)
        key, title, detail = issues[0]
        self.assertTrue(key.startswith("egress_ip_rejected"), key)
        self.assertIn("192.0.2.55", key, "P3：去重键必须带 IP")
        self.assertIn("192.0.2.55", title)
        self.assertIn("-2015", detail)
        self.assertIn("auth_reset", detail, "处置说明必须指向人工恢复方式")
        state = self.read_state()
        self.assertIn("192.0.2.55", state["rejected"], "须进入拒绝冷却桶")
        self.assertEqual(state["verified"], {})
        self.assertEqual(summary, [])

    def test_attribution_mentions_both_causes(self):
        """P2：不得把 -2014/key 权限问题一口咬定成"IP 不在白名单"。"""
        fake = _HTTP(probe_status=400,
                     probe_body='{"code":-2015,"msg":"Invalid API-key, IP, or '
                                'permissions for action"}')
        self.use_http(fake)
        issues, _ = self.run_check_e()
        detail = issues[0][2]
        self.assertIn("不在币安 API key 白名单", detail)
        self.assertIn("key 被禁用或权限不足", detail, "必须给出第二类原因供排查")

    def test_non_auth_http_error_is_silent(self):
        """契约4：网关/限频类错误 ≠ 鉴权拒绝 → 不告警，只记日志。"""
        fake = _HTTP(probe_status=503, probe_body="<html>Service Unavailable</html>")
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state())
        self.assertTrue(self.has_log("未完成"), self.logs)

    def test_clock_skew_1021_is_logged_distinctly(self):
        """P2：-1021（时钟偏移）要能被运维一眼看到，不能混进"未完成"。"""
        fake = _HTTP(probe_status=400,
                     probe_body='{"code":-1021,"msg":"Timestamp for this request '
                                'is outside of the recvWindow"}')
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertTrue(self.has_log("时间戳校验拒绝(-1021)"), self.logs)
        self.assertFalse(self.has_log("未完成"), "时钟问题不应被泛化成网络未完成")

    def test_ip_lookup_failure_is_silent(self):
        """契约4：取不到出口 IP → 不告警、不探活。"""
        fake = _HTTP(ip_raises=OSError("proxy refused"))
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [], "取不到 IP 时不应探活")
        self.assertTrue(self.has_log("出口 IP 获取失败"), self.logs)

    def test_non_ip_response_body_is_rejected(self):
        """P2：HTML/JSON 首行不得被当 IP 登记。"""
        fake = _HTTP(ip_body="<!DOCTYPE html>\n<html>...</html>")
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [], "非 IP 响应不应继续探活")
        self.assertIsNone(self.read_state(), "非 IP 文本不得写入状态文件")
        self.assertTrue(self.has_log("出口 IP 获取失败"), self.logs)

    def test_probe_network_failure_is_silent(self):
        """契约4：探活网络失败 → 不告警（不能把网络抖动当白名单问题）。"""
        fake = _HTTP(probe_raises=OSError("connection reset"))
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state())


class IpLiteralTests(EgressBase):
    def test_ip_literal_validation(self):
        self.assertTrue(p._is_ip_address("1.2.3.4"))
        self.assertTrue(p._is_ip_address(" 2001:db8::1 "))
        for bad in ("", None, "<!DOCTYPE html>", '{"ip": "1.2.3.4"}', "999.1.1.1",
                    "not-an-ip", 12345):
            self.assertFalse(p._is_ip_address(bad), repr(bad))


# ==================== 第三轮复审 P1：探活按响应结构定性 ====================
class ProbeClassificationTests(EgressBase):
    """不搜原始子串：余额里含 -2015 的数字不得误报，2xx 非数组体不得登记。"""

    def test_classify_table(self):
        # 入参与真实调用一致：_egress_http_get 返回的是已解码 str
        cases = [
            (200, '[{"asset":"USDT","balance":"1"}]', "ok"),
            # 实测过的假阳性来源：余额负盈亏含 -20150.5 子串
            (200, '[{"asset":"USDT","balance":"1000.0","crossUnrealizedPnl":-20150.5}]', "ok"),
            (200, '[{"balance":"0.1","crossUnrealizedPnl":-2015}]', "ok"),
            # 拒绝码：任何状态码都要认（含 2xx 带错误体的历史形态）
            (200, '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}', "rejected"),
            (400, '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}', "rejected"),
            (401, '{"code":-2014,"msg":"API-key format invalid"}', "rejected"),
            (400, '{"code":"-2015","msg":"..."}', "rejected"),   # 字符串码
            # 2xx 但体不是 balance 数组 → error，绝不登记
            (200, '{"code":-1021,"msg":"Timestamp outside recvWindow"}', "error"),
            (200, "<html>Bad Gateway</html>", "error"),
            (200, "", "error"),
            (200, "null", "error"),
            # 非 2xx：结构化体按 code 判，无拒绝码即 error
            (503, "<html>Service Unavailable</html>", "error"),
            (400, '{"code":-1001,"msg":"Too many requests"}', "error"),
            # 非 2xx + 非 JSON：才允许按拒绝特征兜底
            (400, "request rejected: invalid api-key, request ip: 1.2.3.4", "rejected"),
        ]
        for status, body, expect in cases:
            with self.subTest(status=status, body=body):
                self.assertEqual(p._classify_probe(status, body), expect)

    def test_balance_with_minus2015_digits_does_not_false_alert(self):
        """实测假阳性回归：余额含 -2015 子串 → 必须 ok + 登记，且零告警。"""
        fake = _HTTP(ip="198.51.100.9", probe_status=200,
                     probe_body='[{"asset":"USDT","balance":"1000.0",'
                                '"crossUnrealizedPnl":-20150.5,'
                                '"availableBalance":"999.0"}]')
        self.use_http(fake)

        issues, summary = self.run_check_e()
        state = self.read_state()

        self.assertEqual(issues, [], "正常余额不得产生任何告警")
        self.assertIn("198.51.100.9", state["verified"])
        self.assertEqual(state["rejected"], {})
        self.assertTrue(any("探活通过" in s for s in summary), summary)

    def test_200_non_array_body_is_not_registered(self):
        """2xx 非 balance 数组体 → error，只记日志，不得登记（否则探活形同虚设）。"""
        fake = _HTTP(ip="198.51.100.10", probe_status=200,
                     probe_body="<html>Bad Gateway</html>")
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state(), "非数组体不得写入状态文件")
        self.assertTrue(self.has_log("未完成"), self.logs)

    def test_200_error_object_not_registered_and_shows_clock_hint(self):
        """2xx 返回错误对象（-1021）→ 不登记，且必须给出校时提示而非泛化成网络未完成。"""
        fake = _HTTP(ip="198.51.100.11", probe_status=200,
                     probe_body='{"code":-1021,"msg":"Timestamp for this request '
                                'is outside of the recvWindow"}')
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(issues, [])
        self.assertIsNone(self.read_state())
        self.assertTrue(self.has_log("时间戳校验拒绝(-1021)"), self.logs)
        self.assertFalse(self.has_log("未完成"), "时钟问题应有专门日志")


# ==================== 契约8：拒绝冷却 ====================
class CooldownTests(EgressBase):
    def test_rejected_within_cooldown_skips_probe_but_still_issues(self):
        """P1：冷却期内不再发失败签名请求，但 issue 持续产出（供 alert 去重控频）。"""
        # 冷却记录必须与"当前出口 IP"同为 203.0.113.7（_HTTP 的默认出口）
        self.write_rejected({"203.0.113.7": time.time() - 60})
        fake = _HTTP(probe_status=400, probe_body='{"code":-2015}')
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(len(issues), 1, issues)
        self.assertIn("203.0.113.7", issues[0][0])
        self.assertEqual(fake.probe_calls, [], "冷却期内必须跳过重复探活")
        self.assertTrue(self.has_log("处于拒绝冷却期"), self.logs)

    def test_cooldown_expired_reprobes(self):
        self.write_rejected({"203.0.113.7": time.time() - (3600 + 5)})
        fake = _HTTP(probe_status=400, probe_body='{"code":-2015}')
        self.use_http(fake)

        issues, _ = self.run_check_e()

        self.assertEqual(len(fake.probe_calls), 1, "冷却过期必须重新探活")
        self.assertEqual(len(issues), 1)
        self.assertIn("203.0.113.7", issues[0][0])

    def test_probe_ok_clears_rejected_entry(self):
        """被拒后加白 → 冷却过期复检通过 → 清 rejected、入 verified、告警消失。"""
        self.write_rejected({"203.0.113.9": time.time() - (3600 + 5)})
        fake = _HTTP(ip="203.0.113.9", probe_status=200)
        self.use_http(fake)

        issues, summary = self.run_check_e()

        state = self.read_state()
        self.assertEqual(issues, [])
        self.assertNotIn("203.0.113.9", state["rejected"])
        self.assertIn("203.0.113.9", state["verified"])
        self.assertTrue(any("探活通过" in s for s in summary), summary)


# ==================== 契约6/7：健壮性 ====================
class RobustnessTests(EgressBase):
    def test_disabled_check_makes_no_calls(self):
        p.EGRESS_CHECK_ENABLED = False
        fake = _HTTP()
        self.use_http(fake)
        self.assertEqual(self.run_check_e()[0], [])
        self.assertEqual(fake.calls, [])

    def test_missing_credentials_skip_probe(self):
        fake = _HTTP()
        self.use_http(fake)
        env = dict(ENV)
        env.pop("BINANCE_API_KEY")
        issues = p.check_egress_ip(env, ENV["BINANCE_PROXY"], [])
        self.assertEqual(issues, [])
        self.assertEqual(fake.probe_calls, [])
        self.assertTrue(self.has_log("跳过"), self.logs)

    def test_corrupt_state_file_treated_as_empty(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write("{not-json!!")
        fake = _HTTP()
        self.use_http(fake)
        issues, _ = self.run_check_e()
        self.assertEqual(issues, [])
        self.assertEqual(len(fake.probe_calls), 1)
        self.assertIn("203.0.113.7", self.read_state()["verified"])

    def test_state_file_capped(self):
        ips = {f"10.0.0.{i}": time.time() - i * 60 for i in range(1, 60)}
        p._egress_state_save({"verified": ips,
                              "rejected": {f"10.1.1.{i}": time.time() - i
                                           for i in range(1, 60)}})
        saved = self.read_state()
        self.assertLessEqual(len(saved["verified"]), p.EGRESS_STATE_MAX_ENTRIES)
        self.assertLessEqual(len(saved["rejected"]), p.EGRESS_STATE_MAX_ENTRIES)
        self.assertIn("10.0.0.1", saved["verified"])       # 最新
        self.assertNotIn("10.0.0.59", saved["verified"])   # 最旧

    def test_self_exception_never_propagates(self):
        """契约6：任何自身异常按设计约束④ 不得抛出。"""
        def boom(*a, **k):
            raise RuntimeError("injected self-failure")
        p.get_egress_ip = boom
        self.addCleanup(lambda: setattr(p, "get_egress_ip", _REAL_GET_IP))
        issues = p.check_egress_ip(dict(ENV), ENV["BINANCE_PROXY"], [])
        self.assertEqual(issues, [])
        self.assertTrue(self.has_log("按设计不抛出"), self.logs)

    def test_env_int_bad_value_falls_back(self):
        """契约6：.env 数值写错不得让巡检在导入期崩掉（约束④）。"""
        saved = list(p._EGRESS_ENV_ERRORS)
        self.addCleanup(lambda: p._EGRESS_ENV_ERRORS.__setitem__(slice(None), saved))
        os.environ["EGRESS_TEST_BAD_INT"] = "not-a-number"
        self.addCleanup(os.environ.pop, "EGRESS_TEST_BAD_INT", None)

        self.assertEqual(p._egress_env_int("EGRESS_TEST_BAD_INT", 77), 77)
        self.assertTrue(any("EGRESS_TEST_BAD_INT" in m for m in p._EGRESS_ENV_ERRORS),
                        p._EGRESS_ENV_ERRORS)
        self.assertEqual(p._egress_env_int("EGRESS_TEST_UNSET_INT", 42), 42)

    def test_env_flag_bad_value_falls_back_with_warning(self):
        saved = list(p._EGRESS_ENV_ERRORS)
        self.addCleanup(lambda: p._EGRESS_ENV_ERRORS.__setitem__(slice(None), saved))
        os.environ["EGRESS_TEST_BAD_FLAG"] = "maybe"
        self.addCleanup(os.environ.pop, "EGRESS_TEST_BAD_FLAG", None)
        self.assertTrue(p._egress_env_flag("EGRESS_TEST_BAD_FLAG", True))
        self.assertTrue(any("EGRESS_TEST_BAD_FLAG" in m
                            for m in p._EGRESS_ENV_ERRORS))

    def test_alert_dedup_key_is_per_ip(self):
        """P3：去重按 key 计，不同被拒 IP 不能被同一个键吞掉。"""
        sent = []
        orig_tg, orig_mail = p.send_tg, p.send_email
        p.send_tg = lambda env, text, use_proxy=True: sent.append(("tg", text)) or True
        p.send_email = lambda *a, **k: sent.append(("mail", a)) or True
        self.addCleanup(lambda: (setattr(p, "send_tg", orig_tg),
                                 setattr(p, "send_email", orig_mail)))
        now = time.time()

        p.alert(dict(ENV), "egress_ip_rejected:1.1.1.1", "t1", "d1", False)
        p.alert(dict(ENV), "egress_ip_rejected:1.1.1.1", "t1", "d1", False)  # 去重
        p.alert(dict(ENV), "egress_ip_rejected:2.2.2.2", "t2", "d2", False)  # 不去重

        self.assertEqual(len(sent), 2, [s[0] for s in sent])
        state = json.load(open(p.ALERT_STATE_FILE, encoding="utf-8"))
        self.assertIn("egress_ip_rejected:1.1.1.1", state)
        self.assertIn("egress_ip_rejected:2.2.2.2", state)

    def test_prune_removes_expired_egress_alert_keys_only(self):
        with open(p.ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"egress_ip_rejected:9.9.9.9": time.time() - 8 * 86400,
                       "egress_ip_rejected:1.1.1.1": time.time() - 60,
                       "stale_heartbeat": time.time() - 8 * 86400,
                       "_bot_dead_count": 3}, f)
        p._prune_egress_alert_keys(time.time())
        state = json.load(open(p.ALERT_STATE_FILE, encoding="utf-8"))
        self.assertNotIn("egress_ip_rejected:9.9.9.9", state)
        self.assertIn("egress_ip_rejected:1.1.1.1", state)
        self.assertIn("stale_heartbeat", state, "只清 egress 键")
        self.assertEqual(state["_bot_dead_count"], 3)


# ==================== P0：真实 urllib HTTPError 路径 ====================
class _Recorder(BaseHTTPRequestHandler):
    status = 200
    body = b"[]"

    def do_GET(self):
        self.__class__.request_path = self.path
        self.send_response(self.__class__.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.__class__.body)))
        self.end_headers()
        self.wfile.write(self.__class__.body)

    def log_message(self, *a):
        pass


class RealHttpErrorTests(EgressBase):
    """走**真实** urllib：币安 4xx 抛 HTTPError，若不转换成 (status, body) 就永远不告警。"""

    def setUp(self):
        super().setUp()
        self.srv = HTTPServer(("127.0.0.1", 0), _Recorder)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self._orig_base = p._FAPI_BASE
        p._FAPI_BASE = f"http://127.0.0.1:{self.port}"
        self.addCleanup(lambda: setattr(p, "_FAPI_BASE", self._orig_base))

    def run_check_e(self, env=None):
        """真实 HTTP 路径必须直连本机测试服务器，不能走 7890 代理。"""
        summary = []
        issues = p.check_egress_ip(dict(env or ENV), "", summary)
        return issues, summary

    def _serve(self, status, body: bytes):
        _Recorder.status, _Recorder.body = status, body

    def test_http_400_2015_body_is_rejected_not_error(self):
        self._serve(400, json.dumps(
            {"code": -2015,
             "msg": "Invalid API-key, IP, or permissions for action, "
                    "request ip: 192.0.2.55"}).encode())
        verdict, detail = p.probe_auth_for_ip(dict(ENV), "")
        self.assertEqual(verdict, "rejected", detail)
        self.assertIn("-2015", detail)

    def test_http_401_2015_body_is_rejected(self):
        self._serve(401, b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions"}')
        verdict, _ = p.probe_auth_for_ip(dict(ENV), "")
        self.assertEqual(verdict, "rejected")

    def test_http_200_with_error_body_is_still_rejected(self):
        """防御"2xx 带错误体"形态：两种都要认。"""
        self._serve(200, b'{"code":-2015,"msg":"invalid api-key"}')
        verdict, _ = p.probe_auth_for_ip(dict(ENV), "")
        self.assertEqual(verdict, "rejected")

    def test_http_200_balance_is_ok(self):
        self._serve(200, b'[{"asset":"USDT","balance":"1.0"}]')
        verdict, _ = p.probe_auth_for_ip(dict(ENV), "")
        self.assertEqual(verdict, "ok")

    def test_http_503_is_error_not_rejected(self):
        self._serve(503, b"<html>bad gateway</html>")
        verdict, _ = p.probe_auth_for_ip(dict(ENV), "")
        self.assertEqual(verdict, "error")

    def test_end_to_end_real_http_400_triggers_alert_issue(self):
        """端到端：真实 400 + -2015 必须产出 issue（这是 P0 的验收判据）。"""
        self._serve(400, b'{"code":-2015,"msg":"Invalid API-key, IP, or '
                         b'permissions for action, request ip: 192.0.2.55"}')
        p.get_egress_ip = lambda env, proxy_url="": "192.0.2.55"
        self.addCleanup(lambda: setattr(p, "get_egress_ip", _REAL_GET_IP))

        issues, _ = self.run_check_e()

        self.assertEqual(len(issues), 1, f"真实 400 路径必须告警，实际 issues={issues}")
        self.assertTrue(issues[0][0].startswith("egress_ip_rejected"))
        self.assertIn("-2015", issues[0][2])
        state = self.read_state()
        self.assertIn("192.0.2.55", state["rejected"])


# ==================== 端到端：run_check ====================
class RunCheckIntegrationTests(unittest.TestCase):
    """端到端：rejected 必须能穿过 run_check 到达 alert；停机期间仍要核验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self._names = ("HEARTBEAT_FILE", "TRADE_STATE_FILE", "HEALTH_DIR",
                       "ALERT_STATE_FILE", "LOG_FILE", "LOG_DIR", "EGRESS_STATE_FILE",
                       "EGRESS_CHECK_ENABLED", "EGRESS_VERIFY_TTL_SECONDS",
                       "EGRESS_REJECT_COOLDOWN_SECONDS", "log", "alert",
                       "get_egress_ip", "probe_auth_for_ip", "_egress_http_get",
                       "proxy_ready")
        self._orig = {n: getattr(p, n) for n in self._names}
        self._write_heartbeat(stopped=False, ts=time.time())
        with open(os.path.join(d, "trade_state.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        health = os.path.join(d, ".bot_health")
        os.makedirs(health, exist_ok=True)
        with open(os.path.join(health, "control.json"), "w", encoding="utf-8") as f:
            json.dump({"instance_id": "i-test", "ts": time.time()}, f)

        p.HEARTBEAT_FILE = os.path.join(d, ".heartbeat.json")
        p.TRADE_STATE_FILE = os.path.join(d, "trade_state.json")
        p.HEALTH_DIR = health
        p.ALERT_STATE_FILE = os.path.join(d, ".patrol_alert.state.json")
        p.LOG_DIR = d
        p.LOG_FILE = os.path.join(d, "patrol.log")
        p.EGRESS_STATE_FILE = os.path.join(d, ".egress_ip.state.json")
        p.EGRESS_CHECK_ENABLED = True
        p.EGRESS_VERIFY_TTL_SECONDS = 21600
        p.EGRESS_REJECT_COOLDOWN_SECONDS = 3600
        p.log = lambda msg: None
        p._egress_http_get = _HTTP()          # 兜底（正常路径会被桩短路）
        p.proxy_ready = lambda u, timeout=2.0: True   # 不依赖本机代理进程是否在跑
        self.captured = []
        p.alert = lambda env, key, title, detail, dry: self.captured.append(
            (key, title, detail))
        self.addCleanup(self._restore)

    def _restore(self):
        for name, val in self._orig.items():
            setattr(p, name, val)

    def _write_heartbeat(self, stopped, ts, bot_alive=True, reason=None):
        with open(os.path.join(self.tmp.name, ".heartbeat.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"ts": ts, "ts_str": "test", "watchdog_pid": 1, "bot_pid": 2,
                       "bot_alive": bot_alive, "stopped": stopped,
                       "stopped_reason": reason, "restarts": 0}, f)

    def test_rejected_egress_reaches_alert(self):
        p.get_egress_ip = lambda env, proxy_url="": "192.0.2.77"
        p.probe_auth_for_ip = lambda env, proxy_url="": (
            "rejected", 'HTTP 400: {"code":-2015,"msg":"Invalid API-key, IP..."}')

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0, "巡检自身必须 exit 0")
        self.assertEqual([k for k, _, _ in self.captured],
                         ["egress_ip_rejected:192.0.2.77"], self.captured)
        self.assertIn("192.0.2.77", self.captured[0][1])

    def test_accepted_egress_stays_silent(self):
        p.get_egress_ip = lambda env, proxy_url="": "203.0.113.7"
        p.probe_auth_for_ip = lambda env, proxy_url="": ("ok", "HTTP 200")

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual(self.captured, [], "合法出口 IP 不得打扰运维")
        with open(p.EGRESS_STATE_FILE, encoding="utf-8") as f:
            self.assertIn("203.0.113.7", json.load(f)["verified"])

    def test_stopped_heartbeat_still_checks_egress(self):
        """P1b：停机期间守护链类问题静默，但出口 IP 拒绝仍必须告警。"""
        # 故意把心跳放得极旧 + 关掉 control → 若 stopped 分支被绕过，会多出
        # stale_heartbeat / health_state_unknown 等 issue，从而被本用例抓住。
        self._write_heartbeat(stopped=True, ts=time.time() - 9999,
                              reason="用户主动停止")
        p.get_egress_ip = lambda env, proxy_url="": "192.0.2.88"
        p.probe_auth_for_ip = lambda env, proxy_url="": ("rejected", 'code -2015')

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual([k for k, _, _ in self.captured],
                         ["egress_ip_rejected:192.0.2.88"], self.captured)

    def test_stopped_without_egress_issue_stays_silent(self):
        self._write_heartbeat(stopped=True, ts=time.time() - 9999,
                              reason="用户主动停止")
        p.get_egress_ip = lambda env, proxy_url="": "203.0.113.7"
        p.probe_auth_for_ip = lambda env, proxy_url="": ("ok", "HTTP 200")

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual(self.captured, [], "停机期守护链类问题必须保持静默")

    def test_proxy_down_still_runs_no_egress_probe(self):
        """代理不通 → 不取出口 IP、不探活（避免无意义日志），proxy_down 正常上报。"""
        p.proxy_ready = lambda u, timeout=2.0: False
        fake = _HTTP()
        p._egress_http_get = fake

        rc = p.run_check(dict(ENV), dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual([k for k, _, _ in self.captured], ["proxy_down"], self.captured)
        self.assertEqual(fake.calls, [], "代理不通时不应发起任何出口查询")


if __name__ == "__main__":
    unittest.main(verbosity=2)
