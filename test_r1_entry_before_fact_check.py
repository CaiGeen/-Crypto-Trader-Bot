# -*- coding: utf-8 -*-
"""R1 验收（ChatGPT R1 执行版，2026-10-07）：清账收敛阶段
「确认本批次归属 → 撤本批次入场单 → 撤后事实复核 → 确认允许清理后才撤保护单」。

针对候选 FD003693…（改前基线），只改 `_converge_batch_orders_before_clear` 一带，
生产仓不碰、不上线。逐条断言：

  R1-1 **全成交竞态**：registry order_id 未知、仅靠 intent 匹配确认归属的入场单，
       必须在**事实复核之前**撤掉，且其 id 必须进撤后成交证据集合；
       该单在撤单瞬间成交 → 拒绝 proof、保留保护单、他批次资产不动。
  R1-2 **部分成交后正常撤单**：撤单成功、事后查到部分成交 → 拒绝清账 +
       阶段顺序可断言（cancel(入场单) < fact_check < cancel(保护单) 从未发生）。
  R1-3 **归属未知**：存在未决 ENTRY 意图 + 交易所上还有无法归属的挂单 →
       拒绝 proof、保留有效保护、**不**把该意图终态化、他批/无主单不被误撤。
  R1-4 **回归**：没有未决 ENTRY 意图时，L3 无主单仍不阻塞 clear（D-B4 不被波及）。
  R1-5 源码钉住 + 生产文件免疫（改动只落在清账收敛阶段）。

纪律：夹具使用**真 trader 实例 + 临时状态目录 + 桩交易所**（零网络、零外发、
不碰生产账本）；不为凑绿放宽生产判据 —— 缺字段/错身份/查询失败的负向用例
由 test_converge_post_cancel.py 继续覆盖，本文件不删不改。
运行：G:\\my-crypto-bot\\.venv\\Scripts\\python.exe test_r1_entry_before_fact_check.py
预期：GREEN、退出码 0；任何 FAIL = 实现回退，退出码 1。
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from unittest import mock

import ccxt
import trader_260725
from trader_260725 import CryptoTrader

SYM = 'BTCUSDT'
BID = 'batch_r1'
OBID = 'batch_r1_other'
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


# ────────────────── 夹具（沿 test_converge_post_cancel 惯例）─────────────

class Ex:
    """可编程交易所桩：只记录调用，绝不联网。

    R1 语义可注入：
      · on_cancel：撤单瞬间的交易所现实变化（成交竞态 / 部分成交）
      · fetch_order 返回终态成交量 → 撤后成交证据
    """

    def __init__(self):
        self.last_response_headers = {}
        self.markets = {}
        self.orders = {}
        self.cancel_calls = []
        self.create_calls = []
        self.positions = []
        self.open_orders = []
        self.trades = []
        self.trades_calls = []          # R1b：解决证据查询必须真实发生（可断言）
        self.on_cancel = None
        self.conditional_missing = set()
        self.trades_error = None

    def _mk(self, oid, otype='STOP_MARKET', amount=1.0, stop=75000.0,
            price=None, side='sell', status='open', filled=0.0):
        _p = price if price is not None else stop
        o = {'id': oid, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'symbol': SYM, 'side': side, 'stopPrice': stop,
             'price': _p, 'average': _p}
        self.orders[oid] = o
        return o

    def fetch_order(self, oid, symbol=None, params=None, **k):
        if oid in self.orders:
            return dict(self.orders[oid])
        if oid in self.conditional_missing:
            raise ccxt.OrderNotFound('binanceusdm -2013 Order does not exist')
        raise Exception('binanceusdm -2011 Unknown order')

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None, **k):
        self.trades_calls.append({'symbol': symbol, 'since': since})
        if self.trades_error is not None:
            raise self.trades_error
        return [dict(t) for t in self.trades]

    def cancel_order(self, oid, symbol=None, params=None, **k):
        self.cancel_calls.append(oid)
        o = self.orders.get(oid)
        if o is None:
            if self.on_cancel:
                self.on_cancel(oid)
            raise Exception('binanceusdm -2011 Unknown order')
        if str(o.get('status') or '').lower() in ('closed', 'filled'):
            if self.on_cancel:
                self.on_cancel(oid)
            raise Exception('binanceusdm -2011 Unknown order (already filled)')
        o['status'] = 'canceled'
        if self.on_cancel:
            self.on_cancel(oid)
        return {'id': oid}

    def amount_to_precision(self, symbol, amount):
        return amount

    def price_to_precision(self, symbol, price):
        return price

    def create_order(self, symbol, otype, side, amount, price=None,
                     params=None, **k):
        nid = 'N%d' % (len(self.create_calls) + 1)
        self.create_calls.append((otype, side, round(float(amount), 6)))
        self._mk(nid, otype=otype, amount=float(amount), side=side)
        return {'id': nid}

    def fetch_positions(self, symbols=None, params=None):
        return list(self.positions)

    def fetch_open_orders(self, symbol=None, params=None, **k):
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
    """把所有可写状态重定向到 tmp（沿 p5/b1/pc 惯例）。"""
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
    t.sent_tg_levels = []

    def _tg(text, **k):
        t.sent_tg.append(str(text))
        t.sent_tg_levels.append(k.get('level', 'info'))

    t.send_tg_notification = _tg
    return t, ex


def _state_write(t, states):
    with open(trader_260725.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(states, f, ensure_ascii=False)


# ────────────────── 意图夹具（真实字段 + 负向用例各自独立）───────────────
# ENTRY intent：registry **没有 order_id**（id_known=False）—— R1 要处理的
# 「靠 intent 匹配确认归属」的入场单；SL intent 带 stop_price/order_type，
# 使 E9 不会被误匹配到 SL（顺序无关的确定性）。
ENTRY_INTENT = {'symbol': SYM, 'qty': 1.0, 'order_type': 'limit',
                'price': 76000.0}
SL_INTENT = {'symbol': SYM, 'qty': 1.0, 'order_type': 'stop_market',
             'stop_price': 75000.0}


def _batch(entry_state='PENDING_VERIFY', entry_order_id=None,
           entry_orders=None, over=None):
    b = {
        'is_active': True, 'batch_id': BID, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'monitor_error': False,
        'entry_orders': list(entry_orders or []), 'target_amounts': [1.0],
        'filled_details': [0.0], 'last_filled_count': 0,
        'total_entry_fee': 0.0, 'batch_total_amount': 1.0,
        'stop_steps': [76000.0], 'take_profit_price': 80000.0,
        'current_sl_id': 'SL1', 'tp_order_id': None,
        'close_phase': 0, 'pending_close': False,
        'is_programmatic_cancel': False, 'settled_by_limit_close': False,
        'user_modified': False, 'close_op_id': 'OP1',
        'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'protection_registry': {
            '%s|ENTRY|L0|LONG' % BID: {
                'state': entry_state, 'order_id': entry_order_id,
                'id_known': entry_order_id is not None,
                'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 0,
                'side': 'LONG', 'intent': dict(ENTRY_INTENT)},
            '%s|SL|L0|LONG' % BID: {
                'state': 'CONFIRMED', 'order_id': 'SL1', 'id_known': True,
                'order_kind': 'conditional', 'role': 'SL', 'layer': 0,
                'side': 'LONG', 'intent': dict(SL_INTENT)},
        },
    }
    if over:
        b.update(over)
    return b


def _other_batch():
    """他批次：只为提供 _owned_ids（oth1 绝不可被本批次触碰）。"""
    return {
        'is_active': False, 'batch_id': OBID, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'entry_orders': [], 'target_amounts': [],
        'filled_details': [], 'last_filled_count': 0, 'total_entry_fee': 0.0,
        'batch_total_amount': 0.0, 'stop_steps': [], 'take_profit_price': None,
        'current_sl_id': 'oth1', 'tp_order_id': None, 'limit_close_order_id': None,
        'close_phase': 0, 'pending_close': False,
        'is_programmatic_cancel': False, 'settled_by_limit_close': False,
        'user_modified': False, 'protection_registry': {},
    }


def _setup(entry_state='PENDING_VERIFY', orphan=False, with_entry=True,
           with_other=True, entry_order_id=None, entry_orders=None, over=None):
    """默认：未入场批次 + 未决 ENTRY 意图（无 order_id）+ 已知保护单 SL1。
    exchange 上：E9 = 仅能靠 intent 确认归属的入场单；
                 SL1 = 已知保护单；oth1 = 他批次资产；
                 X1  = 无法归属的无主单（orphan=True 时）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='r1_'))
    if with_entry:
        ex._mk('E9', otype='LIMIT', amount=1.0, stop=76000.0,
               price=76000.0, side='buy')
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75000.0, side='sell')
    if with_other:
        ex._mk('oth1', otype='STOP_MARKET', amount=0.3, stop=74000.0,
               side='sell')
    if orphan:
        # 数量与两份 intent 都不同 → 匹配不上 → 归属无法确认
        ex._mk('X1', otype='STOP_MARKET', amount=0.5, stop=70000.0,
               side='sell')
    ex.open_orders = [o for o in ex.orders.values()]
    states = {SYM: {BID: _batch(entry_state=entry_state,
                                entry_order_id=entry_order_id,
                                entry_orders=entry_orders, over=over)}}
    if with_other:
        states[SYM][OBID] = _other_batch()
    _state_write(t, states)
    return t, ex


def _reg(t):
    return (_load(t).get(SYM) or {}).get(BID, {}).get('protection_registry') \
        or {}


def _load(t):
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _has(t, *words):
    return any(all(w in m for w in words) for m in t.sent_tg)


def _alerts(t):
    return [m for lv, m in zip(getattr(t, 'sent_tg_levels', []), t.sent_tg)
            if lv == 'critical']


def _probe_order(t, ex):
    """给被测函数装事件探针：记录 cancel 调用与事实复核的先后。"""
    events = []
    _real_fc = t._post_cancel_fact_check

    def _fc(*a, **k):
        events.append('fact_check')
        return _real_fc(*a, **k)

    _real_cancel = t._converge_cancel_order

    def _cancel(oid, symbol):
        events.append('cancel:%s' % oid)
        return _real_cancel(oid, symbol)

    t._post_cancel_fact_check = _fc
    t._converge_cancel_order = _cancel
    return events


# ────────────────── R1-1 全成交竞态 ──────────────────

def r1_1_full_fill_race():
    t, ex = _setup()
    events = _probe_order(t, ex)

    def _fill(oid):
        if oid == 'E9':
            # 撤单瞬间成交：状态变终态、离开挂单集合（fetch_order 仍可查到）
            ex.orders['E9'].update(status='filled', filled=1.0)
            ex.open_orders = [o for o in ex.open_orders if o.get('id') != 'E9']

    ex.on_cancel = _fill
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-1.1 intent 归属入场单全成交竞态 → 不产出 proof',
          proof is None, 'proof=%r' % (proof,))
    check('R1-1.2 告警为「发现本批次成交」（证据覆盖到该入场单）',
          _has(t, '发现本批次成交'), [m[:160] for m in t.sent_tg])
    check('R1-1.3 保护单 SL1 在拒绝前**未被撤掉**',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open',
          'orders=%s cancel_calls=%s' % (list(ex.orders), ex.cancel_calls))
    check('R1-1.4 阶段顺序：E9 在事实复核**之前**已撤',
          events.index('cancel:E9') < events.index('fact_check')
          if ('cancel:E9' in events and 'fact_check' in events) else False,
          'events=%s' % events)
    check('R1-1.5 他批次资产 oth1 绝未被触碰',
          'oth1' in ex.orders and 'oth1' not in ex.cancel_calls,
          'cancel_calls=%s' % (ex.cancel_calls,))
    check('R1-1.6 拒绝不靠改 scope 放行（proof 压根未生成）',
          proof is None, 'proof=%r' % (proof,))


# ────────────────── R1-2 部分成交后正常撤单 ──────────────────

def r1_2_partial_fill_then_cancel():
    t, ex = _setup()
    events = _probe_order(t, ex)

    def _partial(oid):
        if oid == 'E9':
            ex.orders['E9'].update(filled=0.4)   # 部分成交，撤单本身成功

    ex.on_cancel = _partial
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-2.1 部分成交（0.4）后正常撤单 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R1-2.2 告警指明发现本批次成交（转入核对与接续，不静默）',
          _has(t, '发现本批次成交'), [m[:180] for m in t.sent_tg])
    check('R1-2.3 保护单 SL1 保留（有成交期间不撤保护）',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open',
          'cancel_calls=%s' % (ex.cancel_calls,))
    check('R1-2.4 阶段顺序探针：先撤入场单 E9 → 事实复核 → 保护单从未被撤',
          events[:2] == ['cancel:E9', 'fact_check']
          and 'cancel:SL1' not in events,
          'events=%s' % (events,))
    check('R1-2.5 撤单本身成功执行（cancel 已调用 E9）',
          'E9' in ex.cancel_calls, 'cancel_calls=%s' % (ex.cancel_calls,))
    check('R1-2.6 他批次资产 oth1 未被触碰',
          'oth1' in ex.orders and 'oth1' not in ex.cancel_calls, '')


# ────────────────── R1-3 归属未知 ──────────────────

def r1_3_unknown_ownership():
    # 未决 ENTRY 意图（order_id 未知、交易场上也认不出本批次入场单）
    # + 一张无法归属的挂单 X1 → 归属未知
    t, ex = _setup(orphan=True, with_entry=False)
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-3.1 归属未知（未决 ENTRY 意图 + 无法归属挂单）→ 不产出 proof',
          proof is None, 'proof=%r' % (proof,))
    check('R1-3.2 告警写明「归属未知」',
          _has(t, '归属未知'), [m[:200] for m in t.sent_tg])
    check('R1-3.3 有效保护单 SL1 保留',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open',
          'cancel_calls=%s' % (ex.cancel_calls,))
    check('R1-3.4 无主单 X1 不被误撤（不碰无法归属的资产）',
          'X1' in ex.orders and ex.orders['X1']['status'] == 'open'
          and 'X1' not in ex.cancel_calls,
          'cancel_calls=%s' % (ex.cancel_calls,))
    check('R1-3.5 未决 ENTRY 意图**未**被终态化（ABSENT 留给接续路径裁决）',
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state')
          not in ('ABSENT', 'PROGRAMMATIC_CANCELED', 'FAILED'),
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state'))
    check('R1-3.6 他批次资产 oth1 未被触碰',
          'oth1' in ex.orders and 'oth1' not in ex.cancel_calls, '')
    check('R1-3.7 不靠改 scope 放行（proof 压根未生成）',
          proof is None, 'proof=%r' % (proof,))


# ────────────────── R1-4 回归：无未决 ENTRY 意图 → L3 不阻塞（D-B4）───────

def r1_4_regression_l3_nonblocking():
    # 没有未决 ENTRY 意图（ENTRY 已终态 + order_id 已知）→ L3 语义必须原样保留
    t, ex = _setup(entry_state='ABSENT', orphan=True,
                   entry_order_id='E9', entry_orders=['E9'])
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    ok_shape = isinstance(proof, dict) and proof.get('exchange_scan') == 'zero'
    check('R1-4.1 无未决 ENTRY 意图 → L3 无主单不阻塞收敛（D-B4 保留）',
          ok_shape, 'proof=%r' % (proof,))
    l3 = [x.get('id') for x in (proof or {}).get('l3_orphans') or []] \
        if isinstance(proof, dict) else []
    check('R1-4.2 X1 仍列示为 L3（只告警不撤）',
          l3 == ['X1'] and 'X1' in ex.orders and 'X1' not in ex.cancel_calls,
          'l3=%s cancel_calls=%s' % (l3, ex.cancel_calls))
    check('R1-4.3 本批次保护单 SL1 正常撤掉（收敛照常推进）',
          'SL1' not in ex.orders or ex.orders['SL1']['status'] == 'canceled',
          'cancel_calls=%s' % (ex.cancel_calls,))
    if ok_shape:
        cleared, err = None, None
        try:
            ret = t.clear_batch_state(SYM, BID, proof=proof)
            cleared = ret if isinstance(ret, bool) else None
        except Exception as e:      # noqa: BLE001
            err = '%s: %s' % (type(e).__name__, e)
        state = _load(t).get(SYM, {})
        check('R1-4.4 D-B4：L3 在场仍可 clear（批次删除 + 他批次保留）',
              cleared is True and err is None and BID not in state
              and OBID in state,
              'cleared=%r err=%r state=%s' % (cleared, err, list(state)))
    else:
        check('R1-4.4 D-B4：L3 在场仍可 clear（批次删除 + 他批次保留）',
              False, 'proof 未生成 → 前置断言已失败')


def r1_3b_handoff_next_round():
    """接续路径：本轮因归属未知被拒（保留保护、从未调 clear），
    阻塞因素消失后**下一轮**照常收敛并成功清账 —— 证明"拒绝"只是本轮不产出 proof，
    而不是把批次卡死或绕过接续。"""
    t, ex = _setup(orphan=True, with_entry=False)
    clears = []
    _real_clear = t.clear_batch_state

    def _spy_clear(*a, **k):
        clears.append(a)
        return _real_clear(*a, **k)

    t.clear_batch_state = _spy_clear          # 探针：拒绝期间绝不允许 clear

    proof1 = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-3B.1 第 1 轮：归属未知 → 拒绝，且 clear 从未被调用',
          proof1 is None and clears == [],
          'proof=%r clear_calls=%d' % (proof1, len(clears)))
    check('R1-3B.2 第 1 轮：保护单仍有效（接续期间不裸奔）',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open', '')

    # 阻塞因素消失：无主单 X1 不再在场（下一轮重试的触发条件）
    ex.orders.pop('X1', None)
    ex.open_orders = [o for o in ex.orders.values()
                      if str(o.get('status') or 'open').lower() == 'open']
    _trades_calls_before = len(ex.trades_calls)
    proof2 = t._converge_batch_orders_before_clear(SYM, BID)
    ok_shape = isinstance(proof2, dict) and proof2.get('position_zero') is True
    # ── R1b（ChatGPT 复审 2026-10-08 漏项①）：本用例按复审意见**修改** ——
    #    第 2 轮不再允许「只凭挂单不见了」放行：必须真实发起成交明细窗口查询
    #    拿到解决证据，proof 留档里必须能看到该证据。
    check('R1-3B.3 第 2 轮：阻塞消失 → 收敛成功产出 proof（既有接续路径）',
          ok_shape, 'proof=%r' % (proof2,))
    check('R1-3B.3b 第 2 轮：未决 ENTRY 解决证据**真实发起查询**（≥1 次 '
          'fetch_my_trades，不是零查询放行）',
          len(ex.trades_calls) > _trades_calls_before,
          'trades_calls=%s' % ex.trades_calls)
    _ev2 = ((proof2 or {}).get('post_cancel') or {}).get('evidence') or ''
    check('R1-3B.3c 第 2 轮：proof 留档写明未决 ENTRY 解决证据（可审计）',
          '未决ENTRY解决证据' in _ev2 and '无可归属成交' in _ev2, _ev2)
    check('R1-3B.4 第 2 轮：未决 ENTRY 意图按 D-B3 三条件终态化（不再卡死）',
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state')
          == 'ABSENT', (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state'))
    if ok_shape:
        ret = t.clear_batch_state(SYM, BID, proof=proof2)
        st = _load(t).get(SYM, {})
        check('R1-3B.5 第 2 轮：proof 过门 → clear 成功（批次删除、他批次保留）',
              ret is True and BID not in st and OBID in st,
              'ret=%r state=%s' % (ret, list(st)))
    else:
        check('R1-3B.5 第 2 轮：proof 过门 → clear 成功（批次删除、他批次保留）',
              False, '第 2 轮未产出合格 proof')


def r1_3c_resolution_evidence_gate():
    """R1b 负向门（ChatGPT 复审 2026-10-08 漏项①）：阻塞消失后，未决 ENTRY
    **必须拿到解决证据**才可清账 —— 挂单消失 ≠ 解决：
      ② 解决证据查询失败 → UNKNOWN → 拒绝（下轮重试，不标 ABSENT、不 clear）；
      ③ 成交明细命中无法排除属于本批次未决意图的成交 → 拒绝 + 转入成交接续；
      ④ 明细里只有可明确排除的成交（数量明显超量）→ 不误伤，收敛照常。
    """
    # ② 查询失败 → 拒绝
    t, ex = _setup(orphan=True, with_entry=False)
    ex.orders.pop('X1', None)
    ex.open_orders = [o for o in ex.orders.values()]
    ex.trades_error = ccxt.NetworkError('trades window timeout')
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-3C.1 解决证据查询失败 → 拒绝清账（UNKNOWN ≠ EMPTY）',
          proof is None, 'proof=%r' % (proof,))
    check('R1-3C.2 告警写明解决证据未知',
          _has(t, '解决证据', '未知'), [m[:180] for m in t.sent_tg])
    check('R1-3C.3 未决 ENTRY 不被标 ABSENT（未拿到证据不放行）',
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state')
          != 'ABSENT',
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state'))
    check('R1-3C.4 保护单 SL1 保留',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open', '')

    # ③ 命中无法排除的成交 → 拒绝 + 转入接续
    t, ex = _setup(orphan=True, with_entry=False)
    ex.orders.pop('X1', None)
    ex.open_orders = [o for o in ex.orders.values()]
    ex.trades = [{'id': 9001, 'order': 'T9', 'amount': 1.0, 'price': 76000.0,
                  'side': 'buy', 'symbol': SYM,
                  'timestamp': int(time.time() * 1000)}]
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-3C.5 成交明细命中未决 ENTRY → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R1-3C.6 告警指明解决证据发现成交（转入成交接续，不静默）',
          _has(t, '解决证据', '发现成交'), [m[:180] for m in t.sent_tg])
    check('R1-3C.7 未决 ENTRY 不被终态化（成交归接续路径裁决）',
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state')
          not in ('ABSENT', 'PROGRAMMATIC_CANCELED', 'FAILED'),
          (_reg(t).get('%s|ENTRY|L0|LONG' % BID) or {}).get('state'))

    # ④ 可明确排除的成交（数量 9.9 远超意图 1.0）→ 不误伤
    t, ex = _setup(orphan=True, with_entry=False)
    ex.orders.pop('X1', None)
    ex.open_orders = [o for o in ex.orders.values()]
    ex.trades = [{'id': 9002, 'order': 'T10', 'amount': 9.9, 'price': 76000.0,
                  'side': 'buy', 'symbol': SYM,
                  'timestamp': int(time.time() * 1000)}]
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-3C.8 可排除的他单成交（超量）不阻塞 —— 匹配器不误伤',
          isinstance(proof, dict), 'proof=%r' % (proof,))


def r1_6_intent_order_bound_to_ledger():
    """R1c（ChatGPT 复审 2026-10-08 漏项②）：intent 匹配确认的入场单必须
    **订单与层绑定**进账本 —— 骨架批次 entry_orders=[] 时，撤单期间的成交若无
    层可绑定，成交层数恒 0、保护管理不接续；同时拒绝 proof 的守卫不放宽。"""
    t, ex = _setup()                       # 未决 ENTRY（无 order_id）+ E9 挂单

    def _fill(oid):
        if oid == 'E9':
            ex.orders['E9'].update(status='filled', filled=1.0)
            ex.open_orders = [o for o in ex.open_orders
                              if o.get('id') != 'E9']

    ex.on_cancel = _fill                    # 撤单期间成交（E9 全成交竞态）
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-6.1 撤中成交 → 拒绝 proof（全局终态守卫不放宽）',
          proof is None, 'proof=%r' % (proof,))
    b = _load(t).get(SYM, {}).get(BID) or {}
    reg = b.get('protection_registry') or {}
    _ident = '%s|ENTRY|L0|LONG' % BID
    check('R1-6.2 订单绑定：entry_orders 收编真实单号 E9',
          [str(x) for x in (b.get('entry_orders') or [])] == ['E9'],
          'entry_orders=%r' % (b.get('entry_orders'),))
    check('R1-6.3 层绑定：target_amounts 与链等长（层坐标可用）',
          len(b.get('target_amounts') or []) == 1
          and (b.get('target_amounts') or [None])[0] == 1.0,
          'target_amounts=%r' % (b.get('target_amounts'),))
    check('R1-6.4 registry 写入 order_id（身份链可查）',
          str((reg.get(_ident) or {}).get('order_id') or '') == 'E9',
          'order_id=%r' % ((reg.get(_ident) or {}).get('order_id'),))
    check('R1-6.5 保护单 SL1 保留（拒绝期间不裸奔）',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open', '')
    # 绑定后进入既有成交证据链：下一轮复核能看到该单成交（不是 UNKNOWN）
    proof2 = t._converge_batch_orders_before_clear(SYM, BID)
    check('R1-6.6 下一轮仍以成交事实拒绝（证据链覆盖绑定单，不放行）',
          proof2 is None, 'proof=%r' % (proof2,))
    check('R1-6.7 告警为发现本批次成交（既有成交证据链语义）',
          _has(t, '发现本批次成交'), [m[:160] for m in t.sent_tg])


# ────────────────── R1-5 源码钉住 + 生产文件免疫 ──────────────────

def r1_5_source_scope():
    src = open(SRC_PATH, encoding='utf-8', errors='replace').read()

    def _body(name):
        try:
            return src.split('def %s(' % name)[1].split('\n    def ')[0]
        except IndexError:
            return '<missing>'

    check('S1 改动仍在清账收敛阶段（未越界进市价平仓）',
          '_post_cancel_fact_check' not in _body('_close_position_market'),
          '_post_cancel_fact_check 在 _close_position_market 体内=%s'
          % ('_post_cancel_fact_check' in _body('_close_position_market')))
    check('S2 proof 门保持零 I/O（gate 内无 fetch_/cancel 调用）',
          'fetch_' not in _body('_verify_clear_proof')
          and 'cancel_order' not in _body('_verify_clear_proof'),
          'gate 段 fetch_=%d cancel_order=%d'
          % (_body('_verify_clear_proof').count('fetch_'),
             _body('_verify_clear_proof').count('cancel_order')))
    check('S3 收敛函数仍禁止调 clear_batch_state（G-B9 职责分离）',
          'clear_batch_state(' not in _body('_converge_batch_orders_before_clear'),
          'def 体内 clear_batch_state 调用=%d'
          % _body('_converge_batch_orders_before_clear').count(
              'clear_batch_state('))


def t99_prod_immune():
    after = _prod_snapshot()
    diff = [k for k in _PROD_FILES if after.get(k) != _PROD_SNAP_AT_IMPORT.get(k)]
    check('T99 生产文件免疫快照（内容 + mtime 未变）', not diff, 'diff=%s' % diff)


def main():
    print('=' * 78)
    print('R1：确认归属 → 撤入场单 → 撤后事实复核 → 才撤保护单（FD003693… 离线）')
    print('=' * 78)
    r1_1_full_fill_race()
    r1_2_partial_fill_then_cancel()
    r1_3_unknown_ownership()
    r1_3b_handoff_next_round()
    r1_3c_resolution_evidence_gate()
    r1_6_intent_order_bound_to_ledger()
    r1_4_regression_l3_nonblocking()
    r1_5_source_scope()
    t99_prod_immune()

    ok = sum(1 for _, p, _ in RESULTS if p)
    print('\n' + '=' * 78)
    if ok == len(RESULTS):
        print('GREEN: %d/%d' % (ok, len(RESULTS)))
        rc = 0
    else:
        print('RED: %d/%d  FAIL 清单：' % (ok, len(RESULTS)))
        for n, p, d in RESULTS:
            if not p:
                print('  ✗ %s  → %s' % (n, d[:300]))
        rc = 1
    print('=' * 78)
    return rc


if __name__ == '__main__':
    sys.exit(main())
