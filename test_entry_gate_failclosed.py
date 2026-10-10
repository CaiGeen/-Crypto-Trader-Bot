#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_entry_gate_failclosed.py — 现役引擎 ENTRY gate 的 Fail-Closed 负向回归。

被测对象：`trader_260725.py` 的 `CryptoTrader._cancel_and_verify_entry_orders`
与 `_verify_entry_order_terminal`（直接绑定真实实现），不是 `送审附件_v6.x`
里的提议 helper 或文档代码块。

背景（R6 测试交付收口）：
历史 `ENTRY or []` 缺陷会把交易快照 None / 非 list 退化成空列表 → 假确认放行。
现役引擎已改为 `remaining is None or not isinstance(remaining, list)` → Fail-Closed。
但此前该判据的负向断言只存在于 `test_close_confirmation_v6/v62.py`，而其被测对象
是仓内提议 helper 与文档 AFTER 块，并非现役引擎。本文件把同一行为**锁定在现役
引擎上**，作为这条候选路径的负向回归（防止 `or []` 之类的假确认回潮）。

判据：
  - 快照 None / dict / 字符串 / 查询异常 → gate 返回 False（干净 Fail-Closed，
    不得依赖异常兜底，否则与真实拦截无法区分）
  - 快照 [] 但逐 ID 仍未终结（open / filled / OrderNotFound）→ False
  - 快照列表仍含残留 ENTRY → False
  - 快照 [] 且逐 ID 已 canceled（gone）→ True（防止把 gate 写成恒 False）

跑法：`.venv\\Scripts\\python.exe test_entry_gate_failclosed.py`（rc=0 即全过）
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccxt  # noqa: E402
import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SYM = 'BTC/USDT:USDT'
BID = 'batch_entry_gate_001'


class _SnapshotError(Exception):
    """模拟 fetch_open_orders 抛出非 OrderNotFound 的查询异常。"""


def _fake_self(open_orders, order_result):
    """构造只含 ENTRY gate 依赖的最小 self（零 config、零网络、零锁）。"""
    t = CryptoTrader.__new__(CryptoTrader)
    t.tg = []

    def _fetch_open_orders(symbol, params=None):
        if isinstance(open_orders, Exception):
            raise open_orders
        return open_orders

    def _fetch_order(order_id, symbol, params=None, retries=None):
        if isinstance(order_result, Exception):
            raise order_result
        return order_result

    def _cancel_order(order_id, symbol, params=None):
        return {'id': order_id, 'status': 'canceled'}

    t.exchange = types.SimpleNamespace(
        fetch_open_orders=_fetch_open_orders,
        fetch_order=_fetch_order,
        cancel_order=_cancel_order,
    )
    # 真实 _safe_api_call 消费 retries/delay；本桩仅转发端点调用，gate 语义不变。
    t._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    t.send_tg_notification = (
        lambda text, reply_markup=None, level='info': t.tg.append((level, text)))
    return t


def _b_data():
    # len(target_amounts) == len(entry_orders) != last_filled_count
    # → 走非截断分支，只剩 E2 未成交（无需 _pending_entry_ids_for_gate）。
    return {'entry_orders': ['E1', 'E2'], 'target_amounts': [0.001, 0.001],
            'last_filled_count': 1, 'protection_registry': {}}


def _run(open_orders, order_result):
    t = _fake_self(open_orders, order_result)
    try:
        ok = t._cancel_and_verify_entry_orders(SYM, BID, _b_data(), 1)
    except BaseException as e:  # noqa: BLE001 - 目的：把崩溃暴露为可断言的非 bool
        return f'<EXC {type(e).__name__}: {e}>', t
    return ok, t


def main():
    canceled = {'id': 'E2', 'status': 'canceled', 'filled': 0.0}
    cases = []

    # ── 快照不可判定 → 干净 Fail-Closed（不得依赖异常兜底）──
    for tag, oo in (('None', None), ('dict', {}), ('字符串', 'oops'),
                    ('查询异常', _SnapshotError('boom'))):
        ok, t = _run(oo, canceled)
        cases.append((f'S1 快照 {tag} → False（Fail-Closed）', ok, False, t))

    # ── 合法空列表 + 逐 ID 已终结 → True（防恒 False）──
    ok, t = _run([], canceled)
    cases.append(('S2 快照 [] + 逐ID canceled → True', ok, True, t))

    # ── 逐 ID 未终结 → False ──
    ok, t = _run([], {'id': 'E2', 'status': 'open', 'filled': 0.0})
    cases.append(('S3 逐ID open → False', ok, False, t))
    ok, t = _run([], {'id': 'E2', 'status': 'closed', 'filled': 0.001})
    cases.append(('S4 逐ID filled → False（仓位已变化）', ok, False, t))
    ok, t = _run([], ccxt.OrderNotFound('nf'))
    cases.append(('S5 逐ID OrderNotFound → False（UNKNOWN≠EMPTY）', ok, False, t))

    # ── 快照列表仍含残留 ENTRY → False ──
    ok, t = _run([{'id': 'E2', 'status': 'open'}], canceled)
    cases.append(('S6 快照仍含残留 ENTRY → False', ok, False, t))

    print('=' * 68)
    fails = 0
    for name, got, want, _t in cases:
        if not isinstance(got, bool):      # 异常兜底 ≠ 干净拦截
            fails += 1
            print(f'  ❌ {name} | 异常兜底: {got!r}')
            continue
        mark = '✅' if got is want else '❌'
        if got is not want:
            fails += 1
        print(f'  {mark} {name}')

    # 关键告警：快照不可判定必须产生 critical 告警
    _ok, t_none = _run(None, canceled)
    crit = any(level == 'critical' for level, _ in t_none.tg)
    if not crit:
        fails += 1
    print(f"  {'✅' if crit else '❌'} A1 快照 None → critical 告警"
          f"（levels={[l for l, _ in t_none.tg]}）")
    print('=' * 68)

    total = len(cases) + 1
    if fails:
        print(f'🚨 {fails}/{total} 项失败')
        return 1
    print(f'✅ {total}/{total} 全通过（含 critical 告警断言）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
