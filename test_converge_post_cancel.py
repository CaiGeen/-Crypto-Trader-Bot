# -*- coding: utf-8 -*-
"""事故修复 2 验收：清账收敛阶段「两阶段撤单 + 撤后事实复核」。

转审裁决（2026-10-06 第三轮）要点 → 本文件逐条断言：
  ① 条件 2 分场景表（三态，不得把「历史上零成交」设成通用清账条件）：
     A 未入场批次撤单            → 必须**确认未成交**，撤后有仓即拒绝
     B 部分成交后正常撤单        → 成交**转入管理**、拒绝清账、保留保护单
     C 成交/数量/归属**未知**     → 不清账（UNKNOWN ≠ EMPTY）
  ② 「先撤 ENTRY、保留有效 SL、再核验事实」的阶段顺序（H）
  ③ proof 门保持零 I/O，事实来源改生成端（position_zero 不再硬编码）
  ④ 已成交、已平仓批次**不要求零成交**（E，防过修）
  ⑤ `-2011` 重试仅本撤单路径局部收窄；其他错误仍按原预算（G）
  ⑥ 撤后查询报调用量与退避预算（proof['post_cancel']）

背景（事故复现 `tmp_clear_race_probe.py` 曾为 RED）：
  ② 撤前持仓 0 → 撤单返回 -2011/absent → ④ 复扫只看挂单
  → 期间成交被完全掩盖 → 仍产出 position_zero=true 的 proof → 过门 → clear
  = 撤单批次 zero_fill 与 live_positions=1.0 并存。

基建惯例：沿用 test_p5 / test_b1 的 Ex / make_trader / _state_write（真 trader 实例，
不给被测代码任何 MagicMock 顶替的机会）。零网络、不碰生产账本。
运行：G:\my-crypto-bot\.venv\Scripts\python.exe test_converge_post_cancel.py
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
BID = 'batch_post01'
OBID = 'batch_post_other'
SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'trader_260725.py')

# R2 夹具：条件单（入场 STOP_MARKET 层）在 Algo Service 的**真实字段形状**
# （R2c 2026-10-08 复审：按 A1 留档四张原始返回校准 —— algoStatus / triggerTime /
# actualOrderId / isActivated；**没有** state/status，**没有** actualQty
# （官方口径：actualQty 仅成交/部分成交时返回，绝不自行拼出）。
# 含 triggerPrice —— 用于断言触发价绝不被当成交价/成本写进账本。
DEFAULT_ALGO_E1 = {
    'algoId': 'E1', 'symbol': SYM, 'algoType': 'CONDITIONAL',
    'orderType': 'STOP_MARKET', 'side': 'BUY', 'positionSide': 'LONG',
    'timeInForce': 'GTC', 'quantity': '1.0', 'origQty': '1.0',
    'triggerPrice': '76000.0',
    'algoStatus': 'CANCELED', 'actualOrderId': '', 'triggerTime': 0,
    'isActivated': False,
}

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
    """可编程交易所桩：只记录调用，绝不联网。

    事故语义可注入：
      · cancel 后成交落地（hook：on_cancel）——复现「撤单瞬间成交」
      · 条件单 -2013（记录不可取）——复现 Algo Service 迁移后的真实返回
      · 持仓/成交明细查询失败 —— 复现 UNKNOWN
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
        self.on_cancel = None          # fn(oid) -> None，撤单瞬间的交易所现实变化
        self.conditional_missing = set()   # 这些 id 走 -2013（条件单/Algo Service）
        self.trades_error = None
        # R2：条件单身份链（algoId → actualOrderId）。生产实测：账本 id 是 algoId，
        # userTrades 里的 orderId 永远是 actualOrderId。
        self.algo_orders = {}          # algoId -> Algo Service 记录（真实字段形状）
        self.algo_error = None
        self.algo_calls = []
        self.pos_error_after_cancel = False
        self._pos_calls = 0

    def _mk(self, oid, otype='STOP_MARKET', amount=1.0, stop=75001.0,
            status='open', filled=0.0, avg=None, sym=None):
        # R2b：真实 ccxt fetch_order 响应恒带 symbol —— 身份核验需要；
        # 负向用例（错交易对）可显式传 sym 覆盖。
        o = {'id': oid, 'status': status, 'filled': filled, 'amount': amount,
             'type': otype, 'stopPrice': stop, 'side': 'sell',
             'average': avg if avg is not None else stop, 'price': stop,
             'symbol': sym if sym is not None else SYM}
        self.orders[oid] = o
        return o

    def fetch_order(self, oid, symbol=None, params=None, **k):
        if oid in self.orders:
            return dict(self.orders[oid])
        if oid in self.conditional_missing:
            # 生产实测（A1-H）：条件单迁 Algo Service 后普通端点返回 -2013
            raise ccxt.OrderNotFound('binanceusdm -2013 Order does not exist')
        raise Exception('binanceusdm -2011 Unknown order')

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None, **k):
        if self.trades_error is not None:
            raise self.trades_error
        return [dict(t) for t in self.trades]

    def fapiPrivateGetAlgoOrder(self, params=None, **k):
        """Algo Service 条件单查询（R2 身份链的权威来源）。"""
        self.algo_calls.append(dict(params or {}))
        if self.algo_error is not None:
            raise self.algo_error
        _aid = str((params or {}).get('algoId') or '')
        _rec = self.algo_orders.get(_aid)
        if _rec is None:
            raise ccxt.OrderNotFound(
                'binanceusdm -2011 Unknown order, id: %s' % _aid)
        return dict(_rec)

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
        self._mk(nid, otype=otype, amount=float(amount))
        return {'id': nid}

    def fetch_positions(self, symbols=None, params=None):
        self._pos_calls += 1
        if self.pos_error_after_cancel and self.cancel_calls:
            raise ccxt.NetworkError('fetch positions timeout')
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
    """把所有可写状态重定向到 tmp（沿 p5/b1 惯例）。"""
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


def _load(t):
    with open(trader_260725.STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _pos(side, amt=1.0):
    return {'symbol': SYM, 'contracts': amt, 'positionAmt': amt,
            'side': side, 'positionSide': side.upper()}


def _batch(over=None):
    """未入场（pre-entry）批次：账面零成交，但挂了入场单 E1 + 保护单 SL1。"""
    b = {
        'is_active': True, 'batch_id': BID, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'monitor_error': False,
        'entry_orders': ['E1'], 'target_amounts': [1.0],
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
                'state': 'PLACED', 'order_id': 'E1', 'id_known': True,
                'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 0,
                'side': 'LONG', 'intent': {'qty': 1.0, 'price': '76000.0'}},
            '%s|SL|L0|LONG' % BID: {
                'state': 'CONFIRMED', 'order_id': 'SL1', 'id_known': True,
                'order_kind': 'conditional', 'role': 'SL', 'layer': 0,
                'side': 'LONG', 'intent': {'qty': 1.0, 'stop_price': '75000.0'}},
        },
    }
    if over:
        b.update(over)
    return b


def _other_batch(filled=1.0):
    """同 symbol、同方向的第二个活跃批次：用于「聚合同向持仓不归本批次」。"""
    return {
        'is_active': True, 'batch_id': OBID, 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'entry_orders': ['OL1'],
        'target_amounts': [1.0], 'filled_details': [filled],
        'last_filled_count': 1, 'total_entry_fee': 0.12,
        'batch_total_amount': 1.0, 'stop_steps': [74000.0],
        'take_profit_price': 79000.0, 'current_sl_id': 'OL1',
        'tp_order_id': None, 'close_phase': 0, 'pending_close': False,
        'is_programmatic_cancel': False, 'settled_by_limit_close': False,
        'user_modified': False, 'protection_registry': {},
    }


def _setup(over=None, pos=None, drop_entry=False, algo=None, other=None):
    """默认：未入场批次 + 两张挂单 + 无持仓。
    drop_entry=True → 撤单后把入场单从可查询集合移除（复现**条件单迁 Algo Service**
    后普通端点 -2013 记录不可取的真实语义）。
    algo=None → 登记 E1 的**真实字段** Algo 记录（R2 夹具补真实字段）；
       algo=False → 不登记（→ 查询失败负向）；algo=dict → 覆盖（缺字段/错身份负向）。
    other=批次 dict → 额外写入同 symbol 的第二个批次（聚合同向持仓归属用例）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='pc_'))
    ex._mk('E1', otype='STOP_MARKET', amount=1.0, stop=76000.0)
    ex._mk('SL1', otype='STOP_MARKET', amount=1.0, stop=75000.0)
    ex.open_orders = [ex.orders['E1'], ex.orders['SL1']]
    ex.positions = list(pos or [])
    ex.conditional_missing.add('E1')   # fetch_order(E1) 不在 orders 时 → -2013
    if algo is not False:
        ex.algo_orders['E1'] = dict(DEFAULT_ALGO_E1) if algo is None else dict(algo)
    if drop_entry:
        def _drop(oid):
            if oid == 'E1':
                ex.orders.pop('E1', None)
                ex.open_orders = [o for o in ex.open_orders
                                  if o.get('id') != 'E1']
        ex.on_cancel = _drop
    states = {SYM: {BID: _batch(over)}}
    if other:
        states[SYM][OBID] = other
    _state_write(t, states)
    return t, ex


def _crits(t):
    return [(lv, m) for lv, m in zip(getattr(t, 'sent_tg_levels', []),
                                     t.sent_tg) if lv == 'critical']


def _has(t, *words):
    return any(all(w in m for w in words) for m in t.sent_tg)


# ────────────────── A：未入场批次撤单 → 撤后有仓 → 拒绝 ──────────────────

def a_race_fill_during_cancel():
    """事故原案：撤单瞬间成交（-2011 + 持仓落地）→ 不得产出 proof。"""
    t, ex = _setup()

    def _fill(oid):
        if oid == 'E1':
            ex.orders.pop('E1', None)        # 已成交离场
            ex.positions = [_pos('long', 1.0)]
            ex.open_orders = [o for o in ex.open_orders
                              if o.get('id') != 'E1']

    ex.on_cancel = _fill
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('A1 撤单竞态（撤后有仓 1.0）→ 不产出 proof',
          proof is None, 'proof=%r' % (proof,))
    check('A2 拒绝理由为「撤单后持仓复核失败」',
          _has(t, '撤单后', '拒绝清账'),
          [m[:160] for m in t.sent_tg])
    check('A3 保护单 SL1 在事实核验前**未被撤掉**',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open',
          'orders=%s cancel_calls=%s' % (list(ex.orders), ex.cancel_calls))
    check('A4 拒绝不靠改 scope 放行（压根没产出 proof）',
          proof is None, 'proof=%r' % (proof,))


# ────────────────── B：部分成交后正常撤单 → 成交转入管理 ──────────────────

def b_partial_fill_then_clean_cancel():
    t, ex = _setup()

    def _partial(oid):
        ex.positions = [_pos('long', 0.5)]   # 部分成交，撤单本身成功

    ex.on_cancel = _partial
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('B1 部分成交（0.5）后正常撤单 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('B2 告警指明转入核对与接续路径（不静默吞掉）',
          _has(t, '拒绝清账'), [m[:180] for m in t.sent_tg])
    check('B3 保护单 SL1 保留（有仓期间不撤保护）',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open',
          'cancel_calls=%s' % (ex.cancel_calls,))
    check('B4 入场单 E1 已撤（阶段一确实执行了撤单）',
          'E1' not in ex.orders or ex.orders['E1']['status'] == 'canceled',
          'cancel_calls=%s' % (ex.cancel_calls,))


# ────────────────── C：查询失败 → UNKNOWN → 拒绝清账 ──────────────────

def c_position_query_unknown():
    t, ex = _setup()
    ex.pos_error_after_cancel = True     # ② 成功，撤后复核那次失败
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('C1 撤后持仓查询失败 → UNKNOWN → 不产出 proof',
          proof is None, 'proof=%r' % (proof,))
    check('C2 告警为撤后 UNKNOWN（≠ EMPTY）',
          _has(t, '撤单后', '持仓查询失败'), [m[:180] for m in t.sent_tg])
    check('C3 异常不外抛（收敛函数正常返回 None）', proof is None)


def c_fill_evidence_unknown():
    """入场单记录不可取(-2013，条件单语义) 且成交明细也查不到 → 证据未知，
    不得当「零成交」放行。"""
    t, ex = _setup(drop_entry=True)
    ex.trades_error = ccxt.NetworkError('userTrades timeout')
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('D1 记录不可取(-2013) 且成交明细查询失败 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('D2 告警写明「证据未知 / UNKNOWN ≠ EMPTY」',
          _has(t, '证据', '未知'), [m[:180] for m in t.sent_tg])


def c_fill_evidence_hit():
    """成交明细里命中本批次 orderId → 确有成交 → 拒绝并转入管理。"""
    t, ex = _setup(drop_entry=True)
    ex.trades = [{'id': 9001, 'order': 'E1', 'amount': 1.0,
                  'price': 76000.0, 'side': 'buy', 'symbol': SYM,
                  'timestamp': int(time.time() * 1000)}]
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('D3 成交明细命中本批次订单 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('D4 告警写明「发现本批次成交」',
          _has(t, '发现本批次成交'), [m[:180] for m in t.sent_tg])


def c_clear_when_confirmed_no_fill():
    """正对照：记录不可取(-2013，条件单) + 成交明细无命中 → 确认未成交 → 允许收敛。"""
    t, ex = _setup(drop_entry=True)
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('E1 未入场批次 + 确认未成交 → 产出 proof',
          isinstance(proof, dict), 'proof=%r' % (proof,))
    if isinstance(proof, dict):
        check('E2 reason = pre_entry_confirmed_no_fill',
              (proof.get('post_cancel') or {}).get('reason')
              == 'pre_entry_confirmed_no_fill',
              (proof.get('post_cancel') or {}).get('reason'))
        check('E3 position_zero 由撤后复核给出（非硬编码占位）',
              proof.get('position_zero') is True,
              'position_zero=%r' % (proof.get('position_zero'),))
        check('E4 撤后复核留档：调用量 + 退避预算',
              isinstance((proof.get('post_cancel') or {}).get('api_calls'), dict)
              and isinstance((proof.get('post_cancel') or {}).get('retry_budget'),
                             dict),
              json.dumps(proof.get('post_cancel'), ensure_ascii=False,
                         default=str)[:400])
        check('E5 阶段顺序：先撤入场单 E1，再撤保护单 SL1',
              ex.cancel_calls.index('E1') < ex.cancel_calls.index('SL1')
              if ('E1' in ex.cancel_calls and 'SL1' in ex.cancel_calls)
              else False, 'cancel_calls=%s' % (ex.cancel_calls,))
        check('E6 撤后复扫为零（exchange_scan=zero 仍成立）',
              proof.get('exchange_scan') == 'zero',
              proof.get('exchange_scan'))


# ────────────────── F：已成交已平仓批次不要求零成交（防过修）───────────

def f_filled_batch_still_clears():
    t, ex = _setup(over={
        'last_filled_count': 1, 'filled_details': [1.0],
        'total_entry_fee': 0.15, 'target_amounts': [1.0],
    })
    ex.orders.pop('E1')          # 入场单已成交，不在挂单里
    ex.open_orders = [ex.orders['SL1']]
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('F1 已成交已平仓批次 → 仍可收敛（不要求零成交）',
          isinstance(proof, dict), 'proof=%r' % (proof,))
    if isinstance(proof, dict):
        check('F2 reason = filled_batch_ledger_ok（场景判定命中）',
              (proof.get('post_cancel') or {}).get('reason')
              == 'filled_batch_ledger_ok',
              (proof.get('post_cancel') or {}).get('reason'))


def f_ledger_mismatch_refuses():
    """有成交迹象（fee>0）但 last_filled_count=0 → 账实不符 → 拒绝。"""
    t, ex = _setup(over={'total_entry_fee': 0.5, 'last_filled_count': 0,
                         'filled_details': []})
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('G1 账实不符（有 fee 无入账）→ 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('G2 告警写明账实不符',
          _has(t, '账实不符'), [m[:180] for m in t.sent_tg])


# ────────────────── H：-2011 局部收窄 + 其他错误预算不动 ──────────────────

def h_no_retry_terminal_codes():
    t, ex = _setup()
    with mock.patch.object(trader_260725.time, 'sleep', lambda *_: None):
        # ① 事实终态（-2011）在本路径只应尝试 1 次
        ex.cancel_calls = []
        r1 = t._converge_cancel_order('GONE1', SYM)
        check('H1 -2011 事实终态：本路径 1 次尝试即判定（局部收窄）',
              r1 == 'absent' and len(ex.cancel_calls) == 1,
              'r1=%r cancel_calls=%s' % (r1, ex.cancel_calls))

        # ② 其他错误（网络超时）仍按原预算重试 → 全局语义未被改动
        ex.cancel_calls = []

        def _netfail(oid, symbol=None, params=None, **k):
            ex.cancel_calls.append(oid)
            raise ccxt.NetworkError('connect timeout')

        ex.cancel_order = _netfail
        r2 = t._converge_cancel_order('GONE2', SYM)
        check('H2 非终态错误仍走满 retries=5（不动全局重试语义）',
              r2 == 'failed' and len(ex.cancel_calls) == 5,
              'r2=%r cancel_calls=%s' % (r2, ex.cancel_calls))


# ────────────────── R2：条件单身份链 algoId → actualOrderId ──────────────────

def r2_algo_mapping_fill_detected():
    """核心（R2）：成交挂在 actualOrderId 上，账本 id 是 algoId →
    旧实现只按 algoId 匹配成交明细 → 永远查不到 → **UNKNOWN 被判成零成交放行**。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'E1', 'symbol': SYM, 'side': 'BUY',
        'algoStatus': 'CANCELED', 'actualOrderId': 'A777',
        'triggerTime': 1791270000000,
        'actualQty': '1.0', 'origQty': '1.0', 'triggerPrice': '76000.0'})
    ex.trades = [{'id': 9101, 'order': 'A777', 'amount': 1.0,
                  'price': 76100.0, 'side': 'buy', 'symbol': SYM,
                  'timestamp': int(time.time() * 1000)}]
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-1.1 algo 记录 actualQty=1.0 → 拒绝清账（绝不按零成交放行）',
          proof is None, 'proof=%r' % (proof,))
    check('R2-1.2 判为「发现本批次成交」（不是 UNKNOWN、更不是 clear）',
          _has(t, '发现本批次成交'), [m[:180] for m in t.sent_tg])
    check('R2-1.3 身份链确实查了 Algo Service（入参 algoId=E1）',
          any(str(c.get('algoId')) == 'E1' for c in ex.algo_calls),
          ex.algo_calls)
    check('R2-1.4 保护单 SL1 保留', 'SL1' in ex.orders
          and ex.orders['SL1']['status'] == 'open', '')


def r2_actual_order_fill_via_mapping():
    """algo 记录自称零成交，但 actualOrderId 指向的实际单**终态已成交 0.4** →
    必须以实际单的终态证据为准判发现成交（不采信 algo 侧滞后字段）。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'CANCELED',
        'actualOrderId': 'A888', 'triggerTime': 1791270000000,
        'origQty': '1.0'})                     # 已触发：零成交由实际单证据给出
    ex._mk('A888', otype='MARKET', amount=1.0, stop=76100.0,
           status='closed', filled=0.4)
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-2.1 actualOrderId 终态 filled=0.4 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2-2.2 告警为发现本批次成交',
          _has(t, '发现本批次成交'), [m[:180] for m in t.sent_tg])


def r2_missing_field_is_unknown():
    """algo 记录**缺触发事实字段 triggerTime** → 无法区分「未触发已撤」与
    「已触发需查实际单」→ UNKNOWN，不得当零成交（R2c 2026-10-08 复审：
    缺 actualQty 本身不再是 UNKNOWN —— 官方口径该字段未成交不返回）。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'CANCELED',
        'actualOrderId': ''})                      # 无 triggerTime 字段
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-3.1 字段缺失 → UNKNOWN → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2-3.2 告警写明证据未知（UNKNOWN ≠ EMPTY）',
          _has(t, '证据', '未知'), [m[:180] for m in t.sent_tg])
    check('R2-3.3 保护单 SL1 保留', 'SL1' in ex.orders
          and ex.orders['SL1']['status'] == 'open', '')


def r2_algo_query_failure_is_unknown():
    """algo 记录**查询失败** → 未知，不得当零成交（负向用例保留）。"""
    t, ex = _setup(drop_entry=True, algo=False)
    ex.algo_error = ccxt.NetworkError('algo service timeout')
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-4.1 algo 查询失败 → UNKNOWN → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2-4.2 告警写明证据未知',
          _has(t, '证据', '未知'), [m[:180] for m in t.sent_tg])


def r2_wrong_identity_is_unknown():
    """algo 记录**身份不匹配**（algoId / symbol 对不上）→ 不得采信为本批次事实。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'OTHER9', 'symbol': 'ETHUSDT', 'algoStatus': 'CANCELED',
        'actualOrderId': '', 'triggerTime': 0})
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-5.1 错身份 algo 记录 → UNKNOWN → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2-5.2 告警写明证据未知',
          _has(t, '证据', '未知'), [m[:180] for m in t.sent_tg])


def r2_non_terminal_state_is_unknown():
    """algo 状态非终态（NEW）→ 不能确认零成交（可能仍在场/可能触发）。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'NEW',
        'actualOrderId': '', 'triggerTime': 0})
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-6.1 非终态状态 → UNKNOWN → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2-6.2 告警写明证据未知',
          _has(t, '证据', '未知'), [m[:180] for m in t.sent_tg])


def r2_legal_untriggered_cancel_clears():
    """**正对照**：完整匹配的终态证据（真实原文字段：身份对得上 + algoStatus=
    CANCELED 终态 + triggerTime=0 未触发 + actualOrderId 空（从未产生实际单）+
    成交明细无命中）→ 才允许确认零成交，并在证据里写明「合法未触发已撤」——
    与「字段缺失/映射未知」必须可区分；**actualQty 未返回 ≠ 伪造为 0**
    （R2c 2026-10-08 复审按 A1 四张原单校准）。"""
    t, ex = _setup(drop_entry=True)          # 默认夹具 = DEFAULT_ALGO_E1
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-7.1 完整终态零成交证据 → 允许收敛（产出 proof）',
          isinstance(proof, dict), 'proof=%r' % (proof,))
    _ev = ((proof or {}).get('post_cancel') or {}).get('evidence') or ''
    check('R2-7.2 证据写明「合法未触发已撤」（与 UNKNOWN 可区分）',
          '合法未触发已撤' in _ev, _ev)
    check('R2-7.3 reason 仍为 pre_entry_confirmed_no_fill',
          ((proof or {}).get('post_cancel') or {}).get('reason')
          == 'pre_entry_confirmed_no_fill',
          ((proof or {}).get('post_cancel') or {}).get('reason'))


def r2_cost_pending_keeps_ledger():
    """R2 约束①：**缺成本继续保护** —— cost_pending 载荷在册且 PnL 未落盘时，
    即使 proof 全绿也绝不删除账本（续跑通道仍在）。"""
    t, ex = _setup(drop_entry=True, over={
        'cost_pending_settle_payload': {'exit_price': 76000.0, 'exit_qty': 1.0,
                                        'dedup_key': 'cp-1'},
        'close_reason': 'cost_pending_settling',
    })
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-8.1 前置：proof 可产出', isinstance(proof, dict), 'proof=%r' % (proof,))
    ret = True
    if isinstance(proof, dict):
        ret = t.clear_batch_state(SYM, BID, proof=proof)   # 返回值契约：仅 bool
    st = _load(t).get(SYM, {})
    check('R2-8.2 缺成本 → 拒绝删除账本（保护/续跑通道保留）',
          ret is False and BID in st, 'ret=%r st=%s' % (ret, sorted(st)))
    check('R2-8.3 拒绝理由指明待结算载荷/PnL 未落盘',
          _has(t, '待结算载荷存在且 PnL 未落盘'),
          [m[:200] for m in t.sent_tg])


def r2_be_pause_keeps_sl():
    """R2 约束①（/be 侧）：成本待补证 → 只拒绝 /be，**不撤现有 SL**。"""
    t, ex = _setup(over={'cost_pending_layers': [0],
                         'last_filled_count': 1, 'filled_details': [1.0],
                         'total_entry_fee': 0.0})     # 数量已核实、成本缺失
    ok, why = t.set_breakeven_sl(BID)
    check('R2-9.1 缺成本 → 拒绝 /be（不拿未确认成本算保本价）',
          ok is False, 'ok=%r' % (ok,))
    check('R2-9.2 文案写明现有止损保持有效',
          '现有止损保持有效' in str(why), str(why)[:200])
    check('R2-9.3 未发出任何撤单（保护单不动）',
          ex.cancel_calls == [], 'cancel_calls=%s' % (ex.cancel_calls,))


def r2_no_trigger_price_written():
    """R2 约束②：**不填触发价** —— algo 记录带 triggerPrice，收敛全程不得把它
    （或委托价）写进账本成本/成交字段；账本除 registry 终态化外零改动。"""
    t, ex = _setup(drop_entry=True)          # DEFAULT_ALGO_E1 含 triggerPrice
    before = json.loads(json.dumps(_load(t)[SYM][BID]))
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    after = _load(t)[SYM][BID]
    check('R2-10.1 前置：产出 proof', isinstance(proof, dict), 'proof=%r' % (proof,))
    check('R2-10.2 成交/成本字段未被触发价污染',
          after.get('filled_details') == before.get('filled_details')
          and after.get('total_entry_fee') == before.get('total_entry_fee')
          and after.get('last_filled_count') == before.get('last_filled_count'),
          'before=%r after=%r' % ({k: before.get(k) for k in
                                   ('filled_details', 'total_entry_fee',
                                    'last_filled_count')},
                                  {k: after.get(k) for k in
                                   ('filled_details', 'total_entry_fee',
                                    'last_filled_count')}))
    _b2 = dict(after)
    _b2.pop('protection_registry', None)      # 该字段由既有终态化合法改写
    check('R2-10.3 账本其余字段零改动（未新增价格/成本键）',
          _b2 == {k: v for k, v in before.items() if k != 'protection_registry'},
          'diff=%s' % sorted(set(_b2) ^ set(before)))


def r2_aggregate_position_not_attributed():
    """R2 约束③：**不把聚合同向持仓归本批次** —— 同符号同方向另一个批次已持
    1.0，本批次 PRE_ENTRY → 贡献扣减后为 0 → 允许收敛，且本批次账本不被写成已成交。"""
    t, ex = _setup(pos=[_pos('long', 1.0)], other=_other_batch())
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2-11.1 聚合 1.0 由同向他批次解释 → 本批次贡献 0 → 产出 proof',
          isinstance(proof, dict), 'proof=%r' % (proof,))
    check('R2-11.2 position_zero 由贡献扣减给出（非硬编码）',
          (proof or {}).get('position_zero') is True,
          (proof or {}).get('position_zero'))
    b = _load(t)[SYM][BID]
    check('R2-11.3 聚合持仓未被写进本批次账本（仍是 PRE_ENTRY 零成交）',
          b.get('last_filled_count') == 0 and b.get('filled_details') == [0.0]
          and b.get('total_entry_fee') == 0.0,
          json.dumps({k: b.get(k) for k in ('last_filled_count',
                                            'filled_details',
                                            'total_entry_fee')},
                     ensure_ascii=False))


def r2_request_budget_accounting():
    """配套待办（请求预算口径）：proof 留档必须体现 algo 身份链调用 + 失败尝试，
    并写明「每次调用点的尝试次数含失败尝试与各层重试」。"""
    t, ex = _setup(drop_entry=True, algo=False)
    ex.algo_error = ccxt.NetworkError('algo service timeout')
    proof = t._converge_batch_orders_before_clear(SYM, BID)   # → 拒绝，无 proof
    check('R2-12.1（前置）拒绝时无 proof —— 预算口径改在成功 proof 上验证',
          proof is None, 'proof=%r' % (proof,))
    t2, ex2 = _setup(drop_entry=True)
    p2 = t2._converge_batch_orders_before_clear(SYM, BID)
    _pc = (p2 or {}).get('post_cancel') or {}
    _ac = _pc.get('api_calls') or {}
    check('R2-12.2 api_calls 含 algo 身份链调用计数',
          isinstance(_ac.get('algo_order'), int) and _ac.get('algo_order', 0) >= 1,
          json.dumps(_ac, ensure_ascii=False))
    _rb = _pc.get('retry_budget') or {}
    check('R2-12.3 retry_budget 写明失败尝试与各层重试计入口径',
          bool(_rb.get('attempt_accounting'))
          and ('失败' in str(_rb.get('attempt_accounting'))),
          json.dumps(_rb, ensure_ascii=False))


def r2_request_budget_call_site_counting():
    """配套待办（请求预算**校准**，ChatGPT §4① + R5 三层区分 2026-10-08）。

    旧口径「成功后才 +1」→ `-2013`（记录不可取）与异常分支的**主键根本不计数**，
    `api_calls` 系统性低估实际请求量（§4 原话：`calls` 不是实际请求量）。
    校准后**三层区分**：① 调用点 = 发起次数（成功 + 失败，发起即计数），
    `*_err` = 其中失败的次数；② `_API_METRICS` = `_safe_api_call` 每次
    **包装层尝试**（含重试）各入一账 —— 是包装层尝试流水，**不是**底层 HTTP
    请求流水；③ 底层 HTTP 是否重发不可观测，本仓不声称掌握该层计数
    （见 `retry_budget.attempt_accounting`）。
    """
    t, ex = _setup(drop_entry=True)          # 默认夹具 = -2013 记录不可取
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    _ac = ((proof or {}).get('post_cancel') or {}).get('api_calls') or {}
    check('R2-13.1（前置）proof 可产出', isinstance(proof, dict), 'proof=%r' % (proof,))
    check('R2-13.2 fetch_order 发起即计数（-2013 那次也计）',
          _ac.get('fetch_order', 0) >= 1, json.dumps(_ac, ensure_ascii=False))
    check('R2-13.3 失败尝试计数 fetch_order_err ≥1（-2013 属查询失败）',
          _ac.get('fetch_order_err', 0) >= 1, json.dumps(_ac, ensure_ascii=False))
    check('R2-13.4 成交明细兜底查询发起即计数',
          _ac.get('fetch_my_trades', 0) >= 1, json.dumps(_ac, ensure_ascii=False))


def r2b_normal_order_branch_validation():
    """R2b（ChatGPT 复审 2026-10-08 漏项③）：普通订单分支的坏输入必须
    **核身份 + 核终态 + 核有效数量**，任何一环不满足 → UNKNOWN 拒绝清账 ——
    挂单消失/缺字段/NaN/负数/错单/错交易对/open+0 都不能被当成零成交。
    五种输入逐个独立跑全链路（真实 converge，不是只调内部函数）。"""
    def _mut_wrongsid(ex):
        # 错单只出现在 fetch_order 响应里（open 扫描/撤单仍见 E1 本尊）——
        # 精确隔离 R2b 的「身份核验」分支，而不是被归属闸先拦下
        _orig = ex.fetch_order

        def _wrong(oid, symbol=None, params=None, **k):
            r = dict(_orig(oid, symbol=symbol, params=params, **k))
            r['id'] = 'WRONG9'
            return r

        ex.fetch_order = _wrong

    _cases = [
        ('缺 filled 字段', lambda ex: ex.orders['E1'].pop('filled', None)),
        ('filled=NaN', lambda ex: ex.orders['E1'].__setitem__(
            'filled', float('nan'))),
        ('filled=-1（负数）', lambda ex: ex.orders['E1'].__setitem__(
            'filled', -1.0)),
        ('返回错单 id（错身份）', _mut_wrongsid),
        ('返回错交易对', lambda ex: ex.orders['E1'].__setitem__(
            'symbol', 'ETHUSDT')),
    ]
    for _i, (_tag, _mut) in enumerate(_cases, 1):
        t, ex = _setup()
        _mut(ex)
        proof = t._converge_batch_orders_before_clear(SYM, BID)
        check('R2b.%d %s → 拒绝清账（proof=None）' % (_i, _tag),
              proof is None, 'proof=%r' % (proof,))
        check('R2b.%d %s → 告警为证据未知（UNKNOWN ≠ ZERO）' % (_i, _tag),
              _has(t, '证据', '未知'), [m[:160] for m in t.sent_tg])
        check('R2b.%d 保护单 SL1 保留' % _i,
              'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open', '')
    # (6) open + filled=0：撤单「返回成功」但事实复核看到的仍是 open 未终态 → 拒绝
    t, ex = _setup()

    def _keep_open(oid):
        # 撤单接口返回成功，但交易所现实仍是 open（撤单未生效的竞态）
        ex.orders[oid]['status'] = 'open'

    ex.on_cancel = _keep_open
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2b.6 open+filled=0（非终态）→ 拒绝清账',
          proof is None, 'proof=%r' % (proof,))
    check('R2b.6 告警为证据未知（未终态 ≠ 零成交）',
          _has(t, '证据', '未知'), [m[:160] for m in t.sent_tg])


def r2c_a1_raw_archive_replay():
    """R2c（ChatGPT 复审 2026-10-08 漏项④）：用 **A1 留档四张真实 algo 原文**
    回放身份链 —— 未触发已撤（algoStatus=CANCELED / triggerTime=0 /
    actualOrderId 空，且**无 actualQty 字段**）→ 'zero' 合法放行；绝不因缺
    actualQty 误判 UNKNOWN，也绝不把缺失数量伪造成 0。"""
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      '送审附件_复审第2轮_20261007', 'A1_原始返回',
                      'a1h_algo_order.out.json')
    with open(_p, encoding='utf-8') as f:
        _raw = json.load(f)
    _algos = _raw.get('algo') or {}
    check('R2c-A1.0（前置）A1 留档含 4 张 algo 原单', len(_algos) == 4,
          sorted(_algos))
    t, ex = _setup(drop_entry=True)
    for _i, _k in enumerate(sorted(_algos), 1):
        _body = json.loads((_algos[_k] or {}).get('body') or '{}')
        _aid = str(_body.get('algoId') or '')
        check('R2c-A1.%d（前置）原文无 actualQty/无 state 字段' % _i,
              'actualQty' not in _body and 'state' not in _body,
              sorted(_body))
        ex.algo_orders[_aid] = _body
        _calls = {}
        _ev, _det, _act = t._algo_identity_evidence(SYM, _aid, _calls)
        check('R2c-A1.%d 未触发已撤（真实原文）→ zero 合法' % _i,
              _ev == 'zero' and '合法未触发已撤' in _det, _det)
        check('R2c-A1.%d 证据写明 actualQty 未返回、未伪造为 0' % _i,
              'actualQty' in _det and '未伪造为 0' in _det, _det)
        check('R2c-A1.%d 调用点主键计数（algo_order≥1）' % _i,
              _calls.get('algo_order', 0) >= 1, json.dumps(_calls))


def r2c_actual_order_identity_negatives():
    """R2c 漏项④负向：已触发 → 必须核**子订单**（身份/交易对/终态/有效数量）。
    映射缺失 / 错子订单 / 错交易对 / 查询失败 → UNKNOWN 拒绝清账 ——
    「已触发但映射缺失仍判未触发」的旧口径必须被堵死。"""
    # 夹具同时带旧形状字段（state/actualQty）—— 这是**被测输入**而非伪造结论：
    # 父版只看 actualQty=0 + state 终态、完全不看 triggerTime/不核子订单身份
    # → 判零放行（RED）；候选按触发事实 + 子订单身份拒绝（GREEN）。
    _triggered = {'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'CANCELED',
                  'state': 'CANCELED', 'actualOrderId': '',
                  'triggerTime': 1791270000000, 'actualQty': '0'}

    # (a) 已触发（triggerTime>0）但 actualOrderId 空 → unknown（不再当未触发）
    t, ex = _setup(drop_entry=True, algo=dict(_triggered))
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2c-1 已触发但映射缺失 → 拒绝清账（不再当未触发零成交）',
          proof is None and _has(t, '证据', '未知'),
          'proof=%r alerts=%s' % (proof, [m[:150] for m in t.sent_tg]))

    # (b) 子订单身份不符（返回 id 不是请求的 actualOrderId）→ unknown
    t, ex = _setup(drop_entry=True, algo=dict(
        _triggered, actualOrderId='A888'))
    ex._mk('A888', otype='MARKET', amount=1.0, stop=0.0,
           status='closed', filled=0.0)
    ex.orders['A888']['id'] = 'NOT_A888'          # 返回错单
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2c-2 错子订单（id 不符）→ 拒绝清账',
          proof is None and _has(t, '证据', '未知'), 'proof=%r' % (proof,))

    # (c) 子订单错交易对 → unknown
    t, ex = _setup(drop_entry=True, algo=dict(
        _triggered, actualOrderId='A888'))
    ex._mk('A888', otype='MARKET', amount=1.0, stop=0.0,
           status='closed', filled=0.0, sym='ETHUSDT')
    proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2c-3 子订单错交易对 → 拒绝清账',
          proof is None and _has(t, '证据', '未知'), 'proof=%r' % (proof,))

    # (d) 子订单查询失败 → unknown（订单不在普通端点任何集合 → -2011）
    t, ex = _setup(drop_entry=True, algo=dict(
        _triggered, actualOrderId='A888'))
    with mock.patch.object(trader_260725.time, 'sleep', lambda *_: None):
        proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R2c-4 子订单查询失败 → 拒绝清账（UNKNOWN ≠ EMPTY）',
          proof is None and _has(t, '证据', '未知'), 'proof=%r' % (proof,))


def r2c_budget_suborder_failure_counting():
    """R5 漏项⑥：子订单查询失败时 **fetch_order 主键也要计**（发起即计数）——
    调用点层：主键≥1 且 *_err=1；包装层尝试另由 _API_METRICS 记录（不冒充
    底层 HTTP 流水）；不为计数调整全局重试（retries 仍按既有值）。"""
    t, ex = _setup(drop_entry=True, algo={
        'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'CANCELED',
        'state': 'CANCELED',                          # 旧形状（父版可读字段）
        'actualOrderId': 'A888', 'triggerTime': 1791270000000,
        'actualQty': '0'})                            # 使父版走到子订单查询

    def _boom(oid, symbol=None, params=None, **k):
        raise ccxt.NetworkError('boom')

    ex.fetch_order = _boom
    _calls = {}
    with mock.patch.object(trader_260725.time, 'sleep', lambda *_: None):
        _ev, _det, _act = t._algo_identity_evidence(SYM, 'E1', _calls)
    check('R2c-预算.1 子订单查询失败 → unknown', _ev == 'unknown', _det)
    check('R2c-预算.2 fetch_order 主键发起即计数（失败不漏计主键）',
          _calls.get('fetch_order', 0) >= 1, json.dumps(_calls))
    check('R2c-预算.3 fetch_order_err == 1（调用点层失败计数）',
          _calls.get('fetch_order_err') == 1, json.dumps(_calls))


# ────────────────── R5 复审打回（2026-10-08）：三处漏项修复验收 ──────────────────

def r5r1_ccxt_symbol_identity():
    """R5 复审打回①：真实 CCXT 归一化 symbol 'BTC/USDT:USDT' 与本地 'BTCUSDT'
    必须判同一交易对身份。旧 _norm 直接删分隔符会得到 'BTCUSDTUSDT' 永不相等
    → 未决 ENTRY 的成交被当别的交易对跳过（随后产 proof、撤 SL、删账本），
    普通订单/实际子订单被判错交易对 → UNKNOWN。
    三处证据链统一验收：正向 BTC/USDT:USDT 判同；负向 ETH/USDT:USDT 仍拒绝
    （错交易对拒绝判据零放宽）。"""
    # (1) 未决 ENTRY 成交归属：真实 CCXT symbol 的成交必须命中（不再被跳过）
    t, ex = _setup()
    ex.trades = [{'id': 1, 'symbol': 'BTC/USDT:USDT', 'order': 'TX1',
                  'amount': 0.5, 'qty': 0.5, 'price': 76000.0, 'side': 'buy'}]
    _intents = [('E1|ENTRY|L0|LONG', {'side': 'buy', 'qty': 1.0})]
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R1-1 未决ENTRY成交（真实CCXT symbol）→ found（不再被跳过）',
          _ev == 'found', '%s: %s' % (_ev, _det))
    # 负向：别的交易对仍排除
    ex.trades = [{'id': 2, 'symbol': 'ETH/USDT:USDT', 'order': 'TX2',
                  'amount': 0.5, 'price': 76000.0, 'side': 'buy'}]
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R1-2 错交易对（ETH/USDT:USDT）→ 仍排除 clear（拒绝判据保留）',
          _ev == 'clear', '%s: %s' % (_ev, _det))
    # (2) 普通订单：fetch_order 返回真实 CCXT symbol → 身份成立（终态+0 → clear）
    t, ex = _setup()
    ex._mk('N1', otype='LIMIT', amount=1.0, stop=76620.0,
           status='closed', filled=0.0, sym='BTC/USDT:USDT')
    _ev, _det = t._post_cancel_fill_evidence(SYM, BID, _batch(), {'N1'}, {})
    check('R5R1-3 普通订单（真实CCXT symbol·终态0）→ clear 身份成立',
          _ev == 'clear', '%s: %s' % (_ev, _det))
    # 负向：普通订单错交易对仍 unknown
    t, ex = _setup()
    ex._mk('N1', otype='LIMIT', amount=1.0, stop=76620.0,
           status='closed', filled=0.0, sym='ETH/USDT:USDT')
    _ev, _det = t._post_cancel_fill_evidence(SYM, BID, _batch(), {'N1'}, {})
    check('R5R1-4 普通订单错交易对 → unknown（拒绝判据保留）',
          _ev == 'unknown', '%s: %s' % (_ev, _det))
    # (3) 实际子订单：已触发 → fetch_order(actualOrderId) 真实 CCXT symbol → zero
    _trig = {'algoId': 'E1', 'symbol': SYM, 'algoStatus': 'CANCELED',
             'actualOrderId': 'A888', 'triggerTime': 1791270000000}
    t, ex = _setup(drop_entry=True, algo=dict(_trig))
    ex._mk('A888', otype='MARKET', amount=1.0, stop=0.0,
           status='closed', filled=0.0, sym='BTC/USDT:USDT')
    _ev, _det, _act = t._algo_identity_evidence(SYM, 'E1', {})
    check('R5R1-5 实际子订单（真实CCXT symbol·终态0）→ zero 身份成立',
          _ev == 'zero', '%s: %s' % (_ev, _det))
    # 负向：子订单错交易对仍 unknown（R2c-3 判据保留）
    t, ex = _setup(drop_entry=True, algo=dict(_trig))
    ex._mk('A888', otype='MARKET', amount=1.0, stop=0.0,
           status='closed', filled=0.0, sym='ETH/USDT:USDT')
    _ev, _det, _act = t._algo_identity_evidence(SYM, 'E1', {})
    check('R5R1-6 子订单错交易对 → unknown（拒绝判据保留）',
          _ev == 'unknown', '%s: %s' % (_ev, _det))


def r5r3_fill_evidence_completeness():
    """R5 复审打回③：不完整成交查询不得证明零成交——返回 None / 非法行 /
    单页满 500 条（币安默认单页，可能截断未覆盖全部记录）/ 批次创建超 6.9 天
    （官方单次跨度 ≤7 天 → 窗口被截断）→ 一律 UNKNOWN 保留账本与有效 SL；
    有效且完整覆盖所需窗口的证据才允许 clear。本轮**未新增查询**（不加分页），
    既有 fetch_my_trades 主键照常发起即计数。"""
    _intents = [('E1|ENTRY|L0|LONG', {'side': 'buy', 'qty': 1.0})]
    # (1) None → unknown
    t, ex = _setup()
    ex.fetch_my_trades = lambda *a, **k: None
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R3-1 查询返回 None → unknown（不得 clear）',
          _ev == 'unknown', '%s: %s' % (_ev, _det))
    # (2) 非法行 → unknown
    ex.fetch_my_trades = lambda *a, **k: [123]
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R3-2 查询含非法行 → unknown',
          _ev == 'unknown', '%s: %s' % (_ev, _det))
    # (3) 单页满 500 条（未覆盖全部记录）→ unknown（方向不符 → 无命中，走完整性闸）
    ex.fetch_my_trades = lambda *a, **k: [
        {'symbol': SYM, 'order': 'P%d' % i, 'amount': 0.1, 'side': 'sell'}
        for i in range(500)]
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R3-3 单页满500条 → unknown（一次查询不能证明完整窗口）',
          _ev == 'unknown', '%s: %s' % (_ev, _det))
    # (4) 批次创建超 6.9 天 → 窗口截断 → unknown
    t, ex = _setup()
    ex.trades = []
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, 'batch_20260801_120000', _batch(), _intents, {})
    check('R5R3-4 批次>6.9天窗口被截断 → unknown',
          _ev == 'unknown', '%s: %s' % (_ev, _det))
    # (5) 对照：有效且完整覆盖的空窗口 → clear（不误伤正常放行）
    _ev, _det = t._pending_entry_resolution_evidence(
        SYM, BID, _batch(), _intents, {})
    check('R5R3-5 对照：有效完整空窗口 → clear',
          _ev == 'clear', '%s: %s' % (_ev, _det))
    # (6) 撤后兜底路径：-2013 + 合法未触发已撤 + trades=None → 拒绝清账
    t, ex = _setup(drop_entry=True)
    ex.fetch_my_trades = lambda *a, **k: None
    with mock.patch.object(trader_260725.time, 'sleep', lambda *_: None):
        proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R5R3-6 兜底路径 trades=None → 拒绝清账（proof=None）',
          proof is None, 'proof=%r alerts=%s'
          % (proof, [m[:150] for m in t.sent_tg]))
    check('R5R3-6 告警为证据未知（UNKNOWN ≠ EMPTY）',
          _has(t, '证据', '未知'), [m[:150] for m in t.sent_tg])
    check('R5R3-6 保护单 SL1 保留',
          'SL1' in ex.orders and ex.orders['SL1']['status'] == 'open', '')
    # (7) 兜底路径：单页满 500 条 → 拒绝清账
    t, ex = _setup(drop_entry=True)
    ex.fetch_my_trades = lambda *a, **k: [
        {'symbol': 'ETHUSDT', 'order': 'Q%d' % i, 'amount': 0.1}
        for i in range(500)]
    with mock.patch.object(trader_260725.time, 'sleep', lambda *_: None):
        proof = t._converge_batch_orders_before_clear(SYM, BID)
    check('R5R3-7 兜底路径单页满500条 → 拒绝清账',
          proof is None, 'proof=%r' % (proof,))


# ────────────────── T99 生产文件免疫 ──────────────────

def t99_prod_immune():
    after = _prod_snapshot()
    diff = [k for k in _PROD_FILES if after.get(k) != _PROD_SNAP_AT_IMPORT.get(k)]
    check('T99 生产文件免疫快照（内容 + mtime 未变）', not diff, 'diff=%s' % diff)


# ────────────────── 源码钉住：改动只在清账收敛阶段 ──────────────────

def t98_source_scope():
    src = open(SRC_PATH, encoding='utf-8', errors='replace').read()

    def _body(name):
        try:
            return src.split('def %s(' % name)[1].split('\n    def ')[0]
        except IndexError:
            return '<missing>'

    check('S1 撤后复核未越界进市价平仓函数（先平后撤顺序不受影响）',
          '_post_cancel_fact_check' not in _body('_close_position_market'),
          '在 _close_position_market 体内=%s'
          % ('_post_cancel_fact_check' in _body('_close_position_market')))
    check('S2 撤后复核只在清账收敛阶段（monitor 主循环未引入）',
          src.count('def _post_cancel_fact_check') == 1
          and '_post_cancel_fact_check' not in _body('_start_monitoring'),
          'def 数=%s' % src.count('def _post_cancel_fact_check'))
    _gate = _body('_verify_clear_proof')
    check('S3 proof 门保持零 I/O（gate 内无 fetch_/cancel 调用）',
          'fetch_' not in _gate and 'cancel_order' not in _gate,
          'gate 段 fetch_=%d cancel_order=%d'
          % (_gate.count('fetch_'), _gate.count('cancel_order')))


def main():
    print('=' * 78)
    print('事故修复 2：清账收敛两阶段 + 撤后事实复核（转审条件 2/3）')
    print('=' * 78)
    a_race_fill_during_cancel()
    b_partial_fill_then_clean_cancel()
    c_position_query_unknown()
    c_fill_evidence_unknown()
    c_fill_evidence_hit()
    c_clear_when_confirmed_no_fill()
    f_filled_batch_still_clears()
    f_ledger_mismatch_refuses()
    h_no_retry_terminal_codes()
    r2_algo_mapping_fill_detected()
    r2_actual_order_fill_via_mapping()
    r2_missing_field_is_unknown()
    r2_algo_query_failure_is_unknown()
    r2_wrong_identity_is_unknown()
    r2_non_terminal_state_is_unknown()
    r2_legal_untriggered_cancel_clears()
    r2_cost_pending_keeps_ledger()
    r2_be_pause_keeps_sl()
    r2_no_trigger_price_written()
    r2_aggregate_position_not_attributed()
    r2_request_budget_accounting()
    r2_request_budget_call_site_counting()
    r2b_normal_order_branch_validation()
    r2c_a1_raw_archive_replay()
    r2c_actual_order_identity_negatives()
    r2c_budget_suborder_failure_counting()
    r5r1_ccxt_symbol_identity()
    r5r3_fill_evidence_completeness()
    t98_source_scope()
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
