# -*- coding: utf-8 -*-
"""
P0 平仓竞态修复 Batch B 专项测试（proof 门 + converge，2026-08-29）

依据：P0平仓竞态_BatchB_修复规格_v2_终稿.md（ChatGPT APPROVED 2026-08-29）
锁定语义：
  B0   结算段撤 TP（reason=close_settled_canceled_tp，修正2 审计字段）
  B1   _converge_batch_orders_before_clear：两源扫描 + L1/L2/L3 + 单次复扫
       （D-B1 贡献扣减+precision 容差 / D-B2 单次复扫 / D-B3 三条件终态化 /
        D-B4 L3 不阻塞 / converge 内禁止调 clear）
  B2   clear_batch_state(symbol, batch_id, proof=None) proof 门：持锁验证、
       Fail-Closed、无 force/skip_verify 逃生门、close_phase=3 唯一写入点

RED 基线（01bb44f 现状，预期 FAIL = 缺陷/缺口实证）：
  R-B1 结算段 TP 零处理（B0 缺口——8-28 事故结算侧根因）
  R-B2 无 proof 直调 clear → 现状直接删 state（无门）
  R-B3 扫描 UNKNOWN → 现状无 converge 概念
  R-B4 L3 无主单 → 现状无分级无告警
  R-B5 L1 撤单失败 → 现状 clear 照删

GREEN（Batch B 实施后全部翻绿）：G-B1~G-B10 + D-B1/D-B3 锁定语义。

运行：.venv/Scripts/python.exe test_b_batch.py（ccxt 只在项目 .venv）
⚠️ MagicMock 坑（第 7 次实证风险）：新 helper（_converge_* / _verify_clear_proof /
_batch_has_active_exposure / _converge_alert）必须 hasattr 保护绑定 + 
_converge_alert_counts 绑真实 dict（getattr MagicMock 非 None → 告警静默丢失）。
真实文件 I/O 重定向临时目录，绝不触碰实盘 trade_state.json。
"""
import os
import json
import re as _re
import tempfile
import threading
import time
from contextlib import redirect_stdout
from unittest import mock

import ast as _ast

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b"
RESULTS = []

IDENT_TP = f"{BATCH}|TP|L1|LONG"
IDENT_SL = f"{BATCH}|SL|L1|LONG"
IDENT_PV = f"{BATCH}|SL|L2|LONG"      # id_known=False 未决（D-B3）

_ORIG_STATE_FILE = trader_260725.STATE_FILE

# L2 归属测试参数（intent 与订单完全一致）
L2_INTENT = {'symbol': 'BTCUSDT', 'side': 'SELL', 'order_type': 'STOP_MARKET',
             'reduce_only': True, 'stop_price': 70000.0, 'qty': 0.002}


def report(name, passed, detail=""):
    RESULTS.append((name, bool(passed)))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name}\n{'=' * 60}"
          + (f"\n  → {detail}" if detail else ""))
    return bool(passed)


# =====================================================================
# FakeExchange：双 dict + 撤单失败/扫描失败注入 + events
# =====================================================================
class FakeExchange:
    def __init__(self):
        self.orders = {}       # id -> open order
        self.archive = {}
        self.events = []
        self.position_amt = 0.0
        self.fail_cancel = set()       # 撤单失败注入（非 -2011）
        self.scan_fail = False         # fetch_open_orders → NetworkError
        self.markets = None            # precision 注入（D-B1 容差）
        self._id_seq = 0

    def _ev(self, label, detail=""):
        self.events.append((label, str(detail)))

    def fetch_time(self):
        return int(time.time() * 1000)

    def fetch_positions(self, symbols=None, params=None):
        return [{'symbol': SYMBOL, 'side': 'long', 'contracts': self.position_amt,
                 'positionAmt': self.position_amt, 'info': {'symbol': 'BTCUSDT'}}]

    def fetch_open_orders(self, symbol, params=None, **kw):
        if self.scan_fail:
            raise ccxt.NetworkError('connect timeout')
        ids = [o['id'] for o in self.orders.values()]
        self._ev('fetch_open_orders', f"ids={ids}")
        return [dict(o) for o in self.orders.values()]

    def fetch_order(self, order_id, symbol=None, params=None, **kw):
        oid = str(order_id)
        if oid in self.orders:
            return dict(self.orders[oid])
        if oid in self.archive:
            return dict(self.archive[oid])
        raise ccxt.OrderNotFound(f"Unknown order sent {oid}")

    def create_order(self, symbol=None, type=None, side=None, amount=None,
                     price=None, params=None, **kw):
        self._id_seq += 1
        oid = f"ord{self._id_seq}"
        p = params or {}
        info = {'type': str(type)}
        if p.get('stopPrice') is not None:
            info['stopPrice'] = p['stopPrice']
        if p.get('reduceOnly'):
            info['reduceOnly'] = 'true'
        order = {'id': oid, 'symbol': symbol, 'status': 'open', 'type': type, 'side': side,
                 'amount': float(amount) if amount is not None else None,
                 'price': float(price) if price is not None else None,
                 'stopPrice': p.get('stopPrice'), 'params': dict(p), 'info': info,
                 'average': None, 'filled': 0.0}
        self.orders[oid] = order
        return dict(order)

    def cancel_order(self, order_id, symbol=None, params=None, **kw):
        oid = str(order_id)
        if oid in self.fail_cancel:
            self._ev('cancel_order_FAIL', oid)
            raise ccxt.ExchangeError(f'cancel rejected {oid}')
        if oid in self.orders:
            o = self.orders.pop(oid)
            o['status'] = 'canceled'
            self.archive[oid] = o
            self._ev('cancel_order', f"{oid} → canceled")
            return dict(o)
        self._ev('cancel_order', f"{oid} → Unknown order")
        raise ccxt.OrderNotFound(f"Unknown order sent {oid}")

    def price_to_precision(self, symbol, value):
        return float(value)

    def amount_to_precision(self, symbol, value):
        return float(value)

    def seed_order(self, oid, otype, side, amount, stop_price=None):
        order = {'id': oid, 'symbol': SYMBOL, 'status': 'open', 'type': otype, 'side': side,
                 'amount': float(amount), 'price': None, 'stopPrice': stop_price,
                 'params': {}, 'info': {'type': otype, 'stopPrice': stop_price,
                                        'reduceOnly': 'true'},
                 'average': None, 'filled': 0.0}
        self.orders[oid] = order
        return order

    def seed_filled(self, oid, otype, side, amount, avg):
        order = {'id': oid, 'symbol': SYMBOL, 'status': 'closed', 'type': otype, 'side': side,
                 'amount': float(amount), 'price': avg, 'stopPrice': None, 'params': {},
                 'info': {'type': otype}, 'average': avg, 'filled': float(amount)}
        self.archive[oid] = order
        return order

    def labels(self):
        return [e[0] for e in self.events]


# =====================================================================
# Env：STATE_FILE / 墓碑重定向（惯例同 test_c_batch）
# =====================================================================
class _Env:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='p0b_')
        self.state_file = os.path.join(self.dir, 'trade_state.json')
        self.tomb_file = os.path.join(self.dir, 'trade_tombstones.json')
        trader_260725.STATE_FILE = self.state_file
        return self

    def __exit__(self, *a):
        trader_260725.STATE_FILE = _ORIG_STATE_FILE
        return False

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def load_tomb(self):
        if os.path.exists(self.tomb_file):
            with open(self.tomb_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def write_state(self, data):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)


# =====================================================================
# fake trader（惯例：hasattr 保护绑定 + MagicMock 数值坑防御）
# =====================================================================
def make_fake_b(env, ex):
    fake = mock.MagicMock()
    fake._state_lock = threading.Lock()          # 生产同款非重入锁
    fake._api_cooldown_until = 0
    fake._tp_breaker_alerted = None
    fake._tombstone_alerted = set()
    fake._converge_alert_counts = {}             # ⚠️ 不绑 → getattr MagicMock 非 None
    fake.tombstone_file = env.tomb_file
    fake.exchange = ex
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get('level', 'info'), str(text)))
    _bind = ('save_batch_state', 'clear_batch_state', 'load_all_states',
             '_persist_states', '_load_tombstones', '_persist_tombstones',
             '_prune_tombstones', '_merge_batch_state', '_collect_batch_order_ids',
             '_update_registry', '_commit_registry_txn',
             '_find_registry_identity_by_order_id', '_order_matches_intent',
             '_get_current_position_amt', '_registry_has_unresolved_entries',
             # P0 Batch B 新 helper（RED 阶段缺失 → MagicMock → FAIL，即 RED 信号）
             '_converge_batch_orders_before_clear', '_converge_cancel_order',
             '_get_amount_precision', '_batch_has_active_exposure',
             '_verify_clear_proof', '_converge_alert',
             # 🔥 P5：FULL_FILL 共享 finalizer（结算段已从 monitor 抽到该函数；
             # 未绑定 → MagicMock 静默吞掉 → 撤 TP/clear 全不发生，测试假红）
             '_finalize_limit_full_fill', '_claim_settlement_reported',
             '_batch_net_position', '_notify_snapshot',
             # 🔥 T1C-v2A：结算事务 outbox 路径 helper（未绑定 → MagicMock 泄漏进
             # pending_settlement → json.dump 失败 → outbox 持久化失败 → 批次不清算，
             # 复现生产接线后的 G-B1/G-B2/R-B1 假红）。同 P5 绑定惯例。
             '_build_settlement_evidence', '_derive_base_dedup',
             '_derive_settlement_mode',
             '_atomic_outbox_begin', '_try_finalize_outbox',
             '_resume_pending_settlement', '_record_realized_pnl')
    for _n in _bind:
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n, (lambda _n=_n: lambda *a, **k: getattr(CryptoTrader, _n)(
                fake, *a, **k))())
    # 🔥 v2A：_record_realized_pnl 经 __file__ 相邻路径写真实 trade_stats.json，
    # 测试须重定向到 tmp（r99 生产文件零污染哨兵）。本方法忽略 self.stats_file，
    # 故显式注入 stats_file 参数（仅测试隔离，不改生产契约）。
    _stats_tmp = os.path.join(env.dir, 'trade_stats.json')
    fake._stats_file = _stats_tmp
    _real_rec = fake._record_realized_pnl
    fake._record_realized_pnl = lambda *a, **k: _real_rec(*a, stats_file=_stats_tmp, **k)
    return fake


def _batch(**over):
    b = {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'is_hedge_mode': False, 'entry_orders': [], 'stop_steps': [55000.0],
        'take_profit_price': 60000.0, 'current_sl_id': 'sl1', 'tp_order_id': 'tp1',
        # 🔥 P5：限价平仓事务必有 close_op_id（finalizer 代际隔离依赖，生产契约）
        'close_op_id': 'OP1',
        'close_phase': 2, 'settled_by_limit_close': True,
        'batch_total_amount': 0.002, 'target_amounts': [0.002],
        'last_filled_count': 1, 'filled_details': [85000.0], 'total_entry_fee': 0.5,
        'user_modified': False, 'pending_close': True, 'is_programmatic_cancel': True,
        'protection_registry': {},
    }
    b.update(over)
    return b


def _reg(state='CONFIRMED', order_id='sl1', **over):
    e = {'state': state, 'order_id': order_id, 'id_known': order_id is not None,
         'order_kind': 'conditional', 'role': 'SL', 'layer': 1, 'side': 'LONG',
         'intent': None, 'updated_at': time.time()}
    e.update(over)
    return e


def _valid_proof(scope='FULL', batch_id=BATCH, symbol=SYMBOL, **over):
    p = {
        'batch_id': batch_id, 'symbol': symbol, 'checked_at': time.time(),
        'scope': scope, 'position_zero': True,
        'state_ids_resolved': ['sl1', 'tp1'], 'exchange_scan': 'zero',
        'l1_canceled': [], 'l2_canceled': [], 'l3_orphans': [],
    }
    p.update(over)
    return p


def _call(name, self_, *a, **k):
    """异常安全调用（RED 阶段 helper 缺失/签名不符 → 异常即 FAIL 信号）。
    按名字延迟取属性：AttributeError 发生在调用前，必须包进 try。"""
    try:
        fn = getattr(CryptoTrader, name)
    except AttributeError as e:
        return None, f"AttributeError: {e}"
    try:
        return fn(self_, *a, **k), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _crits(fake):
    return [s[1] for s in fake.sent if s[0] == 'critical']


# =====================================================================
# R-B1 / G-B2：B0 结算段撤 TP（_monitor_limit_close 同步驱动）
# =====================================================================
def t_b0_settle_tp():
    with _Env() as env:
        ex = FakeExchange()
        # SL 在场 + TP 残留在场（事故形态：结算只撤 SL 不撤 TP）
        ex.seed_order('sl1', 'STOP_MARKET', 'SELL', 0.002, 75002.0)
        ex.seed_order('tp1', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
        ex.seed_filled('lc1', 'LIMIT', 'SELL', 0.002, 84900.0)
        env.write_state({SYMBOL: {BATCH: _batch(
            limit_close_order_id='lc1',
            protection_registry={
                IDENT_SL: _reg(state='CONFIRMED', order_id='sl1', role='SL'),
                IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP'),
            })}})
        fake = make_fake_b(env, ex)
        with mock.patch('trader_260725.time.sleep', lambda s: None):
            _, err = _call('_monitor_limit_close', fake, SYMBOL, BATCH, 'lc1',
                           0.002, 85000.0, 0.5, 'BUY', 1, [0.002], [85000.0])
        # R-B1：结算后本批次 TP 残单必须 = 0（现状只撤 SL → RED）
        # 🔥 P5 契约更新：FULL_FILL 由共享 finalizer 直接 converge+clear（替代
        # 「不调 clear、留给主循环 finally」旧契约），故 registry 终态证据改查墓碑
        ok1 = err is None and 'tp1' not in ex.orders and 'sl1' not in ex.orders
        tomb = env.load_tomb() or {}
        conv = []
        for v in (tomb.values() if isinstance(tomb, dict) else tomb):
            conv = list((v or {}).get('converged_order_ids') or [])
            if conv:
                break
        ok2 = ('tp1' in conv and 'sl1' in conv)
        report("R-B1/B0结算撤TP+终态(墓碑收敛证据)", ok1 and ok2,
               f"(err={err}, tp_open={'tp1' in ex.orders}, 收敛={conv})")

        # G-B2 幂等：TP 已不在交易所（-2011）→ 视为事实终态，零 critical
        env2_ok = True
        with _Env() as env2:
            ex2 = FakeExchange()
            ex2.seed_filled('lc2', 'LIMIT', 'SELL', 0.002, 84900.0)
            env2.write_state({SYMBOL: {BATCH: _batch(
                limit_close_order_id='lc2',
                protection_registry={IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP')})}})
            fake2 = make_fake_b(env2, ex2)
            with mock.patch('trader_260725.time.sleep', lambda s: None):
                _, err2 = _call('_monitor_limit_close', fake2, SYMBOL, BATCH, 'lc2',
                                0.002, 85000.0, 0.5, 'BUY', 1, [0.002], [85000.0])
            # 🔥 P5 契约更新：finalizer 收敛后状态已清理 → -2011 幂等证据改查墓碑收敛串
            tomb2 = env2.load_tomb() or {}
            conv2 = []
            for v in (tomb2.values() if isinstance(tomb2, dict) else tomb2):
                conv2 = list((v or {}).get('converged_order_ids') or [])
                if conv2:
                    break
            env2_ok = (err2 is None and 'tp1' in conv2 and not _crits(fake2))
            report("G-B2/B0幂等(-2011=事实终态,零critical)", env2_ok,
                   f"(err={err2}, 收敛={conv2}, crit={len(_crits(fake2))})")


# =====================================================================
# R-B2 / G-B6 / G-B1(门侧)：proof 门
# =====================================================================
def t_proof_gate():
    with _Env() as env:
        env.write_state({SYMBOL: {BATCH: _batch(
            protection_registry={IDENT_SL: _reg(state='PROGRAMMATIC_CANCELED', order_id='sl1')})}})
        fake = make_fake_b(env, FakeExchange())
        # R-B2：无 proof 直调 clear → 必须拒绝 + state 保留 + critical（现状直删 → RED）
        ret, err = _call('clear_batch_state', fake, SYMBOL, BATCH)
        st = env.load_state().get(SYMBOL, {})
        crit = _crits(fake)
        ok = (err is None and ret is False and BATCH in st and len(crit) >= 1
              and any(('proof' in c or '收敛' in c) for c in crit))
        report("R-B2/无proof拒绝clear(state保留+critical)", ok,
               f"(err={err}, ret={ret}, state_has={BATCH in st}, crit={len(crit)})")

        # G-B6 三 case：缺键 / batch_id 不匹配 / exchange_scan≠zero
        c1 = _call('clear_batch_state', fake, SYMBOL, BATCH,
                   proof=_valid_proof(scope='FULL', l2_canceled=None) if False else
                   {k: v for k, v in _valid_proof().items() if k != 'exchange_scan'})
        c2 = _call('clear_batch_state', fake, SYMBOL, BATCH,
                   proof=_valid_proof(batch_id='other_batch'))
        c3 = _call('clear_batch_state', fake, SYMBOL, BATCH,
                   proof=_valid_proof(exchange_scan='unknown'))
        ok6 = all(r[0] is False and r[1] is None and BATCH in env.load_state().get(SYMBOL, {})
                  for r in (c1, c2, c3))
        report("G-B6/proof缺键·批次不符·scan≠zero全拒", ok6,
               f"(ret={[r[0] for r in (c1, c2, c3)]}, "
               f"state_has={BATCH in env.load_state().get(SYMBOL, {})})")

        # G-B1(门侧)：合法 FULL proof → clear 通过 → close_phase=3 进墓碑 + converged 升级
        fake.sent.clear()
        ret_ok, err_ok = _call('clear_batch_state', fake, SYMBOL, BATCH,
                               proof=_valid_proof(l1_canceled=['tp1', 'sl1'],
                                                  state_ids_resolved=['tp1', 'sl1']))
        tomb = env.load_tomb().get(BATCH, {})
        ok_g1 = (err_ok is None and ret_ok is True
                 and BATCH not in env.load_state().get(SYMBOL, {})
                 and tomb.get('close_phase') == 3
                 and sorted(tomb.get('converged_order_ids') or []) == ['sl1', 'tp1']
                 and 'tp1' in (tomb.get('known_order_ids') or []))
        report("G-B1/合法proof过门(墓碑close_phase=3+converged升级)", ok_g1,
               f"(err={err_ok}, tomb={ {k: tomb.get(k) for k in ('close_phase', 'converged_order_ids')} })")

        # PRE_ENTRY：零敞口批次接受 scope=PRE_ENTRY
        with _Env() as env3:
            env3.write_state({SYMBOL: {BATCH: _batch(
                last_filled_count=0, close_phase=0, pending_close=False,
                is_programmatic_cancel=False, current_sl_id=None, tp_order_id=None,
                settled_by_limit_close=False)}})
            fake3 = make_fake_b(env3, FakeExchange())
            ret3, err3 = _call('clear_batch_state', fake3, SYMBOL, BATCH,
                               proof=_valid_proof(scope='PRE_ENTRY', state_ids_resolved=[]))
            report("PRE_ENTRY/零敞口接受PRE_ENTRY proof",
                   err3 is None and ret3 is True and BATCH not in env3.load_state().get(SYMBOL, {}),
                   f"(ret={ret3}, err={err3})")

        # 敞口批次拒 PRE_ENTRY proof（修正1：当前敞口判定）
        with _Env() as env4:
            env4.write_state({SYMBOL: {BATCH: _batch()}})   # last_filled=1 → 敞口
            fake4 = make_fake_b(env4, FakeExchange())
            ret4, err4 = _call('clear_batch_state', fake4, SYMBOL, BATCH,
                               proof=_valid_proof(scope='PRE_ENTRY'))
            report("修正1/敞口批次拒PRE_ENTRY proof",
                   err4 is None and ret4 is False and BATCH in env4.load_state().get(SYMBOL, {}),
                   f"(ret={ret4}, err={err4})")


# =====================================================================
# R-B3 / G-B3：扫描 UNKNOWN → 不 clear + close_phase 保持 2 + 告警 3 轮去重
# =====================================================================
def t_converge_unknown():
    with _Env() as env:
        ex = FakeExchange()
        ex.scan_fail = True
        env.write_state({SYMBOL: {BATCH: _batch(close_phase=2)}})
        fake = make_fake_b(env, ex)
        # 5 次 UNKNOWN converge → critical 恰 3 次（同键 3 轮 SILENCED）
        rets = []
        for _ in range(5):
            r, e = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
            rets.append((r, e))
        st = env.load_state().get(SYMBOL, {}).get(BATCH, {})
        crit = _crits(fake)
        ok = all(r is None and e is None for r, e in rets) and \
            BATCH in env.load_state().get(SYMBOL, {}) and \
            int(st.get('close_phase', 0) or 0) == 2 and len(crit) == 3
        report("R-B3·G-B3/UNKNOWN不clear+phase保持2+3轮去重", ok,
               f"(rets_none={all(r is None for r, _ in rets)}, "
               f"phase={st.get('close_phase')}, crit={len(crit)})")
        # 恢复扫描 → converge 成功 → clear 通过（重试回路闭环）
        ex.scan_fail = False
        r_ok, e_ok = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        cleared, e_c = (None, None)
        if isinstance(r_ok, dict):
            cleared, e_c = _call('clear_batch_state', fake, SYMBOL, BATCH, proof=r_ok)
        report("G-B3重试/恢复后converge→proof→clear",
               e_ok is None and isinstance(r_ok, dict) and r_ok.get('exchange_scan') == 'zero'
               and cleared is True and e_c is None
               and BATCH not in env.load_state().get(SYMBOL, {}),
               f"(proof={'✓' if isinstance(r_ok, dict) else r_ok}, cleared={cleared})")


# =====================================================================
# R-B4 / G-B5 / G-B10：L1/L2/L3 分级 + registry 终态
# =====================================================================
def t_converge_l1_l2_l3():
    with _Env() as env:
        ex = FakeExchange()
        # 本批次 L1：tp1（镜像+registry CONFIRMED）
        ex.seed_order('tp1', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
        # 本批次 L2：registry PENDING_VERIFY id_known=False，intent 与订单完全一致
        ex.seed_order('or2', 'STOP_MARKET', 'SELL', 0.002, 70000.0)
        # 他批次资产：other batch SL 单（绝不碰）
        ex.seed_order('oth1', 'STOP_MARKET', 'SELL', 0.003, 74000.0)
        # L3 无主单：不在任何 L1/L2
        ex.seed_order('orphan1', 'STOP_MARKET', 'SELL', 0.5, 70000.0)
        env.write_state({SYMBOL: {
            BATCH: _batch(
                current_sl_id=None, protection_registry={
                    IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP', intent=None),
                    IDENT_PV: _reg(state='PENDING_VERIFY', order_id=None, id_known=False,
                                   intent=dict(L2_INTENT), role='SL', layer=2),
                }),
            'batch_o': _batch(batch_id='batch_o', current_sl_id='oth1', tp_order_id=None,
                              last_filled_count=0, close_phase=0, pending_close=False,
                              is_programmatic_cancel=False, settled_by_limit_close=False,
                              protection_registry={}),
        }})
        fake = make_fake_b(env, ex)
        proof, err = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        ok_shape = err is None and isinstance(proof, dict) and \
            proof.get('exchange_scan') == 'zero' and proof.get('position_zero') is True
        # L1/L2 自动撤；他批次与 L3 不碰
        ok_cancel = ok_shape and 'tp1' not in ex.orders and 'or2' not in ex.orders and \
            'oth1' in ex.orders and 'orphan1' in ex.orders
        # proof 记录：l1/l2/l3_orphans
        ok_proof = ok_shape and 'tp1' in (proof.get('l1_canceled') or []) and \
            'or2' in (proof.get('l2_canceled') or []) and \
            [x.get('id') for x in (proof.get('l3_orphans') or [])] == ['orphan1']
        # G-B10：registry L1 终态化（order_id 保留）+ L2 收编写 id
        reg = env.load_state().get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
        ok_reg = ok_shape and \
            reg.get(IDENT_TP, {}).get('state') == 'PROGRAMMATIC_CANCELED' and \
            reg.get(IDENT_TP, {}).get('order_id') == 'tp1' and \
            reg.get(IDENT_PV, {}).get('state') == 'PROGRAMMATIC_CANCELED' and \
            reg.get(IDENT_PV, {}).get('order_id') == 'or2'
        # L3 告警（critical 列示）
        ok_alert = ok_shape and any('orphan1' in c for c in _crits(fake))
        report("R-B4·G-B5/L1撤+L2撤+L3列示不撤+他批次不碰", ok_cancel and ok_proof,
               f"(tp1={'tp1' in ex.orders}, or2={'or2' in ex.orders}, "
               f"oth1={'oth1' in ex.orders}, orphan1={'orphan1' in ex.orders})")
        report("G-B10/registry终态(L1保留id+L2收编id)", ok_reg,
               f"(tp={reg.get(IDENT_TP, {}).get('state')}, pv={reg.get(IDENT_PV, {}).get('state')}"
               f"/id={reg.get(IDENT_PV, {}).get('order_id')})")
        report("G-B5b/L3 critical告警列单详情", ok_alert, f"(crit={len(_crits(fake))})")
        # D-B4：L3 不阻塞 clear
        cleared, e_c = (None, None)
        if ok_shape:
            cleared, e_c = _call('clear_batch_state', fake, SYMBOL, BATCH, proof=proof)
        ok_clear = ok_shape and cleared is True and e_c is None and \
            BATCH not in env.load_state().get(SYMBOL, {}) and 'batch_o' in env.load_state().get(SYMBOL, {})
        report("D-B4/L3不阻塞clear(批次清理+他批次保留)", ok_clear,
               f"(cleared={cleared}, err={e_c})")


# =====================================================================
# R-B5：L1 撤单失败（非 -2011）→ 不收敛不 clear
# =====================================================================
def t_converge_cancel_fail():
    with _Env() as env:
        ex = FakeExchange()
        ex.seed_order('tp1', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
        ex.fail_cancel = {'tp1'}
        env.write_state({SYMBOL: {BATCH: _batch(
            current_sl_id=None,
            protection_registry={IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP')})}})
        fake = make_fake_b(env, ex)
        proof, err = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        ok = err is None and proof is None and 'tp1' in ex.orders and \
            BATCH in env.load_state().get(SYMBOL, {}) and len(_crits(fake)) >= 1
        report("R-B5/L1撤单失败→不收敛不clear+critical", ok,
               f"(proof={proof}, tp1_open={'tp1' in ex.orders}, crit={len(_crits(fake))})")


# =====================================================================
# G-B7：启动 monitor_error 批次 → converge 撤 L1 → 才 clear
# =====================================================================
def t_g_b7_startup_stale():
    with _Env() as env:
        ex = FakeExchange()
        ex.seed_order('tp7', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
        env.write_state({SYMBOL: {BATCH: _batch(
            batch_id=BATCH, monitor_error=True, current_sl_id=None, tp_order_id='tp7',
            protection_registry={IDENT_TP: _reg(state='CONFIRMED', order_id='tp7', role='TP')})}})
        fake = make_fake_b(env, ex)
        ret, err = _call('recover_active_batches', fake)
        ok = (err is None and ret is True and 'tp7' not in ex.orders
              and BATCH not in env.load_state().get(SYMBOL, {})
              and 'cancel_order' in ex.labels())
        report("G-B7/启动monitor_error批次converge后才clear", ok,
               f"(err={err}, tp7_open={'tp7' in ex.orders}, "
               f"state_has={BATCH in env.load_state().get(SYMBOL, {})})")


# =====================================================================
# G-B8：多批次同 symbol 贡献扣减
# =====================================================================
def t_g_b8_multi_batch():
    with _Env() as env:
        ex = FakeExchange()
        ex.position_amt = 0.001   # 只剩 batch_o 的持仓（batch_b 已平）
        ex.seed_order('tp1', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)   # batch_b 残单
        ex.seed_order('oth1', 'STOP_MARKET', 'SELL', 0.001, 74000.0)         # batch_o SL
        env.write_state({SYMBOL: {
            BATCH: _batch(current_sl_id=None, target_amounts=[0.002],
                          protection_registry={IDENT_TP: _reg(state='CONFIRMED',
                                                               order_id='tp1', role='TP')}),
            'batch_o': _batch(batch_id='batch_o', current_sl_id='oth1', tp_order_id=None,
                              target_amounts=[0.001], last_filled_count=1,
                              close_phase=0, pending_close=False, is_programmatic_cancel=False,
                              settled_by_limit_close=False, protection_registry={}),
        }})
        fake = make_fake_b(env, ex)
        proof, err = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        cleared = None
        if isinstance(proof, dict):
            cleared, _ = _call('clear_batch_state', fake, SYMBOL, BATCH, proof=proof)
        st = env.load_state().get(SYMBOL, {})
        ok = (err is None and isinstance(proof, dict) and proof.get('position_zero') is True
              and cleared is True and 'tp1' not in ex.orders and 'oth1' in ex.orders
              and BATCH not in st and 'batch_o' in st)
        report("G-B8/多批次贡献扣减(A可清+B不被碰)", ok,
               f"(proof={type(proof).__name__}, cleared={cleared}, "
               f"oth1_open={'oth1' in ex.orders}, batch_o_alive={'batch_o' in st})")


# =====================================================================
# D-B1：贡献残留拒绝 + precision 容差
# =====================================================================
def t_d_b1_contribution():
    with _Env() as env:
        # 残留：symbol_pos=0.002 全部为本批次贡献（无其他批次）→ 拒绝
        ex = FakeExchange()
        ex.position_amt = 0.002
        env.write_state({SYMBOL: {BATCH: _batch(current_sl_id=None)}})
        fake = make_fake_b(env, ex)
        p1, e1 = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        ok_rej = e1 is None and p1 is None and any('持仓' in c or '贡献' in c
                                                   for c in _crits(fake))
        report("D-B1/贡献残留>容差→不收敛(不clear)", ok_rej, f"(proof={p1})")

    with _Env() as env:
        # 容差：其他批次 0.001，symbol_pos=0.0015 → 贡献 0.0005 ≤ precision(0.001) → 收敛
        ex = FakeExchange()
        ex.position_amt = 0.0015
        ex.markets = {SYMBOL: {'precision': {'amount': 3}}}   # 3 位小数 → 1e-3 容差
        env.write_state({SYMBOL: {
            BATCH: _batch(current_sl_id=None),
            'batch_o': _batch(batch_id='batch_o', target_amounts=[0.001], last_filled_count=1,
                              close_phase=0, pending_close=False, is_programmatic_cancel=False,
                              settled_by_limit_close=False, protection_registry={})}})
        fake = make_fake_b(env, ex)
        p2, e2 = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        report("D-B1/precision容差内(0.0005≤1e-3)→收敛",
               e2 is None and isinstance(p2, dict) and p2.get('position_zero') is True,
               f"(proof={type(p2).__name__})")


# =====================================================================
# D-B3：未决条目终态化 ABSENT（三条件）
# =====================================================================
def t_d_b3_absent():
    with _Env() as env:
        ex = FakeExchange()   # 空挂单 + position 0
        env.write_state({SYMBOL: {BATCH: _batch(
            current_sl_id=None, tp_order_id=None,
            protection_registry={IDENT_PV: _reg(state='PENDING_VERIFY', order_id=None,
                                                 id_known=False, intent=dict(L2_INTENT),
                                                 role='SL', layer=2)})}})
        fake = make_fake_b(env, ex)
        proof, err = _call('_converge_batch_orders_before_clear', fake, SYMBOL, BATCH)
        reg = env.load_state().get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
        cleared = None
        if isinstance(proof, dict):
            cleared, _ = _call('clear_batch_state', fake, SYMBOL, BATCH, proof=proof)
        ok = (err is None and isinstance(proof, dict)
              and reg.get(IDENT_PV, {}).get('state') == 'ABSENT'
              and reg.get(IDENT_PV, {}).get('terminated_reason') == 'converge_absent'
              and cleared is True)
        report("D-B3/未决条目三条件下终态化ABSENT+可clear", ok,
               f"(pv={reg.get(IDENT_PV, {}).get('state')}, cleared={cleared})")


# =====================================================================
# G-B9：源码守卫（签名无逃生门 / close_phase=3 唯一写入 / converge 不调 clear）
# =====================================================================
def t_g_b9_source_guards():
    with open('trader_260725.py', 'r', encoding='utf-8') as f:
        src = f.read()
    tree = _ast.parse(src)
    # ① 签名：proof 唯一新增参数，无 force/skip_verify/proof_required
    clear_fn = next((n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)
                     and n.name == 'clear_batch_state'), None)
    args = [a.arg for a in clear_fn.args.args] if clear_fn else []
    ok1 = clear_fn is not None and 'proof' in args and not any(
        x in args for x in ('force', 'skip_verify', 'proof_required', 'skip_proof'))
    # ② close_phase = 3 写入点唯一（正则锚定赋值语句）
    writes = _re.findall(r"['\"]close_phase['\"]\]\s*=\s*3\b", src)
    ok2 = len(writes) == 1
    # ③ converge 函数体内不得调用 clear_batch_state（调用栈可审计纪律）
    conv_fn = next((n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)
                    and n.name == '_converge_batch_orders_before_clear'), None)
    calls = [n for n in _ast.walk(conv_fn) if isinstance(n, _ast.Call)
             and isinstance(n.func, _ast.Attribute)
             and n.func.attr == 'clear_batch_state'] if conv_fn else ['missing']
    ok3 = conv_fn is not None and len(calls) == 0
    report("G-B9/签名无逃生门", ok1, f"(args={args})")
    report("G-B9/close_phase=3写入点唯一", ok2, f"(writes={len(writes)})")
    report("G-B9/converge体内零clear调用", ok3, f"(conv_fn={'✓' if conv_fn else '缺失'})")


# =====================================================================
# G-B1 全生命周期：结算(B0) → converge → proof → clear → 墓碑
# =====================================================================
def t_g_b1_full_lifecycle():
    with _Env() as env:
        ex = FakeExchange()
        ex.seed_order('sl1', 'STOP_MARKET', 'SELL', 0.002, 75002.0)
        ex.seed_order('tp1', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
        ex.seed_filled('lc1', 'LIMIT', 'SELL', 0.002, 84900.0)
        env.write_state({SYMBOL: {BATCH: _batch(
            limit_close_order_id='lc1',
            protection_registry={
                IDENT_SL: _reg(state='CONFIRMED', order_id='sl1', role='SL'),
                IDENT_TP: _reg(state='CONFIRMED', order_id='tp1', role='TP'),
            })}})
        fake = make_fake_b(env, ex)
        # 🔥 P5 契约更新：FULL_FILL 由共享 finalizer 直接完成
        # B0 撤 TP/SL → converge 证明 → clear → 墓碑（旧契约「留给主循环 finally」
        # 已被 P5 finalizer 取代）。此处验证：
        #   ① monitor 驱动的完整生命周期（converge+clear+墓碑一次到位）
        #   ② 手工链（未结算态）converge→proof→clear 仍可用（幂等路径不回归）
        with mock.patch('trader_260725.time.sleep', lambda s: None):
            _call('_monitor_limit_close', fake, SYMBOL, BATCH, 'lc1',
                  0.002, 85000.0, 0.5, 'BUY', 1, [0.002], [85000.0])
        tomb = env.load_tomb().get(BATCH, {})
        ok1 = (BATCH not in env.load_state().get(SYMBOL, {})
               and not any(o for o in ex.orders.values())
               and tomb.get('close_phase') == 3
               and sorted(tomb.get('converged_order_ids') or []) == ['sl1', 'tp1'])
        # ② 手工链：另起一份未结算状态，验证 converge/clear 门链未被 P5 破坏
        ok2 = False
        with _Env() as env2:
            ex2 = FakeExchange()
            ex2.seed_order('sl2', 'STOP_MARKET', 'SELL', 0.002, 75002.0)
            ex2.seed_order('tp2', 'TAKE_PROFIT_MARKET', 'SELL', 0.002, 85000.0)
            env2.write_state({SYMBOL: {BATCH: _batch(
                settled_by_limit_close=False, close_phase=0, pending_close=False,
                is_programmatic_cancel=False, close_reason='',
                limit_close_order_id='',
                protection_registry={
                    IDENT_SL: _reg(state='CONFIRMED', order_id='sl2', role='SL'),
                    IDENT_TP: _reg(state='CONFIRMED', order_id='tp2', role='TP'),
                })}})
            fake2 = make_fake_b(env2, ex2)
            proof2, err2 = _call('_converge_batch_orders_before_clear', fake2,
                                 SYMBOL, BATCH)
            cleared2 = None
            if isinstance(proof2, dict):
                cleared2, _ = _call('clear_batch_state', fake2, SYMBOL, BATCH,
                                    proof=proof2)
            ok2 = (err2 is None and isinstance(proof2, dict)
                   and cleared2 is True
                   and BATCH not in env2.load_state().get(SYMBOL, {}))
        ok = ok1 and ok2
        proof = tomb.get('converged_order_ids') or []
        cleared = ok1
        report("G-B1/全生命周期(B0→converge→proof→clear→墓碑)", ok,
               f"(monitor链={ok1}, 手工链={ok2}, residual={len(ex.orders)}, "
               f"tomb_phase={tomb.get('close_phase')}, 收敛={proof})")


# =====================================================================
# main
# =====================================================================
def main():
    import io as _io
    buf = _io.StringIO()
    with redirect_stdout(buf):
        t_b0_settle_tp()
        t_proof_gate()
        t_converge_unknown()
        t_converge_l1_l2_l3()
        t_converge_cancel_fail()
        t_g_b7_startup_stale()
        t_g_b8_multi_batch()
        t_d_b1_contribution()
        t_d_b3_absent()
        t_g_b9_source_guards()
        t_g_b1_full_lifecycle()
    print(buf.getvalue())
    fails = [n for n, p in RESULTS if not p]
    print(f"\n{'=' * 60}\nBatch B 专项：{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    if fails:
        print("FAIL 清单：")
        for n in fails:
            print(f"  ❌ {n}")
    print('=' * 60)
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
