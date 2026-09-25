#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_m2_m4_maintenance.py —— 合并维护批次 M2~M4 专项测试（2026-09-25）

本文件对应**可部署版本**（M2–M4，不含 M1'）。背景（第八轮复审 / ChatGPT 复核 d539da9）：
  M2 消除第 3 批次起的轮询断崖（F6：<=2 → 10~15s，<=4 → 原 75~100s）
  M3 批次硬上限 3 + 修正 D10 误导文案（原称「改 .env 即时生效无需重启」，实为需重启）
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

    def test_m3_batch_cap_matches_live_env_file(self):
        """实盘 `.env`（不入库）必须**显式**写出批次上限，而不是依赖默认值。

        上限值 = 3（用户 2026-09-25 定）：M2 消除了第 3 批次的轮询断崖后，
        3 档为 20~30s，不再承担「保护单补建窗口过长」的代价。
        若日后改为 4 或其他值，**必须同步改这里的断言**，否则配置与测试脱节。
        """
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        self.assertTrue(os.path.exists(env_path), "实盘 .env 不存在，无法核对上限")
        with open(env_path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip().startswith("RISK_MAX_ACTIVE_BATCHES")]
        self.assertTrue(lines, ".env 未显式设置 RISK_MAX_ACTIVE_BATCHES（会退回默认 3）")
        self.assertEqual(lines[0].split("=", 1)[1].strip(), "3")

    def test_m3_batch_cap_3_still_fast_enough(self):
        """上限=3 的前提是 M2 已让 3 档保持 20~30s（否则等于退回断崖）"""
        fake = mock.Mock()
        fake._get_active_batch_count.return_value = 3
        for _ in range(20):
            v = trader_260725.CryptoTrader._calculate_monitoring_interval(fake)
            self.assertLessEqual(v, 30.0, "3 档轮询必须 <=30s")


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
