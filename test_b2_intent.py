# -*- coding: utf-8 -*-
"""
B2-2/B2-1：registry 不可变 intent 指纹 + 自愈 FOUND+intent 完整匹配 —— TDD 测试（红阶段）

背景（ChatGPT 评审②③）：
② _recheck_registry_self_heal 当前 fetch 成功即 CONFIRMED+收编，未比对订单字段 → 错单/旧单
   可能被误收编成保护单。必须：FOUND ≠ CONFIRMED；FOUND + intent 完整匹配 = CONFIRMED。
③ _protection_identity（batch_id|role|L{layer}|side）只回答"是不是同一个逻辑订单"；
   intent 指纹（symbol/side/qty/stopPrice/orderType/reduceOnly）回答"这个逻辑订单具体要下什么"。
   identity 负责幂等仲裁，intent 负责防错收编；intent 落盘后不可变。

TDD：本文件先红（_build_intent/_order_matches_intent 未实现 + _update_registry 无 intent +
自愈未做匹配）→ 实施 B2-2/B2-1 后全绿。
"""
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b2_002"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_base_fake():
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex
    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    return fake


def _bind_helpers(fake, states):
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._recheck_registry_self_heal = lambda s, b: CryptoTrader._recheck_registry_self_heal(fake, s, b)
    if hasattr(CryptoTrader, '_build_intent'):
        fake._build_intent = lambda **kw: CryptoTrader._build_intent(fake, **kw)
    if hasattr(CryptoTrader, '_order_matches_intent'):
        fake._order_matches_intent = lambda o, i, s: CryptoTrader._order_matches_intent(fake, o, i, s)
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': None,
        'tp_order_id': None,
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'pending_sl_orders': [],
        'protection_registry': {},
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


def _intent_ok():
    return {
        'symbol': SYMBOL,
        'side': 'sell',
        'qty': 0.1,
        'order_type': 'STOP_MARKET',
        'stop_price': 55000.0,
        'reduce_only': True,
    }


def _fetched_ok():
    return {
        'id': 'o_sl_1',
        'symbol': SYMBOL,
        'side': 'sell',
        'type': 'STOP_MARKET',
        'stopPrice': 55000.0,
        'amount': 0.1,
        'reduceOnly': True,
        'info': {'stopPrice': '55000.0', 'reduceOnly': 'true'},
    }


# =====================================================================
# T1-T3：intent 落盘 + 不可变
# =====================================================================
def t_intent_registry():
    try:
        # T1: _update_registry 带 intent → entry['intent'] 落盘
        states = _state_batch()
        fake = _bind_helpers(_make_base_fake(), states)
        ident = f"{BATCH}|SL|L0|LONG"
        intent = _intent_ok()
        fake._update_registry(SYMBOL, BATCH, ident, state='PENDING_CREATE', intent=intent,
                              role='SL', layer=0, side='LONG')
        entry = states[SYMBOL][BATCH]['protection_registry'][ident]
        report("T1/intent落盘", entry.get('intent') == intent,
               f"(entry.intent={entry.get('intent')!r} → 必须完整落盘)")

        # T2: intent 不可变——再次写入不同 intent 不得覆盖
        fake._update_registry(SYMBOL, BATCH, ident, state='CONFIRMED', order_id='o_sl_1',
                              intent={'symbol': 'EVIL/SYMBOL'})
        entry_now = states[SYMBOL][BATCH]['protection_registry'][ident]
        report("T2/intent不可变", entry_now.get('intent') == intent,
               f"(二次写入后 intent={entry_now.get('intent')!r} → 必须保持首次值，防参数漂移)")
    except TypeError as e:
        report("T1/intent落盘", False, f"[TDD红] _update_registry 不支持 intent: {e}")
        report("T2/intent不可变", False, "[TDD红] 同 T1")


# =====================================================================
# T3-T6：_build_intent / _order_matches_intent
# =====================================================================
def t_build_and_match():
    fake = _bind_helpers(_make_base_fake(), _state_batch())

    # T3: _build_intent 生成完整字段
    if hasattr(CryptoTrader, '_build_intent'):
        intent = fake._build_intent(symbol=SYMBOL, side='sell', qty=0.1, order_type='STOP_MARKET',
                                    stop_price='55000.0', reduce_only=True)
        ok = (intent['symbol'] == SYMBOL and intent['side'] == 'sell'
              and intent['qty'] == 0.1 and intent['order_type'] == 'STOP_MARKET'
              and intent['stop_price'] == 55000.0 and intent['reduce_only'] is True)
        report("T3/build_intent完整", ok, f"(intent={intent!r})")
    else:
        report("T3/build_intent完整", False, "[TDD红] _build_intent 未实现")

    # T4: 全字段匹配 → True
    if hasattr(CryptoTrader, '_order_matches_intent'):
        ok = fake._order_matches_intent(_fetched_ok(), _intent_ok(), SYMBOL)
        report("T4/全匹配→True", ok is True, f"(返回={ok!r} → 必须 True)")
    else:
        report("T4/全匹配→True", False, "[TDD红] _order_matches_intent 未实现")

    # T5: stopPrice 不匹配 → False（错单防护核心）
    if hasattr(CryptoTrader, '_order_matches_intent'):
        bad = dict(_fetched_ok(), stopPrice=54000.0)
        bad['info'] = dict(bad['info'], stopPrice='54000.0')
        ok = fake._order_matches_intent(bad, _intent_ok(), SYMBOL)
        report("T5/stopPrice不匹配→False", ok is False, f"(返回={ok!r} → 必须 False：错单不得收编)")
    else:
        report("T5/stopPrice不匹配→False", False, "[TDD红] _order_matches_intent 未实现")

    # T6: side 不匹配 → False
    if hasattr(CryptoTrader, '_order_matches_intent'):
        bad = dict(_fetched_ok(), side='buy')
        ok = fake._order_matches_intent(bad, _intent_ok(), SYMBOL)
        report("T6/side不匹配→False", ok is False, f"(返回={ok!r} → 必须 False)")
    else:
        report("T6/side不匹配→False", False, "[TDD红] _order_matches_intent 未实现")


# =====================================================================
# T7-T9：自愈 FOUND+intent 匹配 = CONFIRMED；不匹配 → MISMATCH 不收编
# =====================================================================
def t_self_heal_match():
    ident = f"{BATCH}|SL|L0|LONG"
    intent = _intent_ok()

    # T7: FOUND + intent 匹配 → CONFIRMED + 收编 current_sl_id
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][ident] = {
        'state': 'PENDING_VERIFY', 'order_id': 'o_sl_1', 'id_known': True,
        'intent': intent, 'role': 'SL', 'updated_at': time.time(),
    }
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = _fetched_ok()
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    b = states[SYMBOL][BATCH]
    report("T7/匹配→CONFIRMED+收编", entry.get('state') == 'CONFIRMED' and b.get('current_sl_id') == 'o_sl_1',
           f"(state={entry.get('state')!r}, current_sl_id={b.get('current_sl_id')!r} → 必须 CONFIRMED+收编)")

    # T8: FOUND + intent 不匹配（stopPrice 不同）→ MISMATCH + 不收编 + critical 告警
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][ident] = {
        'state': 'PENDING_VERIFY', 'order_id': 'o_sl_1', 'id_known': True,
        'intent': intent, 'role': 'SL', 'updated_at': time.time(),
    }
    fake = _bind_helpers(_make_base_fake(), states)
    bad_order = dict(_fetched_ok(), stopPrice=54000.0)
    bad_order['info'] = dict(bad_order['info'], stopPrice='54000.0')
    fake.exchange.fetch_order.return_value = bad_order
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    b = states[SYMBOL][BATCH]
    has_critical = any(lv == 'critical' for lv, _ in fake.sent)
    report("T8/不匹配→MISMATCH不收编+告警",
           entry.get('state') == 'MISMATCH' and b.get('current_sl_id') is None and has_critical,
           f"(state={entry.get('state')!r}, current_sl_id={b.get('current_sl_id')!r}, critical={has_critical}"
           f" → 必须 MISMATCH+不收编+critical)")

    # T9: FOUND 但无 intent（旧条目）→ 保守维持原状态，不收编
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][ident] = {
        'state': 'PENDING_VERIFY', 'order_id': 'o_sl_1', 'id_known': True,
        'role': 'SL', 'updated_at': time.time(),
    }
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = _fetched_ok()
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    b = states[SYMBOL][BATCH]
    report("T9/无intent保守不收编", entry.get('state') == 'PENDING_VERIFY' and b.get('current_sl_id') is None,
           f"(state={entry.get('state')!r}, current_sl_id={b.get('current_sl_id')!r}"
           f" → 必须维持 PENDING_VERIFY 不收编：intent 缺失时宁可不收编)")

    # T10: NOT_FOUND（真实 ccxt.OrderNotFound）→ NOT_CONFIRMED 维持（既有 B1 语义不回退）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][ident] = {
        'state': 'PENDING_VERIFY', 'order_id': 'o_sl_1', 'id_known': True,
        'intent': intent, 'role': 'SL', 'updated_at': time.time(),
    }
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    report("T10/OrderNotFound→NOT_CONFIRMED维持", entry.get('state') == 'NOT_CONFIRMED',
           f"(state={entry.get('state')!r} → 必须 NOT_CONFIRMED：B1 语义不回退)")


# =====================================================================
def main():
    t_intent_registry()
    t_build_and_match()
    t_self_heal_match()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\nB2-2/B2-1 intent 落盘与自愈匹配：{passed}/{total} 通过\n{'#' * 60}")
    if passed == total:
        print("⚠️ 红灯阶段提示：若本文件先红（新 helper 未实现 / 自愈未做匹配）→ 红阶段成立，可进入实施；实施后须全绿。")


if __name__ == '__main__':
    main()
