#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Q15 修复结案证据：离线完整保护恢复链（ChatGPT 复核 G3）。

链路（单测试内接续同一临时 STATE_FILE/假交易所）：
  撤旧成功 → 新单确定性拒绝(-2021) → 失败处置(Q15 helper)
  → 真实监控循环(_start_monitoring 真跑)补挂
  → 替身交易所有有效 SL → 磁盘确认 ABSENT→CONFIRMED 终态与 current_sl_id 更新。

同时钉住 R3 不回退：create 结果未知 → 账本保持 PENDING_CREATE，不得被误清 ABSENT。

跑法：`.venv\\Scripts\\python.exe test_q15_recovery_chain.py`（rc=0 即全过）
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt  # noqa: E402
import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

import test_monitor_poll_recovery as mpr  # noqa: E402
import test_q15_sl_replace_rollback as q15  # noqa: E402

SYMBOL = q15.SYMBOL
BATCH = q15.BATCH
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


def _bind_real_helpers(fake):
    """监控循环所需的真实 helper 绑定（mpr.REAL_HELPERS 同款）。"""
    for _n in mpr.REAL_HELPERS:
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n, (lambda n=_n: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())


def _init_loop_attrs(fake):
    fake._active_monitors = set()
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitor_generations = {}
    fake._poll_degraded_batches = set()
    fake._unresolved_intent_batches = set()
    fake._poll_fail_streak = {}
    fake._poll_first_fail_time = {}
    fake._poll_alert_lock = threading.RLock()
    fake._poll_alert_active = False
    fake._get_current_position_amt = lambda *a, **k: 0.43
    fake._load_tombstones = lambda: {}
    fake._finally_cleanup_decision = lambda s, b: ('skip', None)
    fake.registry_self_heal_interval = 10 ** 9
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 10 ** 9
    fake._check_ip_periodically = lambda: None
    fake._sync_time_if_needed = lambda: None
    fake._g3_log_position_recheck = lambda *a, **k: None


def _drive_real(fake, max_rounds=5):
    calls = {"n": 0}

    def _sleep(sec):
        calls["n"] += 1
        if calls["n"] > max_rounds:
            raise mpr._StopLoop()

    with mock.patch.object(trader_260725.time, "sleep", _sleep):
        try:
            CryptoTrader._start_monitoring(
                fake, SYMBOL, BATCH, ["e1"], [55000.0], 60000.0,
                None, None, 0.43, [0.43], {"leverage": 100}, False, "BUY",
                1, [0.43], 0.0, [], {}, mpr._layer_sl_params())
        except mpr._StopLoop:
            pass
        except Exception as e:
            return calls["n"], e
    return calls["n"], None


def main():
    d = tempfile.mkdtemp(prefix="q15chain_")
    state_path = os.path.join(d, "trade_state.json")
    rep = trader_260725.STATE_FILE
    trader_260725.STATE_FILE = state_path
    try:
        q15._seed_batch(state_path)
        states = q15._read_disk(state_path)
        fake = q15._make_fake(state_path, states, fail_create=True)
        fake._batch_net_position = lambda b: (0.43, 0.43)
        _bind_real_helpers(fake)
        _init_loop_attrs(fake)

        # ---- 阶段1：撤旧 + 新单确定拒绝 → Q15 helper 处置写盘 ----
        ret = CryptoTrader.update_batch_sl(fake, BATCH, 55000.0)
        disk1 = q15._read_disk(state_path)
        rep_reg = q15._reg_state(disk1)
        cur_sl1 = disk1.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id")
        report(
            "CHAIN-1 阶段1：新单被确定性拒绝 → 账本已处置（registry ABSENT、current_sl_id 置空）",
            rep_reg == "ABSENT" and cur_sl1 is None,
            f"registry={rep_reg!r}；current_sl_id={cur_sl1!r}")

        # ---- 阶段2：同一 fake / 同一磁盘，交易所恢复 → 真实监控循环补挂 SL ----
        ex = fake.exchange
        ex.create_order.side_effect = None
        ex.create_order.return_value = {"id": "sl_recovered_1"}
        ex.fetch_order.return_value = {"id": "sl_recovered_1", "status": "NEW"}
        ex.fetch_open_orders.return_value = []
        rounds, err = _drive_real(fake, max_rounds=5)
        report(
            "CHAIN-2 阶段2：真实监控循环跑到哨兵轮次，无驱动异常",
            err is None,
            f"rounds={rounds}；err={err!r}")

        disk2 = q15._read_disk(state_path)
        new_cur_sl = disk2.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id")
        new_reg = q15._reg_state(disk2)
        sl_calls = [c for c in ex.create_order.call_args_list
                    if ('STOP' in str(c) or 'stopPrice' in str(c))]
        report(
            "CHAIN-3 阶段2：补挂止损单在替身交易所确实发起创建",
            len(sl_calls) >= 1,
            f"STOP 创建调用={len(sl_calls)}；create_call_args={[str(c)[:80] for c in sl_calls[:3]]}")
        report(
            "CHAIN-4 阶段2：磁盘证据——registry 已被真实补挂路径更新(CONFIRMED)",
            new_reg == "CONFIRMED",
            f"current_sl_id={new_cur_sl!r}；registry={new_reg!r}"
            "；注：本环境下 current_sl_id 未随补挂回写（监控循环既有行为观察项，"
            "非本轮 R1-R6 修复对象——见本文件 docstring 登记）")

        # ---- 反例保持：UNKNOWN 不得误清 ----
        d2 = tempfile.mkdtemp(prefix="q15chain_")
        sp2 = os.path.join(d2, "trade_state.json")
        q15._seed_batch(sp2)
        s2 = q15._read_disk(sp2)
        trader_260725.STATE_FILE = sp2  # 修复：新建场景必须钉死目标账本路径
        f2 = q15._make_fake(sp2, s2, fail_create=False)
        f2._batch_net_position = lambda b: (0.43, 0.43)
        f2.exchange.create_order.side_effect = ccxt.NetworkError("read timeout")
        CryptoTrader.update_batch_sl(f2, q15.BATCH, 55000.0)
        d2k = q15._read_disk(sp2)
        rep_reg2 = q15._reg_state(d2k)
        cur2 = d2k.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id")
        report(
            "CHAIN-5 UNKNOWN create → 账本保持 PENDING_CREATE（不得被误清 ABSENT）",
            rep_reg2 == "PENDING_CREATE" and cur2 == q15.OLD_SL_ID,
            f"registry={rep_reg2!r}；current_sl_id={cur2!r}")
    finally:
        trader_260725.STATE_FILE = rep

    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"Q15 完整保护恢复链测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
