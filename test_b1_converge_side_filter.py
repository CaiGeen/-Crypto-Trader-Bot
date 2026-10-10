# -*- coding: utf-8 -*-
"""D-B1 方向感知贡献扣减 P0 负测（离线、零网络、不碰生产账本）。

背景（ChatGPT 2026-09-27 复审；本机 `2b4cc17` 源码逐行核验）
================================================================
`_converge_batch_orders_before_clear`（trader L12239-12447）做 D-B1 持仓归属时：

    L12278  _side = b_data.get('side') or 'BUY'                      ← 目标方向
    L12280  pos_amt = _get_current_position_amt(..., side=_side)      ← 只读【目标方向】仓位
    L12287  for _bid, _bd in all_states[symbol].items():             ← 遍历同 symbol 【全部】批次
    L12291      if _bid != batch_id and _bd.get('is_active'):        ← 未过滤 side   ❌
    L12305  _contribution = pos_amt - _others_filled                  ← 口径不一致
    L12314  _position_zero = (_contribution <= 0) or (abs(...) <= tol)
    L12321+ 进入 L1/L2/L3 撤单（L12335 _converge_cancel_order，可能含 SL）

对冲模式：LONG 仓位 1.0 + 同 symbol SHORT 批次账面 1.0
  → _contribution = 1 - 1 = 0 → 误判「本批次已无贡献」
  → 撤掉 LONG 自己的订单（含止损）→ L3449 拿到 proof → L3450 clear_batch_state 成功
  → 交易所 LONG 仓位仍在 = **裸仓 + 无止损 + 账本已删**

可达性（本机核验，非推测）
----------------------------------------------------------------
    W1 监视线 L9743  写 b_data['monitor_error'] = True
  → recover_active_batches L3236 读到该标记
  → L3279-3282 未接管 → stale_batches.append((symbol, batch_id))
  → L3448 for symbol, batch_id in stale_batches
  → L3449 _converge_batch_orders_before_clear(...)
  （L3447 硬编码注释：G-B7 —— monitor_error 批次也必须先跑 converge 再后续处理）

期望行为（本文件断言「修复后」，当前实现下应为 RED）
----------------------------------------------------------------
  T1  LONG 有仓 + 同 symbol SHORT 有账面成交量 → 拒绝清理、cancel_order=0、SL 仍在
  T2  方向互换（SHORT 有仓）                    → 同上
  T3  同方向多批次（防过修）                    → 贡献扣减正确、仍可收敛
  T4  其他活跃批次方向不可判定                  → Fail-Closed 拒绝清理
  T5  可达性钉住：恢复链确实把 monitor_error 批次送进 converge（AST）
  T99 生产文件免疫快照（内容 + mtime_ns）

运行：.venv/Scripts/python.exe test_b1_converge_side_filter.py
预期：修复后 GREEN、退出码 0；任何 FAIL = 实现回退，退出码 1。
基建惯例：沿用 test_p5_closecancel.py 的 Ex / make_trader / _state_write。
"""
import ast
import hashlib
import json
import os
import tempfile
import threading
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYM = 'BTCUSDT'
LONG_B = 'batch_LONG'
SHORT_B = 'batch_SHORT'
SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'trader_260725.py')

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), str(detail)))
    print("  [%s] %s" % ('PASS' if passed else 'FAIL', name)
          + (("\n        → " + str(detail)) if detail else ""))
    return bool(passed)


# ────────────────── 生产文件免疫快照（模块导入时采集）──────────────────

_PROD_FILES = ['trade_state.json', 'trade_tombstones.json', 'trade_stats.json',
               'auth_blocked.json', 'signal.json', 'signal_dedup.json']
_PROD_DIR = os.path.dirname(os.path.abspath(__file__))


def _prod_snapshot():
    snap = {}
    for _n in _PROD_FILES:
        _p = os.path.join(_PROD_DIR, _n)
        try:
            with open(_p, 'rb') as _f:
                _d = _f.read()
            snap[_n] = (hashlib.sha256(_d).hexdigest(), len(_d),
                        os.stat(_p).st_mtime_ns)
        except FileNotFoundError:
            snap[_n] = ('<missing>', 0, 0)
    return snap


_PROD_SNAP_AT_IMPORT = _prod_snapshot()


# ────────────────── 夹具 ──────────────────

class Ex:
    """可编程交易所桩：只记录撤单/下单，绝不联网。"""

    def __init__(self):
        self.last_response_headers = {}
        self.markets = {}
        self.orders = {}
        self.cancel_calls = []
        self.create_calls = []
        self.positions = []
        self.open_orders = []

    def _mk(self, oid, otype='STOP_MARKET', amount=1.0, stop=75001.0,
            status='open', filled=0.0, avg=None):
        o = {'id': oid, 'symbol': SYM, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'stopPrice': stop, 'side': 'sell',
             'average': avg if avg is not None else stop, 'price': stop}
        self.orders[oid] = o
        return o

    def fetch_order(self, oid, symbol=None, params=None, **k):
        o = self.orders.get(oid)
        if o is None:
            raise Exception('binanceusdm -2011 Unknown order')
        return dict(o)

    def cancel_order(self, oid, symbol=None, params=None, **k):
        self.cancel_calls.append(oid)
        o = self.orders.get(oid)
        if o is None:
            raise Exception('binanceusdm -2011 Unknown order')
        if str(o.get('status') or '').lower() in ('closed', 'filled'):
            raise Exception('binanceusdm -2011 Unknown order (already filled)')
        o['status'] = 'canceled'
        return {'id': oid}

    def amount_to_precision(self, symbol, amount):
        return amount

    def price_to_precision(self, symbol, price):
        return price

    def create_order(self, symbol, otype, side, amount, price=None,
                     params=None, **k):
        nid = 'N%d' % (len(self.create_calls) + 1)
        self.create_calls.append((otype, side, round(float(amount), 6)))
        stop = float((params or {}).get('stopPrice') or 0)
        self._mk(nid, otype=otype, amount=float(amount), stop=stop)
        return {'id': nid}

    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_open_orders(self, symbol=None, params=None, **k):
        # 真实交易所语义：已撤/已成交订单不再出现在未结列表里。
        # 若返回静态列表，误撤后的二次扫描会把撤掉的单仍算作"遗留"，
        # 从而掩盖 converge 最终产出 proof → clear 成功的完整杀伤链。
        return [o for o in self.open_orders
                if str(o.get('status') or 'open').lower() == 'open']

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
    """把所有可写状态重定向到 tmp（沿 p5 惯例，含 P5k 墓碑/盈亏隔离）。"""
    tmp = str(tmp)
    trader_260725.STATE_FILE = os.path.join(tmp, 'trade_state.json')
    trader_260725.AUTH_BLOCKED_FILE = os.path.join(tmp, 'auth_blocked.json')
    trader_260725.NOTIFY_QUEUE_DIR_TRADER = os.path.join(tmp, '.notify_queue')
    trader_260725.TOMBSTONE_FILE = os.path.join(tmp, 'trade_tombstones.json')
    ex = Ex()
    with mock.patch.object(CryptoTrader, '_daily_report_loop',
                           lambda self: None):
        with mock.patch.object(trader_260725.ccxt, 'binanceusdm') as mk:
            mk.return_value = ex
            t = CryptoTrader('k', 's')
    _stats = os.path.join(tmp, 'trade_stats.json')
    _real = t._record_realized_pnl

    def _iso(*a, **k):
        k.setdefault('stats_file', _stats)
        return _real(*a, **k)

    t._record_realized_pnl = _iso
    t._min_api_interval = 0
    t.ip_file = os.path.join(tmp, 'last_ip.txt')
    t.sent_tg = []
    t.send_tg_notification = lambda text, **k: t.sent_tg.append(str(text))
    return t, ex


def _state_write(t, states):
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(states, f, ensure_ascii=False)


def _state_read():
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


# ────────────────── 批次工厂 ──────────────────

def _pos(side, amt=1.0):
    """对冲模式持仓行；side ∈ long/short。"""
    return {'symbol': SYM, 'contracts': amt, 'positionAmt': amt,
            'side': side, 'positionSide': side.upper()}


def _long_batch(sl_id='SL_LONG', filled=1.0):
    return {
        'is_active': True, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'monitor_error': True,
        'entry_orders': ['E_LONG'], 'target_amounts': [filled],
        'filled_details': [76620.0], 'last_filled_count': 1,
        'total_entry_fee': 0.15, 'current_sl_id': sl_id,
        'take_profit_price': 80000.0,
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'realized_reduce_amount': 0.0, 'realized_reduce_cost': 0.0,
        'batch_total_amount': filled,
        'protection_registry': {
            '%s|SL|L0|LONG' % LONG_B: {
                'state': 'CONFIRMED', 'order_id': sl_id,
                'intent': {'qty': filled, 'stop_price': '75001.0'}},
        },
    }


def _short_batch(filled=1.0, with_side=True):
    b = {
        'is_active': True, 'symbol': SYM,
        'is_hedge_mode': True,
        'entry_orders': ['E_SHORT'], 'target_amounts': [filled],
        'filled_details': [77000.0], 'last_filled_count': 1,
        'total_entry_fee': 0.15, 'current_sl_id': 'SL_SHORT',
        'params_base': {'positionSide': 'SHORT', 'leverage': 100},
        'realized_reduce_amount': 0.0, 'realized_reduce_cost': 0.0,
        'batch_total_amount': filled,
        'protection_registry': {
            '%s|SL|L0|SHORT' % SHORT_B: {
                'state': 'CONFIRMED', 'order_id': 'SL_SHORT',
                'intent': {'qty': filled, 'stop_price': '78000.0'}},
        },
    }
    if with_side:
        b['side'] = 'SELL'
    return b


def _setup_dual(target_side='BUY'):
    """同 symbol 双向批次：目标批次有真实仓位，对向批次有等额账面成交量。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='b1side_'))
    if target_side == 'BUY':
        target, other = LONG_B, SHORT_B
        tgt_b, oth_b = _long_batch(), _short_batch()
        ex.positions = [_pos('long', 1.0), _pos('short', 1.0)]
        target_sl = 'SL_LONG'
    else:
        target, other = SHORT_B, LONG_B
        tgt_b, oth_b = _short_batch(), _long_batch()
        ex.positions = [_pos('long', 1.0), _pos('short', 1.0)]
        target_sl = 'SL_SHORT'
    _state_write(t, {SYM: {target: tgt_b, other: oth_b}})
    # 交易所只挂着【目标批次】自己的订单（含保护单）
    ex._mk(target_sl, otype='STOP_MARKET', amount=1.0, stop=75001.0)
    ex._mk('E_' + target, otype='LIMIT', amount=1.0, stop=76620.0)
    ex.open_orders = [ex.orders[target_sl], ex.orders['E_' + target]]
    return t, ex, target, target_sl


# ────────────────── T1 / T2 双向误判 ──────────────────

def t1_long_refuses_clear_when_short_on_same_symbol():
    t, ex, target, sl = _setup_dual('BUY')
    proof = t._converge_batch_orders_before_clear(SYM, target)
    ok = check('T1 LONG 批次不得因 SHORT 批次账面量而被判 position_zero',
               ex.cancel_calls == [],
               'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    check('T1 未产出 clear proof（删除门拿不到凭据）',
          proof is None, 'proof=%r' % (proof,))
    check('T1 LONG 止损单仍存活', ex.orders[sl]['status'] == 'open',
          'SL 状态=%s' % ex.orders[sl]['status'])
    return ok


def t2_short_refuses_clear_when_long_on_same_symbol():
    t, ex, target, sl = _setup_dual('SELL')
    proof = t._converge_batch_orders_before_clear(SYM, target)
    ok = check('T2 方向互换：SHORT 批次同样拒绝误清理',
               ex.cancel_calls == [],
               'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    check('T2 SHORT 止损单仍存活', ex.orders[sl]['status'] == 'open',
          'SL 状态=%s' % ex.orders[sl]['status'])
    return ok


# ────────────────── T3 同方向多批次（防过修）──────────────────

def t3_same_direction_multi_batch_still_converges():
    """同为 LONG：目标批次真实仓位 1.0，另一同向批次账面 1.0
    → contribution = 0 是【正确】结论，必须仍允许收敛（不能被新规则误拦）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='b1same_'))
    a = _long_batch(sl_id='SL_A')
    a['monitor_error'] = False
    b = _long_batch(sl_id='SL_B')
    b['entry_orders'] = ['E_B']
    b['monitor_error'] = False
    _state_write(t, {SYM: {'batch_A': a, 'batch_B': b}})
    ex.positions = [_pos('long', 1.0)]
    # U2: the target's historical ENTRY is booked and has authoritative terminal quantity.
    ex._mk('E_LONG', otype='LIMIT', amount=1.0, status='closed', filled=1.0)
    ex._mk('SL_A', otype='STOP_MARKET', amount=1.0, stop=75001.0)
    ex._mk('E_batch_A', otype='LIMIT', amount=1.0, stop=76620.0)
    ex.open_orders = [ex.orders['SL_A'], ex.orders['E_batch_A']]
    proof = t._converge_batch_orders_before_clear(SYM, 'batch_A')
    ok = check('T3 同方向多批次仍能进入收敛（修复不许过头）',
               len(ex.cancel_calls) > 0,
               'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    return ok


# ────────────────── T4 方向不可判定 → Fail-Closed ──────────────────

def t4_unknown_direction_of_other_batch_refuses():
    """对向批次缺 side → 贡献无法归属，必须拒绝清理而不是猜。"""
    t, ex, target, sl = _setup_dual('BUY')
    st = _state_read()
    st[SYM][SHORT_B].pop('side', None)
    _state_write(t, st)
    proof = t._converge_batch_orders_before_clear(SYM, target)
    ok = check('T4 其他活跃批次方向不可判定 → 拒绝清理（Fail-Closed）',
               ex.cancel_calls == [] and proof is None,
               'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    check('T4 保护单仍存活', ex.orders[sl]['status'] == 'open',
          'SL 状态=%s' % ex.orders[sl]['status'])
    return ok


def t4b_unknown_direction_of_target_batch_refuses():
    """目标批次自己缺 side：对向批次存在时同样必须拒绝。"""
    t, ex, target, sl = _setup_dual('BUY')
    st = _state_read()
    st[SYM][target].pop('side', None)
    _state_write(t, st)
    proof = t._converge_batch_orders_before_clear(SYM, target)
    ok = check('T4b 目标批次方向不可判定 + 存在其他批次 → 拒绝清理',
               ex.cancel_calls == [] and proof is None,
               'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    return ok


def t4c_single_batch_without_side_keeps_legacy_behavior():
    """单批次且无 side：无其他批次可混淆 → 保持既有行为（回归护栏）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='b1one_'))
    b = _long_batch(sl_id='SL_ONE')
    b.pop('side', None)
    b['monitor_error'] = False
    _state_write(t, {SYM: {'batch_ONE': b}})
    ex.positions = [_pos('long', 1.0)]
    ex._mk('SL_ONE', otype='STOP_MARKET', amount=1.0, stop=75001.0)
    ex._mk('E_batch_ONE', otype='LIMIT', amount=1.0, stop=76620.0)
    ex.open_orders = [ex.orders['SL_ONE'], ex.orders['E_batch_ONE']]
    proof = t._converge_batch_orders_before_clear(SYM, 'batch_ONE')
    # 单批次下 contribution = 1.0 > 0 → 正常拒绝（position_residual），
    # 但【不得】因“方向不可判定”这条新规则而额外改变路径。
    check('T4c 单批次无 side 不被新规则误伤（仍走持仓残留分支）',
          ex.cancel_calls == [] and proof is None,
          'cancel_calls=%s proof=%r' % (ex.cancel_calls, proof))
    return True


# ────────────────── T5 可达性钉住（AST）──────────────────

def t5_recovery_routes_monitor_error_into_converge():
    with open(SRC_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    lines = src.splitlines()
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == 'recover_active_batches':
            fn = n
            break
    if fn is None:
        return check('T5 恢复链函数存在', False, 'recover_active_batches 未找到')

    has_converge = False
    has_stale_append_after_me = False
    me_line = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == '_converge_batch_orders_before_clear':
            has_converge = True
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == 'append':
            # stale_batches.append(...)
            try:
                if isinstance(n.func.value, ast.Name) \
                        and n.func.value.id == 'stale_batches':
                    if me_line is not None and n.lineno > me_line:
                        has_stale_append_after_me = True
            except Exception:
                pass
        if isinstance(n, ast.If):
            # `if b_data.get('monitor_error', False):` 的 test 是 Call，不是 Compare，
            # 必须按 If 节点取 test 再判关键词（否则判定行恒为 None → 假 FAIL）。
            try:
                seg = ast.unparse(n.test)
            except Exception:
                seg = ''
            if 'monitor_error' in seg:
                me_line = n.lineno

    a = check('T5 恢复链确会调用 _converge_batch_orders_before_clear',
              has_converge, '在 recover_active_batches 内')
    b = check('T5 monitor_error 判定之后存在 stale_batches.append',
              has_stale_append_after_me,
              'monitor_error 判定行=%s；可达性=monitor_error→stale→converge'
              % me_line)
    return a and b


# ────────────────── T99 生产免疫 ──────────────────

def t99_production_files_untouched():
    after = _prod_snapshot()
    diff = [k for k in _PROD_SNAP_AT_IMPORT
            if after.get(k) != _PROD_SNAP_AT_IMPORT[k]]
    ok = check('T99 生产账本/墓碑/统计/鉴权态零变化（内容+mtime_ns）',
               not diff, 'diff=%s' % diff)
    # 确认写的是临时目录而非生产
    in_prod = os.path.abspath(trader_260725.STATE_FILE).startswith(_PROD_DIR + os.sep)
    check('T99 STATE_FILE 已重定向到临时目录',
          not in_prod, 'STATE_FILE=%s' % trader_260725.STATE_FILE)
    return ok


def main():
    print('=' * 68)
    print('D-B1 方向感知贡献扣减 P0 负测（离线）')
    print('=' * 68)
    for fn in (t1_long_refuses_clear_when_short_on_same_symbol,
               t2_short_refuses_clear_when_long_on_same_symbol,
               t3_same_direction_multi_batch_still_converges,
               t4_unknown_direction_of_other_batch_refuses,
               t4b_unknown_direction_of_target_batch_refuses,
               t4c_single_batch_without_side_keeps_legacy_behavior,
               t5_recovery_routes_monitor_error_into_converge,
               t99_production_files_untouched):
        print('\n---- %s ----' % fn.__name__)
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(fn.__name__ + ' 异常', False, repr(e))

    npass = sum(1 for _, p, _ in RESULTS if p)
    print('\n' + '=' * 68)
    print('GREEN: %d/%d' % (npass, len(RESULTS)))
    print('=' * 68)
    for n, p, d in RESULTS:
        if not p:
            print('  FAIL: %s  %s' % (n, d))
    return 0 if npass == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
