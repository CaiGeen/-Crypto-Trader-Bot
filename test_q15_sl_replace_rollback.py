#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Q15 换挂止损失败后失去旧保护 —— 反例 / 修后 / 健康阳性对照（v1.2 P0/P1 地图首位）。

## 缺陷（外部复审复现 + 主审逐行核验，审计报告 E4①）

换挂两条路径同构（`update_batch_sl` 用户改 SL / `_update_sl_no_validation` 保本损）：
「撤旧成功（registry→ABSENT，4069/4320）→ 意图 PENDING_CREATE（4102/4346）→
create 新单（4115/4361）」。create 被交易所**确定性拒绝**（Binance -2021
Order would immediately trigger）时旧实现只 `return False`（4169-4170/4422-4423）：

1. **裸奔窗口**：交易所旧单已撤、新单未立，零告警零处置；
2. **补挂堵死**：意图残留 PENDING_CREATE → F3 裁决恒 `'hold'`
   （`_adjudicate_recreate_before_repair` 7891-7892）→ 监控永远不补挂；
3. **账本谎报**：`current_sl_id` 仍指向已撤旧单（账本说有 SL、交易所实际没有）。

修后（`_dispose_sl_replace_failure`，6 个撤旧后失败出口全接）：残留置终态
ABSENT 放行 F3 + `current_sl_id` 置空（触发监控 9587 缺失检测 → 按 stop_steps
旧价自动补挂，等效回滚旧保护）+ critical（邮箱兜底，同键 3 轮去重）+ 返回消息
携带处置说明。create 结果未知（网络异常）时不改账本（UNKNOWN ≠ EMPTY），只告警。

## 三组

- **反例组（修前红 → 修后绿）**：R1 update_batch_sl 确定性拒绝（R1-1..R1-5）；
  R2 `_update_sl_no_validation` 同构；R3 F3 裁决由 `'hold'` 变 `'allow'`。
- **健康阳性对照**：C1 撤旧 + 新单成功 → 行为与修前完全一致
  （True + 新 id + CONFIRMED + 单条非 critical 成功通知 + 无处置文案）。
- **结构断言**：S1 两条换挂路径的 6 个撤旧后失败出口全部接
  `_dispose_sl_replace_failure`（防新增出口漏接）。

零网络、零生产文件（STATE_FILE 重定向临时目录）。
跑法：`.venv\\Scripts\\python.exe test_q15_sl_replace_rollback.py`（rc=0 即全过）
"""

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

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_q15_001"
IDENT_SL = f"{BATCH}|SL|L0|LONG"
OLD_SL_ID = "sl_old_1"
NEW_SL_ID = "sl_ex_1"  # 与 test_protection_write_gates 对照组一致（verify 链已验证可绿）

RESULTS = []
_real_state_file = trader_260725.STATE_FILE


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


# --------------------------------------------------------------------------
# 脚手架（沿用 test_protection_write_gates.py 模式：MagicMock 假实例 +
# 真实持久化 / 锁 / registry 语义；STATE_FILE 重定向临时目录）
# --------------------------------------------------------------------------

def _fresh_state_file():
    d = tempfile.mkdtemp(prefix="q15_")
    path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = path
    return d, path


def _restore_state_file():
    trader_260725.STATE_FILE = _real_state_file


def _seed_batch(state_path):
    """种子：活跃批次 + 旧 SL 已 CONFIRMED 在场（current_sl_id=sl_old_1）。"""
    b = {
        "is_active": True,
        "batch_id": BATCH,
        "symbol": SYMBOL,
        "side": "BUY",
        "entry_orders": ["e1"],
        "stop_steps": [55000.0],
        "take_profit_price": 60000.0,
        "current_sl_id": OLD_SL_ID,
        "tp_order_id": None,
        "close_phase": 0,
        "pending_sl_orders": [],
        "last_filled_count": 1,
        "target_amounts": [0.43],
        "params_base": {"leverage": 100},
        "is_hedge_mode": False,
        "user_modified": False,
        "protection_registry": {
            IDENT_SL: {"state": "CONFIRMED", "order_id": OLD_SL_ID, "id_known": True,
                       "order_kind": "conditional", "role": "SL", "layer": 0,
                       "side": "LONG", "fail_count": 0, "updated_at": time.time()},
        },
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({SYMBOL: {BATCH: b}}, f, ensure_ascii=False, indent=2)
    return b


def _read_disk(state_path):
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def _make_fake(state_path, states, fail_create):
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_ticker.return_value = {"last": 60000.0, "close": 60000.0}
    ex.fetch_order.return_value = {"id": NEW_SL_ID, "status": "NEW"}
    ex.create_order.return_value = {"id": NEW_SL_ID}
    ex.fetch_open_orders.return_value = [{"id": NEW_SL_ID, "status": "NEW"}]
    if fail_create:
        # 反例注入：交易所**确定性拒绝**（-2021 Order would immediately trigger）
        ex.create_order.side_effect = ccxt.ExchangeError(
            "binance {" + '{"code":-2021,"msg":"Order would immediately trigger."}' + "}")
    fake.exchange = ex

    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get("level", "info"), str(text)))

    fake._api_cooldown_until = 0
    fake._process_start_ts = time.time()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._defer_state_corrupt_alert = False

    fake._state_lock = threading.Lock()
    fake.load_all_states = lambda: states
    fake._load_all_states_ex = lambda: (states, False, "")
    fake._persist_states = lambda all_s: CryptoTrader._persist_states(fake, all_s)
    # save_batch_state 必须绑真实实现：处置落盘（current_sl_id 置空）走它。
    # ⚠️ 连带 _merge_batch_state 也必须绑真实——它不在既有夹具绑定清单里
    # （N6 从不调 save_batch_state 所以从未暴露），漏绑 → merge 返回 MagicMock →
    # 落盘 JSON 序列化失败 → save 返回 False + 假 critical，健康对照假红。
    fake.save_batch_state = (
        lambda s, b, d: CryptoTrader.save_batch_state(fake, s, b, d))
    fake._merge_batch_state = (
        lambda disk, snap: CryptoTrader._merge_batch_state(fake, disk, snap))
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._update_registry_locked = (
        lambda s, b, i, **f: CryptoTrader._update_registry_locked(fake, s, b, i, **f))
    fake._update_registry_checked = (
        lambda s, b, i, **f: CryptoTrader._update_registry_checked(fake, s, b, i, **f))
    fake._converge_alert = (
        lambda key, msg, level='critical': CryptoTrader._converge_alert(fake, key, msg, level=level))

    for name in ("_protection_identity", "_build_intent", "_order_matches_intent",
                 "_assert_create_allowed", "_final_pre_create_check",
                 "_commit_protection_with_g3", "_g3a_converge_race_order",
                 "_verify_order_created", "_classify_create_exception",
                 "_verify_and_update_registry", "_recheck_registry_self_heal",
                 "_is_stale_pre_launch_entry", "_verify_failure_msg",
                 "_adjudicate_recreate_before_repair",
                 "_dispose_sl_replace_failure"):
        if hasattr(CryptoTrader, name):
            setattr(fake, name, (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())
    return fake


def _run_replace(mode, fail_create):
    """驱动真实换挂入口。mode='sl' → update_batch_sl；mode='be' → 保本损路径。

    返回 (返回值, 通知列表, 磁盘)。"""
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states, fail_create)
        fake._batch_net_position = lambda b: (0.43, 0.43)
        if mode == 'sl':
            ret = CryptoTrader.update_batch_sl(fake, BATCH, 55000.0)
        else:
            b_data = states[SYMBOL][BATCH]
            ret = CryptoTrader._update_sl_no_validation(
                fake, SYMBOL, BATCH, b_data, 55000.0)
        return ret, list(fake.sent), _read_disk(state_path), fake
    finally:
        _restore_state_file()


def _reg_state(disk, ident=IDENT_SL):
    return (disk.get(SYMBOL, {}).get(BATCH, {})
            .get("protection_registry", {}).get(ident, {}).get("state"))


# --------------------------------------------------------------------------
# 反例组1：update_batch_sl × 确定性拒绝 → 处置齐全（修前红）
# --------------------------------------------------------------------------

def check_r1_replace_rejected_disposed():
    ret, sent, disk, fake = _run_replace('sl', fail_create=True)
    crit = [m for lvl, m in sent if lvl == "critical"]
    reason = str(ret[1]) if isinstance(ret, (tuple, list)) and len(ret) > 1 else ""

    report(
        "R1-1 前提：撤旧确实发生（cancel 被调、旧 id）",
        fake.exchange.cancel_order.call_count == 1
        and OLD_SL_ID in str(fake.exchange.cancel_order.call_args),
        f"cancel 调用={fake.exchange.cancel_order.call_count} 次，"
        f"args={fake.exchange.cancel_order.call_args}")

    report(
        "R1-2 拒绝后 registry 意图残留置终态 ABSENT（修前=PENDING_CREATE → F3 永久 hold）",
        _reg_state(disk) == "ABSENT",
        f"磁盘 registry state={_reg_state(disk)!r}（期望 'ABSENT'）")

    report(
        "R1-3 拒绝后 current_sl_id 置空（修前=仍指向已撤旧单，账本谎报有 SL）",
        disk.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id") is None,
        f"磁盘 current_sl_id={disk.get(SYMBOL, {}).get(BATCH, {}).get('current_sl_id')!r}（期望 None）")

    report(
        "R1-4 裸奔窗口发 critical（修前零告警）且返回消息携带处置说明",
        bool(crit) and "换挂止损失败" in crit[0]
        and "换挂止损失败处置" in reason and ret[0] is False,
        f"critical={len(crit)} 条；首条正文前80={crit[0][:80] if crit else None!r}；"
        f"返回 reason 前120={reason[:120]!r}")

    verdict, _ = _f3_verdict_after(disk)
    report(
        "R1-5 F3 补挂裁决 = 'allow'（修前='hold' → 监控补挂被堵死 = 无人接管）",
        verdict == "allow",
        f"F3 verdict={verdict!r}（期望 'allow' → 监控 9587 下一轮自动补挂旧价）")


def _f3_verdict_after(disk):
    """用磁盘终态构造 fake 驱动 F3（裁决读 load_all_states）。"""
    fake = mock.MagicMock()
    fake.load_all_states = lambda: disk
    fake._load_all_states_ex = lambda: (disk, False, "")
    fake._state_lock = threading.Lock()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.exchange = mock.MagicMock()
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get("level", "info"), str(text)))
    fake._converge_alert = (
        lambda key, msg, level='critical': CryptoTrader._converge_alert(fake, key, msg, level=level))
    verdict, found = CryptoTrader._adjudicate_recreate_before_repair(
        fake, SYMBOL, BATCH, IDENT_SL)
    return verdict, found


# --------------------------------------------------------------------------
# 反例组2：_update_sl_no_validation（保本损）× 确定性拒绝 → 同构处置
# --------------------------------------------------------------------------

def check_r2_no_validation_rejected_disposed():
    ret, sent, disk, fake = _run_replace('be', fail_create=True)
    crit = [m for lvl, m in sent if lvl == "critical"]
    reason = str(ret[1]) if isinstance(ret, (tuple, list)) and len(ret) > 1 else ""

    report(
        "R2-1 保本损路径撤旧确实发生",
        fake.exchange.cancel_order.call_count == 1
        and OLD_SL_ID in str(fake.exchange.cancel_order.call_args),
        f"cancel 调用={fake.exchange.cancel_order.call_count} 次")

    report(
        "R2-2 保本损拒绝后 registry 置终态 ABSENT + current_sl_id 置空（修前裸奔）",
        _reg_state(disk) == "ABSENT"
        and disk.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id") is None,
        f"registry={_reg_state(disk)!r}；current_sl_id="
        f"{disk.get(SYMBOL, {}).get(BATCH, {}).get('current_sl_id')!r}")

    report(
        "R2-3 保本损裸奔窗口发 critical + 返回消息携带处置说明",
        bool(crit) and "换挂止损失败" in crit[0] and "换挂止损失败处置" in reason,
        f"critical={len(crit)} 条；reason 前120={reason[:120]!r}")


# --------------------------------------------------------------------------
# 反例组3：create 结果未知（网络异常）→ 不改账本 + critical（UNKNOWN ≠ EMPTY）
# --------------------------------------------------------------------------

def check_r3_unknown_result_conservative():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states, fail_create=False)
        fake.exchange.create_order.side_effect = ccxt.NetworkError("read timeout")
        fake._batch_net_position = lambda b: (0.43, 0.43)
        ret = CryptoTrader.update_batch_sl(fake, BATCH, 55000.0)
        sent, disk = list(fake.sent), _read_disk(state_path)
        crit = [m for lvl, m in sent if lvl == "critical"]
        reason = str(ret[1]) if isinstance(ret, (tuple, list)) and len(ret) > 1 else ""
        report(
            "R3-1 结果未知 → 账本维持 PENDING_CREATE 保守裁决（不盲目改写防双挂/丢单）",
            _reg_state(disk) == "PENDING_CREATE"
            and disk.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id") == OLD_SL_ID,
            f"registry={_reg_state(disk)!r}；current_sl_id="
            f"{disk.get(SYMBOL, {}).get(BATCH, {}).get('current_sl_id')!r}")
        report(
            "R3-2 结果未知 → critical 提示人工核实 + 返回消息含『结果未知』",
            bool(crit) and "结果未知" in crit[0] and "结果未知" in reason and ret[0] is False,
            f"critical={len(crit)} 条；reason 前140={reason[:140]!r}")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# 健康阳性对照：撤旧 + 新单成功 → 行为与修前完全一致
# --------------------------------------------------------------------------

def check_c1_happy_replace_unchanged():
    ret, sent, disk, fake = _run_replace('sl', fail_create=False)
    levels = [lvl for lvl, _ in sent]
    reason = str(ret[1]) if isinstance(ret, (tuple, list)) and len(ret) > 1 else ""
    report(
        "C1-1 成功换挂：True + 新 id + cancel 旧单 + registry CONFIRMED",
        ret[0] is True
        and disk.get(SYMBOL, {}).get(BATCH, {}).get("current_sl_id") == NEW_SL_ID
        and fake.exchange.cancel_order.call_count == 1
        and _reg_state(disk) == "CONFIRMED",
        f"ok={ret[0]!r}；current_sl_id="
        f"{disk.get(SYMBOL, {}).get(BATCH, {}).get('current_sl_id')!r}；"
        f"registry={_reg_state(disk)!r}")
    report(
        "C1-2 成功换挂：恰 1 条非 critical 成功通知、无处置文案（健康路径零回归）",
        len(sent) == 1 and "critical" not in levels and "换挂止损失败处置" not in reason,
        f"通知级别={levels}；reason 前120={reason[:120]!r}")


# --------------------------------------------------------------------------
# 结构断言：6 个撤旧后失败出口全部接处置
# --------------------------------------------------------------------------

def check_s1_all_failure_exits_disposed():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_260725.py")
    text = open(path, encoding="utf-8").read()
    n = text.count("_dispose_sl_replace_failure(")
    report(
        "S1 结构：_dispose_sl_replace_failure = 1 定义 + 6 调用点（两路径 × 仲裁/意图/异常）",
        n == 7,
        f"出现 {n} 次（期望 7：1 def + update_batch_sl 3 个 + _update_sl_no_validation 3 个）。"
        f"≠7 = 新增/删除撤旧后失败出口须同步审处置")


CHECKS = [
    check_r1_replace_rejected_disposed,
    check_r2_no_validation_rejected_disposed,
    check_r3_unknown_result_conservative,
    check_c1_happy_replace_unchanged,
    check_s1_all_failure_exits_disposed,
]


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f"测试自身异常: {type(e).__name__}: {e}")
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"Q15 换挂止损失败处置测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if failed == 0 and ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
