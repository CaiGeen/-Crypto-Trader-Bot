# -*- coding: utf-8 -*-
"""T1C-v2A-estimated-base 验收测试（E1–E9，v2.2 §10.2，RED-first）。

harness：复用 test_p5_closecancel 隔离版 make_trader（32eb10c 基线分支）。
"""
import json
import os
import tempfile

import test_p5_closecancel as p5
from trader_260725 import CryptoTrader

SYM = p5.SYM
SRC = open(os.path.abspath(
    __import__('trader_260725').__file__), encoding='utf-8').read()


def _mk_batch(tmp, side='BUY', tef=0.077):
    t, ex = p5.make_trader(tmp)
    b = p5._lp_batch(net=0.002, reason='limit_pending_normal')
    b['side'] = side
    b['target_amounts'] = [0.002]
    b['filled_details'] = [76885.20]
    b['total_entry_fee'] = tef
    b['realized_reduce_amount'] = 0.0
    b['realized_reduce_cost'] = 0.0
    p5._state_write(t, p5._single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           stop=77885.20, avg=77885.20)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side':
                     'long' if side == 'BUY' else 'short',
                     'positionSide': 'LONG' if side == 'BUY' else 'SHORT'}]
    return t, ex


def _last_record(tmp):
    stats = json.load(open(os.path.join(tmp, 'trade_stats.json'),
                           encoding='utf-8'))
    return stats['trades'][-1], stats['trades']


# E1：入场费只扣一次 + 净成本均价（finalizer 端到端，读实际落盘）
def e1_fee_counted_once_net_cost_basis():
    tmp = tempfile.mkdtemp(prefix='v2a_e1_')
    t, ex = _mk_batch(tmp)
    ok, msg = t._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok, msg
    rec, trades = _last_record(tmp)
    assert rec['record_type'] == 'settlement', rec
    assert rec['pnl_status'] == 'ESTIMATED' and rec['fee_status'] == 'ESTIMATED'
    assert rec['quantity_status'] == 'PROVEN' and rec['cost_basis_status'] == 'PROVEN'
    assert abs(rec['avg_price'] - 76885.20) < 0.01, f'净成本均价: {rec}'
    # gross = (77885.2-76885.2)*0.002 = 2.0
    # net = 2.0 - entry_est(0.077) - exit_est(0.002*77885.2*0.0002=0.03115)
    assert abs(rec['net_pnl'] - (2.0 - 0.077 - 0.03115408)) < 1e-4, f'入场费只扣一次（旧双扣公式 1.8688 不得出现）: {rec}'

    assert abs(rec['entry_fee'] - 0.077) < 1e-6, rec
    assert abs(rec['exit_fee'] - 0.03115408) < 1e-6, rec
    # schema_activation 原子注入且位于 settlement 之前
    acts = [i for i, r in enumerate(trades)
            if r.get('dedup_key') == 'schema_activation:v2']
    assert len(acts) == 1, trades
    assert acts[0] < len(trades) - 1, 'activation 必须位于新 settlement 之前'


# E2：BUY 与 SELL 公式镜像
def e2_buy_sell_mirror():
    tmp1 = tempfile.mkdtemp(prefix='v2a_e2b_')
    t1, ex1 = _mk_batch(tmp1, side='BUY')
    ok1, _ = t1._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok1
    rec_b = _last_record(tmp1)[0]

    tmp2 = tempfile.mkdtemp(prefix='v2a_e2s_')
    t2, ex2 = _mk_batch(tmp2, side='SELL')
    ok2, m2 = t2._finalize_limit_full_fill(SYM, p5.BID, 'L1')
    assert ok2, m2
    rec_s = _last_record(tmp2)[0]

    # 镜像：同价格下 BUY 盈利 2.0、SELL 亏损 2.0（gross 符号相反，费用相同）
    assert abs(rec_b['net_pnl'] - 1.89184592) < 1e-4, rec_b
    assert abs(rec_s['net_pnl'] - (-2.10815408)) < 1e-4, rec_s

    assert rec_b['side'] == 'BUY' and rec_s['side'] == 'SELL'


# E3：限价 Maker / SL·TP·市价 Taker fallback（AST：四路径费率常量）
def e3_rate_fallbacks():
    assert 'MAKER_FEE_RATE' in SRC and 'TAKER_FEE_RATE' in SRC
    # 限价平仓 finalizer 用 Maker；SL/TP/市价用 Taker（结构断言）
    fin = SRC.split('def _finalize_limit_full_fill')[1].split('def ')[0]
    assert 'MAKER_FEE_RATE' in fin, '限价平仓 fallback 必须 Maker'
    mon = SRC.split('def _start_monitoring')[1].split('def _')[0]
    assert mon.count('TAKER_FEE_RATE') >= 2, 'SL/TP fallback 必须 Taker'


# E4：结算路径零新增 fee API（v2B 才引入 reconciler）
def e4_no_fee_api_in_hot_path():
    assert '_resolve_order_fees' not in SRC, 'v2A 不得引入手续费 API 查询（归 v2B 异步对账）'

    assert 'fetch_my_trades' not in SRC, '结算热路径零 userTrades 查询'


# E5：数量双证据一致 → PROVEN settlement
def e5_proven_quantity():
    tmp = tempfile.mkdtemp(prefix='v2a_e5_')
    t, ex = p5.make_trader(tmp)
    t.send_tg_notification = lambda *a, **k: None
    sf = os.path.join(tmp, 'trade_stats.json')
    ok = t._record_realized_pnl(
        'bE', SYM, 'BUY', 0.001, 76885.20, 77885.20, 0.9, '市价平仓',
        dedup_key=f'{SYM}:M1', stats_file=sf,
        expected_qty=0.001, observed_qty=0.001,
        entry_notional=76.8852, allocation_status='PROVEN',
        entry_order_refs=['A1'], exit_order_ref={'order_id': 'M1'})
    assert ok
    rec = json.load(open(sf, encoding='utf-8'))['trades'][-1]
    assert rec['record_type'] == 'settlement' and rec['pnl_status'] == 'ESTIMATED'
    assert rec['quantity_status'] == 'PROVEN' and abs(rec['amount'] - 0.001) < 1e-9
    assert rec['fee_risk_basis']['entry_notional'] == 76.8852
    assert rec['exit_order_ref'] == {'order_id': 'M1'}


# E6：expected/observed 不一致 → DISPUTED，无权威 net_pnl
def e6_mismatch_disputed():
    tmp = tempfile.mkdtemp(prefix='v2a_e6_')
    t, ex = p5.make_trader(tmp)
    t.send_tg_notification = lambda *a, **k: None
    sf = os.path.join(tmp, 'trade_stats.json')
    ok = t._record_realized_pnl(
        'bD', SYM, 'BUY', 0.001, 76885.20, 77885.20, 0.9, '市价平仓',
        dedup_key=f'{SYM}:M2', stats_file=sf,
        expected_qty=0.001, observed_qty=0.0008,
        entry_notional=76.8852, allocation_status='PROVEN',
        entry_order_refs=['A1'], exit_order_ref={'order_id': 'M2'})
    assert ok
    rec = json.load(open(sf, encoding='utf-8'))['trades'][-1]
    assert rec['record_type'] == 'settlement_dispute' and rec['pnl_status'] == 'DISPUTED', rec
    assert rec['reason'] == 'qty_mismatch' and rec.get('net_pnl') is None
    assert 'net_pnl_estimate' in rec


# E7：四条生产记录点均传显式 side/dedup/expected/observed/引用（结构断言）
def e7_four_call_sites_wired():
    import re
    n_expected = len(re.findall(r'expected_qty=', SRC))
    assert n_expected >= 5, f'writer 签名 + 四调用点（实际 {n_expected}）'
    n_obs = len(re.findall(r'observed_qty=', SRC))
    assert n_obs >= 5, f'四调用点 + writer（实际 {n_obs}）'
    n_refs = len(re.findall(r'entry_order_refs=', SRC))
    assert n_refs >= 5, f'四调用点 + writer（实际 {n_refs}）'
    # 市价/限价/SL/TP 的 dedup 均延续 <symbol>:<order_id> 格式
    assert "dedup_key=f'{symbol}:{order_id}'" in SRC
    assert "dedup_key=f'{symbol}:{current_sl_id}'" in SRC
    assert "dedup_key=f'{symbol}:{tp_order_id}'" in SRC
    assert "dedup_key=f'{target_symbol}:{close_order_id}'" in SRC


# E8：升级连续性——旧 dedup 键记录 + 同键重试 → 零新增
def e8_upgrade_dedup_continuity():
    tmp = tempfile.mkdtemp(prefix='v2a_e8_')
    t, ex = p5.make_trader(tmp)
    t.send_tg_notification = lambda *a, **k: None
    sf = os.path.join(tmp, 'trade_stats.json')
    # 升级前的旧格式记录（无 v2 字段）
    json.dump({'trades': [{'time': '2026-09-03 10:00:00', 'batch_id': 'old',
                           'symbol': SYM, 'side': 'BUY', 'amount': 0.002,
                           'avg_price': 76885.2, 'exit_price': 77885.2,
                           'net_pnl': 1.0, 'mode': '市价平仓',
                           'dedup_key': f'{SYM}:L1'}]},
              open(sf, 'w', encoding='utf-8'), ensure_ascii=False)
    before = len(json.load(open(sf, encoding='utf-8'))['trades'])
    ok = t._record_realized_pnl(
        'old', SYM, 'BUY', 0.002, 76885.20, 77885.20, 1.0, '市价平仓',
        dedup_key=f'{SYM}:L1', stats_file=sf,
        expected_qty=0.002, observed_qty=0.002,
        entry_notional=153.77, allocation_status='PROVEN',
        entry_order_refs=['A1'], exit_order_ref={'order_id': 'L1'})
    assert ok
    trades = json.load(open(sf, encoding='utf-8'))['trades']
    assert len(trades) == before, f'升级后同键重试不得新增: {len(trades)}'
    # 新订单写入 → activation 才注入
    ok2 = t._record_realized_pnl(
        'bNew', SYM, 'BUY', 0.001, 76885.20, 77885.20, 0.9, '市价平仓',
        dedup_key=f'{SYM}:M9', stats_file=sf,
        expected_qty=0.001, observed_qty=0.001,
        entry_notional=76.88, allocation_status='PROVEN',
        entry_order_refs=['A1'], exit_order_ref={'order_id': 'M9'})
    assert ok2
    trades2 = json.load(open(sf, encoding='utf-8'))['trades']
    acts = [r for r in trades2 if r.get('dedup_key') == 'schema_activation:v2']
    assert len(acts) == 1 and acts[0].get('legacy_count') == before


# E9：毛量不得通过数量证明（expected=毛量、observed=净量 → DISPUTED）
def e9_gross_qty_cannot_pass_as_proven():
    tmp = tempfile.mkdtemp(prefix='v2a_e9_')
    t, ex = p5.make_trader(tmp)
    t.send_tg_notification = lambda *a, **k: None
    sf = os.path.join(tmp, 'trade_stats.json')
    ok = t._record_realized_pnl(
        'bG', SYM, 'BUY', 0.001, 76885.20, 77885.20, 0.9, '市价平仓',
        dedup_key=f'{SYM}:G1', stats_file=sf,
        expected_qty=0.002, observed_qty=0.001,     # 毛量 vs 实际
        entry_notional=153.77, allocation_status='PROVEN',
        entry_order_refs=['A1'], exit_order_ref={'order_id': 'G1'})
    assert ok
    rec = json.load(open(sf, encoding='utf-8'))['trades'][-1]
    assert rec['pnl_status'] == 'DISPUTED' and rec['reason'] == 'qty_mismatch'
    assert rec.get('quantity_status') != 'PROVEN'


TESTS = [e1_fee_counted_once_net_cost_basis,
         e2_buy_sell_mirror,
         e3_rate_fallbacks,
         e4_no_fee_api_in_hot_path,
         e5_proven_quantity,
         e6_mismatch_disputed,
         e7_four_call_sites_wired,
         e8_upgrade_dedup_continuity,
         e9_gross_qty_cannot_pass_as_proven]


def main():
    fails = []
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
        except AssertionError as e:
            fails.append(fn.__name__)
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            fails.append(fn.__name__)
            print(f'💥 {fn.__name__}: {type(e).__name__}: {e}')
    print(f'GREEN: {len(TESTS) - len(fails)}/{len(TESTS)}')
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
