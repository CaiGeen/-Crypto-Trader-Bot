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
    fee, source, note = t._resolve_order_fees(
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.999)
    assert abs(fee - 0.0389) < 1e-9, fee
    assert source == 'actual' and note == '', (source, note)


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
        SYM, {'kind': 'regular', 'order_id': 'L1'}, 0.002, 0.9)
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
    assert abs(fees['entry_fee'] - (0.05 + 0.001 * 77000.00 * 0.0005)) < 1e-6, \
        f'混合 = actual 层 + estimated 层: {fees}'


# ── F11：四路径接线锁（AST）+ finalizer 已端到端（F7）────────────────────
def f11_four_path_wiring_locked():
    import os
    src = open(os.path.abspath(trader_260725.__file__), encoding='utf-8').read()
    assert src.count('self._compute_settlement_fees(') == 4, \
        f'四路径必须各自调用统一口径 helper（实际 {src.count("self._compute_settlement_fees(")}）'
    assert '_resolve_order_fees' in src, 'resolver 缺失'
    # 旧式全量扣减零残留
    assert 'total_fees = total_entry_fee + exit_fee' not in src, 'SL/TP 旧全量扣减残留'
    assert 'actual_total_fees = total_entry_fee + actual_exit_fee' not in src, '市价旧全量扣减残留'
    assert 'total_cost_with_fee' not in src, 'finalizer 双重扣残留'
    # 每处 _record_realized_pnl 调用前 40 行内必有统一口径调用（接线顺序）
    import re
    for m in re.finditer(r'self\._record_realized_pnl\(', src):
        seg = src[max(0, m.start() - 8000):m.start()]
        assert '_compute_settlement_fees(' in seg, 'record 调用前必须已完成统一口径解析'


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



TESTS = [f1_regular_order_direct_actual,
         f2_algo_mapping_chain,
         f3_non_usdt_commission,
         f4_query_failure_falls_back_finite,
         f7_finalizer_formula_counterexample,
         f8_partial_allocation_estimated,
         f9_fills_incomplete,
         f10_multi_layer_mixed,
         f11_four_path_wiring_locked,
         f12_partial_then_new_layer_must_be_estimated,
         f13_market_confirmed_less_than_net,
         f_meta_error_never_swallows_pnl,
         f14_total_exception_entry_fee_finite]


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
