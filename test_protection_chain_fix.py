# -*- coding: utf-8 -*-
"""
事件3 修复专项测试（R-A/B/C/D 四件套，2026-08-21）

背景：成交后保护单生成链非原子化（事件3）——verify 假阴性（algo 可见性延迟）→
NOT_CONFIRMED 永久卡死（自愈只在启动调用）→ 旧层单永不撤销（滚动撤销链断裂）→
pending_sl_orders 永不清空（无限循环）。

R-A：_verify_order_created 对 OrderNotFound 增加 2s×3 短窗口重试（消除瞬时假阴性）
R-B：主循环每 ~30s 调用 _recheck_registry_self_heal（解开 NOT_CONFIRMED 永久卡死）+
     持续未确认升级告警（L1：失败状态通知 + 人工接管入口）
R-C：_reconcile_stale_protection_layers 滚动撤销链补强（新汇总单确认后撤销旧层同 role 单）
R-D：_prune_pending_sl_by_registry 把 registry 已有 order_id 的层移出待挂列表

运行：./.venv/Scripts/python.exe test_protection_chain_fix.py
"""
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_pcf_001"
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
    ex.fetch_order.return_value = {'id': 'o1', 'status': 'NEW'}
    ex.cancel_order.return_value = {}
    fake.exchange = ex
    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    return fake


def _bind_verify(fake):
    fake._verify_order_created = lambda oid, sym, kind='conditional': CryptoTrader._verify_order_created(
        fake, oid, sym, kind)
    return fake


def _bind_self_heal(fake, states):
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake._order_matches_intent = lambda o, i, s: CryptoTrader._order_matches_intent(fake, o, i, s)
    fake._recheck_registry_self_heal = lambda s, b: CryptoTrader._recheck_registry_self_heal(fake, s, b)
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


def _sl_intent(qty=0.43, stop_price=55000.0, side='sell'):
    return {'symbol': SYMBOL, 'side': side, 'qty': qty, 'order_type': 'STOP_MARKET',
            'stop_price': stop_price, 'reduce_only': True}


# =====================================================================
# R-A：_verify_order_created OrderNotFound 短窗口重试（2s×3）
# =====================================================================
def t_ra_verify_retry():
    with mock.patch('time.sleep'):
        # T1: 初次 OrderNotFound → 重试第3次成功 → 'success'（假阴性自愈）
        fake = _bind_verify(_make_base_fake())
        fake.exchange.fetch_order.side_effect = [
            ccxt.OrderNotFound('Order does not exist'),
            ccxt.OrderNotFound('Order does not exist'),
            ccxt.OrderNotFound('Order does not exist'),
            {'id': 'o1', 'status': 'NEW'},
        ]
        res = fake._verify_order_created('o1', SYMBOL, 'conditional')
        n_calls = fake.exchange.fetch_order.call_count
        report("R-A/T1 重试后确认成功", res == 'success' and n_calls == 4,
               f"(返回={res!r}, fetch 调用 {n_calls} 次 → 须 4 次: 初查+3重试)")

        # T2: 持续 OrderNotFound（真不存在）→ 'not_found'（重试封顶后仍返回 not_found）
        fake = _bind_verify(_make_base_fake())
        fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
        res = fake._verify_order_created('o2', SYMBOL, 'conditional')
        n_calls = fake.exchange.fetch_order.call_count
        report("R-A/T2 重试封顶仍not_found", res == 'not_found' and n_calls == 4,
               f"(返回={res!r}, fetch 调用 {n_calls} 次 → 须 4 次后仍 not_found)")

        # T3: 重试期网络异常 → 'unknown'（UNKNOWN ≠ EMPTY，禁止误判不存在）
        fake = _bind_verify(_make_base_fake())
        fake.exchange.fetch_order.side_effect = [
            ccxt.OrderNotFound('Order does not exist'),
            ccxt.NetworkError('connection reset'),
        ]
        res = fake._verify_order_created('o3', SYMBOL, 'conditional')
        report("R-A/T3 重试期网络异常→unknown", res == 'unknown',
               f"(返回={res!r} → 必须 unknown：结果未知 ≠ 不存在)")

        # T4: 普通单（normal）路径不受影响
        fake = _bind_verify(_make_base_fake())
        fake.exchange.fetch_order.return_value = {'id': 'o4', 'status': 'NEW'}
        res = fake._verify_order_created('o4', SYMBOL, 'normal')
        report("R-A/T4 normal 路径", res == 'success',
               f"(返回={res!r} → normal 走默认端点无 params)")

        # T5: 首次即成功（无可见性延迟）→ 1 次调用即 'success'（无多余延迟）
        fake = _bind_verify(_make_base_fake())
        fake.exchange.fetch_order.side_effect = None
        fake.exchange.fetch_order.return_value = {'id': 'o5', 'status': 'NEW'}
        res = fake._verify_order_created('o5', SYMBOL, 'conditional')
        n_calls = fake.exchange.fetch_order.call_count
        report("R-A/T5 首次即成功无重试", res == 'success' and n_calls == 1,
               f"(返回={res!r}, fetch 调用 {n_calls} 次 → 须 1 次)")


# =====================================================================
# R-B：运行期周期自愈重查 + 持续未确认升级告警
# =====================================================================
def t_rb_self_heal():
    # T6: NOT_CONFIRMED + order_id + intent 匹配 → CONFIRMED + 收编 Commit current_sl_id
    ident = f"{BATCH}|SL|L0|LONG"
    reg = {ident: {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'order_kind': 'conditional',
                   'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': _sl_intent()}}
    states = _state_batch(protection_registry=reg)
    fake = _bind_self_heal(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = {
        'id': 'o_sl0', 'symbol': SYMBOL, 'side': 'sell', 'type': 'STOP_MARKET',
        'amount': 0.43, 'stopPrice': 55000.0, 'reduceOnly': True, 'status': 'NEW', 'info': {}}
    fake._self_heal_unconfirmed_rounds = {}
    fake._self_heal_escalate_rounds = 10
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    b = states[SYMBOL][BATCH]
    report("R-B/T6 NOT_CONFIRMED→CONFIRMED+收编", entry.get('state') == 'CONFIRMED' and b.get('current_sl_id') == 'o_sl0',
           f"(state={entry.get('state')!r}, current_sl_id={b.get('current_sl_id')!r} → 必须 CONFIRMED + Commit)")

    # T7: FOUND 但 intent 不匹配 → MISMATCH + critical + 不收编
    ident = f"{BATCH}|SL|L0|LONG"
    reg = {ident: {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'order_kind': 'conditional',
                   'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': _sl_intent(stop_price=99999.0)}}
    states = _state_batch(protection_registry=reg)
    fake = _bind_self_heal(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = {
        'id': 'o_sl0', 'symbol': SYMBOL, 'side': 'sell', 'type': 'STOP_MARKET',
        'amount': 0.43, 'stopPrice': 55000.0, 'reduceOnly': True, 'status': 'NEW', 'info': {}}
    fake._self_heal_unconfirmed_rounds = {}
    fake._self_heal_escalate_rounds = 10
    fake._recheck_registry_self_heal(SYMBOL, BATCH)
    entry = states[SYMBOL][BATCH]['protection_registry'][ident]
    b = states[SYMBOL][BATCH]
    mismatch_alerted = any('MISMATCH' in s for _, s in fake.sent)
    report("R-B/T7 intent不匹配→MISMATCH+critical", entry.get('state') == 'MISMATCH' and b.get('current_sl_id') is None and mismatch_alerted,
           f"(state={entry.get('state')!r}, current_sl_id={b.get('current_sl_id')!r}, alert={mismatch_alerted})")

    # T8: 持续 OrderNotFound → 第10轮 critical 升级告警一次（L1 失败状态通知）
    ident = f"{BATCH}|SL|L0|LONG"
    reg = {ident: {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'order_kind': 'conditional',
                   'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': _sl_intent()}}
    states = _state_batch(protection_registry=reg)
    fake = _bind_self_heal(_make_base_fake(), states)
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    fake._self_heal_unconfirmed_rounds = {}
    fake._self_heal_escalate_rounds = 10
    for _ in range(9):
        fake._recheck_registry_self_heal(SYMBOL, BATCH)
    alerted_before = len(fake.sent)
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 第10轮
    alerted_after = len(fake.sent)
    escalated = any('持续无法确认' in s for _, s in fake.sent)
    report("R-B/T8 持续未确认第10轮升级告警", escalated and alerted_after == alerted_before + 1,
           f"(sent {alerted_before}→{alerted_after}, 升级={escalated})")

    # T9: 确认成功后计数清零（新的一轮失败循环可再次触发告警）
    ident = f"{BATCH}|SL|L0|LONG"
    reg = {ident: {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'order_kind': 'conditional',
                   'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': _sl_intent()}}
    states = _state_batch(protection_registry=reg)
    fake = _bind_self_heal(_make_base_fake(), states)
    fake._self_heal_unconfirmed_rounds = {}
    fake._self_heal_escalate_rounds = 2
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 第1轮
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 第2轮 → 告警 #1
    fake.exchange.fetch_order.side_effect = None
    fake.exchange.fetch_order.return_value = {
        'id': 'o_sl0', 'symbol': SYMBOL, 'side': 'sell', 'type': 'STOP_MARKET',
        'amount': 0.43, 'stopPrice': 55000.0, 'reduceOnly': True, 'status': 'NEW', 'info': {}}
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 确认成功 → CONFIRMED + 计数清零
    # 新一轮失败循环（同一 identity 新 order_id：旧单已替换/重新创建后 verify 又假阴性）
    states[SYMBOL][BATCH]['protection_registry'][ident]['state'] = 'NOT_CONFIRMED'
    states[SYMBOL][BATCH]['protection_registry'][ident]['order_id'] = 'o_sl0b'
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 清零后第1轮
    fake._recheck_registry_self_heal(SYMBOL, BATCH)  # 清零后第2轮 → 告警 #2
    n_escalated = sum(1 for _, s in fake.sent if '持续无法确认' in s)
    report("R-B/T9 确认后计数清零可再触发", n_escalated == 2,
           f"(升级告警 {n_escalated} 次 → 须 2: 第一段2轮 + 清零后新一轮2轮)")


# =====================================================================
# R-C：滚动撤销链补强（_reconcile_stale_protection_layers）
# =====================================================================
def t_rc_reconcile():
    def _mk_states(sl_l0_state='CONFIRMED'):
        ident_l0 = f"{BATCH}|SL|L0|LONG"
        ident_l1 = f"{BATCH}|SL|L1|LONG"
        reg = {
            ident_l0: {'state': sl_l0_state, 'order_id': 'o_old0', 'order_kind': 'conditional',
                       'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': _sl_intent(0.43)},
            ident_l1: {'state': 'CONFIRMED', 'order_id': 'o_new1', 'order_kind': 'conditional',
                       'role': 'SL', 'layer': 1, 'side': 'LONG', 'intent': _sl_intent(0.817)},
        }
        return _state_batch(current_sl_id='o_new1', protection_registry=reg)

    # T10: 旧层单真实存在 → 撤销 + 条目终结 ABSENT；新单(keep)不受影响
    states = _mk_states()
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    CryptoTrader._reconcile_stale_protection_layers(fake, SYMBOL, BATCH, 'SL', keep_order_id='o_new1')
    reg = states[SYMBOL][BATCH]['protection_registry']
    l0 = reg[f"{BATCH}|SL|L0|LONG"]
    l1 = reg[f"{BATCH}|SL|L1|LONG"]
    cancel_ids = [c.args[0] for c in fake.exchange.cancel_order.call_args_list]
    report("R-C/T10 撤销旧层+终结ABSENT", l0.get('state') == 'ABSENT' and 'o_old0' in cancel_ids and l1.get('state') == 'CONFIRMED',
           f"(L0={l0.get('state')!r}, cancel={cancel_ids}, L1={l1.get('state')!r} → 旧层撤+终结，新单保留)")

    # T11: 旧层单 OrderNotFound → 终结 ABSENT，不调 cancel
    states = _mk_states()
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound('Order does not exist')
    CryptoTrader._reconcile_stale_protection_layers(fake, SYMBOL, BATCH, 'SL', keep_order_id='o_new1')
    reg = states[SYMBOL][BATCH]['protection_registry']
    l0 = reg[f"{BATCH}|SL|L0|LONG"]
    n_cancel = fake.exchange.cancel_order.call_count
    report("R-C/T11 OrderNotFound→ABSENT不调cancel", l0.get('state') == 'ABSENT' and n_cancel == 0,
           f"(L0={l0.get('state')!r}, cancel 调用 {n_cancel} 次 → 须 0)")

    # T12: 网络异常 → 保留条目（未知≠不存在），不撤销不终结
    states = _mk_states()
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake.exchange.fetch_order.side_effect = ccxt.NetworkError('connection reset')
    CryptoTrader._reconcile_stale_protection_layers(fake, SYMBOL, BATCH, 'SL', keep_order_id='o_new1')
    reg = states[SYMBOL][BATCH]['protection_registry']
    l0 = reg[f"{BATCH}|SL|L0|LONG"]
    n_cancel = fake.exchange.cancel_order.call_count
    report("R-C/T12 网络异常保留条目", l0.get('state') == 'CONFIRMED' and n_cancel == 0,
           f"(L0={l0.get('state')!r}, cancel 调用 {n_cancel} 次 → 保留下轮)")

    # T13: TP role 独立处理（不误撤 SL）
    ident_tp0 = f"{BATCH}|TP|L0|LONG"
    ident_sl0 = f"{BATCH}|SL|L0|LONG"
    reg = {
        ident_tp0: {'state': 'CONFIRMED', 'order_id': 'o_tp0', 'order_kind': 'conditional',
                    'role': 'TP', 'layer': 0, 'side': 'LONG', 'intent': {}},
        ident_sl0: {'state': 'CONFIRMED', 'order_id': 'o_sl0', 'order_kind': 'conditional',
                    'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': {}},
    }
    states = _state_batch(current_sl_id='o_sl0', tp_order_id='o_tp0', protection_registry=reg)
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    CryptoTrader._reconcile_stale_protection_layers(fake, SYMBOL, BATCH, 'TP', keep_order_id=None)
    reg = states[SYMBOL][BATCH]['protection_registry']
    report("R-C/T13 role 独立", reg[ident_tp0].get('state') == 'ABSENT' and reg[ident_sl0].get('state') == 'CONFIRMED',
           f"(TP0={reg[ident_tp0].get('state')!r}, SL0={reg[ident_sl0].get('state')!r} → 只动 TP)")


# =====================================================================
# R-D：pending_sl_orders 按 registry 实况裁决（_prune_pending_sl_by_registry）
# =====================================================================
def t_rd_prune():
    # T14: registry 有 order_id 的层（无论状态）→ 移出 pending；无 order_id 的保留
    ident0 = f"{BATCH}|SL|L0|LONG"
    ident1 = f"{BATCH}|SL|L1|LONG"
    ident2 = f"{BATCH}|SL|L2|LONG"
    reg = {
        ident0: {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'role': 'SL', 'layer': 0, 'side': 'LONG'},
        ident1: {'state': 'PENDING_CREATE', 'order_id': None, 'role': 'SL', 'layer': 1, 'side': 'LONG'},
        ident2: {'state': 'CONFIRMED', 'order_id': 'o_sl2', 'role': 'SL', 'layer': 2, 'side': 'LONG'},
    }
    states = _state_batch(protection_registry=reg)
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    pending = [0, 1, 2]
    removed = CryptoTrader._prune_pending_sl_by_registry(fake, SYMBOL, BATCH, pending)
    report("R-D/T14 有order_id移出无order_id保留", removed and pending == [1] and states[SYMBOL][BATCH]['pending_sl_orders'] == [1],
           f"(pending={pending} → 须 [1]：L0/L2 有 order_id 移出，L1 无 id 保留)")

    # T15: 全部已有 order_id → pending 清空
    reg = {
        f"{BATCH}|SL|L0|LONG": {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl0', 'role': 'SL', 'layer': 0, 'side': 'LONG'},
        f"{BATCH}|SL|L1|LONG": {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl1', 'role': 'SL', 'layer': 1, 'side': 'LONG'},
    }
    states = _state_batch(protection_registry=reg)
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    pending = [0, 1]
    removed = CryptoTrader._prune_pending_sl_by_registry(fake, SYMBOL, BATCH, pending)
    report("R-D/T15 全部移出", removed and pending == [],
           f"(pending={pending} → 须 []：NOT_CONFIRMED 但已有 order_id → 不再待创建)")

    # T16: 无任何匹配 → 不动作
    reg = {
        f"{BATCH}|SL|L5|LONG": {'state': 'CONFIRMED', 'order_id': 'o_sl5', 'role': 'SL', 'layer': 5, 'side': 'LONG'},
    }
    states = _state_batch(protection_registry=reg)
    fake = _make_base_fake()
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    pending = [0, 1]
    removed = CryptoTrader._prune_pending_sl_by_registry(fake, SYMBOL, BATCH, pending)
    report("R-D/T16 无匹配不动", (not removed) and pending == [0, 1] and len(fake.saved) == 0,
           f"(removed={removed}, pending={pending} → 不变且不落盘)")


# =====================================================================
# 源码断言：4 项修复全部接入（防锚点漂移用子串计数）
# =====================================================================
def t_source_asserts():
    src = open('trader_260725.py', encoding='utf-8').read()

    # T17: R-A 重试标记存在
    ra_marker = src.count('R-A（事件3根因A）')
    report("T17/R-A重试已接入", ra_marker >= 1, f"(R-A 注释标记 {ra_marker} 处 → 须 >=1)")

    # T18: R-B 主循环周期自愈（时间戳 + 调用）存在
    rb_ts = 'last_registry_self_heal_time' in src
    rb_call = 'self._recheck_registry_self_heal(symbol, batch_id)' in src
    rb_esc = 'self_heal_escalate_rounds' in src
    report("T18/R-B周期自愈+升级告警已接入", rb_ts and rb_call and rb_esc,
           f"(时间戳={rb_ts}, 周期调用={rb_call}, 升级阈值={rb_esc})")

    # T19: R-C 调用点（主循环SL/TP + 预生成SL/TP + 兜底SL ≥ 5）+ helper 定义
    n_rc_calls = src.count('_reconcile_stale_protection_layers(')
    report("T19/R-C调用点已接入", n_rc_calls >= 6,
           f"(调用+定义共 {n_rc_calls} 处 → 须 >=6: 5 调用点 + 1 定义)")

    # T20: R-D 接入"处理待补挂止损"块
    rd_marker = 'R-D（事件3根因D）' in src
    n_rd = src.count('_prune_pending_sl_by_registry(')
    report("T20/R-D已接入", rd_marker and n_rd >= 2,
           f"(标记={rd_marker}, helper 引用 {n_rd} 处 → 须 >=2: 1 调用 + 1 定义)")


# =====================================================================
def main():
    t_ra_verify_retry()
    t_rb_self_heal()
    t_rc_reconcile()
    t_rd_prune()
    t_source_asserts()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\n事件3修复专项测试（R-A/B/C/D）：{passed}/{total} 通过\n{'#' * 60}")
    if passed < total:
        sys.exit(1)


if __name__ == '__main__':
    main()
