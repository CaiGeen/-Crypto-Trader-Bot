#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F2/F3「真实成交价入账修复」——候选补丁的针对性 RED/GREEN 验收。

## 覆盖（对应转审 §五 的五项）

| # | 用例 | 断言 |
|---|------|------|
| T1 | **同一真实成交价基准** | 响应含 `actualPrice=85506.5` + `stopPrice=85500` → 入账必须是 **85506.5**（基准代码写 85500 = 触发价冒充 → RED） |
| T2 | 缺价 | 无任何真实成交均价 → `cost_pending`：**不写价**、登记待补证、**保护照常挂出** |
| T3 | 非法价格 | `actualPrice` = 负数 / 非数字 → 同样 `cost_pending`（价格须**有限正数**） |
| T4 | 数量不匹配 | `actualQty` ≠ 计划量 / 方向不符 → `qty_reconcile_pending`：**不按计划量冒充**、不为该层挂保护 |
| T5 | 分支② 保护与安全退出 | 前缀门放行（安全退出可执行）+ `/be` 被暂停 + 无证据的 0 仍 Fail-Closed |
| T6 | 补证 | 原精度入账 + 手续费**一次性**补记 + 重复调用不重复计量 + 触发价**不放行** |
| T7 | 重启恢复不重复计量 | 补证完成后再次驱动监控（模拟重启）→ 价格与手续费不翻倍 |
| T8 | 已平仓待结算 | **不复活订单**（零 `create_order`）+ **不重复结算**（dedup 单次记账） |

七复审 R1–R6 闭环（同文件跑基准/候选，基准必须 RED）：

| # | 缺口 | 断言 |
|---|------|------|
| T9 | R1 安全退出只通了前缀门 | 前缀门放行 ⇒ `_close_amount_guard`（覆盖勘察）也放行；无证据 0 / 数量不符仍拒绝 |
| T10 | R2 只证明 helper 被调用（日志里 G1 恒拦） | 真实 `create_order` 下达 STOP_MARKET、覆盖量=已核实成交量、**无 G1 拦截**、registry 意图落盘。**八复审收口**：成功判据必须是 **CONFIRMED** + 订单身份=create 返回 id + intent.qty=已核实量；`PENDING_CREATE/PENDING_VERIFY`（确认未知）**不得**通过「已创建并确认」，另有反例分支强制 verify→unknown 验证判据会 FAIL |
| T11 | R2 数量异常时既有保护可能被撤 | 数量证据异常 → 既有 CONFIRMED SL **未被撤销/换挂** |
| T12 | R3 费用查询在清理重入时反复发生 | 首次解析后：多轮清理失败 / 重启 / PnL 写盘失败 **端点调用数零增长** |
| T13 | R4 用补证前旧快照按 0 成本首记 | 每次读取独立快照 + 真实零仓位 + 收敛成功 → 均价/数量/退出价/净额逐项核对 |
| T14 | R5 归零分支与 finally 可绕过待补证 | finally 授权=skip；归零轮不清理、不记 PnL、不建单 |
| T15 | R6 补证发现数量不符只更新 qty 却仍留 cost_pending | 原子升级 `qty_reconcile_pending`（状态/两列表/critical 告警/实际数量 0.30）+ 结算门与前缀门双拦 + 无撤单无建单 |

八复审退回项（2 项 P1 + 预算/身份/T10 收口，同文件跑基准/候选，基准必须 RED）：

| # | 缺口 | 断言 |
|---|------|------|
| T16 | **P1-1** 普通市价结算仍用旧成本 | 实际驱动 `close_position_market`：门内补证成功后**重读并核验本次事务账本**再算 → 记账均价=85506.5（**禁 0 成本首记**）、数量/退出价/净额逐项核对（≈-27.2266），费用按补证后真实价解析 |
| T17 | **P1-2** 待结算状态落盘断点 | ① 载荷+阶段+原因**一次落盘**（写失败=整体未写，不留半写态）；② op 迁移不覆盖；③ 载荷在册而 PnL 未落盘（原因串缺失的中间态）→ finally=skip、`clear_batch_state` **拒删**（无墓碑、告警指名载荷）、PnL 0 次；④ 恢复原因后 finalizer 正常记账并收敛 |
| T18 | 请求预算仍未证明 | ① 首轮成功结算端点调用数**实测并钉死**（构成逐项）；② 费用缓存落盘失败 → **显式退避**：退避期零新请求、不记账不清账，窗口过期后才有下一轮 → 3 轮总调用 ≤ 2×单轮预算（旧实现 12 次），退避时长递增 |
| T19 | 成交身份证据仍可缺失放行 | 缺 `id`（含 `info.orderId`）/ 缺 `side`（含 `info.side`）→ `qty_unverified`，**不得 confirmed**；规范原始字段可用（`info.orderId`）则放行；登记证据必须标 `order_id_source=expected / identity_observed=False`，驱动整轮后进 `qty_reconcile_pending` |

⚠️ 本文件全绿 ≠ 全仓审计通过；只覆盖 F2/F3 候选补丁的上述点。
现有 F2 5/5 探针与 B1 9/9 **不能替代**本文件。

## 驱动方式

`time.sleep` 换成计数哨兵（继承 `BaseException`，避免被循环 `except Exception`
当成真实故障）；`fetch_open_orders` 返回空 → 每层走 `fetch_order` 回查分支。
全程零网络。

⚠️ MagicMock 陷阱（见 `test_monitor_poll_recovery.py` 头注 / `test_b1_state_machine.py`）：
未显式绑定的方法会退化为自动 mock，解包处抛 TypeError 又被 `except` 吞掉 →
路径静默不执行。因此 REAL_HELPERS 逐个显式绑定真实实现，未绑定一律给确定性桩。

跑法：`.venv\\Scripts\\python.exe test_f2f3_fill_cost.py`（rc=0 即全过）
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader, TAKER_FEE_RATE  # noqa: E402

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_f2f3_001"
ENTRY_ID = "e1"
PLANNED = 0.43
REAL_PRICE = 85506.5
TRIGGER_PRICE = 85500.0

RESULTS = []
_real_state_file = trader_260725.STATE_FILE


class _StopLoop(BaseException):
    """把 `while True` 限定成确定轮次；必须是 BaseException（见头注）。"""


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


# ---------------------------------------------------------------------------
# 需要绑真实实现的方法（其余给确定性桩，避免 MagicMock 自动 mock 吞语义）
# ---------------------------------------------------------------------------
REAL_HELPERS = (
    "_persist_states", "_update_registry", "_protection_identity", "_build_intent",
    "_batch_has_active_exposure", "_collect_batch_order_ids", "_verify_clear_proof",
    "clear_batch_state", "_check_protection_order_validity",
    "_adjudicate_recreate_before_repair", "_merge_batch_state",
    "_order_matches_intent", "_assert_create_allowed", "_final_pre_create_check",
    "_commit_protection_with_g3", "_g3a_converge_race_order", "_verify_order_created",
    "_classify_create_exception", "_verify_and_update_registry",
    "_recheck_registry_self_heal", "_is_stale_pre_launch_entry",
    "_place_prepared_orders_immediately", "_monitor_lifecycle_check",
    "_calculate_monitoring_interval", "_get_active_batch_count",
    "_alert_poll_degraded", "_monitor_takeover_handoff", "_monitor_terminal_evidence",
    "_finish_monitor_takeover", "_run_monitor_takeover",
    "_batch_net_position", "_self_heal_no_id", "_rebuild_entry_orders_from_registry",
    "_registry_has_unresolved_entries", "_write_monitor_error_if_owner",
    "_finally_cleanup_decision", "_unresolved_intent_batches",
    # ---- F2/F3 候选补丁的新方法（未绑定 → 自动 mock → 解包/比较静默失效）----
    "_resolve_entry_fill_evidence", "_record_fill_evidence",
    "_backfill_entry_costs", "_settlement_cost_gate",
    "_defer_settlement_for_cost", "_finalize_cost_pending_settlement",
    "_derive_close_txn_vars", "set_breakeven_sl", "_set_close_reason_if_current",
    # ---- R2/R3 验收要求：保护创建与费用解析必须走**真实实现** ----
    # （漏绑 _update_registry_checked/_update_registry_locked → G1 恒拦 →
    #   测试只看到「尝试」却报保护挂出；漏绑费用解析 → 返回 MagicMock 无法计数）
    "_update_registry_checked", "_update_registry_locked",
    "_compute_settlement_fees", "_resolve_order_fees",
    # ---- R1：安全退出必须驱动真实的数量守卫 + 覆盖勘察（两门判据一致性）----
    "_close_amount_guard", "_survey_same_side_batches",
    # ---- T16（八复审 P1-1）：市价平仓全流程要真实事务机（BEGIN/确认/收敛）----
    # 漏绑 → 自动 mock → 四元组解包 TypeError 或「假成功」，断言失真。
    "_begin_close_request_if_active", "_rollback_close_request_if_current",
    "_confirm_close_filled", "_fetch_close_order_state",
    "_cancel_and_verify_entry_orders", "_find_registry_identity_by_order_id",
    "_cancel_limit_close_order",
)


def _base_batch(**over):
    b = {
        "is_active": True, "batch_id": BATCH, "symbol": SYMBOL, "side": "BUY",
        "entry_orders": [ENTRY_ID], "stop_steps": [55000.0],
        "take_profit_price": 60000.0, "current_sl_id": None, "tp_order_id": None,
        "close_phase": 0, "pending_sl_orders": [], "protection_registry": {},
        "target_amounts": [PLANNED], "last_filled_count": 0,
        "filled_details": [0.0], "total_entry_fee": 0.0,
        "params_base": {"leverage": 100, "priceProtect": True},
        "is_hedge_mode": False,
    }
    b.update(over)
    return b


def _states(**over):
    return {SYMBOL: {BATCH: _base_batch(**over)}}


def _layer_sl_params():
    return [{"symbol": SYMBOL, "type": "STOP_MARKET", "side": "sell", "amount": PLANNED,
             "params": {"stopPrice": 55000.0, "reduceOnly": True,
                        "workingType": "MARK_PRICE", "priceProtect": True}}]


def _make_fake(states, position_amt=PLANNED):
    fake = mock.MagicMock()
    # 真实 _safe_api_call 消费 retries/delay/auth_probe，不转发给端点；
    # F2/F3-R3 起费用解析显式传 retries=2，桩必须同形剥离（同 test_t1c_fees）。
    fake._safe_api_call = lambda fn, *a, **k: fn(
        *a, **{x: y for x, y in k.items() if x not in ('retries', 'delay', 'auth_probe')})
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex

    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get("level", "info"), str(text)))
    fake._send_email_alert = lambda text, subject="": None

    fake._api_cooldown_until = 0
    # 八复审·请求预算：费用缓存落盘失败的显式退避游标（生产用 getattr 默认值，
    # 这里必须给真值 —— MagicMock 自动属性会让 float() 抛 TypeError）。
    fake._fee_resolve_backoff_until = 0.0
    fake._fee_resolve_fail_count = 0
    fake._process_start_ts = time.time()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._defer_state_corrupt_alert = False
    fake._active_monitors = set()
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitor_generations = {}
    fake._state_lock = threading.RLock()
    fake._registry_self_heal_interval = 10 ** 9
    fake.registry_self_heal_interval = 10 ** 9
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 10 ** 9
    fake._check_ip_periodically = lambda: None
    fake._sync_time_if_needed = lambda: None
    fake._g3_log_position_recheck = lambda *a, **k: None
    fake._load_tombstones = lambda: {}
    fake._get_current_position_amt = lambda *a, **k: position_amt
    fake._finally_cleanup_decision = lambda s, b: ('skip', None)
    fake._unresolved_intent_batches = set()
    fake._poll_degraded_batches = set()
    fake._poll_fail_streak = {}
    fake._poll_first_fail_time = {}
    fake._poll_last_success_time = {}
    fake._poll_alert_attempted = {}
    fake._poll_alert_active = False
    fake._poll_alert_lock = threading.RLock()
    fake._sg3_alerted = set()
    fake._freeze_print_state = {}
    fake._freeze_alerted = {}
    # converge 证明固定返回 None → clear 永不发生（本文件不测清理收敛）
    fake._converge_batch_orders_before_clear = lambda *a, **k: None

    fake.load_all_states = lambda: states
    fake._load_all_states_ex = lambda: (states, False, "")
    # ⚠️ 关键保真度修复：_fee_float 是 @staticmethod，不在 REAL_HELPERS 的
    # 「传 self」绑定形态里。漏绑 → 自动 mock → 返回 MagicMock →
    # `f > 0.0` 抛 TypeError（实测），被各层 except 吞成「路径静默不执行」。
    fake._fee_float = CryptoTrader._fee_float
    fake.saved = []

    def _record_save(s, b, d, **_k):
        # 记录式桩 + **落实契约**：save_batch_state 的真实语义是「把本段变化的
        # 字段写回该批次」。只 append 不写回 → 断言读 states 永远读不到本轮写入
        # （第一版 RED 就栽在这里，那是桩的保真度问题、不是产品行为）。
        fake.saved.append((s, b, dict(d)))
        if s in states and b in states[s] and isinstance(d, dict):
            states[s][b].update(d)
        return True
    fake.save_batch_state = _record_save

    fake.sl_place_calls = []
    for name in REAL_HELPERS:
        if hasattr(CryptoTrader, name):
            setattr(fake, name,
                    (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())
    _real_place = fake._place_prepared_orders_immediately

    def _spy_place(*a, **k):
        fake.sl_place_calls.append(a[1] if len(a) > 1 else None)
        return _real_place(*a, **k)
    fake._place_prepared_orders_immediately = _spy_place
    return fake


def _drive(fake, batch=None, max_rounds=5, stop_at_sleep=None):
    """驱动 `_start_monitoring` 至哨兵；返回 (sleep次数, 异常)。"""
    b = batch or _base_batch()
    n = {"c": 0}

    def _sleep(sec):
        n["c"] += 1
        if n["c"] > max_rounds:
            raise _StopLoop()
    with mock.patch.object(trader_260725.time, "sleep", _sleep):
        try:
            CryptoTrader._start_monitoring(
                fake, SYMBOL, BATCH, [ENTRY_ID], [55000.0], 60000.0,
                None, None, PLANNED, [PLANNED],
                {"leverage": 100, "priceProtect": True}, False, "BUY",
                int(b.get("last_filled_count", 0)),
                list(b.get("filled_details") or [0.0]),
                float(b.get("total_entry_fee") or 0.0),
                list(b.get("pending_sl_orders") or []),
                None, _layer_sl_params())
        except _StopLoop:
            pass
        except Exception as e:
            return n["c"], e
    return n["c"], None


# ---------------------------------------------------------------------------
# 交易所回查响应（同一真实成交价基准：actualPrice 与触发价不一致）
# ---------------------------------------------------------------------------
def _closed_order(**over):
    d = {
        "id": ENTRY_ID, "status": "closed", "side": "buy", "symbol": SYMBOL,
        "average": 0, "price": 0, "filled": PLANNED,
        "stopPrice": "85500.00",
        "info": {"orderId": ENTRY_ID, "actualPrice": "85506.50000",
                 "actualQty": "0.43", "cumQuote": "0", "executedQty": "0.43"},
    }
    d.update(over)
    return d


def _with_order(order):
    return lambda oid, symbol, **kw: dict(order)


def _fee_of(price, qty=PLANNED):
    return price * qty * TAKER_FEE_RATE


# ===========================================================================
# T1 同一真实成交价：基准 RED / 候选 GREEN
# ===========================================================================
def t1_real_fill_price():
    states = _states()
    fake = _make_fake(states)
    fake.exchange.fetch_open_orders = lambda symbol: []
    fake.exchange.fetch_order = _with_order(_closed_order())
    _drive(fake, states[SYMBOL][BATCH])
    b = states[SYMBOL][BATCH]
    got_fd = b.get("filled_details", [None])[0]
    ev = (b.get("fill_evidence") or {}).get("0") or {}
    got_ev = ev.get("price")
    ok = (got_fd == REAL_PRICE) and (got_ev == REAL_PRICE)
    report("T1 真实成交价入账（actualPrice，禁触发价冒充）",
           ok,
           f"filled_details={got_fd!r}（期望 {REAL_PRICE}）, "
           f"fill_evidence.price={got_ev!r}; 触发价={TRIGGER_PRICE} "
           f"（基准代码会写 {TRIGGER_PRICE}）")
    return ok


# ===========================================================================
# T2 缺价 → cost_pending（不写价 + 登记待补证 + 保护照常）
# ===========================================================================
def t2_missing_price():
    states = _states()
    fake = _make_fake(states)
    fake.exchange.fetch_open_orders = lambda symbol: []
    order = _closed_order()
    order["info"] = {"orderId": ENTRY_ID, "actualQty": "0.43",
                     "cumQuote": "0", "executedQty": "0.43"}   # 无真实成交均价
    fake.exchange.fetch_order = _with_order(order)
    _drive(fake, states[SYMBOL][BATCH])
    b = states[SYMBOL][BATCH]
    fd = (b.get("filled_details") or [None])[0]
    cp = b.get("cost_pending_layers")
    ev = (b.get("fill_evidence") or {}).get("0") or {}
    placed = len(fake.sl_place_calls) > 0
    ok = (fd == 0.0) and (cp == [0]) and ev.get("status") == "cost_pending" and placed
    report("T2 缺价 → cost_pending（不写价 / 登记待补证 / 保护照常）",
           ok,
           f"filled_details={fd!r}（必须为 0.0）, cost_pending_layers={cp!r}, "
           f"evidence={ev.get('status')!r}, 保护挂出={placed}（sl_place_calls="
           f"{fake.sl_place_calls!r}）")


# ===========================================================================
# T3 非法价格（负数 / 非数字）→ cost_pending
# ===========================================================================
def t3_illegal_price():
    all_ok = True
    for bad in ("-1", "abc", "0", "NaN"):
        states = _states()
        fake = _make_fake(states)
        fake.exchange.fetch_open_orders = lambda symbol: []
        order = _closed_order()
        order["info"] = {"orderId": ENTRY_ID, "actualPrice": bad,
                         "actualQty": "0.43", "cumQuote": "0", "executedQty": "0.43"}
        fake.exchange.fetch_order = _with_order(order)
        _drive(fake, states[SYMBOL][BATCH])
        b = states[SYMBOL][BATCH]
        fd = (b.get("filled_details") or [None])[0]
        cp = b.get("cost_pending_layers")
        good = (fd == 0.0) and (cp == [0])
        all_ok = all_ok and good
        print(f"        actualPrice={bad!r} → filled_details={fd!r}, "
              f"cost_pending_layers={cp!r} {'OK' if good else 'BAD'}")
    report("T3 非法价格（负数/非数字/0/NaN）→ cost_pending（须有限正数）", all_ok,
           "四种非法值均未入账，全部进入待补证队列")


# ===========================================================================
# T4 数量不匹配 / 方向不符 → qty_reconcile_pending（不按计划量冒充）
# ===========================================================================
def t4_qty_mismatch():
    cases = {
        "数量不匹配": {"info": {"orderId": ENTRY_ID, "actualPrice": "85506.50000",
                               "actualQty": "0.30", "cumQuote": "0",
                               "executedQty": "0.30"}},
        # filled=0 → 数量三源（actualQty/executedQty/filled）全缺，禁按计划量冒充
        "数量缺失": {"filled": 0,
                    "info": {"orderId": ENTRY_ID, "actualPrice": "85506.50000"}},
        "方向不符": {"side": "sell"},
        "身份不符": {"id": "OTHER"},
    }
    all_ok = True
    for label, over in cases.items():
        states = _states()
        fake = _make_fake(states)
        fake.exchange.fetch_open_orders = lambda symbol: []
        fake.exchange.fetch_order = _with_order(_closed_order(**over))
        _drive(fake, states[SYMBOL][BATCH])
        b = states[SYMBOL][BATCH]
        fd = (b.get("filled_details") or [None])[0]
        qr = b.get("qty_reconcile_pending")
        ev = (b.get("fill_evidence") or {}).get("0") or {}
        no_fake_qty = 0 not in (b.get("pending_sl_orders") or [])
        good = (fd == 0.0) and (qr == [0]) and ev.get("status") == "qty_unverified" \
            and no_fake_qty and not fake.sl_place_calls
        all_ok = all_ok and good
        print(f"        {label} → filled_details={fd!r}, qty_reconcile_pending={qr!r}, "
              f"evidence={ev.get('status')!r}, 未挂保护={not fake.sl_place_calls} "
              f"{'OK' if good else 'BAD'}")
    report("T4 数量/方向/身份不符 → qty_reconcile_pending（禁按计划量冒充、保留有效保护）",
           all_ok, "四种证据缺口均未入账、未按计划量计数、未为该层挂保护")


# ===========================================================================
# T5 分支②：保护与安全退出仍可执行（前缀门放行 + /be 暂停 + 无证据仍 Fail-Closed）
# ===========================================================================
def t5_protection_and_safe_exit():
    pending_ev = {"0": {"status": "cost_pending", "qty": PLANNED, "planned": PLANNED,
                        "side": "buy", "order_id": ENTRY_ID,
                        "why": "cost_unavailable（测试）"}}
    good = _states(cost_pending_layers=[0], fill_evidence=pending_ev,
                   last_filled_count=1, filled_details=[0.0])
    fake = _make_fake(good)

    ok_gate, _cv, why_gate = fake._derive_close_txn_vars(good[SYMBOL][BATCH], BATCH)
    ok_be, why_be = fake.set_breakeven_sl(BATCH)

    # 反例 A：无证据的 0 → 必须仍 Fail-Closed（守卫不删、不弱化）
    noev = _states(cost_pending_layers=[0], fill_evidence={},
                   last_filled_count=1, filled_details=[0.0])
    bad_gate, _ = fake._derive_close_txn_vars(noev[SYMBOL][BATCH], BATCH)[:2]

    # 反例 B：数量证据与计划量不符 → 不放行
    wrong = _states(cost_pending_layers=[0],
                    fill_evidence={"0": {"status": "cost_pending", "qty": 0.30,
                                         "side": "buy", "order_id": ENTRY_ID}},
                    last_filled_count=1, filled_details=[0.0])
    wrong_gate, _ = fake._derive_close_txn_vars(wrong[SYMBOL][BATCH], BATCH)[:2]

    # 反例 C：数量不匹配层待核对 → 平仓入口直接拒绝
    # 反例 C：数量不匹配层待核对 → 平仓入口直接拒绝
    qr = _states(qty_reconcile_pending=[0], last_filled_count=1,
                 filled_details=[85506.5])
    qr_gate, _qr_v, qr_why = fake._derive_close_txn_vars(qr[SYMBOL][BATCH], BATCH)

    ok = bool(ok_gate) and (not ok_be) and ("补证" in str(why_be)) \
        and (bad_gate is False) and (wrong_gate is False) and (qr_gate is False)
    report("T5 分支② 安全退出可执行 + /be 暂停 + 无证据仍 Fail-Closed",
           ok,
           f"有证据前缀门={ok_gate}（{why_gate}）; /be={ok_be}（{str(why_be)[:60]}）; "
           f"无证据0→{bad_gate}; 数量不符→{wrong_gate}; qty_reconcile→{qr_gate}"
           f"（{qr_why}）")


# ===========================================================================
# T6 补证：原精度入账 + 手续费一次性补记 + 重复调用不重复计量 + 触发价不放行
# ===========================================================================
def t6_backfill_idempotent():
    def seed():
        return _states(cost_pending_layers=[0], last_filled_count=1,
                       filled_details=[0.0], total_entry_fee=0.0,
                       fill_evidence={"0": {"status": "cost_pending", "qty": PLANNED,
                                            "planned": PLANNED, "side": "buy",
                                            "order_id": ENTRY_ID,
                                            "why": "cost_unavailable（测试）",
                                            "next_probe_at": 0}})

    # 6a：真实成交价补证 → 原精度 + 手续费一次性
    st = seed()
    fake = _make_fake(st)
    fake.exchange.fetch_order = _with_order(_closed_order())
    n1 = fake._backfill_entry_costs(SYMBOL, BATCH)
    b = st[SYMBOL][BATCH]
    fee_after_1 = b.get("total_entry_fee")
    n2 = fake._backfill_entry_costs(SYMBOL, BATCH)
    fee_after_2 = st[SYMBOL][BATCH].get("total_entry_fee")
    ok_a = (n1 == 1) and (b.get("filled_details")[0] == REAL_PRICE) \
        and (b.get("cost_pending_layers") == []) \
        and abs(fee_after_1 - _fee_of(REAL_PRICE)) < 1e-9 \
        and (n2 == 0) and (fee_after_2 == fee_after_1)

    # 6b：只有触发价 / 委托价 → 不放行，费用不变
    st2 = seed()
    fake2 = _make_fake(st2)
    fake2.exchange.fetch_order = _with_order(
        _closed_order(info={"orderId": ENTRY_ID, "actualQty": "0.43",
                            "stopPrice": "85500.00"}))
    n3 = fake2._backfill_entry_costs(SYMBOL, BATCH)
    b2 = st2[SYMBOL][BATCH]
    ok_b = (n3 == 0) and (b2.get("filled_details")[0] == 0.0) \
        and (b2.get("cost_pending_layers") == [0]) \
        and (b2.get("total_entry_fee") == 0.0)

    # 6c：补证期间回查失败 → 保持待补证，费用不变（下轮节流重试）
    st3 = seed()
    fake3 = _make_fake(st3)
    fake3.exchange.fetch_order = lambda *a, **k: (_ for _ in ()).throw(
        Exception("network"))
    n4 = fake3._backfill_entry_costs(SYMBOL, BATCH)
    b3 = st3[SYMBOL][BATCH]
    ok_c = (n4 == 0) and (b3.get("cost_pending_layers") == [0]) \
        and (b3.get("total_entry_fee") == 0.0)

    ok = ok_a and ok_b and ok_c
    report("T6 补证：原精度入账 + 手续费一次性 + 重复不计量 + 触发价不放行",
           ok,
           f"6a 补证{n1}次 fee={fee_after_1!r} 再调{n2}次 fee={fee_after_2!r} → {ok_a}; "
           f"6b 触发价不放行={ok_b}; 6c 回查失败保持待补证={ok_c}")


# ===========================================================================
# T7 重启恢复不重复计量
# ===========================================================================
def t7_restart_no_double():
    st = _states(cost_pending_layers=[0], last_filled_count=1,
                 filled_details=[0.0], total_entry_fee=0.0,
                 fill_evidence={"0": {"status": "cost_pending", "qty": PLANNED,
                                      "planned": PLANNED, "side": "buy",
                                      "order_id": ENTRY_ID, "next_probe_at": 0}})
    fake = _make_fake(st)
    fake.exchange.fetch_open_orders = lambda symbol: []
    fake.exchange.fetch_order = _with_order(_closed_order())

    # 第一次驱动（= 运行期补证成功）
    _drive(fake, st[SYMBOL][BATCH])
    b1 = dict(st[SYMBOL][BATCH])
    fee1 = b1.get("total_entry_fee")
    price1 = (b1.get("filled_details") or [None])[0]

    # 第二次驱动（= 重启：fresh 局部变量由账本重新灌入）
    fake2 = _make_fake(st)
    fake2.exchange.fetch_open_orders = lambda symbol: []
    fake2.exchange.fetch_order = _with_order(_closed_order())
    _drive(fake2, b1)
    b2 = st[SYMBOL][BATCH]
    fee2 = b2.get("total_entry_fee")
    price2 = (b2.get("filled_details") or [None])[0]

    ok = (price1 == REAL_PRICE) and (price2 == price1) \
        and (fee2 == fee1) and abs(fee1 - _fee_of(REAL_PRICE)) < 1e-9 \
        and (b2.get("cost_pending_layers") == []) \
        and (b2.get("total_entry_fee") == fee1)
    report("T7 重启恢复不重复计量（价格与手续费不翻倍）", ok,
           f"first: price={price1!r} fee={fee1!r}; "
           f"after restart: price={price2!r} fee={fee2!r}; "
           f"期望 fee={_fee_of(REAL_PRICE)!r}")


# ===========================================================================
# T8 已平仓待结算：不复活订单、不重复结算
# ===========================================================================
def t8_closed_pending_settlement():
    tmpdir = tempfile.mkdtemp(prefix="f2f3_")
    stats_file = os.path.join(tmpdir, "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl

    st = _states(close_phase=2, close_reason="cost_pending_settling",
                 last_filled_count=1, filled_details=[0.0], total_entry_fee=0.0,
                 cost_pending_layers=[0],
                 fill_evidence={"0": {"status": "cost_pending", "qty": PLANNED,
                                      "planned": PLANNED, "side": "buy",
                                      "order_id": ENTRY_ID, "next_probe_at": 0}},
                 cost_pending_settle_payload={
                     "mode": "市价平仓", "exit_price": 85528.7, "exit_qty": PLANNED,
                     "side": "BUY", "exit_fee": 0.0, "exit_fee_source": "estimated",
                     "close_op_id": "op1", "dedup_key": f"{SYMBOL}:{BATCH}:op1:costpending",
                     "created_at": time.time()})

    def _pnl_wrap(self, *a, **k):
        k["stats_file"] = stats_file
        return real_pnl(self, *a, **k)

    # ---- 8a：补证失败 → 不记 PnL、不清理、零建单 ----
    fake = _make_fake(st, position_amt=PLANNED)
    fake._record_realized_pnl = types.MethodType(_pnl_wrap, fake)
    fake.exchange.fetch_open_orders = lambda symbol: []
    fake.exchange.create_order = mock.MagicMock(side_effect=AssertionError("禁止建单"))
    fake.exchange.fetch_order = lambda *a, **k: (_ for _ in ()).throw(
        Exception("network"))
    _drive(fake, st[SYMBOL][BATCH], max_rounds=3)
    b = st[SYMBOL][BATCH]
    pnl_written_8a = os.path.exists(stats_file) and os.path.getsize(stats_file) > 2
    no_create_8a = fake.exchange.create_order.call_count == 0
    still_pending_8a = b.get("close_reason") == "cost_pending_settling" \
        and b.get("is_active") is True

    # ---- 8b：补证成功 → 记一次；重复轮转仍只记一次 ----
    # 8a 的失败回查已按既有退避把 next_probe_at 推到未来（节流属预期行为），
    # 这里模拟「退避窗口过去、补证后来成功」，故重置节流游标。
    st[SYMBOL][BATCH]["fill_evidence"]["0"]["next_probe_at"] = 0
    st[SYMBOL][BATCH]["fill_evidence"]["0"].pop("probe_count", None)
    fake2 = _make_fake(st, position_amt=PLANNED)
    fake2._record_realized_pnl = types.MethodType(_pnl_wrap, fake2)
    fake2.exchange.fetch_open_orders = lambda symbol: []
    fake2.exchange.create_order = mock.MagicMock(side_effect=AssertionError("禁止建单"))
    fake2.exchange.fetch_order = _with_order(_closed_order())

    r1 = fake2._finalize_cost_pending_settlement(SYMBOL, BATCH)
    trades_1 = _read_trades(stats_file)
    r2 = fake2._finalize_cost_pending_settlement(SYMBOL, BATCH)
    trades_2 = _read_trades(stats_file)

    no_create_8b = fake2.exchange.create_order.call_count == 0
    ok = (not pnl_written_8a) and no_create_8a and still_pending_8a \
        and (trades_1 == 1) and (trades_2 == 1) and no_create_8b \
        and (r1[0] is True) and (r2[0] is True)
    report("T8 已平仓待结算：不复活订单 + 不重复结算（dedup 单次记账）", ok,
           f"8a 补证失败→ PnL已写={pnl_written_8a}（须 False）, 建单={fake.exchange.create_order.call_count}, "
           f"仍待结算={still_pending_8a}; 8b 首次={r1}, trades={trades_1}; "
           f"重复={r2}, trades={trades_2}（两次都须为 1）, 建单={fake2.exchange.create_order.call_count}")


def _read_trades(path):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            return len((json.load(f) or {}).get("trades") or [])
    except Exception:
        return -1


# ===========================================================================
# 七复审 R1–R6 闭环验收（T9–T14）
# ===========================================================================
def _disk_fake(base_states, **kw):
    """独立快照替身：每次 load 都反序列化出新对象，persist 先做序列化校验。

    真实 JSON 读取**不共享字典** —— R4「用补证前旧快照结算」这类缺陷只有在
    这种替身下才会暴露（共享字典的替身会把补证结果自动带进旧引用）。
    `json.dumps` 同时把 MagicMock 等脏值挡在落盘之外，让落盘失败可见。
    """
    disk = json.loads(json.dumps(base_states))
    fake = _make_fake(base_states, **kw)

    def _load():
        return json.loads(json.dumps(disk))

    fake.load_all_states = _load
    fake._load_all_states_ex = lambda: (_load(), False, "")

    def _persist(all_states, **_k):
        try:
            payload = json.dumps(all_states)
        except Exception as e:
            print(f"⚠️ 保存状态文件失败: {e}")
            return False
        disk.clear()
        disk.update(json.loads(payload))
        return True

    fake._persist_states = _persist

    def _save(s, b, d, **_k):
        fake.saved.append((s, b, dict(d)))
        if s in disk and b in disk[s] and isinstance(d, dict):
            try:
                disk[s][b].update(json.loads(json.dumps(d)))
            except Exception as e:
                print(f"⚠️ 保存状态文件失败: {e}")
                return False
        return True

    fake.save_batch_state = _save
    fake._disk = disk

    # 墓碑同样按「真实 JSON 往返」处理：clear 的前置硬门是 _persist_tombstones
    # 返回 True（未绑 → 自动 mock → 恒 False → 清理永远失败，假红）。
    tomb = {"entries": {}}

    def _load_tomb():
        return json.loads(json.dumps(tomb["entries"]))

    def _persist_tomb(t):
        try:
            payload = json.dumps(t)
        except Exception as e:
            print(f"⚠️ 保存墓碑失败: {e}")
            return False
        tomb["entries"] = json.loads(payload)
        return True

    fake._load_tombstones = _load_tomb
    fake._persist_tombstones = _persist_tomb
    fake._tombstones = tomb
    return fake


def _capture_stdout(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


def _pending_ev():
    return {"0": {"status": "cost_pending", "qty": PLANNED, "planned": PLANNED,
                  "side": "buy", "order_id": ENTRY_ID,
                  "why": "cost_unavailable（测试）", "next_probe_at": 0}}


def _settle_batch(**over):
    """已进入「成本待补证结算」的批次（平仓单已成交、成本待补证）。"""
    b = dict(
        close_phase=2, close_reason="cost_pending_settling",
        last_filled_count=1, filled_details=[0.0], total_entry_fee=0.0,
        cost_pending_layers=[0], fill_evidence=_pending_ev(),
        cost_pending_settle_payload={
            "mode": "市价平仓", "exit_price": 85528.7, "exit_qty": PLANNED,
            "side": "BUY", "exit_fee": 0.0, "exit_fee_source": "estimated",
            "exit_order_id": "EXIT_1", "close_op_id": "op1",
            "dedup_key": f"{SYMBOL}:{BATCH}:op1:costpending",
            "created_at": time.time()})
    b.update(over)
    return _states(**b)


def _wire_fee_endpoints(fake, ctr):
    """费用解析端点计数桩：algo 映射 1 次 + 出场 userTrades 1 次 / 每次解析。"""
    fake.exchange.fetch_open_orders = lambda symbol: []
    fake.exchange.fetch_order = _with_order(_closed_order())

    def _algo(*a, **k):
        ctr["algo"] += 1
        return {"actualOrderId": ""}

    def _trades(*a, **k):
        ctr["trades"] += 1
        return []

    fake.exchange.fapiPrivateGetAlgoOrder = _algo
    fake.exchange.fetch_my_trades = _trades
    return ctr


def _wire_counted(fake, ctr, entry_order=None, algo_qty=True):
    """**全端点**计数桩（请求预算实测用）：每个端点先计数再返回确定值。

    `_wire_fee_endpoints` 只数 algo/userTrades；八复审要的是**总预算**，
    因此 fetch_order / fetch_open_orders 也一并入账。"""
    eo = dict(entry_order or _closed_order())

    def _count(name, fn):
        def _w(*a, **k):
            ctr[name] += 1
            return fn(*a, **k)
        return _w

    fake.exchange.fetch_open_orders = _count("open_orders", lambda symbol: [])
    fake.exchange.fetch_order = _count(
        "fetch_order", lambda oid, symbol, **kw: dict(eo))
    fake.exchange.fapiPrivateGetAlgoOrder = _count(
        "algo", lambda *a, **k: ({"actualOrderId": "REAL_E1",
                                  "actualQty": str(PLANNED)}
                                 if algo_qty else {"actualOrderId": ""}))
    fake.exchange.fetch_my_trades = _count(
        "trades", lambda *a, **k: [
            {"amount": PLANNED,
             "info": {"qty": str(PLANNED), "commission": _fee_of(REAL_PRICE),
                      "commissionAsset": "USDT"}}])
    return ctr


# ---------------------------------------------------------------------------
# T9（R1）：前缀门放行 ⇒ 数量守卫/覆盖勘察也必须放行（安全退出真能下单）
# ---------------------------------------------------------------------------
def t9_safe_exit_guard_consistency():
    def _guard(b, ledger=PLANNED):
        f = _make_fake(b, position_amt=PLANNED)
        f._read_position_amt = lambda *a, **k: PLANNED
        amt, _det = f._close_amount_guard(SYMBOL, "BUY", False, ledger, BATCH)
        return f, amt

    # 用例 1：数量已核实、成本待补证 → 两门同判据，守卫必须给出可下单数量
    st1 = _states(cost_pending_layers=[0], fill_evidence=_pending_ev(),
                  last_filled_count=1, filled_details=[0.0])
    f1 = _make_fake(st1, position_amt=PLANNED)
    f1._read_position_amt = lambda *a, **k: PLANNED
    prefix_ok, _cv, _w1 = f1._derive_close_txn_vars(st1[SYMBOL][BATCH], BATCH)
    amt1, det1 = f1._close_amount_guard(SYMBOL, "BUY", False, PLANNED, BATCH)
    ok_allow = bool(prefix_ok) and amt1 is not None and abs(amt1 - PLANNED) < 1e-9

    # 反例 A：无任何证据的 0 → 两门都拒绝（守卫不弱化）
    stA = _states(cost_pending_layers=[0], fill_evidence={},
                  last_filled_count=1, filled_details=[0.0])
    _fA, amtA = _guard(stA)

    # 反例 B：数量证据与计划量不符 → 拒绝（只放行「缺成本」这一种缺口）
    stB = _states(cost_pending_layers=[0],
                  fill_evidence={"0": {"status": "cost_pending", "qty": 0.30,
                                       "planned": PLANNED, "side": "buy",
                                       "order_id": ENTRY_ID}},
                  last_filled_count=1, filled_details=[0.0])
    _fB, amtB = _guard(stB)

    ok = ok_allow and (amtA is None) and (amtB is None)
    report("T9(R1) 安全退出两门判据一致：prefix 放行 ⇒ 数量守卫放行，其余拒绝保留",
           ok,
           f"放行: prefix={bool(prefix_ok)} guard={amt1!r}（{det1}）; "
           f"无证据0→{amtA!r}; 数量不符→{amtB!r}")


# ---------------------------------------------------------------------------
# T10（R2 + 八复审收口）：保护**真实创建 + 真实确认**（不是「调用过 helper」）
# ---------------------------------------------------------------------------
def _t10_confirmed_sl_keys(fake, st):
    """「已创建并确认」的成功判据（八复审 T10 收口）。

    必须同时满足：state=**CONFIRMED**（verify 真的成功）、订单身份 == create
    返回的 id（不是期望值）、intent 在册且 qty == 已核实成交量。
    PENDING_CREATE / PENDING_VERIFY（**确认未知**）一律不算通过 —— 旧判据把
    它们算进 `verified`，于是「强制 verify 返回 unknown」也能假绿。"""
    b = st[SYMBOL][BATCH]
    hits = []
    for k, v in (b.get("protection_registry") or {}).items():
        if not (isinstance(v, dict) and v.get("role") == "SL"):
            continue
        if str(v.get("state") or "") != "CONFIRMED":
            continue
        if str(v.get("order_id") or "") != "SL_REAL_1":
            continue
        it = v.get("intent")
        if not (isinstance(it, dict)
                and abs(float(it.get("qty") or 0) - PLANNED) < 1e-9):
            continue
        hits.append(k)
    return hits


def t10_real_protection_create():
    # ---- 场景 A：verify 成功 → 必须真的 CONFIRMED（身份 + 数量一致）----
    st = _states()
    fake = _make_fake(st)
    fake.exchange.fetch_open_orders = lambda symbol: []
    order = _closed_order()
    order["info"] = {"orderId": ENTRY_ID, "actualQty": "0.43",
                     "cumQuote": "0", "executedQty": "0.43"}   # 无真实成交均价
    fake.exchange.fetch_order = _with_order(order)
    fake.exchange.create_order = mock.MagicMock(return_value={"id": "SL_REAL_1"})
    out = _capture_stdout(_drive, fake, st[SYMBOL][BATCH], 5)

    b = st[SYMBOL][BATCH]
    sl_entries = [(k, v) for k, v in (b.get("protection_registry") or {}).items()
                  if isinstance(v, dict) and v.get("role") == "SL"]
    creates = [c for c in fake.exchange.create_order.call_args_list
               if c.kwargs.get("type") == "STOP_MARKET"
               or (len(c.args) >= 2 and c.args[1] == "STOP_MARKET")]
    amt_ok = any(abs(float(c.kwargs.get("amount") or 0) - PLANNED) < 1e-9
                 for c in creates)
    no_g1 = ("[G1]" not in out) and ("已阻止创建首次止损单" not in out)
    confirmed_keys = _t10_confirmed_sl_keys(fake, st)
    ok_a = bool(creates) and amt_ok and no_g1 and bool(confirmed_keys)

    # ---- 场景 B（八复审反例）：强制 verify 返回 unknown → 必须判 FAIL ----
    st2 = _states()
    fake2 = _make_fake(st2)
    fake2.exchange.fetch_open_orders = lambda symbol: []
    entry_only = _closed_order()
    entry_only["info"] = {"orderId": ENTRY_ID, "actualQty": "0.43",
                          "cumQuote": "0", "executedQty": "0.43"}

    def _fetch_order_unknown(oid, symbol, **kw):
        if str(oid) != ENTRY_ID:          # 保护单 verify 回查 → 网络异常
            raise Exception("network（verify=unknown）")
        return dict(entry_only)           # 入场层补证照常

    fake2.exchange.fetch_order = _fetch_order_unknown
    fake2.exchange.create_order = mock.MagicMock(return_value={"id": "SL_REAL_1"})
    _capture_stdout(_drive, fake2, st2[SYMBOL][BATCH], 5)
    creates2 = [c for c in fake2.exchange.create_order.call_args_list
                if c.kwargs.get("type") == "STOP_MARKET"
                or (len(c.args) >= 2 and c.args[1] == "STOP_MARKET")]
    states2 = [str(v.get("state"))
               for _k, v in (st2[SYMBOL][BATCH].get("protection_registry") or {}).items()
               if isinstance(v, dict) and v.get("role") == "SL"]
    confirmed2 = _t10_confirmed_sl_keys(fake2, st2)
    # 下了单、但确认结果未知 → 不是 CONFIRMED、更不能通过「已创建并确认」
    ok_b = bool(creates2) and bool(states2) \
        and all(s != "CONFIRMED" for s in states2) and not confirmed2

    ok = ok_a and ok_b
    report("T10(R2) 成本待补证时保护单真实创建并**真实确认**（确认未知必须判 FAIL）",
           ok,
           f"A: create(STOP_MARKET)={len(creates)} 覆盖量匹配={amt_ok} "
           f"G1拦截={not no_g1} registry={[(k, v.get('state')) for k, v in sl_entries]} "
           f"CONFIRMED+身份+数量={confirmed_keys} → {ok_a}; "
           f"B: create={len(creates2)} states={states2} CONFIRMED={confirmed2} "
           f"（须为空） → {ok_b}")


# ---------------------------------------------------------------------------
# T11（R2）：数量异常时**既有有效保护不被撤销**
# ---------------------------------------------------------------------------
def t11_existing_protection_kept():
    st = _states(last_filled_count=0,
                 current_sl_id="SL_OLD_9",
                 protection_registry={
                     f"{BATCH}|SL|L0|SHORT": {
                         "role": "SL", "state": "CONFIRMED",
                         "order_id": "SL_OLD_9", "id_known": True, "layer": 0}})
    fake = _make_fake(st)
    fake.exchange.fetch_open_orders = lambda symbol: []
    bad = _closed_order(info={"orderId": ENTRY_ID, "actualPrice": "85506.50000",
                              "actualQty": "0.30", "executedQty": "0.30"})
    fake.exchange.fetch_order = _with_order(bad)
    fake.exchange.cancel_order = mock.MagicMock(return_value={"id": "SL_OLD_9"})
    _capture_stdout(_drive, fake, st[SYMBOL][BATCH], 4)

    b = st[SYMBOL][BATCH]
    entry = (b.get("protection_registry") or {}).get(f"{BATCH}|SL|L0|SHORT") or {}
    cancelled = [str(c.args[0]) for c in fake.exchange.cancel_order.call_args_list
                 if c.args]
    ok = ("SL_OLD_9" not in cancelled
          and entry.get("state") == "CONFIRMED"
          and str(entry.get("order_id")) == "SL_OLD_9"
          and b.get("qty_reconcile_pending") == [0])
    report("T11(R2) 数量证据异常 → 既有有效保护未撤销、未被换挂",
           ok,
           f"撤单调用={cancelled or '无'}; registry={entry.get('state')}/"
           f"{entry.get('order_id')}; qty_reconcile={b.get('qty_reconcile_pending')!r}")


# ---------------------------------------------------------------------------
# T12（R3）：费用端点请求**有界**（连续清理失败 / PnL 写盘失败 / 重启重入）
# ---------------------------------------------------------------------------
def t12_fee_api_budget():
    tmp = tempfile.mkdtemp(prefix="f2f3_r3_")
    stats = os.path.join(tmp, "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl

    def _pnl_ok(self, *a, **k):
        k["stats_file"] = stats
        return real_pnl(self, *a, **k)

    # ---- 场景 1：首次结算 → 后续多轮清理失败 + 重启，均不得新增请求 ----
    ctr = {"algo": 0, "trades": 0}
    st = _settle_batch()
    fake = _disk_fake(st, position_amt=0.0)
    _wire_fee_endpoints(fake, ctr)
    fake._record_realized_pnl = types.MethodType(_pnl_ok, fake)
    r1 = fake._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c_after_first = dict(ctr)
    r2 = fake._finalize_cost_pending_settlement(SYMBOL, BATCH)
    r3 = fake._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c_after_rounds = dict(ctr)
    # 重启：另一替身读同一份「磁盘」
    fake_rs = _disk_fake(fake._disk, position_amt=0.0)
    _wire_fee_endpoints(fake_rs, ctr)
    fake_rs._record_realized_pnl = types.MethodType(_pnl_ok, fake_rs)
    r4 = fake_rs._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c_after_restart = dict(ctr)
    trades_n = _read_trades(stats)
    disk_pl = ((fake._disk.get(SYMBOL) or {}).get(BATCH)
               or {}).get("cost_pending_settle_payload") or {}

    first_parsed = (c_after_first["algo"] >= 1 and c_after_first["trades"] >= 1)
    s1_ok = (r1[0] is True) and first_parsed \
        and (c_after_rounds == c_after_first) \
        and (c_after_restart == c_after_first) \
        and (trades_n == 1) and disk_pl.get("pnl_recorded") is True \
        and isinstance(disk_pl.get("fees_resolved"), dict)

    # ---- 场景 2：PnL 写盘失败 → 费用结果复用，重试不新增请求 ----
    ctr2 = {"algo": 0, "trades": 0}
    st2 = _settle_batch()
    fake2 = _disk_fake(st2, position_amt=0.0)
    _wire_fee_endpoints(fake2, ctr2)
    fake2._record_realized_pnl = lambda *a, **k: False     # 写盘失败
    q1 = fake2._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c2_first = dict(ctr2)
    q2 = fake2._finalize_cost_pending_settlement(SYMBOL, BATCH)
    q3 = fake2._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c2_after = dict(ctr2)
    s2_ok = (q1[0] is False) and (c2_first["algo"] >= 1) \
        and (c2_after == c2_first) and (q2[0] is False) and (q3[0] is False)

    ok = s1_ok and s2_ok
    report("T12(R3) 费用端点请求有界（清理多轮/重启/PnL写盘失败均不重查）", ok,
           f"场景1: 首次={c_after_first} 后续={c_after_rounds} 重启={c_after_restart} "
           f"trades={trades_n} pnl_recorded={disk_pl.get('pnl_recorded')} → {s1_ok}; "
           f"场景2: 首次={c2_first} 重试={c2_after} 结果={q1[0]}/{q2[0]}/{q3[0]} → {s2_ok}")


# ---------------------------------------------------------------------------
# T13（R4）：补证后**重读账本**结算 —— 金额逐项核对（禁 0 成本首记）
# ---------------------------------------------------------------------------
def t13_fresh_snapshot_amounts():
    tmp = tempfile.mkdtemp(prefix="f2f3_r4_")
    stats = os.path.join(tmp, "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl
    captured = []

    def _spy(self, *a, **k):
        captured.append(a)
        k["stats_file"] = stats
        return real_pnl(self, *a, **k)

    proof = {"batch_id": BATCH, "symbol": SYMBOL, "scope": "FULL",
             "position_zero": True, "state_ids_resolved": True,
             "exchange_scan": "zero", "l1_canceled": [], "l2_canceled": []}

    st = _settle_batch()
    fake = _disk_fake(st, position_amt=0.0)          # 真实零仓位 + 每次读取独立快照
    fake.exchange.fetch_order = _with_order(_closed_order())   # 补证 → 85506.5
    fake.exchange.fapiPrivateGetAlgoOrder = lambda *a, **k: {"actualOrderId": ""}
    fake.exchange.fetch_my_trades = lambda *a, **k: []
    fake._record_realized_pnl = types.MethodType(_spy, fake)
    fake._converge_batch_orders_before_clear = lambda *a, **k: dict(proof)

    r = fake._finalize_cost_pending_settlement(SYMBOL, BATCH)

    exp_avg = REAL_PRICE
    exp_gross = (85528.7 - REAL_PRICE) * PLANNED
    exp_fee = REAL_PRICE * PLANNED * TAKER_FEE_RATE
    exp_net = exp_gross - exp_fee
    if captured:
        a = captured[0]
        got = {"batch": a[0], "symbol": a[1], "side": a[2], "qty": a[3],
               "avg": a[4], "exit_px": a[5], "net": a[6]}
        amt_ok = (got["batch"] == BATCH and got["symbol"] == SYMBOL
                  and got["side"] == "BUY"
                  and abs(got["qty"] - PLANNED) < 1e-12
                  and abs(got["avg"] - exp_avg) < 1e-9
                  and abs(got["exit_px"] - 85528.7) < 1e-9
                  and abs(got["net"] - exp_net) < 1e-6)
    else:
        got, amt_ok = {}, False
    cleared = BATCH not in (fake._disk.get(SYMBOL) or {})
    ok = (r[0] is True) and (len(captured) == 1) and amt_ok and cleared
    report("T13(R4) 补证后重读账本结算：均价/数量/退出价/净额逐项核对 + 收敛清理", ok,
           f"结果={r}; 记账次数={len(captured)}; got={got}; "
           f"期望 avg={exp_avg} net≈{exp_net:.7f}; 已清理={cleared}")


# ---------------------------------------------------------------------------
# T14（R5）：归零分支与 finally 清理都不得绕过成本待补证
# ---------------------------------------------------------------------------
def t14_no_bypass_pending_settlement():
    # A：finally 授权必须是 skip（不许 converge+clear 掉缺成本的账本）
    stA = _settle_batch()
    fakeA = _disk_fake(stA)
    dec, snap = fakeA._finally_cleanup_decision(SYMBOL, BATCH)
    okA = (dec == "skip")

    # B：持仓归零 + 成本补证不到 → 不清账、不记 PnL、不建单
    stB = _settle_batch()
    fakeB = _disk_fake(stB, position_amt=0.0)
    fakeB.exchange.fetch_open_orders = lambda symbol: []
    fakeB.exchange.fetch_order = lambda *a, **k: (_ for _ in ()).throw(
        Exception("network"))
    fakeB.exchange.create_order = mock.MagicMock(
        side_effect=AssertionError("成本待补证期间禁止建单"))
    pnl_calls = []
    fakeB._record_realized_pnl = lambda *a, **k: (pnl_calls.append(a), True)[1]
    out = _capture_stdout(_drive, fakeB, stB[SYMBOL][BATCH], 3)
    bB = fakeB._disk.get(SYMBOL, {}).get(BATCH) or {}
    okB = (bB.get("is_active") is True
           and bB.get("close_reason") == "cost_pending_settling"
           and bool(bB.get("cost_pending_settle_payload"))
           and len(pnl_calls) == 0
           and fakeB.exchange.create_order.call_count == 0
           and BATCH in fakeB._disk.get(SYMBOL, {}))
    ok = okA and okB
    report("T14(R5) 归零分支/finally 均尊重成本待补证（不清账、不记账、不建单）", ok,
           f"finally授权={dec!r}(须 skip) → {okA}; 归零轮: 活跃={bB.get('is_active')} "
           f"reason={bB.get('close_reason')!r} PnL调用={len(pnl_calls)} "
           f"建单={fakeB.exchange.create_order.call_count} → {okB}")
    if "F2/F3-R5" not in out and "cost_pending" not in out:
        print("        （提示：归零分支未打印转交日志，需人工确认分支命中）")


# ---------------------------------------------------------------------------
# T15（R6）：补证发现数量/身份不符 → **原子升级** qty_reconcile_pending
# ---------------------------------------------------------------------------
def t15_backfill_escalates_qty():
    """缺口：旧实现只更新 ent['qty'] 并留在 cost_pending（只加退避），系统仍按
    旧数量继续推进。升级必须同时改状态、派生两个待办列表、发 critical 告警，
    并让结算门与平仓前缀门都继续拦截；既有保护不得被撤/建。"""
    st = _states(cost_pending_layers=[0], fill_evidence=_pending_ev(),
                 last_filled_count=1, filled_details=[0.0])
    fake = _make_fake(st)
    bad = _closed_order(info={"orderId": ENTRY_ID, "actualPrice": "85506.50000",
                              "actualQty": "0.30", "executedQty": "0.30"})
    fake.exchange.fetch_order = _with_order(bad)
    n_ok = fake._backfill_entry_costs(SYMBOL, BATCH)
    b = st[SYMBOL][BATCH]
    ev = (b.get("fill_evidence") or {}).get("0") or {}

    ok1 = (b.get("qty_reconcile_pending") == [0]
           and b.get("cost_pending_layers") == []
           and ev.get("status") == "qty_unverified"
           and abs(float(ev.get("qty") or 0) - 0.30) < 1e-12)
    ok2 = any(lvl == "critical" for lvl, _t in fake.sent)

    gate_ok, gate_why = fake._settlement_cost_gate(SYMBOL, BATCH)
    prefix_ok, _cv, _w = fake._derive_close_txn_vars(b, BATCH)
    ok3 = (gate_ok is False) and (prefix_ok is False) \
        and "qty_reconcile" in str(gate_why)

    cancels = [str(c.args[0]) for c in fake.exchange.cancel_order.call_args_list
               if c.args]
    ok4 = (not cancels) and fake.exchange.create_order.call_count == 0

    ok = ok1 and ok2 and ok3 and ok4
    report("T15(R6) 补证发现数量不符 → 原子升级 qty_reconcile_pending + 结算门/前缀门双拦",
           ok,
           f"resolved={n_ok}（须0）; 该层: status={ev.get('status')!r} "
           f"qty={ev.get('qty')!r} cp={b.get('cost_pending_layers')!r} "
           f"qr={b.get('qty_reconcile_pending')!r}; critical告警={ok2}; "
           f"结算门={gate_ok}({gate_why}); 前缀门={prefix_ok}; "
           f"撤单={cancels or '无'} 建单={fake.exchange.create_order.call_count}")


# ===========================================================================
# 八复审退回项：P1-1 / P1-2 / 请求预算 / 身份证据（T16–T19）
# ===========================================================================
def t16_market_close_uses_fresh_ledger():
    """P1-1：市价平仓必须在**成本门之后**才计算成本/费用/盈亏。

    旧实现：均价与费用取 BEGIN 的 claimed 快照（补证前 0 成本），门内刚补上的
    真实成交价被丢弃 → 账本落 avg=0、净额 +36758.95 的错首记（且被 dedup
    永久锁死；同口径应 ≈ -27.23）。这里**实际驱动 close_position_market**
    （不是直接调结算函数）：门内补证 85506.5 → 记账必须 avg=85506.5、
    净额逐项核对，且不得出现 cost_pending_settling（门已通过）。"""
    tmp = tempfile.mkdtemp(prefix="f2f3_p1a_")
    stats = os.path.join(tmp, "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl
    captured = []

    def _spy(self, *a, **k):
        captured.append(a)
        k["stats_file"] = stats
        return real_pnl(self, *a, **k)

    st = _states(close_phase=0, last_filled_count=1, filled_details=[0.0],
                 total_entry_fee=0.0, cost_pending_layers=[0],
                 fill_evidence=_pending_ev())
    fake = _disk_fake(st, position_amt=PLANNED)
    # @staticmethod 不吃 REAL_HELPERS 的「传 self」绑定形态（同 _fee_float），
    # 漏绑 → 自动 mock → `_fee_note_line, _pnl_label = mock` 解包 TypeError。
    fake._pnl_display_label = CryptoTrader._pnl_display_label
    fake._is_pnl_authoritative = CryptoTrader._is_pnl_authoritative
    fake._read_position_amt = lambda *a, **k: PLANNED
    fake._record_realized_pnl = types.MethodType(_spy, fake)

    entry_order = _closed_order()              # 真实价 85506.5 → 门内补证
    close_order = {"id": "CLOSE_1", "status": "closed", "side": "sell",
                   "symbol": SYMBOL, "average": 85528.7, "price": 0,
                   "filled": PLANNED,
                   "info": {"orderId": "CLOSE_1", "executedQty": "0.43",
                            "cumQuote": str(85528.7 * PLANNED)}}
    fake.exchange.fetch_ticker = lambda s, **k: {"last": 85528.7, "close": 85528.7}
    fake.exchange.fetch_open_orders = lambda s: []
    fake.exchange.fetch_order = lambda oid, s, **kw: dict(
        close_order if str(oid) == "CLOSE_1" else entry_order)
    fake.exchange.create_order = mock.MagicMock(return_value={"id": "CLOSE_1"})

    entry_fee, exit_fee = _fee_of(REAL_PRICE), _fee_of(85528.7)
    fake.exchange.fapiPrivateGetAlgoOrder = lambda *a, **k: {
        "actualOrderId": "REAL_E1", "actualQty": str(PLANNED)}

    def _trades(s, limit=None, since=None, params=None, **kw):
        fee = entry_fee if (params or {}).get("orderId") == "REAL_E1" else exit_fee
        return [{"amount": PLANNED,
                 "info": {"qty": str(PLANNED), "commission": fee,
                          "commissionAsset": "USDT"}}]

    fake.exchange.fetch_my_trades = _trades

    ret, msg = CryptoTrader.close_position_market(fake, BATCH)

    exp_avg = REAL_PRICE
    exp_gross = (85528.7 - REAL_PRICE) * PLANNED
    exp_net = exp_gross - (entry_fee + exit_fee)
    if captured:
        a = captured[0]
        got = {"batch": a[0], "symbol": a[1], "side": a[2], "qty": a[3],
               "avg": a[4], "exit_px": a[5], "net": a[6]}
        amt_ok = (got["batch"] == BATCH and got["symbol"] == SYMBOL
                  and got["side"] == "BUY"
                  and abs(got["qty"] - PLANNED) < 1e-12
                  and abs(got["avg"] - exp_avg) < 1e-9
                  and abs(got["exit_px"] - 85528.7) < 1e-9
                  and abs(got["net"] - exp_net) < 1e-6)
    else:
        got, amt_ok = {}, False
    b_now = fake._disk.get(SYMBOL, {}).get(BATCH) or {}
    fd = b_now.get("filled_details") or []
    ledger_ok = (fd and fd[0] == REAL_PRICE) and b_now.get("cost_pending_layers") == []
    no_defer = b_now.get("close_reason") != "cost_pending_settling"
    zero_cost_rejected = bool(got) and got.get("avg") != 0     # 基准会记 0
    ok = (ret is True) and (len(captured) == 1) and amt_ok and no_defer \
        and ledger_ok and zero_cost_rejected
    report("T16(P1-1) 市价平仓：门后重读并核验事务账本再算成本/费用/盈亏", ok,
           f"ret={ret}({str(msg)[:90]}); 记账次数={len(captured)} got={got}; "
           f"期望 avg={exp_avg} net≈{exp_net:.7f}; 补证后 ledger price={fd} "
           f"cp={b_now.get('cost_pending_layers')} reason={b_now.get('close_reason')!r}")


# ---------------------------------------------------------------------------
# T17（P1-2）：待结算状态一次落盘 + 清理链不得只凭原因字符串放行
# ---------------------------------------------------------------------------
def t17_defer_atomic_and_cleanup_guard():
    proof = {"batch_id": BATCH, "symbol": SYMBOL, "scope": "FULL",
             "position_zero": True, "state_ids_resolved": True,
             "exchange_scan": "zero", "l1_canceled": [], "l2_canceled": []}
    kw = dict(mode="市价平仓", exit_price=85528.7, exit_qty=PLANNED, side="BUY",
              fees={"exit_fee": 1.0, "exit_fee_source": "actual"},
              exit_order_id="CLOSE_1")

    # A：载荷 + 阶段 + 原因必须**一次落盘**（写失败 = 整体未写，无半写态）
    stA = _states(close_phase=1, close_op_id="op1", close_reason="market_confirming")
    fakeA = _disk_fake(stA)
    real_persist = fakeA._persist_states
    calls = {"n": 0}

    def _fail_first(all_states, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real_persist(all_states, **_k)

    fakeA._persist_states = _fail_first
    rA = fakeA._defer_settlement_for_cost(SYMBOL, BATCH, "op1", **kw)
    bA = fakeA._disk.get(SYMBOL, {}).get(BATCH) or {}
    okA = (rA is False) and calls["n"] == 1 \
        and not bA.get("cost_pending_settle_payload") \
        and bA.get("close_reason") == "market_confirming" \
        and int(bA.get("close_phase") or 0) == 1

    # B：close_op 已迁移 → 拒绝覆盖新一代事务（代际隔离）
    stB = _states(close_phase=1, close_op_id="op1", close_reason="market_confirming")
    fakeB = _disk_fake(stB)
    rB = fakeB._defer_settlement_for_cost(SYMBOL, BATCH, "op99", **kw)
    bB = fakeB._disk.get(SYMBOL, {}).get(BATCH) or {}
    okB = (rB is False) and not bB.get("cost_pending_settle_payload") \
        and bB.get("close_op_id") == "op1"

    # C：「载荷在册 + 原因串没写上」的中间态（第二次落盘失败的现场）→
    #    finally 不得授权、真实 clear 必须拒删（无墓碑 + 告警指名载荷）、PnL 0 次
    stC = _settle_batch()
    stC[SYMBOL][BATCH]["close_reason"] = "market_confirming"
    fakeC = _disk_fake(stC, position_amt=0.0)
    alerts = []
    fakeC._converge_alert = lambda key, text, level=None: alerts.append(str(text))
    pnlC = []
    fakeC._record_realized_pnl = lambda *a, **k: (pnlC.append(a), True)[1]
    dec, _snap = fakeC._finally_cleanup_decision(SYMBOL, BATCH)
    cleared = fakeC.clear_batch_state(SYMBOL, BATCH, proof=dict(proof))
    still = BATCH in (fakeC._disk.get(SYMBOL) or {})
    no_tomb = not fakeC._tombstones["entries"]
    named = any("待结算载荷" in t for t in alerts)
    okC = (dec == "skip") and (cleared is False) and still and no_tomb \
        and named and (len(pnlC) == 0)

    # D：拒绝是**可恢复**的 —— 原因串复位后 finalizer 正常记账并收敛
    stD = _settle_batch()
    fakeD = _disk_fake(stD, position_amt=0.0)
    statsD = os.path.join(tempfile.mkdtemp(prefix="f2f3_p1b_"), "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl

    def _pnlD(self, *a, **k):
        k["stats_file"] = statsD
        return real_pnl(self, *a, **k)

    fakeD._record_realized_pnl = types.MethodType(_pnlD, fakeD)
    fakeD.exchange.fetch_order = _with_order(_closed_order())
    fakeD.exchange.fapiPrivateGetAlgoOrder = lambda *a, **k: {"actualOrderId": ""}
    fakeD.exchange.fetch_my_trades = lambda *a, **k: []
    fakeD._converge_batch_orders_before_clear = lambda *a, **k: dict(proof)
    rD = fakeD._finalize_cost_pending_settlement(SYMBOL, BATCH)
    okD = (rD[0] is True) and (_read_trades(statsD) == 1) \
        and BATCH not in (fakeD._disk.get(SYMBOL) or {})

    ok = okA and okB and okC and okD
    report("T17(P1-2) 待结算一次落盘 + 载荷在册时清理链拒绝凭原因串放行", ok,
           f"A 一次写={calls['n']}(须=1) 半写态无={okA}; B op迁移拒绝={okB}; "
           f"C finally={dec!r}(须skip) clear={cleared}(须False) 仍在={still} "
           f"墓碑={bool(no_tomb)} 告警指名载荷={named} PnL={len(pnlC)} → {okC}; "
           f"D 复位后收敛={rD[0]} PnL={_read_trades(statsD)} → {okD}")


# ---------------------------------------------------------------------------
# T18（请求预算）：端点调用有界实测 + 费用缓存落盘失败的显式退避
# ---------------------------------------------------------------------------
def t18_request_budget_and_backoff():
    # A：首轮成功结算的**总**端点预算（fetch_order / algo / userTrades 全计数）
    ctrA = {"fetch_order": 0, "algo": 0, "trades": 0, "open_orders": 0}
    stA = _settle_batch()
    fakeA = _disk_fake(stA, position_amt=0.0)
    _wire_counted(fakeA, ctrA)
    statsA = os.path.join(tempfile.mkdtemp(prefix="f2f3_budget_"), "trade_stats.json")
    real_pnl = CryptoTrader._record_realized_pnl

    def _pnlA(self, *a, **k):
        k["stats_file"] = statsA
        return real_pnl(self, *a, **k)

    fakeA._record_realized_pnl = types.MethodType(_pnlA, fakeA)
    rA = fakeA._finalize_cost_pending_settlement(SYMBOL, BATCH)
    totalA = sum(ctrA.values())
    # 构成（**实测**，含 ChatGPT 点名的四类）：
    #   补证 fetch_order 1 + 入场映射 1（algo）+ 成交明细 2（入场/出场 userTrades）
    #   + 出场数量兜底 fetch_order 1 = 5
    #   （数量兜底：finalizer 侧无 order_snapshot → 必须按单取权威成交量）
    #   单层端到端 = 3–4 次（兜底触发即 4）；每处 `_safe_api_call(retries=2)`
    #   只约束**那一次**调用的尝试次数，不是整轮预算。
    BUDGET = 5
    okA = (rA[0] is True) and (totalA == BUDGET) and (_read_trades(statsA) == 1)

    # B：费用缓存**落盘失败** → 显式退避（退避期零新请求、不记账、不清账）
    ctrB = {"fetch_order": 0, "algo": 0, "trades": 0, "open_orders": 0}
    stB = _settle_batch()
    fakeB = _disk_fake(stB, position_amt=0.0)
    _wire_counted(fakeB, ctrB)
    statsB = os.path.join(tempfile.mkdtemp(prefix="f2f3_budget2_"), "trade_stats.json")

    def _pnlB(self, *a, **k):
        k["stats_file"] = statsB
        return real_pnl(self, *a, **k)

    fakeB._record_realized_pnl = types.MethodType(_pnlB, fakeB)
    realB = fakeB._persist_states

    def _fail_fee_cache(all_states, **_k):
        b = (all_states.get(SYMBOL) or {}).get(BATCH) or {}
        pl = b.get("cost_pending_settle_payload") or {}
        if "fees_resolved" in pl and not pl.get("pnl_recorded"):
            return False                      # 费用缓存写盘失败
        return realB(all_states, **_k)

    fakeB._persist_states = _fail_fee_cache
    q1 = fakeB._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c1 = dict(ctrB)
    q2 = fakeB._finalize_cost_pending_settlement(SYMBOL, BATCH)     # 退避窗口内
    c2 = dict(ctrB)
    fakeB._fee_resolve_backoff_until = 0.0                           # 窗口过期
    q3 = fakeB._finalize_cost_pending_settlement(SYMBOL, BATCH)
    c3 = dict(ctrB)
    total3 = sum(c3.values())
    delay_grew = (fakeB._fee_resolve_backoff_until - time.time()) > 50
    stillB = BATCH in (fakeB._disk.get(SYMBOL) or {})
    # 退避期零新请求；窗口过期后才允许下一轮，且总调用 ≤ 2×单轮预算
    okB = (q1[0] is False) and ("fee_cache_persist_failed" in str(q1[1])) \
        and (q2[0] is False) and ("fee_resolve_backoff" in str(q2[1])) \
        and (c2 == c1) \
        and (q3[0] is False) and (total3 <= 2 * BUDGET) and delay_grew \
        and (_read_trades(statsB) == 0) and stillB                  # 不记账不清账

    ok = okA and okB
    report("T18(预算) 端点调用实测有界 + 费用缓存落盘失败走显式退避", ok,
           f"A 首轮总调用={totalA}(须={BUDGET}) 分项={dict(ctrA)} 成功={rA[0]} → {okA}; "
           f"B r1={q1[1][:46]} {dict(c1)}; r2={q2[1][:40]} {dict(c2)}(须与r1同); "
           f"r3总调用={total3}(≤{2 * BUDGET}) 退避={delay_grew} PnL="
           f"{_read_trades(statsB)} 仍在={stillB} → {okB}")


# ---------------------------------------------------------------------------
# T19（身份证据）：缺 id / 缺 side 不得 confirmed，登记须标「期望≠观测」
# ---------------------------------------------------------------------------
def t19_identity_evidence_required():
    fake = _make_fake(_states())
    info_full = {"orderId": ENTRY_ID, "actualQty": "0.43",
                 "actualPrice": "85506.50000", "executedQty": "0.43"}

    # A：响应缺订单身份（顶层 id 与 info.orderId 都没有）→ 不得 confirmed
    a = _closed_order()
    a["id"] = None
    a["info"] = dict(info_full)
    a["info"].pop("orderId")
    rA = fake._resolve_entry_fill_evidence(a, PLANNED, expected_side="buy",
                                           expected_order_id=ENTRY_ID)

    # B：响应缺方向（顶层 side 与 info.side 都没有）→ 不得 confirmed
    b = _closed_order()
    b["side"] = ""
    b["info"] = dict(info_full)
    b["info"].pop("side", None)
    rB = fake._resolve_entry_fill_evidence(b, PLANNED, expected_side="buy",
                                           expected_order_id=ENTRY_ID)

    # C：顶层缺 id 但**规范原始字段** info.orderId 在 → 观测证据成立，放行
    c = _closed_order()
    c["id"] = None
    c["info"] = dict(info_full)
    rC = fake._resolve_entry_fill_evidence(c, PLANNED, expected_side="buy",
                                           expected_order_id=ENTRY_ID)

    # D：身份不符（既有判据保留）
    d = _closed_order(id="OTHER")
    rD = fake._resolve_entry_fill_evidence(d, PLANNED, expected_side="buy",
                                           expected_order_id=ENTRY_ID)

    # E：登记证据必须标注 order_id 来源 = **期望值**（不是观测结果）
    stE = _states(cost_pending_layers=[0], fill_evidence={},
                  last_filled_count=1, filled_details=[0.0])
    fE = _make_fake(stE)
    fE._record_fill_evidence(SYMBOL, BATCH, 0, ENTRY_ID,
                             {"status": "qty_unverified",
                              "why": "identity_evidence_missing（测试）",
                              "qty": PLANNED, "side": "buy"})
    evE = (stE[SYMBOL][BATCH].get("fill_evidence") or {}).get("0") or {}

    # F：整轮驱动（响应缺 id）→ 进入既有数量核对，不入价、不挂保护
    stF = _states()
    fF = _make_fake(stF)
    fF.exchange.fetch_open_orders = lambda symbol: []
    bad = _closed_order()
    bad["id"] = None
    bad["info"] = dict(info_full)
    bad["info"].pop("orderId")
    fF.exchange.fetch_order = _with_order(bad)
    _capture_stdout(_drive, fF, stF[SYMBOL][BATCH], 4)
    bF = stF[SYMBOL][BATCH]

    okA = (rA[0] == "qty_unverified" and "identity_evidence_missing" in str(rA[3]))
    okB = (rB[0] == "qty_unverified" and "side_evidence_missing" in str(rB[3]))
    okC = (rC[0] in ("confirmed", "cost_pending"))
    okD = (rD[0] == "qty_unverified" and "order_identity_mismatch" in str(rD[3]))
    okE = (evE.get("order_id_source") == "expected"
           and evE.get("identity_observed") is False
           and evE.get("status") == "qty_unverified")
    okF = (bF.get("qty_reconcile_pending") == [0]
           and bF.get("filled_details") == [0.0]
           and not fF.sl_place_calls)
    ok = okA and okB and okC and okD and okE and okF
    report("T19(身份) 缺 id/side 不得 confirmed，且期望值不得冒充观测证据", ok,
           f"A缺id={rA[0]}/{str(rA[3])[:44]}→{okA}; B缺side={rB[0]}/"
           f"{str(rB[3])[:34]}→{okB}; C原始字段info.orderId={rC[0]}→{okC}; "
           f"D身份不符={rD[0]}→{okD}; E登记来源={evE.get('order_id_source')}/"
           f"observed={evE.get('identity_observed')}→{okE}; "
           f"F整轮: qr={bF.get('qty_reconcile_pending')} fd={bF.get('filled_details')} "
           f"挂保护={len(fF.sl_place_calls)}→{okF}")


# ===========================================================================
def main():
    trader_260725.STATE_FILE = os.path.join(
        tempfile.mkdtemp(prefix="f2f3_state_"), "trade_state.json")
    # 离线环境没有 health control.json → current_instance_id() 返回 None →
    # _health_instance 为假 → 监控线程的 M3 落盘（trader L11012：filled_details /
    # total_entry_fee / last_filled_count）被跳过。生产进程持有该文件，这里补齐
    # 这一**环境前提**（不是放宽被测逻辑）。
    _real_ciid = trader_260725.current_instance_id
    trader_260725.current_instance_id = lambda: "offline-test-instance"

    def _run(fn):
        try:
            fn()
        except Exception as e:
            report(f"{fn.__name__} 执行异常（基准代码跑此分支 = 预期 RED）", False,
                   f"{type(e).__name__}: {e}")

    try:
        _run(t1_real_fill_price)
        _run(t2_missing_price)
        _run(t3_illegal_price)
        _run(t4_qty_mismatch)
        _run(t5_protection_and_safe_exit)
        _run(t6_backfill_idempotent)
        _run(t7_restart_no_double)
        _run(t8_closed_pending_settlement)
        _run(t9_safe_exit_guard_consistency)
        _run(t10_real_protection_create)
        _run(t11_existing_protection_kept)
        _run(t12_fee_api_budget)
        _run(t13_fresh_snapshot_amounts)
        _run(t14_no_bypass_pending_settlement)
        _run(t15_backfill_escalates_qty)
        _run(t16_market_close_uses_fresh_ledger)
        _run(t17_defer_atomic_and_cleanup_guard)
        _run(t18_request_budget_and_backoff)
        _run(t19_identity_evidence_required)
    finally:
        trader_260725.STATE_FILE = _real_state_file
        trader_260725.current_instance_id = _real_ciid
    passed = sum(1 for _, p in RESULTS if p)
    print(f"\n===== F2/F3 针对性验收: {passed}/{len(RESULTS)} PASS =====")
    if passed != len(RESULTS):
        print("🔴 RED：候选补丁未通过针对性验收（或基准代码跑出的 RED 未转 GREEN）")
        return 1
    print("🟢 GREEN：F2/F3 候选补丁针对性验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
