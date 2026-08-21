# -*- coding: utf-8 -*-
"""
F1/F2/F3（2026-08-21 事件4：TP 死锁）专项测试

事故：首层成交 → prefill 挂出 TP/SL（registry CONFIRMED）→ 同 cycle 风险段
"先撤销再闸门检查（未传 replace_order_id）"→ CONFIRMED 拦截 → tp_order_id=None 落盘 →
R14 每轮补挂 → 闸门永久拦截（死锁，registry 永不终结）。

修复：
  F1 风险维护段——撤销前先过仲裁闸门（replace 语义），撤销确认/已不存在 → registry ABSENT；
     网络异常 fail-closed（不清 id、不创建）。SL/TP 对称。
  F2 监控循环 terminal 检测（canceled/expired）→ 同步写 registry ABSENT（含 terminated_reason）。
  F3 R14 补挂前 registry 实况裁决（_adjudicate_recreate_before_repair）：
     allow（已终结/无条目）/ adopt（在场匹配，防双挂）/ mismatch（在场不匹配，告警）/ hold（未知，保守）。

本文件覆盖：F3 helper 11 场景 + F1 闸门替换语义 + ABSENT 终结序列 + F2 源码断言。
运行：.venv/Scripts/python.exe test_tp_deadlock_fix.py（ccxt 只在项目 .venv）
"""
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_tp_deadlock"
RESULTS = []

# registry identity（与 _protection_identity 输出格式一致：batch|role|L层|side）
IDENT_TP = f"{BATCH}|TP|L0|LONG"
IDENT_SL = f"{BATCH}|SL|L0|LONG"


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


# ccxt 实盘归一化格式订单（对照 _order_matches_intent 的 F1 现实映射）
ORDER_OPEN = {
    'id': 'tp_1',
    'symbol': 'BTC/USDT:USDT',
    'side': 'sell',
    'type': 'market',
    'info': {'type': 'TAKE_PROFIT_MARKET', 'reduceOnly': 'true', 'stopPrice': '60000.00'},
    'status': 'new',
    'stopPrice': 60000.0,
    'amount': 0.003,
    'reduceOnly': True,
}
INTENT_TP = {
    'symbol': 'BTCUSDT',
    'side': 'sell',
    'order_type': 'TAKE_PROFIT_MARKET',
    'stop_price': 60000.0,
    'reduce_only': True,
    'qty': 0.003,
}


def _make_fake(states):
    """F3/F1 通用 fake：真实 helper 绑定 + 交易所调用透传（防 MagicMock 假路径）"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    # ⚠️ MagicMock 数值比较必炸教训：_assert_create_allowed 读取 _api_cooldown_until → 必须绑定真实数值
    fake._api_cooldown_until = 0
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex
    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._assert_create_allowed = lambda s, b, i, **kw: CryptoTrader._assert_create_allowed(fake, s, b, i, **kw)
    fake._order_matches_intent = lambda o, i, s: CryptoTrader._order_matches_intent(fake, o, i, s)
    fake._adjudicate_recreate_before_repair = lambda s, b, i: CryptoTrader._adjudicate_recreate_before_repair(fake, s, b, i)
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


def _reg_entry(state='CONFIRMED', order_id='tp_1', intent=INTENT_TP, **over):
    e = {'state': state, 'order_id': order_id, 'intent': intent, 'updated_at': time.time()}
    e.update(over)
    return e


# =====================================================================
# T1-T12：F3 _adjudicate_recreate_before_repair 裁决矩阵
# =====================================================================
def t_f3_adjudicate():
    # T1: 无条目 → allow（首次补挂）
    states = _state_batch()
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T1/无条目→allow", v == 'allow' and oid is None, f"(verdict={v!r})")

    # T2: ABSENT → allow
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='ABSENT')})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T2/ABSENT→allow", v == 'allow' and oid is None, f"(verdict={v!r})")

    # T3: FAILED → allow
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='FAILED')})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T3/FAILED→allow", v == 'allow' and oid is None, f"(verdict={v!r})")

    # T4: MISMATCH → mismatch（禁止自动处理）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='MISMATCH')})
    fake = _make_fake(states)
    fake.sent.clear()
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T4/MISMATCH→mismatch", v == 'mismatch' and oid is None, f"(verdict={v!r})")

    # T5: CONFIRMED + fetch status=canceled → allow + registry ABSENT + terminated_reason
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    fake.exchange.fetch_order.return_value = dict(ORDER_OPEN, status='canceled')
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    report("T5/CONFIRMED+已终结→ABSENT放行",
           v == 'allow' and entry.get('state') == 'ABSENT'
           and entry.get('terminated_reason') == 'f3_adjudicate_status_canceled',
           f"(verdict={v!r}, state={entry.get('state')!r}, reason={entry.get('terminated_reason')!r})")

    # T6: CONFIRMED + OrderNotFound → allow + ABSENT
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    report("T6/CONFIRMED+OrderNotFound→ABSENT放行",
           v == 'allow' and entry.get('state') == 'ABSENT'
           and entry.get('terminated_reason') == 'f3_adjudicate_order_not_found',
           f"(verdict={v!r}, state={entry.get('state')!r})")

    # T7: CONFIRMED + 在场 + intent 匹配 → adopt（收养防双挂）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    fake.exchange.fetch_order.return_value = dict(ORDER_OPEN)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T7/CONFIRMED+在场匹配→adopt", v == 'adopt' and oid == 'tp_1', f"(verdict={v!r}, oid={oid!r})")

    # T8: CONFIRMED + 在场 + intent 不匹配 → mismatch + critical 告警
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(intent=dict(INTENT_TP, stop_price=99999.0))})
    fake = _make_fake(states)
    fake.exchange.fetch_order.return_value = dict(ORDER_OPEN)
    fake.sent.clear()
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    critical = any(lv == 'critical' for lv, _ in fake.sent)
    report("T8/CONFIRMED+在场不匹配→mismatch+critical",
           v == 'mismatch' and oid is None and critical,
           f"(verdict={v!r}, critical_sent={critical})")

    # T9: CONFIRMED + NetworkError → hold（registry 保持 CONFIRMED，保守保留下轮）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    fake.exchange.fetch_order.side_effect = ccxt.NetworkError('connection reset')
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    report("T9/CONFIRMED+网络异常→hold", v == 'hold' and oid is None and entry.get('state') == 'CONFIRMED',
           f"(verdict={v!r}, state保持={entry.get('state')!r})")

    # T10: PENDING_VERIFY 无 order_id → hold
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='PENDING_VERIFY', order_id=None)})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T10/PENDING_VERIFY无id→hold", v == 'hold' and oid is None, f"(verdict={v!r})")

    # T11: PENDING_CREATE → hold（create 可能已发出，结果未知）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='PENDING_CREATE', order_id=None)})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    report("T11/PENDING_CREATE→hold", v == 'hold' and oid is None, f"(verdict={v!r})")

    # T12: PENDING_VERIFY + 在场匹配 → adopt + 状态升级 CONFIRMED（补 Commit 防双挂）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry(state='PENDING_VERIFY')})
    fake = _make_fake(states)
    fake.exchange.fetch_order.return_value = dict(ORDER_OPEN)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_TP)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    report("T12/PENDING_VERIFY+在场匹配→adopt+CONFIRMED",
           v == 'adopt' and oid == 'tp_1' and entry.get('state') == 'CONFIRMED',
           f"(verdict={v!r}, state={entry.get('state')!r})")


# =====================================================================
# T13-T15：F1 闸门 replace 语义 + ABSENT 终结序列（先撤后建适配仲裁）
# =====================================================================
def t_f1_gate_replace():
    # T13: CONFIRMED + replace_order_id 匹配 → 允许替换（撤销前的闸门前置检查）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, IDENT_TP,
                                                  desc='替换止盈单', replace_order_id='tp_1')
    report("T13/CONFIRMED+replace匹配→放行", allowed is True, f"(allowed={allowed!r}, reason={reason!r})")

    # T14: CONFIRMED + replace_order_id 不匹配 → 拒绝替换（批次级 id 与 registry 不一致 = 异常态）
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, IDENT_TP,
                                                  desc='替换止盈单', replace_order_id='tp_999')
    report("T14/CONFIRMED+replace不匹配→拒绝", allowed is False, f"(allowed={allowed!r}, reason={reason!r})")

    # T15: 撤销确认序列（F1 原子逻辑）——撤销成功 → ABSENT 终结 → 再闸门（无 replace）放行
    states = _state_batch(protection_registry={IDENT_TP: _reg_entry()})
    fake = _make_fake(states)
    # 模拟 F1 撤销确认后的 _update_registry(ABSENT, terminated_reason='canceled_by_update_replace')
    fake._update_registry(SYMBOL, BATCH, IDENT_TP, state='ABSENT',
                          terminated_reason='canceled_by_update_replace')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, IDENT_TP, desc='补挂止盈单')
    report("T15/撤销确认→ABSENT→再闸门放行",
           allowed is True and entry.get('state') == 'ABSENT'
           and entry.get('terminated_reason') == 'canceled_by_update_replace',
           f"(allowed={allowed!r}, state={entry.get('state')!r}, reason={entry.get('terminated_reason')!r})")


# =====================================================================
# T16：_update_registry 支持 terminated_reason 落盘（F1/F2 基础设施）
# =====================================================================
def t_update_registry_reason():
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='CONFIRMED', order_id='sl_1')})
    fake = _make_fake(states)
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='ABSENT',
                          terminated_reason='terminal_status_canceled')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("T16/update_registry写terminated_reason",
           entry.get('state') == 'ABSENT' and entry.get('terminated_reason') == 'terminal_status_canceled',
           f"(state={entry.get('state')!r}, reason={entry.get('terminated_reason')!r})")


# =====================================================================
# T17-T21：源码断言（F1/F2/F3 落位检查）
# =====================================================================
def t_source_asserts():
    src = open('trader_260725.py', encoding='utf-8').read()
    lines = src.splitlines()

    # T17: F1——TP/SL 更新段撤销前必须过闸门且传 replace_order_id
    n_tp_replace = src.count("desc='替换止盈单'")
    n_sl_replace = src.count("desc='替换止损单'")
    report("T17/F1撤销前闸门(replace)落位", n_tp_replace >= 1 and n_sl_replace >= 1,
           f"(TP替换={n_tp_replace}处, SL替换={n_sl_replace}处 → 各需>=1)")

    # T18: F1——撤销确认后必须写 ABSENT + terminated_reason（canceled_by_update_replace 等）
    n_absent_reason = src.count('canceled_by_update_replace') + src.count('order_not_found_on_replace')
    report("T18/F1撤销确认→ABSENT+reason", n_absent_reason >= 2,
           f"(取消替换/订单不存在终结标记共 {n_absent_reason} 处 → 需>=2：SL段+TP段)")

    # T19: F2——terminal 检测分支写 registry ABSENT（canceled/expired 同步终结）
    # 第二轮审查（2026-08-21）改多行三元表达式（精确+_fallback 双 reason），
    # "terminated_reason=f'..." 连续子串被拆断 → 改统计 f'terminal_status_（当前 4 处：SL 2 + TP 2）
    n_term = src.count("f'terminal_status_")
    report("T19/F2 terminal→ABSENT", n_term >= 2,
           f"(terminal_status_ 终结标记 {n_term} 处 → 需>=2：SL+TP)")

    # T20: F3——裁决 helper 定义 1 + 调用点 2（SL 缺失检测 + TP R14）
    n_def = src.count('def _adjudicate_recreate_before_repair')
    n_call = src.count('self._adjudicate_recreate_before_repair(')
    report("T20/F3裁决helper定义+调用", n_def == 1 and n_call >= 2,
           f"(定义={n_def}, 调用={n_call} → 定义=1且调用>=2)")

    # T21: F3——R14 补挂前必须裁决（allow 才补挂）；TP 段保留原单分支防二次清 id
    n_verdict_allow = src.count("verdict == 'allow'")
    n_f1_skip = src.count("F1_replace_blocked_skip_create")
    report("T21/F3裁决放行+TP保留原单", n_verdict_allow >= 2 and n_f1_skip >= 1,
           f"(verdict==allow {n_verdict_allow}处, F1保留分支 {n_f1_skip}处)")

    # T22: F1——TP 段撤销失败 fail-closed（不清 id、不创建），SL 段 continue 保留
    n_failclosed_tp = src.count('保留原单下轮再试')
    n_failclosed_sl = src.count('撤销旧止损单失败')
    report("T22/F1网络异常fail-closed", n_failclosed_tp >= 1 and n_failclosed_sl >= 1,
           f"(TP保留原单={n_failclosed_tp}处, SL失败分支={n_failclosed_sl}处)")


def main():
    t_f3_adjudicate()
    t_f1_gate_replace()
    t_update_registry_reason()
    t_source_asserts()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\nF1/F2/F3 TP死锁修复专项：{passed}/{total} 通过\n{'#' * 60}")
    if passed != total:
        print("❌ 存在失败项，请检查上方 FAIL 明细")
        sys.exit(1)


if __name__ == '__main__':
    main()
