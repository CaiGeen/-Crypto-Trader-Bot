#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_m2_m4_maintenance.py —— 合并维护批次 M2~M4 专项测试（2026-09-25）

本文件对应**可部署版本**（M2–M4，不含 M1'）。背景（第八轮复审 / ChatGPT 复核 d539da9）：
  M2 消除第 3 批次起的轮询断崖（F6：<=2 → 10~15s，<=4 → 原 75~100s）
  M3 批次上限与实盘 `.env` **联动**（2026-09-27 改法：不变量 + 行为断言，
     不再把 `"3"` 钉死在入库测试里 → 调上限只改 .env 即可）
     + 修正 D10 误导文案（原称「改 .env 即时生效无需重启」，实为需重启）
  M4 启动配置横幅：打印**进程内真正生效**的值（永久解决 A9 类验收缺口）

⚠️ M1'（崩溃邮件在途去重 + 迟到回灌）**刻意不含**在内：ChatGPT 复核 d539da9 指出
   两条未闭环时序（① 在途抑制仍消耗失败轮次，可致「只发一次即永久静默」；
   ② 吸收迟到标记与占槽之间仍有竞态）。其实现保存在分支 `notify-m1-timeline`，
   补测后再评审。本文件末尾的护栏测试会在 M1' 被误合入可部署分支时**响亮失败**。

运行：.venv/Scripts/python.exe -m pytest test_m2_m4_maintenance.py -q
"""
import importlib.util
import inspect
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import email_gate  # noqa: E402
import bot_runner  # noqa: E402
import trader_260725  # noqa: E402


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
        # 连续性判据：对**声明档位上界**做确定性检查（随机采样做比值会抖动）
        ordered = [expected[n] for n in sorted(expected)]
        for (lo1, hi1), (lo2, hi2) in zip(ordered, ordered[1:]):
            self.assertLessEqual(hi2, hi1 * 2 + 1e-6,
                                 f"档位 {lo1}~{hi1} → {lo2}~{hi2} 出现断崖式跃升")
        self.assertLessEqual(max(hi for n, (lo, hi) in expected.items() if n >= 3), 60.0,
                             "第 3 档起必须快于 60s")

    def test_m2_source_no_longer_has_old_tiers(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "trader_260725.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("base_interval = 75.0", src)


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

    # ── M3 批次上限：与实盘 `.env` **联动**（2026-09-27 改法，用户拍板）─────────
    # 原实现 `assertEqual(..., "3")` 把**不入库、每机可不同**的 `.env` 值钉死在
    # **入库**的测试里 → 调一次上限必须同步改测试，否则门禁 EXIT=1。
    # 改为「不变量 + 行为」两层断言：用户**只改 .env 即可**，
    # 但 0/负数/非整数/低于 3/档位过慢 仍会被响亮拦下。
    #
    # 留痕：`.env` 不入库（.gitignore:4），值变更靠**部署步骤 0 记 SHA256 +
    #       步骤 5 判据 ⑥ 复核**留痕，不在本测试里另开日志。
    WINDOW_LIMIT = 45.0
    # 裸仓发现窗口上限（秒）。45s 拦住 `>6` 档（45~60s）→ 等价于「上限最多到 6」。
    #   想更严 → 30（上限最多到 4）；想放开档位 → 60。
    #   **改它是决策、不是修 bug**，请与 `.env` 一并评审。

    def _live_batch_cap(self) -> int:
        """读实盘 `.env` 的批次上限并解析为 int；任何异常形态直接 fail。"""
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        self.assertTrue(os.path.exists(env_path), "实盘 .env 不存在，无法核对上限")
        with open(env_path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip().startswith("RISK_MAX_ACTIVE_BATCHES")]
        self.assertTrue(lines, ".env 未显式设置 RISK_MAX_ACTIVE_BATCHES（会退回默认 3）")
        raw = lines[0].split("=", 1)[1].strip()
        try:
            return int(raw)
        except ValueError:
            # 非整数必须响：trader L1634-1636 会**静默回落默认 3**，
            # 届时「.env 写了 99、进程跑 3」而无人知晓（配置假生效）。
            self.fail(f"RISK_MAX_ACTIVE_BATCHES={raw!r} 不是整数 → "
                      f"运行时静默回落 3，配置与实际不符（假生效）")

    def test_m3_batch_cap_is_sane(self):
        """不变量层：改 `.env` **不必改本测试**，但三类非法值仍被拦。

        - 非整数 → 代码侧静默回落 3（见 `_live_batch_cap`）
        - <= 0   → trader L1628「限额 <=0 视为禁用」= **整个账户层闸门被关掉**
        - < 3    → 违反既定约束「至少 3 个活跃批次」，且批次数不得被当作安全参数
        """
        cap = self._live_batch_cap()
        self.assertGreater(cap, 0,
                           "0/负数 = 关闭账户层闸门"
                           "（trader L1628「限额 <=0 视为禁用」），禁止")
        self.assertGreaterEqual(cap, 3,
                                "既定约束：至少 3 个活跃批次；"
                                "批次数不得被当作安全参数")

    def test_m3_configured_cap_still_fast_enough(self):
        """行为层：用 `.env` 的**真实上限**跑轮询分档，断言裸仓发现窗口够快。

        本测试保护的是「成交 → 首次发现 SL/TP 缺失」的窗口
        （v6.2-P0-1 判定 ~80s 不可接受），**不是数字 3**。因此：
          - `.env` 调成 4 / 6   → 自动跟随通过，**无需改测试**；
          - 上限进到 `>6` 档    → 被 `WINDOW_LIMIT` 拦下；
          - 将来分档实现改变     → 本断言自动跟随，不会与实现脱节。

        ⚠️ 本测试只证明**磁盘 `.env` 自洽，不证明运行进程已加载** ——
        `load_dotenv()` 只在启动时执行一次（D10），改完不重启则主程序仍用旧值、
        而 `健康巡检.py` 每次重读磁盘用新值 = 两侧行为不一致（F10 机制）。
        **生效证据是重启后 `bot_runner.log_effective_config()` 横幅里的同名键值。**
        """
        cap = self._live_batch_cap()
        fake = mock.Mock()
        fake._get_active_batch_count.return_value = cap
        for _ in range(20):
            v = trader_260725.CryptoTrader._calculate_monitoring_interval(fake)
            self.assertLessEqual(
                v, self.WINDOW_LIMIT,
                f"上限={cap} 时裸仓发现窗口可达 {v:.1f}s > {self.WINDOW_LIMIT}s —— "
                f"应调整轮询分档或下调 .env 上限，**不是放宽本断言**")


class M4ConfigBannerTests(unittest.TestCase):
    def test_m4_banner_reflects_in_process_env(self):
        env = {"RISK_MAX_ACTIVE_BATCHES": "2", "MAX_LEVERAGE": "100",
               "DAILY_REPORT_EMAIL_ENABLED": "false"}
        with mock.patch.dict(os.environ, env, clear=False):
            line = bot_runner.log_effective_config()
        self.assertIn("RISK_MAX_ACTIVE_BATCHES=2", line)
        self.assertIn("DAILY_REPORT_EMAIL_ENABLED=False", line)
        self.assertIn("致命事件白名单=", line)   # 标签避开 watchdog 崩溃判定的 CRASH/FATAL 词
        for ev in ("crash", "health", "critical"):
            self.assertIn(ev, line)
        for key in ("RISK_MAX_ACTIVE_SYMBOLS", "EMAIL_ALERT_ENABLED",
                    "EMAIL_ALERT_ONLY_WITH_POSITION", "MAX_LEVERAGE"):
            self.assertIn(key, line)

    def test_m4_banner_shows_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            line = bot_runner.log_effective_config()
        self.assertIn("RISK_MAX_ACTIVE_BATCHES=3（默认）", line)


class DeployableVersionGuardTests(unittest.TestCase):
    """护栏：未完成的 M1' 不得混入可部署版本（ChatGPT 决策 1：排除提前加载风险）"""

    def test_m1_not_present_in_deployable_tree(self):
        for name in ("claim_mail_slot", "release_mail_slot", "mail_send_key",
                     "mail_in_flight", "MAIL_RESULT_TIMEOUT_IN_FLIGHT"):
            self.assertFalse(hasattr(email_gate, name),
                             f"email_gate 出现未完成 M1' 符号 {name}；"
                             f"M1' 需补测两条时序后才能进入可部署分支")
        self.assertFalse(hasattr(bot_runner, "_LATE_CRASH_CONFIRM"))
        self.assertFalse(hasattr(bot_runner, "_mark_crash_email_late"))
        params = inspect.signature(bot_runner.send_email_alert).parameters
        for p in ("dedup_key", "on_late_result"):
            self.assertNotIn(p, params, f"send_email_alert 仍带未完成 M1' 参数 {p}")
        tparams = inspect.signature(
            trader_260725.CryptoTrader._send_email_alert).parameters
        for p in ("dedup_key", "on_late_result"):
            self.assertNotIn(p, tparams, f"_send_email_alert 仍带未完成 M1' 参数 {p}")

    def test_known_defects_are_documented_in_code(self):
        """摘除 M1' 后，缺陷必须**写在代码里**，不能让后来者以为已修"""
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "bot_runner.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("已知缺陷", src)
        self.assertIn("notify-m1-timeline", src)


class BatchSkeletonPersistenceGateTests(unittest.TestCase):
    """Intent Before Side Effect：骨架未确认落盘时，交易所 create_order 必须为零。"""

    def _fixture(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'tests_archive', 'test_b2_crashsafe_entry.py')
        spec = importlib.util.spec_from_file_location('b2_crashsafe_entry_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_persist_failure_blocks_all_create_order(self):
        module = self._fixture()
        fake = module.make_fake()
        fake.save_batch_state = lambda symbol, batch_id, data: False
        with mock.patch.object(trader_260725.threading, 'Thread', module.FakeThread):
            result = trader_260725.CryptoTrader.execute_signal(fake, module.FakeSignal())
        self.assertIsNone(result)
        self.assertEqual(fake._create_n, 0)

    def test_legacy_none_return_also_blocks_create_order(self):
        """旧 fake 成功时返回 None；在 is not True 硬门下 None 同样必须阻断（不得放宽）。"""
        module = self._fixture()
        fake = module.make_fake()
        fake.save_batch_state = lambda symbol, batch_id, data: None
        with mock.patch.object(trader_260725.threading, 'Thread', module.FakeThread):
            result = trader_260725.CryptoTrader.execute_signal(fake, module.FakeSignal())
        self.assertIsNone(result)
        self.assertEqual(fake._create_n, 0)

    def test_execute_signal_emits_no_generic_persist_alert(self):
        """告警唯一源是 save_batch_state：调用方只阻断，不得再发泛化"磁盘/权限"告警。"""
        module = self._fixture()
        fake = module.make_fake()
        fake.save_batch_state = lambda symbol, batch_id, data: False
        with mock.patch.object(trader_260725.threading, 'Thread', module.FakeThread):
            trader_260725.CryptoTrader.execute_signal(fake, module.FakeSignal())
        self.assertEqual([t for lv, t in fake.sent if lv == 'critical'], [])


class TombstoneEntryIntegrityTests(unittest.TestCase):
    """2026-09-25 复审 P1：根 JSON 合法但条目类型损坏 → 不得 Fail-Open 放行复活。"""

    SYMBOL = 'BTC/USDT:USDT'

    def _fake(self, tomb_path, sent):
        fake = mock.Mock()
        fake._state_lock = threading.Lock()
        fake.tombstone_file = tomb_path
        fake.sent = sent
        fake._tombstone_alerted = set()
        fake._persist_alerted = set()
        fake._state_corrupted = False
        fake._state_corruption_detail = ''
        fake.send_tg_notification = (
            lambda text, **k: sent.append((k.get('level', 'info'), str(text))))
        fake.load_all_states = lambda: trader_260725.CryptoTrader.load_all_states(fake)
        # G3/默认读取已改用「本次读取」三元组接口（ChatGPT 复审⑤）
        fake._load_all_states_ex = (
            lambda: trader_260725.CryptoTrader._load_all_states_ex(fake))
        fake._persist_states = (
            lambda all_states: trader_260725.CryptoTrader._persist_states(fake, all_states))
        # 必须绑定真实墓碑读取：MagicMock 会吞掉 _load_tombstones 与
        # _tombstones_degraded，使"条目损坏 → DEGRADED"路径被静默跳过（假绿）。
        fake._load_tombstones = (
            lambda: trader_260725.CryptoTrader._load_tombstones(fake))
        fake._persist_tombstones = (
            lambda tombstones: trader_260725.CryptoTrader._persist_tombstones(fake, tombstones))
        fake._merge_batch_state = (
            lambda disk, snap: trader_260725.CryptoTrader._merge_batch_state(fake, disk, snap))
        # clear_batch_state 需要的簿记（缺了会被 MagicMock 吞掉 → 假绿/假红）
        fake._collect_batch_order_ids = (
            lambda b: trader_260725.CryptoTrader._collect_batch_order_ids(fake, b))
        fake._verify_clear_proof = lambda *a, **k: None
        fake._converge_alert = lambda key, msg, **k: sent.append((k.get('level', 'info'), str(msg)))
        fake._tp_breaker_alerted = {}
        fake._converge_alert_counts = {}
        return fake

    def _write(self, tmp, name, payload):
        path = os.path.join(tmp, name)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def test_corrupt_entry_blocks_new_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {'batch_bad': 'not-a-dict'})
            state = self._write(tmp, 'trade_state.json', {})
            sent = []
            fake = self._fake(tomb, sent)
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                ok = trader_260725.CryptoTrader.save_batch_state(
                    fake, self.SYMBOL, 'batch_bad', {'is_active': True})
                degraded = fake._tombstones_degraded
                with open(state, 'r', encoding='utf-8') as f:
                    written = json.load(f)
        self.assertFalse(ok, "条目损坏时全新批次必须被拒绝（Fail-Closed）")
        self.assertTrue(degraded)
        self.assertEqual(written, {}, "被拒批次不得写入 trade_state.json")
        self.assertTrue(any('batch_bad' in t and '墓碑' in t
                            for lv, t in sent if lv == 'critical'))

    def test_corrupt_entry_still_allows_existing_batch_update(self):
        """D-009 Q3 分治：墓碑不可信只阻断新建，已存在批次的状态更新必须放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {'batch_bad': None})
            state = self._write(tmp, 'trade_state.json',
                                {self.SYMBOL: {'batch_bad': {'is_active': True,
                                                             'close_phase': 1}}})
            sent = []
            fake = self._fake(tomb, sent)
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                ok = trader_260725.CryptoTrader.save_batch_state(
                    fake, self.SYMBOL, 'batch_bad', {'is_active': True, 'close_phase': 2})
                with open(state, 'r', encoding='utf-8') as f:
                    written = json.load(f)
        self.assertTrue(ok)
        self.assertEqual(written[self.SYMBOL]['batch_bad']['close_phase'], 2)
        self.assertTrue(fake._tombstones_degraded)

    def test_empty_dict_entry_must_degrade_and_block_new_batch(self):
        """ChatGPT 反例：{batch_x: {}} 是 dict 但无 cleared_at —— 不能靠类型校验放行。

        旧实现按 cleared_at 缺失取 0 → age=now-0 远超 TTL → 当作"墓碑已过期"，
        随后 _degraded=False 直接放行新建（Fail-Open），且 prune 会把损坏证据剪掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {'batch_x': {}})
            tomb_bytes_before = open(tomb, 'rb').read()
            state = self._write(tmp, 'trade_state.json', {})
            sent = []
            fake = self._fake(tomb, sent)
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                ok = trader_260725.CryptoTrader.save_batch_state(
                    fake, self.SYMBOL, 'batch_x', {'is_active': True})
                degraded = fake._tombstones_degraded
                with open(state, 'r', encoding='utf-8') as f:
                    written = json.load(f)
                # 同一不可信文件下，TTL prune 不得删除损坏证据
                trader_260725.CryptoTrader._prune_tombstones(fake)
            tomb_bytes_after = open(tomb, 'rb').read()
        self.assertFalse(ok, "空 dict 墓碑条目必须降级并拒绝新建（Fail-Closed）")
        self.assertTrue(degraded)
        self.assertEqual(written, {}, "被拒批次不得写入 trade_state.json")
        self.assertEqual(tomb_bytes_before, tomb_bytes_after, "降级态不得改写墓碑证据")

    def test_clear_keeps_ledger_when_tombstone_write_fails(self):
        """ChatGPT 反例：墓碑写盘失败时绝不能删活跃账本（否则变成无反复活防线的孤儿仓）。"""
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {})
            state = self._write(tmp, 'trade_state.json',
                                {self.SYMBOL: {'batch_y': {'is_active': True,
                                                           'close_phase': 3}}})
            sent = []
            fake = self._fake(tomb, sent)
            # proof 门本身由 test_b_batch 覆盖；此处钉住的是"proof 之后、删账本之前"的
            # 墓碑落盘失败分支，故将 proof 校验直接放行。
            fake._verify_clear_proof = lambda *a, **k: None
            fake._persist_tombstones = lambda t: False  # 模拟磁盘写墓碑失败
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                rc = trader_260725.CryptoTrader.clear_batch_state(
                    fake, self.SYMBOL, 'batch_y',
                    proof={'l1_canceled': [], 'l2_canceled': []})
                with open(state, 'r', encoding='utf-8') as f:
                    written = json.load(f)
            tomb_now = json.load(open(tomb, 'r', encoding='utf-8'))
        self.assertFalse(rc, "墓碑未落盘时 clear 必须返回失败")
        self.assertIn('batch_y', written.get(self.SYMBOL, {}),
                      "墓碑写失败必须保留活跃账本（不得出现无墓碑的孤儿清理）")
        self.assertEqual(tomb_now, {}, "墓碑文件应保持原样")
        self.assertTrue(any(lv == 'critical' and '墓碑' in t for lv, t in sent),
                        "必须锁外 critical 告警")

    def test_clear_returns_false_when_ledger_persist_fails(self):
        """ChatGPT 反例二：墓碑写成功后账本落盘失败，clear 不得谎报成功。

        13 个调用方全都把 False 当作「未清理、待下轮重试」（打印清理未完成 /
        置 _cleanup_pending / 返回 clear_failed），而 True 会让它们报告批次已完成
        —— 磁盘上批次其实还在。_persist_states 是 os.replace 原子写，失败时磁盘
        保持原样，所以「状态保留」是可如实宣称的。
        另：_persist_states 写盘失败**只 print 不告警**（损坏态才告警），这条路径
        目前既返回谎话又零告警，必须由 clear_batch_state 自己补锁外 critical。
        """
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {})
            state = self._write(tmp, 'trade_state.json',
                                {self.SYMBOL: {'batch_z': {'is_active': True,
                                                           'close_phase': 3}}})
            sent = []
            fake = self._fake(tomb, sent)
            fake._verify_clear_proof = lambda *a, **k: None
            real_persist = fake._persist_states
            proof = {'l1_canceled': [], 'l2_canceled': []}
            buf = io.StringIO()
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                # 第一次：墓碑写成功，账本写失败（"第二次写盘失败"）
                fake._persist_states = lambda s: False
                with contextlib.redirect_stdout(buf):
                    rc1 = trader_260725.CryptoTrader.clear_batch_state(
                        fake, self.SYMBOL, 'batch_z', proof=proof)
                with open(state, 'r', encoding='utf-8') as f:
                    after_fail = json.load(f)
                # 第二次：磁盘恢复后重试，必须幂等成功
                fake._persist_states = real_persist
                with contextlib.redirect_stdout(io.StringIO()):
                    rc2 = trader_260725.CryptoTrader.clear_batch_state(
                        fake, self.SYMBOL, 'batch_z', proof=proof)
                with open(state, 'r', encoding='utf-8') as f:
                    after_retry = json.load(f)
            tomb_now = json.load(open(tomb, 'r', encoding='utf-8'))
        self.assertFalse(rc1, "账本落盘失败时 clear 必须返回 False（不得谎报成功）")
        self.assertIn('batch_z', after_fail.get(self.SYMBOL, {}),
                      "写盘失败 → 磁盘批次必须原样保留（重试才有对象）")
        self.assertNotIn('清理完毕', buf.getvalue(),
                         "落盘失败时不得打印「清理完毕」")
        crit = [t for lv, t in sent if lv == 'critical']
        self.assertTrue(any('账本' in t or '落盘' in t for t in crit),
                        f"必须锁外 critical 且写明账本落盘失败: {crit}")
        self.assertIn('batch_z', tomb_now, "墓碑应已登记（重试幂等、无复活风险）")
        self.assertTrue(rc2, "磁盘恢复后重试必须成功")
        self.assertNotIn('batch_z', after_retry.get(self.SYMBOL, {}))

    def test_tombstone_entry_valid_boundary_and_legacy_rule(self):
        """口径收口：_tombstone_entry_valid 是「TTL/存在性判据所需的最小结构」，
        **不是**完整 schema 校验。

        兼容规则（本轮明确定义，防止后人顺手收紧）：
        - 必需：非空 dict + cleared_at 为有限正数（这才是 TTL 与"是否已过期"
          判定所依赖的字段，缺了就会被兜底成 0 而误判过期）。
        - **不要求** close_phase —— Batch B 之前写入的墓碑没有该字段，
          test_c_batch 的夹具同样不写；要求它会把合法历史文件判成 DEGRADED，
          连带禁止一切新建批次（把兼容问题升级成可用性事故）。
        - **不要求** symbol / converged_order_ids / known_order_ids —— 它们是
          溯源审计字段，不参与 TTL、存在性或复活判定，缺失不影响防线正确性。
        若日后确实要校验这些字段，必须先给出旧文件的迁移或降级兼容方案。
        """
        now = time.time()
        valid = trader_260725._tombstone_entry_valid
        # 合法（含旧版形态）
        self.assertTrue(valid({'cleared_at': now}), "最小可用条目必须有效")
        self.assertTrue(valid({'symbol': self.SYMBOL, 'cleared_at': now}),
                        "旧版（无 close_phase）条目必须有效，否则历史文件被误判 DEGRADED")
        self.assertTrue(valid({'symbol': self.SYMBOL, 'cleared_at': now,
                               'close_phase': 3, 'converged_order_ids': []}),
                        "现行完整条目必须有效")
        # 非法
        for bad in ({},                                  # 本轮反例一
                    {'cleared_at': 0},                   # 非正数 → age 兜底误判
                    {'cleared_at': -1},
                    {'cleared_at': None},
                    {'cleared_at': 'not-a-number'},
                    {'cleared_at': True},                # bool 是 int 子类
                    {'cleared_at': float('nan')},
                    {'cleared_at': float('inf')},
                    {'symbol': self.SYMBOL},             # 缺 cleared_at
                    'not-a-dict', None, [], 42):
            self.assertFalse(valid(bad), f"必须判为非法: {bad!r}")

    def test_clear_returns_false_when_ledger_unreadable(self):
        """ChatGPT 反例三：损坏账本 → 占位 {} →「找不到批次」≠「已清理」。

        load_all_states 的 docstring 明写：「损坏时返回 {}（占位，返回值在损坏态
        下无意义）。调用方读取返回值前必须先判 _state_corrupted」。
        clear_batch_state 违反了这条契约：b_data is None 直接 return True，
        于是损坏账本下所有清理都谎报成功（调用方打印「清理完毕」/ 返回 finalized），
        而磁盘上的批次状态无人知晓、也不会重试。
        另：load_all_states 对损坏只 print 不发 TG，_persist_states 的 critical
        在此路径根本不会被调用 → 该路径原本零告警。
        """
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {})
            state = os.path.join(tmp, 'trade_state.json')
            with open(state, 'w', encoding='utf-8') as f:
                f.write('{broken json')          # 真实损坏账本
            broken_bytes = open(state, 'rb').read()
            sent = []
            fake = self._fake(tomb, sent)
            fake._verify_clear_proof = lambda *a, **k: None
            buf = io.StringIO()
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                with contextlib.redirect_stdout(buf):
                    rc = trader_260725.CryptoTrader.clear_batch_state(
                        fake, self.SYMBOL, 'batch_ghost',
                        proof={'l1_canceled': [], 'l2_canceled': []})
                corrupted = fake._state_corrupted
            after_bytes = open(state, 'rb').read()
        self.assertTrue(corrupted, "前置：load_all_states 必须已置损坏标志")
        self.assertFalse(rc, "账本不可读时 clear 必须返回 False（未知 ≠ 已清理）")
        self.assertNotIn('清理完毕', buf.getvalue(),
                         "账本不可读时不得打印「清理完毕」")
        crit = [t for lv, t in sent if lv == 'critical']
        self.assertTrue(any(('损坏' in t or '不可读' in t) for t in crit),
                        f"必须锁外 critical 且写明账本不可读: {crit}")
        self.assertEqual(broken_bytes, after_bytes,
                         "损坏现场必须原样保留（不得被清理流程覆盖）")

    def test_clear_rejects_valid_json_with_corrupt_inner_structure(self):
        """ChatGPT 第十六轮反例：**合法 JSON** 但内部结构损坏 ≠ 账本可读。

        load_all_states 只校验根节点是 dict，所以以下文件都不会置
        _state_corrupted：
          {"BTC/USDT:USDT": []}                       → ([] or {}) → {} → None
                                                         → 「找不到批次」→ return True（谎报）
          {"BTC/USDT:USDT": "x"}                      → "x".get(batch_id)
                                                         → AttributeError（锁内崩溃）
          {"BTC/USDT:USDT": {"b1": 123}}              → b_data=123 交给 proof 门
        三者都是「不知道有哪些批次」，必须与 JSON 解析失败同等对待（D-009 语义）。
        """
        import contextlib
        shapes = {
            'symbol_is_empty_list': {"BTC/USDT:USDT": []},
            'symbol_is_str':        {"BTC/USDT:USDT": "x"},
            'batch_node_is_int':    {"BTC/USDT:USDT": {"b1": 123}},
        }
        for name, payload in shapes.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    tomb = self._write(tmp, 'tomb.json', {})
                    state = os.path.join(tmp, 'trade_state.json')
                    with open(state, 'w', encoding='utf-8') as f:
                        json.dump(payload, f)
                    before = open(state, 'rb').read()
                    sent = []
                    fake = self._fake(tomb, sent)
                    fake._verify_clear_proof = lambda *a, **k: None
                    buf = io.StringIO()
                    err = None
                    try:
                        with mock.patch.object(trader_260725, 'STATE_FILE', state):
                            with contextlib.redirect_stdout(buf):
                                rc = trader_260725.CryptoTrader.clear_batch_state(
                                    fake, self.SYMBOL, 'b1',
                                    proof={'l1_canceled': [], 'l2_canceled': []})
                    except Exception as e:
                        rc, err = None, f'{type(e).__name__}: {e}'
                    after = open(state, 'rb').read()
                self.assertIsNone(
                    err, f'{name}: 清理在 _state_lock 内抛异常（结构损坏直接放行）: {err}')
                self.assertFalse(
                    rc, f'{name}: 结构损坏时必须返回 False，'
                        f'不得把「节点读不出来」当成「批次不存在」')
                # 直接钉住契约本身：False 必须来自 load_all_states 的损坏标志，
                # 而不是碰巧走进了别的分支。
                self.assertIs(fake._state_corrupted, True,
                              f'{name}: load_all_states 必须置 _state_corrupted')
                detail = fake._state_corruption_detail
                self.assertIsInstance(detail, str, f'{name}: 必须给出具体损坏原因')
                self.assertTrue(detail.strip(),
                                f'{name}: 损坏原因不得为空字符串')
                self.assertNotIn('清理完毕', buf.getvalue(), f'{name}: 不得打印清理完毕')
                self.assertEqual(before, after, f'{name}: 损坏现场必须原样保留')

    def test_prune_is_skipped_when_degraded(self):
        """DEGRADED 时不得基于不可信数据改写墓碑（避免把损坏条目静默剪掉）。"""
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json',
                               {'batch_bad': 'not-a-dict',
                                'batch_old': {'cleared_at': 0}})
            sent = []
            fake = self._fake(tomb, sent)
            before = os.path.getsize(tomb)
            trader_260725.CryptoTrader._prune_tombstones(fake)
            after_size = os.path.getsize(tomb)
            with open(tomb, 'r', encoding='utf-8') as f:
                after = json.load(f)
        self.assertEqual(len(after), 2, "降级态不得改写墓碑")
        self.assertEqual(after_size, before)

    def test_persist_failure_alerts_once_with_real_reason_outside_lock(self):
        """告警契约：唯一来源、真实原因、锁外发送、按键去重。"""
        with tempfile.TemporaryDirectory() as tmp:
            tomb = self._write(tmp, 'tomb.json', {})
            state = os.path.join(tmp, 'trade_state.json')
            with open(state, 'w', encoding='utf-8') as f:
                f.write('{broken json')
            sent = []
            fake = self._fake(tomb, sent)
            with mock.patch.object(trader_260725, 'STATE_FILE', state):
                first = trader_260725.CryptoTrader.save_batch_state(
                    fake, self.SYMBOL, 'batch_new', {'is_active': True})
                second = trader_260725.CryptoTrader.save_batch_state(
                    fake, self.SYMBOL, 'batch_new', {'is_active': True})
                lock_free = fake._state_lock.acquire(blocking=False)
                if lock_free:
                    fake._state_lock.release()
        self.assertFalse(first)
        self.assertFalse(second)
        crit = [t for lv, t in sent if lv == 'critical']
        self.assertEqual(len(crit), 1, f"同一批次落盘失败只应告警一次: {crit}")
        self.assertIn('已损坏', crit[0], "告警必须写明真实原因（账本损坏）")
        self.assertNotIn('磁盘/权限', crit[0], "不得误导为磁盘权限问题")
        self.assertTrue(lock_free, "告警必须在 _state_lock 释放后发送")


class CrashDetectorHardeningTests(unittest.TestCase):
    """watchdog 只排空/记录输出；进程死亡由 poll/returncode，健康停滞由巡检判定。"""

    def test_monitor_process_drains_text_without_killing_process(self):
        import watchdog as wd
        lines = (
            "INFO - FATAL_EVENTS=['crash', 'health']\n",
            "CRASH_ALERT enqueued\n",
            "ERROR - 已捕获业务异常\n",
            "Traceback (most recent call last):\n",
            "ValueError: handled and still running\n",
        )
        process = mock.Mock(stdout=io.StringIO(''.join(lines)))
        with mock.patch.object(wd, 'write_bot_log') as write_log, \
                mock.patch.object(wd, '_CONSOLE_QUEUE') as console_queue:
            result = wd.monitor_process(process)
        self.assertTrue(result, "普通日志/已捕获 traceback 不得触发进程终止")
        self.assertEqual(write_log.call_count, len(lines))
        self.assertEqual(console_queue.put_nowait.call_count, len(lines))

    def test_real_config_banner_is_recorded_without_killing_process(self):
        """真实 M4 横幅必须完整落盘，且 monitor_process 不返回崩溃。"""
        import watchdog as wd
        line = bot_runner.log_effective_config() + '\n'
        process = mock.Mock(stdout=io.StringIO(line))
        with mock.patch.object(wd, 'write_bot_log') as write_log, \
                mock.patch.object(wd, '_CONSOLE_QUEUE'):
            result = wd.monitor_process(process)
        self.assertTrue(result)
        write_log.assert_called_once_with(line)

    def test_pipe_read_error_does_not_trigger_restart(self):
        """stdout 观察失败不能被当成进程死亡；returncode 仍由主循环裁决。"""
        import watchdog as wd

        class _BrokenStdout:
            def __iter__(self):
                return self

            def __next__(self):
                raise OSError("simulated pipe read failure")

        process = mock.Mock(stdout=_BrokenStdout())
        with mock.patch.object(wd, 'write_bot_log'), \
                mock.patch.object(wd, '_CONSOLE_QUEUE'), \
                mock.patch.object(wd, 'log_message') as log_message:
            result = wd.monitor_process(process)
        self.assertTrue(result)
        self.assertTrue(any('不据此重启进程' in str(c) for c in log_message.call_args_list))

    def test_text_classifier_is_not_a_crash_control_signal(self):
        """防止以后重新引入 `_looks_like_crash` 一类日志文本控制函数。"""
        import watchdog as wd
        self.assertFalse(hasattr(wd, '_looks_like_crash'))


if __name__ == "__main__":
    unittest.main()
