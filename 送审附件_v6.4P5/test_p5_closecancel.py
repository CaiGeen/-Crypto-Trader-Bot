# -*- coding: utf-8 -*-
"""P5 /closecancel RED-first 测试（v3.1 冻结规格，ChatGPT FULLY ALIGNED）。

R1 资格矩阵 / R2 PURE_CANCEL 全链 / R3 PARTIAL 归属+恢复 / R4 FULL_FILL finalizer
（monitor 死亡仍收敛）/ R5 守恒门失败不恢复 / R6 幂等 / R7 crash 自动续跑 /
R8 monitor 自身发现 canceled 闭环（本源缺陷）/ R9 并发单一提交单一 PnL /
R10 归属不双计 / R11 恢复后 flag 干净（结算不被吞）/ R12 PnL dedup 单元
"""
import json
import os
import tempfile
import threading
import time
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYM = 'BTCUSDT'
BID = 'batch_A'
OP = 'OP1'


# ────────────────── harness ──────────────────

class Ex:
    """可编程交易所桩：fetch_order 按 orders 表派发；cancel/create 带计数。"""

    def __init__(self):
        self.last_response_headers = {}
        self.orders = {}
        self.cancel_calls = []
        self.create_calls = []
        self.positions = []
        self.open_orders = []

    def _mk(self, oid, otype='STOP_MARKET', amount=0.002, stop=75001.0,
            status='open', filled=0.0, avg=None, _gone=False):
        o = {'id': oid, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'stopPrice': stop, 'side': 'sell',
             'average': avg if avg is not None else stop, 'price': stop}
        if _gone:
            o['_gone'] = True
        self.orders[oid] = o
        return o

    def fetch_order(self, oid, symbol=None, params=None, **k):
        o = self.orders.get(oid)
        if o is None or o.pop('_gone', False):
            raise Exception('binanceusdm OrderNotFound -2011 Unknown order')
        return dict(o)

    def cancel_order(self, oid, symbol=None, params=None, **k):
        self.cancel_calls.append(oid)
        o = self.orders.get(oid)
        if o is None or o.pop('_gone', False):
            raise Exception('binanceusdm -2011 Unknown order')
        # Binance 语义：已成交(closed/filled)的订单不可撤
        if str(o.get('status') or '').lower() in ('closed', 'filled'):
            raise Exception('binanceusdm -2011 Unknown order (order already filled)')
        o['status'] = 'canceled'
        return {'id': oid}

    def create_order(self, symbol, otype, side, amount, price=None, params=None, **k):
        nid = f'N{len(self.create_calls) + 1}'
        self.create_calls.append((otype, side, round(float(amount), 6)))
        stop = float((params or {}).get('stopPrice') or 0)
        self._mk(nid, otype=otype, amount=float(amount), stop=stop)
        return {'id': nid}

    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_open_orders(self, symbol=None, params=None, **k):
        return self.open_orders

    def fetch_balance(self):
        return {'USDT': {'total': 16000}}

    def set_leverage(self, *a, **k):
        return {}

    def load_time_difference(self):
        return True

    def load_markets(self, *a, **k):
        return {}

    def fetch_time(self):
        return 1234567890


def make_trader(tmp):
    state_file = os.path.join(str(tmp), 'trade_state.json')
    trader_260725.STATE_FILE = state_file
    trader_260725.AUTH_BLOCKED_FILE = os.path.join(str(tmp), 'auth_blocked.json')
    trader_260725.NOTIFY_QUEUE_DIR_TRADER = os.path.join(str(tmp), '.notify_queue')
    ex = Ex()
    with mock.patch.object(CryptoTrader, '_daily_report_loop', lambda self: None):
        with mock.patch.object(trader_260725.ccxt, 'binanceusdm') as mk:
            mk.return_value = ex
            t = CryptoTrader('k', 's')
    t._min_api_interval = 0
    t.ip_file = os.path.join(str(tmp), 'last_ip.txt')
    t.sent_tg = []
    t.send_tg_notification = lambda text, **k: t.sent_tg.append(str(text))
    return t, ex


def _state_write(t, states):
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(states, f, ensure_ascii=False)


def _state_read(t):
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _lp_batch(net=0.002, op=OP, lfo='L1', reason='limit_pending_normal',
              settled=False, entry_price=76620.0):
    """limit_pending_normal 冻结态批次（close 事务在途、限价单已挂）。"""
    gross = net  # 单层：gross == net（无 partial 历史）
    return {
        'is_active': True, 'symbol': SYM, 'side': 'BUY', 'is_hedge_mode': True,
        'close_phase': 1, 'pending_close': True, 'is_programmatic_cancel': True,
        'close_op_id': op, 'close_reason': reason,
        'limit_close_order_id': lfo, 'limit_close_price': 76500.0,
        'limit_close_mode': 'best',
        'entry_orders': ['E1'], 'target_amounts': [net], 'filled_details': [entry_price],
        'last_filled_count': 1, 'total_entry_fee': 0.15,
        'stop_steps': [75001.0], 'take_profit_price': 80000.0,
        'current_sl_id': 'S1', 'tp_order_id': 'T1',
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'realized_reduce_amount': 0.0, 'realized_reduce_cost': 0.0,
        'batch_total_amount': net,
        'settled_by_limit_close': settled,
        'protection_registry': {
            f'{BID}|SL|L0|LONG': {'state': 'CONFIRMED', 'order_id': 'S1',
                                  'intent': {'qty': net, 'stop_price': '75001.0'}},
            f'{BID}|TP|L0|LONG': {'state': 'PROGRAMMATIC_CANCELED', 'order_id': 'T1',
                                  'terminated_reason': 'close_requested_canceled',
                                  'intent': {'qty': net, 'stop_price': '80000.0'}},
        },
    }


def _single(b):
    return {SYM: {BID: b}}


# ────────────────── R1 资格矩阵 ──────────────────

def r1_eligibility_matrix():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    # a) ACTIVE（reason=''）
    b = _lp_batch(reason='')
    b['close_phase'] = 0
    b['pending_close'] = False
    _state_write(t, _single(b))
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok and 'no_close_inflight' in msg, (ok, msg)
    # b) partial_resize_pending（不属于 P5 撤销范围）
    b2 = _lp_batch(reason='partial_resize_pending')
    _state_write(t, _single(b2))
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok and 'not_cancellable' in msg, (ok, msg)
    # c) 已结算
    b3 = _lp_batch(settled=True)
    _state_write(t, _single(b3))
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok and ('already_settled' in msg or 'not_cancellable' in msg), (ok, msg)
    # d) 缺 limit_close_order_id
    b4 = _lp_batch()
    b4.pop('limit_close_order_id')
    _state_write(t, _single(b4))
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok, (ok, msg)
    # e) 批次缺失
    _state_write(t, {SYM: {}})
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok and 'batch_missing' in msg, (ok, msg)


# ────────────────── R2 PURE_CANCEL 全链 ──────────────────

def r2_pure_cancel_full_restore():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)     # SL 在场
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)                              # TP 已被 close 撤
    ex._mk('L1', otype='LIMIT', amount=0.002, status='canceled', filled=0.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.002, 'side': 'long',
                     'positionSide': 'LONG'}]
    ok, msg = t._submit_closecancel(SYM, BID)
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    assert b2['close_phase'] == 0 and b2['pending_close'] is False, b2
    assert b2['is_programmatic_cancel'] is False, b2  # R11 前置：flag 干净
    assert b2['close_reason'] == '', b2
    assert 'limit_close_order_id' not in b2 and 'limit_close_price' not in b2, b2
    assert b2['close_op_id'] == OP, 'close_op_id 保留审计'
    assert b2['tp_order_id'] == 'N1', b2  # TP 重挂新代
    assert b2['current_sl_id'] == 'S1', '匹配 SL 收编不重建'
    assert b2['realized_reduce_amount'] == 0.0, 'PURE_CANCEL 无归属量'
    reg = b2['protection_registry']
    assert reg[f'{BID}|TP|L0|LONG']['state'] == 'CONFIRMED', reg
    assert reg[f'{BID}|TP|L0|LONG']['order_id'] == 'N1', reg
    assert reg[f'{BID}|SL|L0|LONG']['state'] == 'CONFIRMED', reg
    assert any(a.get('identity', '').endswith('|TP|L0|LONG') for a in b2.get('rearm_audit', [])), \
        're-arm 必须留审计'
    # R6：恢复后重复命令 → 拒绝
    ok2, msg2 = t._submit_closecancel(SYM, BID)
    assert not ok2 and 'no_close_inflight' in msg2, (ok2, msg2)


# ────────────────── R3 PARTIAL 归属 + 恢复 ──────────────────

def r3_partial_fill_attribution_and_restore():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='canceled', filled=0.001,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.001, 'side': 'long',
                     'positionSide': 'LONG'}]
    ok, msg = t._submit_closecancel(SYM, BID)
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    # 归属：durable 净账本（成本 = /partial 口径净成本比例分摊）
    assert abs(b2['realized_reduce_amount'] - 0.001) < 1e-12, b2
    pre_net_cost = 0.002 * 76620.0           # gross cost（无历史 reduce）
    expect_cost = 0.001 * pre_net_cost / 0.002
    assert abs(b2['realized_reduce_cost'] - expect_cost) < 1e-6, \
        f'成本必须按净成本比例分摊 {expect_cost}: {b2["realized_reduce_cost"]}'
    assert b2['close_phase'] == 0 and b2['pending_close'] is False, b2
    assert b2['is_programmatic_cancel'] is False, b2
    # 双腿按剩余净量 0.001
    assert ('STOP_MARKET', 'sell', 0.001) in ex.create_calls, ex.create_calls
    assert ('TAKE_PROFIT_MARKET', 'sell', 0.001) in ex.create_calls, ex.create_calls
    assert b2['current_sl_id'] == 'N1' and b2['tp_order_id'] == 'N2', b2


# ────────────────── R4 FULL_FILL finalizer ──────────────────

def r4_full_fill_finalizer_monitor_dead():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    cleared = []
    t._converge_batch_orders_before_clear = lambda s, bid: {'proof': 'FULL'}
    t.clear_batch_state = lambda s, bid, proof=None: cleared.append(bid) or True
    pnl = []

    def _rec(*a, **k):  # 模拟真函数的 dedup 契约（真行为由 R12 单测）
        if k.get('dedup_key') and any(r.get('dedup_key') == k['dedup_key']
                                      for _, r in pnl):
            return
        pnl.append((a, k))
    t._record_realized_pnl = _rec
    ok, msg = t._submit_closecancel(SYM, BID)
    assert ok, msg
    assert cleared == [BID], 'FULL_FILL 必须 converge+clear'
    assert len(pnl) == 1, f'PnL 恰好一次: {pnl}'
    # monitor 死亡场景：直接再调 finalizer（接管语义）→ PnL 不重记、不崩溃
    ok2, msg2 = t._finalize_limit_full_fill(SYM, BID, 'L1')
    assert len(pnl) == 1, f'PnL 幂等: {len(pnl)}'


# ────────────────── R5 守恒门失败 ──────────────────

def r5_guard_fail_no_restore():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='canceled', filled=0.0)
    ex.positions = []  # 实际仓位 0（SL 窗口内触发的等价形态）
    # 守卫 stub：返回 None（Fail-Closed 形态）
    t._close_amount_guard = lambda s, sd, ih, nq, bid: (None, 'stub：actual < Σnet')
    ok, msg = t._submit_closecancel(SYM, BID)
    assert not ok and ('guard' in msg or 'restore' in msg), (ok, msg)
    b2 = _state_read(t)[SYM][BID]
    assert b2['close_phase'] >= 1, '绝不恢复 ACTIVE'
    assert any('守恒' in m or 'critical' in m for m in t.sent_tg), t.sent_tg
    assert not ex.create_calls, f'守恒失败绝不得补挂: {ex.create_calls}'


# ────────────────── R7 crash 自动续跑 ──────────────────

def r7_crash_restart_auto_resume():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(reason='limit_cancel_restore_pending')  # 归属已 durable 后崩溃
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex.positions = [{'symbol': SYM, 'contracts': 0.002, 'side': 'long',
                     'positionSide': 'LONG'}]
    # 路由：_resume_partial_resize 必须把 restore_pending 路由到恢复链
    ok, msg = t._resume_partial_resize(SYM, BID, OP)
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    assert b2['close_phase'] == 0 and b2['pending_close'] is False, b2
    # 绝不再发平仓单（create_calls 只允许保护单）
    assert all(ct[0] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET') for ct in ex.create_calls), \
        ex.create_calls


# ────────────────── R8 monitor 自身发现 canceled ──────────────────

def r8_monitor_canceled_self_heal():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='canceled', filled=0.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.002, 'side': 'long',
                     'positionSide': 'LONG'}]
    # monitor 路径：不 cancel（外部已取消），直接终态裁决
    ok, msg = t._adjudicate_closed_limit_close(SYM, BID, 'L1')
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    assert b2['close_phase'] == 0 and b2['pending_close'] is False, b2
    # 结构断言：monitor canceled 分支已接线裁决器（防回归）
    src = open(r'G:\my-crypto-bot\trader_260725.py', encoding='utf-8').read()
    i = src.find("elif status == 'canceled' or status == 'expired':")
    assert i > 0
    seg = src[i:i + 1200]
    assert '_adjudicate_closed_limit_close' in seg, 'monitor canceled 分支必须接线裁决器'
    assert "pop('limit_close_order_id'" not in seg, '旧的只清字段不恢复缺陷不得回归'


# ────────────────── R9 并发单一提交单一 PnL ──────────────────

def r9_concurrent_single_commit_and_pnl():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    cleared = []
    t._converge_batch_orders_before_clear = lambda s, bid: {'proof': 'FULL'}
    t.clear_batch_state = lambda s, bid, proof=None: cleared.append(bid) or True
    pnl = []

    def _rec(*a, **k):  # dedup 契约（真行为由 R12 单测）
        if k.get('dedup_key') and any(r.get('dedup_key') == k['dedup_key']
                                      for _, r in pnl):
            return
        pnl.append((a, k))
    t._record_realized_pnl = _rec
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        try:
            results.append(t._submit_closecancel(SYM, BID))
        except Exception as e:
            results.append((False, f'exc:{e}'))
    ths = [threading.Thread(target=run) for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert len(pnl) <= 1, f'PnL 绝不双记: {pnl}'
    assert cleared.count(BID) <= 1, f'clear 幂等: {cleared}'
    assert any(r[0] for r in results if isinstance(r, tuple)), results


# ────────────────── R10 归属不双计 ──────────────────

def r10_no_double_accounting():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ok, msg = t._commit_closecancel_attribution(SYM, BID, OP, 0.001)
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    assert abs(b2['realized_reduce_amount'] - 0.001) < 1e-12
    # 同 op 重试（crash 后重入）：reason 已迁移 → CAS 拒绝，绝不双计
    ok2, msg2 = t._commit_closecancel_attribution(SYM, BID, OP, 0.001)
    assert not ok2, (ok2, msg2)
    b3 = _state_read(t)[SYM][BID]
    assert abs(b3['realized_reduce_amount'] - 0.001) < 1e-12, b3
    assert b3['close_reason'] == 'limit_cancel_restore_pending', b3


# ────────────────── R11 flag 干净 → 正常结算不被吞 ──────────────────

def r11_flags_clean_after_restore():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    b = _lp_batch(net=0.002)
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=0.002, stop=75001.0)
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=0.002, stop=80000.0,
           status='canceled', _gone=True)
    ex._mk('L1', otype='LIMIT', amount=0.002, status='canceled', filled=0.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.002, 'side': 'long',
                     'positionSide': 'LONG'}]
    ok, msg = t._submit_closecancel(SYM, BID)
    assert ok, msg
    b2 = _state_read(t)[SYM][BID]
    # L5786 归零结算分支的判据必须为 False（否则未来正常归零被吞）
    assert not (b2.get('pending_close', False) or b2.get('is_programmatic_cancel', False)), b2


# ────────────────── R12 PnL dedup 单元 ──────────────────

def r12_pnl_dedup_unit():
    t, ex = make_trader(tempfile.mkdtemp(prefix='p5_'))
    sf = os.path.join(tempfile.mkdtemp(prefix='pnl_'), 'trade_stats.json')
    t._record_realized_pnl(BID, SYM, 'BUY', 0.002, 76620.0, 76500.0, -0.3,
                           '限价平仓', dedup_key=f'{SYM}:L1', stats_file=sf)
    t._record_realized_pnl(BID, SYM, 'BUY', 0.002, 76620.0, 76500.0, -0.3,
                           '限价平仓', dedup_key=f'{SYM}:L1', stats_file=sf)
    stats = json.load(open(sf, encoding='utf-8'))
    assert len(stats['trades']) == 1, stats


TESTS = [r1_eligibility_matrix,
         r2_pure_cancel_full_restore,
         r3_partial_fill_attribution_and_restore,
         r4_full_fill_finalizer_monitor_dead,
         r5_guard_fail_no_restore,
         r7_crash_restart_auto_resume,
         r8_monitor_canceled_self_heal,
         r9_concurrent_single_commit_and_pnl,
         r10_no_double_accounting,
         r11_flags_clean_after_restore,
         r12_pnl_dedup_unit]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {str(e)[:400]}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {str(e)[:400]}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
