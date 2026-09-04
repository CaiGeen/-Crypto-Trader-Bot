# -*- coding: utf-8 -*-
"""T1-C entry-fee 记账决定性测试（F1–F13，RED-first）。

设计依据：T1C_entry_fee_设计草案_送审ChatGPT.md v1.3 + 三条补充条款
（ChatGPT NEARLY ALIGNED → 补齐即批准实施）。
- 统一公式：入场费只扣一次（finalizer 双重扣反例 F7）；
- partial 估算边界：realized_reduce_amount>0 或 settlement<net → 一律 estimated；
- resolver 契约：expected_qty 数量归因 + estimated_fee 有限降级（绝不返 0）；
- 落盘契约：fee_breakdown 白名单 + fee_metadata_error + prior_reduction_unknown
  兑现（docstring 声称但实现从未写入的历史缺陷）；
- F11 四路径接线锁（AST）+ finalizer 端到端行为断言（读实际 trade_stats.json）。

harness：复用 test_p5_closecancel 的隔离版 make_trader（P5k 教训：墓碑/PnL
全部重定向到临时目录，r99 生产文件免疫沿用）。
"""
import copy
import json
import os
import tempfile
import threading

import test_p5_closecancel as p5
import trader_260725
from trader_260725 import CryptoTrader

SYM = p5.SYM
TMP = tempfile.mkdtemp(prefix='t1c_')


# ── 轻量 resolver harness（__new__ 绕 __init__，纯函数离线）──────────────
def _resolver_trader(fills_by_oid=None, algo_by_id=None, fail=False):
    t = CryptoTrader.__new__(CryptoTrader)
    t._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    t._fills_by_oid = fills_by_oid or {}
    t._algo_by_id = algo_by_id or {}
    t._fail = fail

    def _fetch_my_trades(symbol, since=None, limit=None, params=None):
        if t._fail:
            raise RuntimeError('injected query failure')
        oid = str((params or {}).get('orderId') or '')
        return t._fills_by_oid.get(oid, [])

    def _algo_order(params, **k):
        aid = str(params.get('algoId') or '')
        if aid not in t._algo_by_id:
            raise RuntimeError('algo not found')
        return t._algo_by_id[aid]

    ex = type('Ex', (), {})()
    ex.fetch_my_trades = _fetch_my_trades
    ex.fapiPrivateGetAlgoOrder = _algo_order
    t.exchange = ex
    return t


def _fill(qty, commission, asset='USDT', order='X', tid=1):
    return {'order': order, 'amount': qty,
            'info': {'orderId': int(order) if order.isdigit() else order,
                     'id': str(tid), 'qty': str(qty), 'commission': str(commission),
                     'commissionAsset': asset}}


# ── F1：普通单直查 actual ────────────────────────────────────────────────
def f1_regular_order_direct_actual():
    t = _resolver_trader(fills_by_oid={'L1': [_fill(0.002, 0.0389, order='L1')]})
    # 权威数量可得（快照）→ actual
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.999,
        order_snapshot={'executedQty': '0.002', 'filled': 0.002})
    assert abs(fee - 0.0389) < 1e-9, fee
    assert source == 'actual' and note == '', (source, note)
    # 权威数量不可得 → 必须 estimated + qty_unknown（UNKNOWN 绝不猜 actual）
    fee2, source2, note2 = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.999)
    assert source2 == 'estimated' and note2 == 'qty_unknown', (source2, note2)
    assert abs(fee2 - 0.999) < 1e-9, fee2


# ── F2：条件单映射链（actualOrderId 命中 / 空 → 降级）────────────────────
def f2_algo_mapping_chain():
    t = _resolver_trader(
        fills_by_oid={'1123067286925': [_fill(0.001, 0.0389426, order='1123067286925')]},
        algo_by_id={'3000002168427079': {'algoId': 3000002168427079,
                                          'actualOrderId': '1123067286925',
                                          'actualQty': '0.001'}})
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'algo', 'order_id': '3000002168427079'}, 0.001, 0.999)
    assert abs(fee - 0.0389426) < 1e-9 and source == 'actual', (fee, source)
    # CANCELED：actualOrderId 空 → estimated + note（绝不等同零费）
    t2 = _resolver_trader(algo_by_id={'3000002170593663': {'algoId': 3000002170593663,
                                                            'actualOrderId': ''}})
    fee2, source2, note2 = t2._resolve_order_fees(
        SYM, {'kind': 'algo', 'order_id': '3000002170593663'}, 0.001, 0.5)
    assert source2 == 'estimated' and note2 == 'algo_no_actual_order', (source2, note2)
    assert abs(fee2 - 0.5) < 1e-9, f'降级必须返回调用方估算值（绝不返 0）: {fee2}'


# ── F3：非 USDT → estimated + note ──────────────────────────────────────
def f3_non_usdt_commission():
    t = _resolver_trader(fills_by_oid={'L1': [_fill(0.002, 0.001, asset='BNB', order='L1')]})
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.9,
        order_snapshot={'filled': 0.002})
    assert source == 'estimated' and note == 'non_usdt_commission', (source, note)
    assert abs(fee - 0.9) < 1e-9, fee


# ── F4：查询失败 → 有限估算值，绝不抛出/绝不返 0 ─────────────────────────
def f4_query_failure_falls_back_finite():
    t = _resolver_trader(fail=True)
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.9)
    assert source == 'estimated' and note == 'query_failed', (source, note)
    assert abs(fee - 0.9) < 1e-9, f'降级返回有限估算值: {fee}'
    assert fee == fee and abs(fee) != float('inf'), '必须有限数'


# ── F4b：fee 查询降级不得额外改变 close_phase / clear gate / 恢复语义 ─────
def f4b_degrade_does_not_change_gate_or_recovery():
    tmp = tempfile.mkdtemp(prefix='t1c_f4b_')
    t, ex = p5.make_trader(tmp)
    b = p5._lp_batch(net=0.002, reason='limit_pending_normal')
    b['target_amounts'] = [0.002]
    b['filled_details'] = [76885.20]
    b['total_entry_fee'] = 0.077
    b['realized_reduce_amount'] = 0.0
    p5._state_write(t, p5._single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           stop=77885.20, avg=77885.20)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]

    def _boom(*a, **k):
        raise RuntimeError('injected query failure')
    ex.fetch_my_trades = _boom                 # 手续费查询全降级

    ok, msg = t._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok, f'fee 降级不得阻断限价 full-fill 结算（仍应进 phase2 并清理）: {msg}'
    stats = json.load(open(os.path.join(tmp, 'trade_stats.json'), encoding='utf-8'))
    rec = stats['trades'][-1]
    assert rec['entry_fee_source'] == 'estimated' and rec['exit_fee_source'] == 'estimated', rec
    # 落盘数量仍为完整结算数量（不是部分）
    assert abs(rec['amount'] - 0.002) < 1e-9, rec
    # 恢复语义：再次调用（崩溃重试/接管）必须幂等 dedup，不产生第二条记录
    ok2, msg2 = t._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok2, msg2
    stats2 = json.load(open(os.path.join(tmp, 'trade_stats.json'), encoding='utf-8'))
    assert len([r for r in stats2['trades'] if r.get('dedup_key') == f'{SYM}:L1']) == 1, \
        '恢复路径必须 dedup 幂等，不得双记'


# ── F20：实际成交量 ≠ 账本净量（数量冲突）────────────────────────────────
def f20_actual_qty_conflict_records_actual_qty():
    t = make_settlement_trader()
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.001
    b['realized_reduce_cost'] = 76885.20 * 0.001
    # 账本净量 0.001，交易所实际只成交 0.0008
    r = t._settle_protection_fill(SYM, 'bS', b, 77885.20,
                                  {'kind': 'algo', 'order_id': 'S1'},
                                  snapshot={'actualQty': '0.0008'})
    assert r['qty_conflict'] is True, r
    assert abs(r['qty'] - 0.0008) < 1e-9, f'必须按实际成交量记账: {r}'
    assert abs(r['ledger_qty'] - 0.001) < 1e-9, r
    assert abs(r['gross_pnl'] - 0.8) < 1e-9, r
    # partial 历史存在时为 partial_allocation_unknown；关键证据是入场费按
    # 实际成交量份额缩放（0.077 × 0.0008/0.001 = 0.0616），而非按账本净量全扣
    assert r['fees']['entry_note'] in ('prior_reduction_unknown',
                                       'partial_allocation_unknown'), r['fees']
    assert abs(r['fees']['entry_fee'] - 0.0616) < 1e-9, \
        f'入场费必须按实际成交量份额缩放: {r[chr(39)+chr(39)] if False else r}'
    taker = 77885.20 * 0.0008 * 0.0005
    assert abs(r['fees']['exit_fee'] - taker) < 1e-9, r['fees']
    assert abs(r['net_pnl'] - (0.8 - 0.0616 - taker)) < 1e-5, r


# ── F21：权威数量不可得 → qty_unknown，绝不猜 actual ─────────────────────
def f21_authoritative_qty_unknown():
    t = _resolver_trader(fills_by_oid={'L1': [_fill(0.002, 0.0389, order='L1')]})
    # fills 与 expected 都自洽，但权威订单数量取不到（无快照且 stub 无 fetch_order）
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.999)
    assert source == 'estimated', f'UNKNOWN 不得猜 actual: {(source, note)}'
    assert note == 'qty_unknown', note
    assert abs(fee - 0.999) < 1e-9, f'降级必须返回调用方有限估算: {fee}'


# ── F7：finalizer 公式反例（端到端，读实际 trade_stats.json）────────────
def f7_finalizer_formula_counterexample():
    # net_cost=0.002@76885.20=153.7704；fee_rem=0.0770；exit 0.002@77885.20；
    # exit_fee(actual)=0.0389426。
    # 旧公式（双扣）：gross=1.9230，net=1.8071
    # 新公式：      gross=2.0000，net=1.8841
    tmp = tempfile.mkdtemp(prefix='t1c_f7_')
    t, ex = p5.make_trader(tmp)
    b = p5._lp_batch(net=0.002, reason='limit_pending_normal')
    b['target_amounts'] = [0.002]
    b['filled_details'] = [76885.20]
    b['total_entry_fee'] = 0.077
    b['realized_reduce_amount'] = 0.0
    p5._state_write(t, p5._single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           stop=77885.20, avg=77885.20)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    # 入场单（STOP_MARKET 条件单）映射链：algoId → actualOrderId → fills
    ex.fapiPrivateGetAlgoOrder = lambda params, **k: {
        'algoId': b['entry_orders'][0], 'actualOrderId': 'E1FILL',
        'actualQty': '0.002'}
    ex.fetch_my_trades = lambda symbol, since=None, limit=None, params=None, **k: {
        'E1FILL': [_fill(0.002, 0.077, order='E1FILL')],
        'L1': [_fill(0.002, 0.0389426, order='L1')],
    }.get(str((params or {}).get('orderId') or ''), [])

    ok, msg = t._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok, msg

    stats = json.load(open(os.path.join(tmp, 'trade_stats.json'), encoding='utf-8'))
    rec = stats['trades'][-1]
    assert abs(rec['net_pnl'] - 1.8841) < 0.001, \
        f'新公式 net=1.8841（旧双扣公式 1.8071）: {rec}'
    assert abs(rec['gross_implied'] - 2.0) < 0.01 if 'gross_implied' in rec else True
    assert rec.get('entry_fee_source') == 'actual' and abs(rec['entry_fee'] - 0.077) < 1e-6, rec
    assert rec.get('exit_fee_source') == 'actual' and abs(rec['exit_fee'] - 0.0389426) < 1e-4, rec
    assert not rec.get('fee_metadata_error'), rec
    # 落盘契约：净成本基准 avg_entry（未含手续费）
    assert abs(rec['avg_price'] - 76885.20) < 0.01, rec


# ── F8：partial 估算分摊（SL 语义：扣净份额而非全量）────────────────────
def f8_partial_allocation_estimated():
    t = make_settlement_trader()
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.001          # 曾 partial（历史分摊不可知）
    b['realized_reduce_cost'] = 76885.20 * 0.001  # 净成本减半 → ratio=0.5
    fees = t._compute_settlement_fees(SYM, b, 0.001,
                                      {'kind': 'algo', 'order_id': 'S1'}, 0.04)
    assert fees['entry_fee_source'] == 'estimated', fees
    assert fees['entry_note'] == 'partial_allocation_unknown', fees
    assert abs(fees['entry_fee'] - 0.077) < 1e-9, \
        f'必须扣净份额 0.077 而非全量 0.154: {fees}'
    # 曾 partial → 不发起 actual 解析（估算基数，零 API）
    assert t.exchange.fetch_my_trades_calls == 0, 'partial 历史下不得查询 actual'


# ── F9：fills 数量不完整 → estimated + fills_incomplete ─────────────────
def f9_fills_incomplete():
    t = _resolver_trader(fills_by_oid={'L1': [_fill(0.001, 0.02, order='L1')]})  # 差一半
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.9)
    assert source == 'estimated' and note == 'fills_incomplete', (source, note)
    assert abs(fee - 0.9) < 1e-9


# ── F10：多层 mixed → 整体 estimated，note 记 estimated 层原因 ───────────
def f10_multi_layer_mixed():
    t = make_settlement_trader()
    b = _settle_batch(entry_orders=['A1', 'A2'], target_amounts=[0.001, 0.001],
                      filled_details=[76885.20, 77000.00], last_filled_count=2)
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.0
    # 层1 actual（USDT fills），层2 algo 无 actualOrderId
    t.exchange.fetch_my_trades = lambda symbol, since=None, limit=None, params=None, **k: {
        'A1FILL': [_fill(0.001, 0.05, order='A1FILL')]}.get(
        str((params or {}).get('orderId') or ''), [])
    t.exchange.fapiPrivateGetAlgoOrder = lambda params, **k: (
        {'actualOrderId': 'A1FILL'} if str(params.get('algoId')) == 'A1'
        else {'actualOrderId': ''})
    fees = t._compute_settlement_fees(SYM, b, 0.002,
                                      {'kind': 'algo', 'order_id': 'S1'}, 0.04)
    assert fees['entry_fee_source'] == 'estimated', fees
    assert 'algo_no_actual_order' in fees['entry_note'], fees
    est1 = 0.001 * 76885.20 * 0.0005
    est2 = 0.001 * 77000.00 * 0.0005
    assert abs(fees['entry_fee'] - (est1 + est2)) < 1e-6, \
        f'降级层使用调用方保守估算: {fees}'


# ── F11：四路径接线锁（AST）+ finalizer 已端到端（F7）────────────────────
def f11_four_path_wiring_locked():
    """接线锁（AST）+ 行为覆盖分工说明（本测试不做行为验证）：
    - finalizer / 市价：生产代码直接调 _compute_settlement_fees（行为见 F7/F13）；
    - SL / TP：生产代码经 _settle_protection_fill 调同一口径（行为见 F16/F17）；
    - 本测试只锁「接线存在且位于对应调用路径上」。"""
    import re
    src = open(os.path.abspath(trader_260725.__file__), encoding='utf-8').read()
    n_direct = src.count('self._compute_settlement_fees(')
    n_helper = src.count('self._settle_protection_fill(')
    assert n_direct == 3, 'finalizer/市价直调 + helper 内调用 = 3（实际 %d）' % n_direct
    assert n_helper == 2, 'SL 与 TP 各调用 1 次 _settle_protection_fill（实际 %d）' % n_helper
    assert '_resolve_order_fees' in src, 'resolver 缺失'
    assert 'total_fees = total_entry_fee + exit_fee' not in src, 'SL/TP 旧全量扣减残留'
    assert 'actual_total_fees = total_entry_fee + actual_exit_fee' not in src, '市价旧全量扣减残留'
    assert 'total_cost_with_fee' not in src, 'finalizer 双重扣残留'
    # 生产参数正确性：SL/TP 必须把真实订单详情作为权威快照传入（不是 None）
    assert src.count('snapshot=sl_detail') == 1, 'SL 必须传入 sl_detail 权威快照'
    assert src.count('snapshot=tp_detail') == 1, 'TP 必须传入 tp_detail 权威快照'
    assert src.count('order_snapshot=order)') >= 2, 'finalizer/市价必须传入订单详情'
    # 数量冲突必须阻断完整结算清理（清理调用必须位于 qty_conflict 的 else 分支）
    assert src.count("if _sl['qty_conflict']:") == 1, 'SL 缺数量冲突分支'
    assert src.count("if _tp['qty_conflict']:") == 1, 'TP 缺数量冲突分支'
    for _seg in src.split("if _sl['qty_conflict']:")[1:]:
        assert 'else:' in _seg[:2000] and '_cancel_remaining_entries' in _seg[:2000], \
            'SL 数量冲突时不得执行残余挂单清理'
    for _seg in src.split("if _tp['qty_conflict']:")[1:]:
        assert 'else:' in _seg[:2000] and '_cancel_remaining_entries' in _seg[:2000], \
            'TP 数量冲突时不得执行残余挂单清理'
    for m in re.finditer(r'self\._record_realized_pnl\(', src):
        seg = src[max(0, m.start() - 8000):m.start()]
        assert ('_compute_settlement_fees(' in seg) or ('_settle_protection_fill(' in seg), \
            'record 调用前必须已完成统一口径解析'


# ── F12：partial 后新层成交反例（聚合比例式必错 → 强制 estimated）─────────
def f12_partial_then_new_layer_must_be_estimated():
    t = make_settlement_trader()
    b = _settle_batch(entry_orders=['A1', 'A2'], target_amounts=[0.001, 0.001],
                      filled_details=[76885.20, 77000.00], last_filled_count=2)
    b['total_entry_fee'] = 0.70        # 0.50(L1) + 0.20(L2)
    b['realized_reduce_amount'] = 0.001  # partial 减半后 L2 才成交
    fees = t._compute_settlement_fees(SYM, b, 0.002,
                                      {'kind': 'algo', 'order_id': 'S1'}, 0.04)
    # 聚合比例式会得 0.70×(net_cost/gross_cost) 并冒充 actual——必须 estimated
    assert fees['entry_fee_source'] == 'estimated', fees
    assert fees['entry_note'] == 'partial_allocation_unknown', fees


# ── F13：market confirmed<净量 → 按比例 + prior_reduction_unknown 落盘 ───
def f13_market_confirmed_less_than_net():
    t = make_settlement_trader()
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.0
    fees = t._compute_settlement_fees(SYM, b, 0.001,       # settlement 0.001 < net 0.002
                                      {'kind': 'regular', 'order_id': 'M1'}, 0.04)
    assert fees['entry_fee_source'] == 'estimated', fees
    assert fees['entry_note'] == 'prior_reduction_unknown', fees
    assert abs(fees['entry_fee'] - 0.077) < 1e-9, f'按结算比例份额: {fees}'
    # 落盘契约：pnl_partial=True 必须真正落 prior_reduction_unknown（历史缺陷兑现）
    sf = os.path.join(TMP, 'trade_stats_f13.json')
    t._record_realized_pnl('bX', SYM, 'BUY', 0.001, 76885.20, 77885.20,
                           0.9, '市价平仓', pnl_partial=True, stats_file=sf,
                           fee_breakdown=fees)
    rec = json.load(open(sf, encoding='utf-8'))['trades'][-1]
    assert rec.get('prior_reduction_unknown') is True, rec
    assert abs(rec['net_pnl'] - 0.9) < 1e-9, rec


# ── 补 3：fee_breakdown 元数据异常不吞 net_pnl ───────────────────────────
def f_meta_error_never_swallows_pnl():
    t = make_settlement_trader()
    sf = os.path.join(TMP, 'trade_stats_meta.json')
    # 缺必需字段
    t._record_realized_pnl('b1', SYM, 'BUY', 0.001, 100.0, 101.0, 0.9, '市价平仓',
                           stats_file=sf, fee_breakdown={'entry_note': 'x'})
    rec = json.load(open(sf, encoding='utf-8'))['trades'][-1]
    assert abs(rec['net_pnl'] - 0.9) < 1e-9, '元数据异常不得吞掉 net_pnl'
    assert rec.get('fee_metadata_error') is True, rec
    # 未知扩展键：忽略 + 保存
    t._record_realized_pnl('b2', SYM, 'BUY', 0.001, 100.0, 101.0, 0.8, '市价平仓',
                           stats_file=sf,
                           fee_breakdown={'entry_fee': 0.01, 'entry_fee_source': 'actual',
                                          'exit_fee': 0.01, 'exit_fee_source': 'actual',
                                          'totally_unknown_key': 123})
    recs = json.load(open(sf, encoding='utf-8'))['trades']
    assert recs[-1]['batch_id'] == 'b2' and abs(recs[-1]['net_pnl'] - 0.8) < 1e-9
    assert 'totally_unknown_key' not in recs[-1], recs[-1]
    # NaN/inf 金额：resolver 出口拦截 + 落盘层防御
    t._record_realized_pnl('b3', SYM, 'BUY', 0.001, 100.0, 101.0, 0.7, '市价平仓',
                           stats_file=sf,
                           fee_breakdown={'entry_fee': float('nan'),
                                          'entry_fee_source': 'actual',
                                          'exit_fee': 0.01, 'exit_fee_source': 'actual'})
    recs = json.load(open(sf, encoding='utf-8'))['trades']
    assert recs[-1].get('fee_metadata_error') is True, recs[-1]
    assert 'entry_fee' not in recs[-1], recs[-1]


# ── settlement 测试 harness ─────────────────────────────────────────────
def _settle_batch(**extra):
    b = {
        'is_active': True, 'batch_id': 'bS', 'symbol': SYM, 'side': 'BUY',
        'target_amounts': [0.002], 'filled_details': [76885.20],
        'last_filled_count': 1, 'total_entry_fee': 0.0,
        'entry_orders': ['A1'], 'current_sl_id': 'S1',
        'realized_reduce_amount': 0.0, 'realized_reduce_cost': 0.0,
        'close_phase': 0, 'pending_close': False, 'close_reason': '', 'close_op_id': '',
    }
    b.update(extra)
    return b


def make_settlement_trader():
    t = CryptoTrader.__new__(CryptoTrader)
    t._state_lock = threading.RLock()
    t._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    t.exchange = type('Ex', (), {})()
    t.exchange.fetch_my_trades_calls = 0
    t.exchange.fetch_my_trades = lambda *a, **k: (
        t.exchange.__dict__.update(fetch_my_trades_calls=t.exchange.fetch_my_trades_calls + 1) or [])
    t.exchange.fapiPrivateGetAlgoOrder = lambda params, **k: {'actualOrderId': ''}
    return t




# ── F14：总异常兜底 → entry_fee 必须有限非 0（补 2 契约）──────────────────
def f14_total_exception_entry_fee_finite():
    t = make_settlement_trader()
    def _boom(b):
        raise RuntimeError('injected')
    t._batch_net_position = _boom
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    fees = t._compute_settlement_fees(SYM, b, 0.002,
                                      {'kind': 'algo', 'order_id': 'S1'}, 0.04)
    assert fees['entry_fee_source'] == 'estimated', fees
    assert fees['entry_fee'] == fees['entry_fee'] and abs(fees['entry_fee']) != float('inf'), fees
    assert fees['entry_fee'] > 0, f'总异常兜底不得返 0（补 2）: {fees}'
    assert abs(fees['entry_fee'] - 0.154) < 1e-9, f'兜底=账本全量入场费: {fees}'
    assert fees['entry_note'] == 'query_failed', fees




# ── F15：权威数量不一致 → 不得标 actual（P0-2 双重校验）──────────────────
def f15_authoritative_qty_mismatch():
    t = _resolver_trader(fills_by_oid={'L1': [_fill(0.001, 0.02, order='L1')]})
    # actualQty=0.002 但 fills 只 0.001，expected_qty 恰好 0.001 → 旧实现会误判 actual
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.001, 0.9,
        order_snapshot={'executedQty': '0.002', 'filled': 0.002})
    assert source == 'estimated', '权威数量与 expected 不一致不得 actual: %r' % ((fee, source, note),)
    assert note == 'qty_mismatch', note
    assert abs(fee - 0.9) < 1e-9, '降级返回调用方有限估算: %r' % fee


# ── F16：partial → SL 必须按净量/净成本结算（P0-1 行为验证）──────────────
def f16_partial_sl_uses_net_qty():
    t = make_settlement_trader()
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.001          # 曾 partial 减半
    b['realized_reduce_cost'] = 76885.20 * 0.001
    r = t._settle_protection_fill(SYM, 'bS', b, 77885.20,
                                  {'kind': 'algo', 'order_id': 'S1'})
    assert abs(r['qty'] - 0.001) < 1e-9, '结算数量必须是净量 0.001（非毛量 0.002）: %r' % (r,)
    assert abs(r['avg_entry'] - 76885.20) < 0.01, '净成本均价: %r' % (r,)
    assert abs(r['gross_pnl'] - 1.0) < 1e-6, 'gross=(77885.2-76885.2)*0.001: %r' % (r,)
    assert r['fees']['entry_fee_source'] == 'estimated', r['fees']
    assert r['fees']['entry_note'] == 'partial_allocation_unknown', r['fees']
    assert abs(r['fees']['entry_fee'] - 0.077) < 1e-9, r['fees']
    assert abs(r['fees']['exit_fee'] - 77885.20 * 0.001 * 0.0005) < 1e-9, r['fees']
    assert abs(r['net_pnl'] - (1.0 - 0.077 - 0.038943)) < 1e-5, r


# ── F17：partial → TP 同样净口径 + 降级费率 TAKER（P1）───────────────────
def f17_partial_tp_uses_taker_on_degrade():
    t = make_settlement_trader()
    b = _settle_batch()
    b['total_entry_fee'] = 0.154
    b['realized_reduce_amount'] = 0.001
    b['realized_reduce_cost'] = 76885.20 * 0.001
    r = t._settle_protection_fill(SYM, 'bS', b, 77885.20,
                                  {'kind': 'algo', 'order_id': 'T1'})
    taker = 77885.20 * 0.001 * 0.0005
    maker = 77885.20 * 0.001 * 0.0002
    assert abs(r['fees']['exit_fee'] - taker) < 1e-9, \
        'TP 触发后按市价成交 → 降级费率必须 TAKER(%r) 而非 MAKER(%r): %r' % (taker, maker, r)
    assert abs(r['qty'] - 0.001) < 1e-9, r
    assert abs(r['net_pnl'] - (1.0 - 0.077 - taker)) < 1e-5, r


# ── F18：非有限/残缺账本 → 有限降级 + 明确 note，绝不静默归零 ────────────
def f18_bad_ledger_and_non_finite():
    t = make_settlement_trader()
    b = _settle_batch()
    b['last_filled_count'] = 3                    # 数组只有 1 层 → 账本残缺
    b['total_entry_fee'] = 0.154
    fees = t._compute_settlement_fees(SYM, b, 0.002,
                                      {'kind': 'algo', 'order_id': 'S1'}, 0.04)
    assert fees['entry_fee_source'] == 'estimated', fees
    assert fees['entry_note'] == 'entry_ledger_incomplete', fees
    t2 = _resolver_trader(fail=True)
    f2, s2, n2 = t2._resolve_order_fees(SYM, {'kind': 'regular', 'order_id': 'L1'},
                                        0.002, float('nan'))
    # 调用方估算非有限 + 查询失败 → 必须显式 unknown（None），绝不能用 0 伪装
    assert s2 == 'estimated' and 'estimated_fee_unknown' in n2, (s2, n2)
    assert f2 is None, f'非有限估算必须落到 unknown(None)，而非 0: {f2}'
    # 上层：退出费 unknown → fee_note 明确标记（落盘时会置 fee_metadata_error）
    b2 = _settle_batch()
    fees2 = t._compute_settlement_fees(SYM, b2, 0.002,
                                       {'kind': 'algo', 'order_id': 'S1'}, float('nan'))
    assert fees2['fee_note'] == 'fee_unknown', fees2


# ── F19：TP 查询失败 → 退出费走 TAKER 估算，net_pnl 不虚高 ───────────────
def f19_tp_query_failure_not_inflate_pnl():
    t = make_settlement_trader()

    def _boom(*a, **k):
        raise RuntimeError('query down')
    t.exchange.fetch_my_trades = _boom
    b = _settle_batch()
    b['total_entry_fee'] = 0.077
    r = t._settle_protection_fill(SYM, 'bS', b, 77885.20,
                                  {'kind': 'algo', 'order_id': 'T1'})
    taker = 77885.20 * 0.002 * 0.0005
    maker = 77885.20 * 0.002 * 0.0002
    assert r['fees']['exit_fee_source'] == 'estimated', r['fees']
    assert abs(r['fees']['exit_fee'] - taker) < 1e-9, \
        '降级必须用 TAKER(%r)，用 MAKER(%r) 会虚增净 PnL: %r' % (taker, maker, r)
    assert r['net_pnl'] <= r['gross_pnl'] - maker, '净 PnL 不得因费率错配被抬高'



TESTS = [f1_regular_order_direct_actual,
         f2_algo_mapping_chain,
         f3_non_usdt_commission,
         f4_query_failure_falls_back_finite,
         f4b_degrade_does_not_change_gate_or_recovery,
         f7_finalizer_formula_counterexample,
         f8_partial_allocation_estimated,
         f9_fills_incomplete,
         f10_multi_layer_mixed,
         f11_four_path_wiring_locked,
         f12_partial_then_new_layer_must_be_estimated,
         f13_market_confirmed_less_than_net,
         f_meta_error_never_swallows_pnl,
         f14_total_exception_entry_fee_finite,
         f15_authoritative_qty_mismatch,
         f16_partial_sl_uses_net_qty,
         f17_partial_tp_uses_taker_on_degrade,
         f18_bad_ledger_and_non_finite,
         f19_tp_query_failure_not_inflate_pnl,
         f20_actual_qty_conflict_records_actual_qty,
         f21_authoritative_qty_unknown]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            print(f'💥 {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
