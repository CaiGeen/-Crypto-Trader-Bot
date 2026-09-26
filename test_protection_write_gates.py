#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""保护单写盘门禁的离线注入测试（第二十轮）。

## 背景

已登记缺陷（一项根因、两处门禁）：**保护单路径的「Intent Before Side Effect」
不变量只有注释、没有代码强制**，而入场路径有（L6012 `save_batch_state(...) is not True`
→ Fail-Closed、零 `create_order`）。

- **T3 / L9677**：首次挂止损前先 `_update_registry(state='PENDING_CREATE', intent=...)`，
  但 `_update_registry` 内部 L6621 **不检查** `_persist_states()` 返回值，调用方也不检查
  → 意图写盘失败时**仍然 `create_order`**，磁盘上没有可靠锚点。
- **T4 / L6395**：`_commit_protection_with_g3` 在持锁段内直写 `entry['state']='CONFIRMED'`
  后 `self._persist_states(all_states)`（**不检查返回**）→ 写盘失败仍 `return 'committed'`。

## 本文件的性质：**characterization（表征）测试，不是 RED**

> ⚠️ **本文件全绿 = 缺陷被成功复现，不代表保护链通过验收。**
> 门禁日志里 `test_protection_write_gates.py PASS rc=0` 这一行**不可**读成
> 「止损写盘安全已验证」——恰恰相反，它钉住的是当前两处门禁缺失的行为。
> 真正的验收要等定点修复落地、并把本文件改写成检验期望行为的负测之后。

断言的是**代码当前实际行为**，因此全绿。这是刻意选择：根目录 `test_*.py` 非 0 退出会被
`run_test_gate.py` 记为 `FAIL` / `BASELINE-FAIL`，留一批永久红测试只会污染门禁语义。
修法设计时（前置问题见送审文档：运行期如何禁止重建 + 重启后如何对账）再补 RED 负测。

因此每条断言都写成「当前行为 = X（缺陷）」并在名称里标注，修复后这些断言会失败，
提醒同步更新——这是有意的变更探测器，不是回归。

## 注入方式

`_persist_states` 的写盘链是 `NamedTemporaryFile → flush → os.fsync → os.replace`（L2519-2524），
任何异常 → 打印并 `return False`（L2527-2529）。故 patch `os.replace` 抛错即可**确定性**注入
写盘失败，无需真的损坏磁盘。`STATE_FILE` 全程重定向到临时目录，**不触碰生产 `trade_state.json`**。

## 脚手架来源

helper 绑定清单沿用 `test_b1_state_machine.py` 的 `_bind_helpers`（含它记录的
「MagicMock 未绑定 → 自动 mock 返回值 → 路径被 except 吞」陷阱），但**必须绑真实
`_persist_states` / `_update_registry` / `_state_lock`**——现有 fake 都把持久化桩掉了
（`test_b1_state_machine.py:74`「states 为共享引用，无需落盘」），那样注入不到写盘失败。

零网络：`_safe_api_call` 透传到 MagicMock 交易所；不连 Telegram、不连交易所。

跑法：`.venv\\Scripts\\python.exe test_protection_write_gates.py`（rc=0 即全过）
"""

import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_wgate_001"
IDENT_SL = f"{BATCH}|SL|L0|LONG"

RESULTS = []
_real_state_file = trader_260725.STATE_FILE


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

def _fresh_state_file():
    """把 STATE_FILE 重定向到全新临时目录，返回 (dir, path)。"""
    d = tempfile.mkdtemp(prefix="wgate_")
    path = os.path.join(d, "trade_state.json")
    trader_260725.STATE_FILE = path
    return d, path


def _restore_state_file():
    trader_260725.STATE_FILE = _real_state_file


def _seed_batch(state_path, **over):
    b = {
        "is_active": True,
        "batch_id": BATCH,
        "symbol": SYMBOL,
        "side": "BUY",
        "entry_orders": ["e1"],
        "stop_steps": [55000.0],
        "take_profit_price": 60000.0,
        "current_sl_id": None,
        "tp_order_id": None,
        "close_phase": 0,
        "pending_sl_orders": [],
        "protection_registry": {},
    }
    b.update(over)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({SYMBOL: {BATCH: b}}, f, ensure_ascii=False, indent=2)
    return b


def _read_disk(state_path):
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def _make_fake(state_path, states):
    """MagicMock 假实例 + 真实持久化/锁/registry 语义。"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_order.return_value = {"id": "sl_ex_1", "status": "NEW"}
    ex.create_order.return_value = {"id": "sl_ex_1"}
    ex.fetch_open_orders.return_value = [{"id": "sl_ex_1", "status": "NEW"}]
    fake.exchange = ex

    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get("level", "info"), str(text)))

    # ⚠️ MagicMock 陷阱（见 test_b1_state_machine.py:48）：不显式置 0 会被自动 mock 吞掉
    fake._api_cooldown_until = 0
    fake._process_start_ts = time.time()
    fake._state_corrupted = False
    fake._state_corruption_detail = ""
    fake._defer_state_corrupt_alert = False

    # 真实锁 + 真实落盘（本文件与既有 fake 的唯一区别）
    fake._state_lock = threading.Lock()
    fake.load_all_states = lambda: states
    fake._persist_states = lambda all_s: CryptoTrader._persist_states(fake, all_s)
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)

    for name in ("_protection_identity", "_build_intent", "_order_matches_intent",
                 "_assert_create_allowed", "_final_pre_create_check",
                 "_commit_protection_with_g3", "_g3a_converge_race_order",
                 "_verify_order_created", "_classify_create_exception",
                 "_verify_and_update_registry", "_recheck_registry_self_heal",
                 "_is_stale_pre_launch_entry"):
        if hasattr(CryptoTrader, name):
            setattr(fake, name, (lambda n=name: lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k))())
    return fake


def _layer_sl_params(amount=0.43, stop=55000.0):
    return [{
        "symbol": SYMBOL,
        "type": "STOP_MARKET",
        "side": "sell",
        "amount": amount,
        "params": {"stopPrice": stop, "reduceOnly": True, "workingType": "MARK_PRICE"},
    }]


def _tp_params():
    return {"symbol": SYMBOL, "type": "TAKE_PROFIT_MARKET", "side": "sell",
            "params": {"stopPrice": 60000.0, "reduceOnly": True}}


def _params_base():
    return {"leverage": 100}


# --------------------------------------------------------------------------
# T3：意图写盘失败（L9677）→ 是否仍 create_order / 磁盘是否留下锚点
# --------------------------------------------------------------------------

def check_t3_intent_write_failure_still_creates_order():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)

        with mock.patch("os.replace", side_effect=OSError(13, "Permission denied")):
            CryptoTrader._place_prepared_orders_immediately(
                fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
                False, _params_base(), [55000.0])

        created = fake.exchange.create_order.called
        disk = _read_disk(state_path)
        reg = disk.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        anchored = any(v.get("state") == "PENDING_CREATE" for v in reg.values())

        # 当前实际行为 = 缺陷：意图没落盘，交易所照收单
        report(
            "T3a 意图写盘失败时仍 create_order（当前行为=缺陷）",
            created is True,
            f"create_order 被调用={created}；磁盘 PENDING_CREATE 锚点存在={anchored}。"
            f"期望（修复后）= 不调用，且必须先有锚点")

        report(
            "T3b 意图写盘失败 → 磁盘无可靠锚点（当前行为=缺陷）",
            anchored is False,
            f"protection_registry={ {k: v.get('state') for k, v in reg.items()} }；"
            f"无锚点即『交易所有单、本地无账本』窗口（L6010 注释要防的正是这个）")

        report(
            "T3c 该分支未发出 critical 告警（仅限本路径，不等于全系统无告警）",
            not any(lvl == "critical" for lvl, _ in fake.sent),
            f"send_tg_notification 记录={fake.sent}（期望修复后有资金安全 critical）。"
            f"⚠️ 口径：本项只覆盖**该保护单创建路径**；未驱动新开仓信号验证风险闸门、"
            f"未覆盖独立巡检（健康巡检.py），**全系统告警结论未验证**")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# T3b：重启后能否识别交易所已存在的止损单
# --------------------------------------------------------------------------

def check_t3_restart_cannot_reconcile_orphan_sl():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        with mock.patch("os.replace", side_effect=OSError(13, "Permission denied")):
            CryptoTrader._place_prepared_orders_immediately(
                fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
                False, _params_base(), [55000.0])

        # 模拟重启：新实例读同一份磁盘（磁盘上没有 PENDING_CREATE / CONFIRMED），
        # 交易所上那张单存在。
        states2 = _read_disk(state_path)
        fake2 = _make_fake(state_path, states2)
        reg2 = states2.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        try:
            CryptoTrader._recheck_registry_self_heal(fake2, SYMBOL, BATCH)
            healed = True
        except Exception as e:
            healed = False
            heal_err = f"{type(e).__name__}: {e}"
        after = _read_disk(state_path)
        reg_after = after.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})

        report(
            "T3d 重启后自愈：磁盘无锚点 → 无法收编交易所已存在的 SL（当前行为）",
            len(reg_after) == len(reg2),
            f"自愈前 registry 条目={len(reg2)}，自愈后={len(reg_after)}，"
            f"调用未抛异常={healed}。交易所侧 fetch_order 已被调用="
            f"{fake2.exchange.fetch_order.call_count} 次。"
            f"结论：没有 identity/intent 锚点，重启后**无机制**把这张单收编回账本")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# T4：确认写盘失败（L6395）→ 仍返回 committed？
# --------------------------------------------------------------------------

def check_t4_confirm_write_failure_still_returns_committed():
    d, state_path = _fresh_state_file()
    try:
        # 隔离 T4：意图先成功落盘，只让**确认**那一次写盘失败
        _seed_batch(state_path, protection_registry={
            IDENT_SL: {"state": "PENDING_CREATE", "id_known": False,
                       "order_kind": "conditional", "role": "SL", "layer": 0,
                       "side": "LONG", "intent": None}})
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)

        with mock.patch("os.replace", side_effect=OSError(13, "Permission denied")):
            ret = CryptoTrader._commit_protection_with_g3(
                fake, SYMBOL, BATCH, IDENT_SL, "sl_ex_1", order_kind="conditional")

        disk = _read_disk(state_path)
        on_disk = disk.get(SYMBOL, {}).get(BATCH, {}).get(
            "protection_registry", {}).get(IDENT_SL, {}).get("state")

        # 当前实际行为 = 缺陷：写盘失败但返回 committed，且磁盘仍是 PENDING_CREATE
        report(
            "T4a 确认写盘失败仍返回 committed（当前行为=缺陷）",
            ret == "committed",
            f"返回={ret!r}（期望修复后= 能区分『交易所已存在/账本未确认』）；"
            f"磁盘实际状态={on_disk!r}")

        report(
            "T4b 确认写盘失败 → 磁盘无 CONFIRMED（交易所已有单、账本无记录）",
            on_disk != "CONFIRMED",
            f"磁盘 state={on_disk!r}；交易所侧订单 id=sl_ex_1 已存在 → "
            f"『交易所有单、本地无账本』窗口")

        report(
            "T4c 该确认路径未发出 critical 告警（仅限本路径，不等于全系统无告警）",
            not any(lvl == "critical" for lvl, _ in fake.sent),
            f"send_tg_notification 记录={fake.sent}。⚠️ 口径同 T3c：只覆盖该确认路径，"
            f"全系统告警结论未验证")
    finally:
        _restore_state_file()


# --------------------------------------------------------------------------
# 对照组：写盘正常时，两处门禁都应正常落盘（证明注入本身有效）
# --------------------------------------------------------------------------

def check_control_persist_works_without_injection():
    d, state_path = _fresh_state_file()
    try:
        _seed_batch(state_path)
        states = _read_disk(state_path)
        fake = _make_fake(state_path, states)
        CryptoTrader._place_prepared_orders_immediately(
            fake, SYMBOL, BATCH, 0, 0.43, _tp_params(), _layer_sl_params(),
            False, _params_base(), [55000.0])
        disk = _read_disk(state_path)
        reg = disk.get(SYMBOL, {}).get(BATCH, {}).get("protection_registry", {})
        states_seen = [v.get("state") for v in reg.values()]
        # 严格钉住「PENDING_CREATE → CONFIRMED」完整链：registry 每个 identity 只有一条、
        # 原地覆盖（L6619 只刷新 updated_at），所以成功链的**终态**必须是 CONFIRMED。
        # 早期版本写成 `PENDING_CREATE or CONFIRMED`，那太松——停在 PENDING_CREATE
        # 也算过，而那正是 T4 注入后的状态。
        report(
            "C1 对照组：无注入时 PENDING_CREATE → CONFIRMED 完整链落盘（证明注入有效）",
            states_seen == ["CONFIRMED"],
            f"registry 各条 state={states_seen}（期望恰为 ['CONFIRMED']：意图已推进到确认），"
            f"create_order 调用={fake.exchange.create_order.call_count} 次")
    finally:
        _restore_state_file()


CHECKS = [
    check_control_persist_works_without_injection,
    check_t3_intent_write_failure_still_creates_order,
    check_t3_restart_cannot_reconcile_orphan_sl,
    check_t4_confirm_write_failure_still_returns_committed,
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
    print(f"保护单写盘门禁注入测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if failed == 0 and ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
