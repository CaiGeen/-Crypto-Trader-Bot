# -*- coding: utf-8 -*-
"""
B2-0：Verify 阶段 OrderNotFound 语义统一（操作阶段区分）—— TDD 测试（红阶段）

背景（ChatGPT 评审①）：verify 一个"刚刚 create 的订单"时 OrderNotFound ≠ create 阶段
ExchangeError。可能为：真不存在 / algo 端点查询延迟 / 路由参数错误 / 交易所暂时不可见 /
订单已状态变化。因此 verify 阶段 not_found 必须按 NOT_CONFIRMED 处理（不 Commit、不计数、
不自动重挂），不得沿用 create 阶段 ExchangeError→FAILED→可重试 的语义。

B1 已在预生成 SL/TP 实现该语义；本批把 _start_monitoring 补挂 SL / 降级恢复 / 补挂 TP
三处残留的 not_found→raise→计数→重试 统一收编（C5 事故模式结构性根除）。

TDD：本文件先红（统一入口 helper 未实现 + 3 处残留未改）→ 实施 B2-0 后全绿。
"""
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b2_001"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_base_fake():
    """通用 fake：真实 registry/verify/classify helper 绑定，交易所调用透传"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_order.return_value = {'id': 'o1', 'status': 'NEW'}
    ex.create_order.return_value = {'id': 'o_new'}
    fake.exchange = ex

    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    return fake


def _bind_helpers(fake, states):
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._verify_order_created = lambda oid, sym, kind='conditional': CryptoTrader._verify_order_created(fake, oid, sym, kind)
    fake._classify_create_exception = lambda e: CryptoTrader._classify_create_exception(fake, e)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    # B2-0 新增统一入口（红灯阶段未实现 → 跳过绑定，调用走 MagicMock 自动 mock 恒 FAIL）
    if hasattr(CryptoTrader, '_verify_and_update_registry'):
        fake._verify_and_update_registry = lambda s, b, i, oid, **kw: CryptoTrader._verify_and_update_registry(
            fake, s, b, i, oid, **kw)
    else:
        fake._verify_and_update_registry = lambda *a, **k: mock.MagicMock()
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': None,
        'tp_order_id': 'tp_1',
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'pending_sl_orders': [],
        'protection_registry': {},
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


# =====================================================================
# T1-T3：_verify_and_update_registry 统一入口三态迁移
#   success    → registry CONFIRMED（可 Commit）
#   not_found  → registry NOT_CONFIRMED（不 Commit、不计数、不自动重挂）  ← ChatGPT①核心
#   unknown    → registry PENDING_VERIFY（结果未知，不计数不补单）
# =====================================================================
def t_verify_and_update_registry():
    # T1: success
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = {'id': 'o1', 'status': 'NEW'}
    ident = "batch_b2_001|SL|L0|LONG"
    res = fake._verify_and_update_registry(SYMBOL, BATCH, ident, 'o1', desc='补挂止损单')
    entry = states[SYMBOL][BATCH]['protection_registry'].get(ident, {})
    report("T1/success→CONFIRMED", res == 'success' and entry.get('state') == 'CONFIRMED',
           f"(返回={res!r}, registry.state={entry.get('state')!r} → 必须 CONFIRMED 才可 Commit)")

    # T2: not_found（真实 ccxt.OrderNotFound）→ NOT_CONFIRMED，不得 raise
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    res = fake._verify_and_update_registry(SYMBOL, BATCH, ident, 'o1', desc='补挂止损单')
    entry = states[SYMBOL][BATCH]['protection_registry'].get(ident, {})
    report("T2/not_found→NOT_CONFIRMED不raise", res == 'not_found' and entry.get('state') == 'NOT_CONFIRMED',
           f"(返回={res!r}, registry.state={entry.get('state')!r} → 必须 NOT_CONFIRMED：禁计数禁自动重挂)")

    # T3: unknown（真实 ccxt.NetworkError）→ PENDING_VERIFY（id_known=True，create 已返回 id）
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.side_effect = ccxt.NetworkError('connection reset')
    res = fake._verify_and_update_registry(SYMBOL, BATCH, ident, 'o1', desc='补挂止损单')
    entry = states[SYMBOL][BATCH]['protection_registry'].get(ident, {})
    report("T3/unknown→PENDING_VERIFY", res == 'unknown' and entry.get('state') == 'PENDING_VERIFY',
           f"(返回={res!r}, registry.state={entry.get('state')!r} → 必须 PENDING_VERIFY：UNKNOWN≠EMPTY)")


# =====================================================================
# T4-T6：源码断言 —— 残留模式根除（C5 事故模式结构性清零）
# =====================================================================
def t_source_asserts():
    src = open('trader_260725.py', encoding='utf-8').read()
    lines = src.splitlines()

    # T4: 全文件不得存在 "验证失败: OrderNotFound" 的 raise（not_found→raise→计数 残留）
    bad = src.count('验证失败: OrderNotFound')
    report("T4/无not_found→raise残留", bad == 0,
           f"(现存 {bad} 处 → 必须 0：verify 阶段 OrderNotFound 禁 raise)")

    # T5: create 异常必须经 _classify_create_exception 分流（补挂段新增 1 处）
    n_classify = src.count('_classify_create_exception(e)')
    report("T5/create异常按classify分流", n_classify >= 4,
           f"(现存 {n_classify} 处 → 必须 >=4：B1 预生成 3 处 + B2-0 补挂段 1 处)")

    # T6: _start_monitoring 内 verify 必须经统一入口（补挂 SL + 降级恢复 + 补挂 TP 至少 3 处）
    start = src.index('    def _start_monitoring')
    end = src.index('    def _place_prepared_orders_immediately')
    seg = src[start:end]
    n_uni = seg.count('_verify_and_update_registry(')
    report("T6/补挂段接入统一入口", n_uni >= 3,
           f"(现存 {n_uni} 处 → 必须 >=3：补挂SL + 降级恢复 + 补挂TP)")


# =====================================================================
def main():
    t_verify_and_update_registry()
    t_source_asserts()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\nB2-0 verify 语义统一：{passed}/{total} 通过\n{'#' * 60}")
    if passed == total:
        print("⚠️ 红灯阶段提示：若本文件先红（T1-T3 因 helper 未实现 FAIL、T4-T6 因残留 FAIL），"
              "红阶段成立 → 可进入 B2-0 实施；实施后须全绿。")


if __name__ == '__main__':
    main()
