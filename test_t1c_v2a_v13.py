# -*- coding: utf-8 -*-
"""T1C-v2A（v1.2 终签）行为级测试（独立文件，不并入 P5 套件）。

锁定 ChatGPT 八项阻断中「必须靠真实行为而非 P5 流程」才能证明的契约：
  T1  四路径真实结算 → v2 富记录**原样落盘** trade_stats.json（不压扁成旧 8 参，项1）；
  T2  DISPUTED 证据矩阵 → 任一核心资格缺失即 DISPUTED，net_pnl_estimate 恒为 None
        （绝不 0 伪装普通零盈亏，项2/3 零猜测）；
  T3  DISPUTED 流程 → 写 settlement_dispute、**不撤保护不 clear**、批次保留待人工核对；
  T4  §9 财务授权门 → 无 stats_committed / authorization≠base_dedup_key / DISPUTED
        三种情况 clear_batch_state 一律拒绝（Fail-Closed）；
  T5  崩溃恢复 → outbox 持久化后重启 _resume_pending_settlement 幂等（同 dedup 只记一次）；
  T6  出场费率归属 → LIMIT=Maker、TP/SL/MARKET=Taker（项7）；
  T7  observed_qty 取真实成交 → 用 order.filled/executedQty，不人为构造 expected==observed（项6）。

所有用例经 make_trader 隔离到临时目录，不触碰生产账本（结尾生产文件免疫比对）。
"""
import copy
import hashlib
import os
import tempfile
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader
from test_p5_closecancel import (
    make_trader, _lp_batch, _single, _state_write, _state_read,
    SYM, BID, OP,
)

_PROD_FILES = ['trade_state.json', 'trade_tombstones.json', 'trade_stats.json',
               'auth_blocked.json']
_PROD_DIR = os.path.dirname(os.path.abspath(__file__))


def _v2_trades(t):
    return t._stats_trades()


def _settlement_records(t):
    """仅取结算/争议富记录（排除 schema_activation 边界记录）。"""
    return [x for x in _v2_trades(t)
            if x.get('record_type') in ('settlement', 'settlement_dispute')]


def _prod_snapshot():
    snap = {}
    for _n in _PROD_FILES:
        _p = os.path.join(_PROD_DIR, _n)
        try:
            with open(_p, 'rb') as _f:
                _data = _f.read()
            snap[_n] = (hashlib.sha256(_data).hexdigest(), len(_data), os.stat(_p).st_mtime_ns)
        except FileNotFoundError:
            snap[_n] = ('<missing>', 0, 0)
    return snap


_PROD_SNAP_AT_IMPORT = _prod_snapshot()


# 合法 proof（等同生产 _converge_batch_orders_before_clear 产物，避免依赖交易所 I/O）
_VALID_PROOF = {
    'batch_id': BID, 'symbol': SYM, 'scope': 'FULL',
    'position_zero': True, 'state_ids_resolved': ['L1'],
    'exchange_scan': 'zero',
}


# ───────────────────────── T1：四路径真实 v2 富记录落盘 ─────────────────────────

def _finalize_limit(t, ex, order_id, spec, side='BUY'):
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['side'] = side
    b['close_op_id'] = OP
    b['limit_close_order_id'] = order_id
    _state_write(t, _single(b))
    ex._mk(order_id, **spec)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    return t._finalize_limit_full_fill(SYM, BID, order_id)


def test_limit_close_writes_real_v2_stats():
    """T1-LIMIT：限价全平 → v2 富记录（PROVEN）原样落盘，含 net_pnl_estimate 与
    legacy 兼容别名 net_pnl；批次经授权 clear 归档。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    ok, msg = _finalize_limit(t, ex, 'L1',
                              dict(otype='LIMIT', amount=0.002, status='closed',
                                   filled=0.002, avg=76500.0), side='BUY')
    assert ok, f'finalize 应成功: {msg}'
    trades = _settlement_records(t)
    assert len(trades) == 1, f'必须恰好 1 条 v2 结算记录（非压扁）: {_v2_trades(t)}'
    r = trades[0]
    assert r.get('record_type') == 'settlement', r
    assert r.get('core_status') == 'PROVEN', r
    assert r.get('mode') == 'LIMIT', r
    # 🔥 项1/项2：富记录不得压扁；net_pnl_estimate 必须是有限非 None 浮点
    assert isinstance(r.get('net_pnl_estimate'), float), r
    assert r.get('net_pnl') == r.get('net_pnl_estimate'), 'legacy 别名必须与估算一致'
    assert r.get('gross_pnl') is not None, 'gross_pnl 必须结算'
    assert r.get('pnl_status') == 'ESTIMATED', 'PROVEN 仍标 ESTIMATED（异步对账，非最终权威）'
    assert BID not in _state_read(t).get(SYM, {}), 'PROVEN 结算后批次应归档'


def _finalize_mode(t, ex, mode, order_id, spec, exit_ref, side='BUY'):
    """SL/TP/MARKET 经各自 outbox BEGIN + 共享 finalizer（非 _finalize_limit_full_fill）。"""
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['side'] = side
    b['close_op_id'] = OP
    b['current_sl_id'] = 'S1'
    b['tp_order_id'] = 'T1'
    b['limit_close_order_id'] = 'L1'
    _state_write(t, _single(b))
    ex._mk(order_id, **spec)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long', 'positionSide': 'LONG'}]
    _net_qty, _net_cost = t._batch_net_position(b)
    rec = t._atomic_outbox_begin(
        batch_id=BID, symbol=SYM, mode=mode, generation=order_id,
        exit_order_ref=exit_ref, observed_qty=0.002, exit_price=spec.get('avg', 76000.0),
        expected_qty=_net_qty, net_cost=_net_cost, entry_fee_estimate=0.15)
    if not isinstance(rec, dict):
        return False, f'BEGIN 失败: {rec}'
    if rec.get('core_status') != 'PROVEN':
        t._try_finalize_outbox(BID, SYM)
        return False, 'DISPUTED'
    return t._try_finalize_outbox(BID, SYM), 'ok'


def test_sl_close_writes_real_v2_stats():
    """T1-SL：止损全平 → v2 富记录 PROVEN、mode=SL。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    ok, msg = _finalize_mode(t, ex, 'SL', 'S1',
                              dict(otype='STOP_MARKET', amount=0.002, status='closed',
                                   filled=0.002, avg=74000.0),
                              exit_ref={'kind': 'algo', 'order_id': 'S1'})
    assert ok, f'finalize 应成功: {msg}'
    trades = _settlement_records(t)
    assert len(trades) == 1, trades
    assert trades[0].get('core_status') == 'PROVEN' and trades[0].get('mode') == 'SL', trades[0]


def test_tp_close_writes_real_v2_stats():
    """T1-TP：止盈全平 → v2 富记录 PROVEN、mode=TP。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    ok, msg = _finalize_mode(t, ex, 'TP', 'T1',
                              dict(otype='TAKE_PROFIT_MARKET', amount=0.002, status='closed',
                                   filled=0.002, avg=80000.0),
                              exit_ref={'kind': 'algo', 'order_id': 'T1'})
    assert ok, f'finalize 应成功: {msg}'
    trades = _settlement_records(t)
    assert len(trades) == 1, trades
    assert trades[0].get('core_status') == 'PROVEN' and trades[0].get('mode') == 'TP', trades[0]


def test_market_close_writes_real_v2_stats():
    """T1-MARKET：市价全平 → v2 富记录 PROVEN、mode=MARKET。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    ok, msg = _finalize_mode(t, ex, 'MARKET', OP,
                              dict(otype='MARKET', amount=0.002, status='closed',
                                   filled=0.002, avg=76000.0),
                              exit_ref={'kind': 'regular', 'order_id': OP})
    assert ok, f'finalize 应成功: {msg}'
    trades = _settlement_records(t)
    assert len(trades) == 1, trades
    assert trades[0].get('core_status') == 'PROVEN' and trades[0].get('mode') == 'MARKET', trades[0]


# ───────────────────────── T2：DISPUTED 证据矩阵（零猜测） ─────────────────────────

def _evidence_ok(t, **over):
    base = dict(batch_id=BID, symbol=SYM, side='BUY', mode='LIMIT',
                base_dedup_key=f'{SYM}:L1', settlement_id='v2a_x',
                exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
                entry_order_refs=[{'kind': 'regular', 'order_id': 'E1',
                                   'expected_qty': 0.002}],
                expected_qty=0.002, observed_qty=0.002, net_cost=153.24,
                exit_price=76500.0, generation='L1')
    base.update(over)
    return t._build_settlement_evidence(**base)


def test_disputed_evidence_matrix_no_silent_zero():
    """T2：任一核心资格缺失 → DISPUTED；net_pnl_estimate 恒为 None（绝不压成 0）；
    dispute_reasons 必须枚举缺失项（零猜测）。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    matrix = {
        'side 非法': dict(side='WEIRD'),
        'exit_ref 缺 id': dict(exit_order_ref={'kind': 'regular', 'order_id': ''}),
        'exit_ref 缺类型': dict(exit_order_ref={'order_id': 'L1'}),
        'expected_qty<=0': dict(expected_qty=0.0),
        'observed_qty<=0': dict(observed_qty=0.0),
        'net_cost<=0': dict(net_cost=0.0),
        'exit_price<=0': dict(exit_price=0.0),
        'entry_refs 空': dict(entry_order_refs=[]),
        'generation 空': dict(generation=''),
    }
    for name, over in matrix.items():
        rec = _evidence_ok(t, **over)['record']
        assert rec.get('core_status') == 'DISPUTED', \
            f'[{name}] 应 DISPUTED: {rec}'
        assert rec.get('net_pnl_estimate') is None, \
            f'[{name}] DISPUTED 不得写权威 net_pnl（None，非 0）: {rec}'
        assert rec.get('record_type') == 'settlement_dispute', rec
        assert rec.get('dispute_reasons'), \
            f'[{name}] 必须给出缺失原因: {rec}'


def test_proven_evidence_requires_all_fields():
    """T2 反例：全部资格齐备 → PROVEN 且 net_pnl_estimate 为有限浮点。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    rec = _evidence_ok(t)['record']
    assert rec.get('core_status') == 'PROVEN', rec
    assert isinstance(rec.get('net_pnl_estimate'), float), rec


# ───────────────────────── T3：DISPUTED 流程不 clear、保留 None 盈亏 ─────────────────────────

def test_disputed_flow_keeps_batch_and_none_pnl():
    """T3：真实结算路径（entry_refs 缺失）触发 DISPUTED —— 写 settlement_dispute、
    不撤保护单、不 clear、批次保留待人工核对；stats 中 net_pnl_estimate 为 None。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    _state_write(t, _single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002, avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long', 'positionSide': 'LONG'}]
    # 🔥 触发 DISPUTED：入场引用推导为空（真实故障模式）
    with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
        ok, msg = t._finalize_limit_full_fill(SYM, BID, 'L1')
    # §8.3（ChatGPT 裁定 2026-09-05）：DISPUTED 是自动化终态 / 人工核对态，
    # 不是可重试失败 → 必须返回 SETTLE_MANUAL_REVIEW（与 SETTLE_CLEARED 同属终态）
    assert ok == trader_260725.SETTLE_MANUAL_REVIEW, \
        f'DISPUTED 应返回终态 SETTLE_MANUAL_REVIEW（不可重试）: {ok!r} / {msg}'
    trades = _settlement_records(t)
    assert len(trades) == 1, trades
    d = trades[0]
    assert d.get('core_status') == 'DISPUTED', d
    assert d.get('net_pnl_estimate') is None, 'DISPUTED 不得写权威净盈亏'
    # 批次未被 clear（仍 active，close_reason 改为 settlement_disputed）
    bb = _state_read(t).get(SYM, {}).get(BID)
    assert bb is not None and bb.get('is_active'), 'DISPUTED 批次必须保留'
    assert bb.get('close_reason') == 'settlement_disputed', bb


# ───────────────────────── T4：§9 财务授权门（Fail-Closed） ─────────────────────────

def _write_outbox(t, *, core_status='PROVEN', stats_committed=True, dedup=f'{SYM}:L1',
                  disputed=False):
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    b['close_phase'] = 2
    b['pending_close'] = True
    rec = {
        'record_type': 'settlement_dispute' if disputed else 'settlement',
        'core_status': core_status,
        'net_pnl_estimate': None if disputed else 0.5,
        'dedup_key': dedup, 'batch_id': BID, 'symbol': SYM,
    }
    b['pending_settlement'] = {
        'schema': 2, 'base_dedup_key': dedup, 'settlement_id': 'v2a_x',
        'record': rec, 'evidence': {}, 'stats_committed': stats_committed,
    }
    _state_write(t, _single(b))
    return b


def test_clear_rejected_without_stats_committed():
    """T4-a：stats_committed=False → clear 拒绝（保留 outbox 待重试）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    _write_outbox(t, core_status='PROVEN', stats_committed=False)
    ok = t.clear_batch_state(SYM, BID, proof=_VALID_PROOF, authorization=f'{SYM}:L1')
    assert not ok, 'stats 未提交必须拒绝 clear'
    assert BID in _state_read(t).get(SYM, {}), '批次必须保留'


def test_clear_rejected_on_authorization_mismatch():
    """T4-b：authorization ≠ base_dedup_key → clear 拒绝（防旧路径伪造授权清批）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    _write_outbox(t, core_status='PROVEN', stats_committed=True, dedup=f'{SYM}:L1')
    ok = t.clear_batch_state(SYM, BID, proof=_VALID_PROOF, authorization='WRONG:KEY')
    assert not ok, '授权不匹配必须拒绝 clear'
    assert BID in _state_read(t).get(SYM, {}), '批次必须保留'


def test_clear_rejected_on_disputed():
    """T4-c：核心证据 DISPUTED（即便 stats_committed=True）→ clear 拒绝（§8.3）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    _write_outbox(t, core_status='DISPUTED', stats_committed=True, disputed=True)
    ok = t.clear_batch_state(SYM, BID, proof=_VALID_PROOF, authorization=f'{SYM}:L1')
    assert not ok, 'DISPUTED 必须拒绝 clear'
    assert BID in _state_read(t).get(SYM, {}), 'DISPUTED 批次必须保留'


def test_clear_allowed_when_proven_and_authorized():
    """T4-d：PROVEN + stats_committed + authorization 匹配 → clear 成功（对照）。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    _write_outbox(t, core_status='PROVEN', stats_committed=True, dedup=f'{SYM}:L1')
    ok = t.clear_batch_state(SYM, BID, proof=_VALID_PROOF, authorization=f'{SYM}:L1')
    assert ok, 'PROVEN+授权匹配必须允许 clear'
    assert BID not in _state_read(t).get(SYM, {}), '应已归档'


# ───────────────────────── T5：崩溃恢复幂等（dedup） ─────────────────────────

def test_resume_pending_settlement_dedup_once():
    """T5：outbox 持久化后，_resume_pending_settlement 重跑 → 同 dedup 只记一次
    （崩溃窗口 stats 已追加但 stats_committed 未持久化由 dedup 闭合）。"""
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    _state_write(t2, _single(b))
    # 只落 outbox（stats_committed 默认 False）—— 模拟 BEGIN 成功但未 finalize 的崩溃点
    rec = t2._atomic_outbox_begin(
        batch_id=BID, symbol=SYM, mode='LIMIT', generation='L1',
        exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
        observed_qty=0.002, exit_price=76500.0,
        expected_qty=0.002, net_cost=153.24, entry_fee_estimate=0.15)
    assert isinstance(rec, dict) and rec.get('core_status') == 'PROVEN', rec
    # 第一次 resume：记 stats + clear
    assert t2._resume_pending_settlement(BID, SYM) == trader_260725.SETTLE_CLEARED, \
        'resume 应成功结算并清理（SETTLE_CLEARED）'
    n1 = len(_settlement_records(t2))
    assert n1 == 1, f'resume 后必须恰好 1 条结算记录: {_v2_trades(t2)}'
    # 第二次 resume（批次已被 clear，不在账本）→ 必须幂等不报错、不重复记
    # 批次已 clear → 无可接管事务 → 必须仍是三态之一（此处为非终态 PENDING_RETRY）
    _st2 = t2._resume_pending_settlement(BID, SYM)
    assert _st2 in (trader_260725.SETTLE_CLEARED,
                    trader_260725.SETTLE_MANUAL_REVIEW,
                    trader_260725.SETTLE_PENDING_RETRY), f'返回值必须是三态: {_st2!r}'
    assert len(_settlement_records(t2)) == n1, 'dedup 必须保证同批次只记一次'


# ───────────────────────── T6：出场费率归属（Maker/Taker） ─────────────────────────

def test_fee_mode_maker_for_limit_taker_for_others():
    """T6：LIMIT→Maker、TP/SL/MARKET→Taker（项7）。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    for mode, expected_rate in (('LIMIT', trader_260725.MAKER_FEE_RATE),
                                ('TP', trader_260725.TAKER_FEE_RATE),
                                ('SL', trader_260725.TAKER_FEE_RATE),
                                ('MARKET', trader_260725.TAKER_FEE_RATE)):
        rec = _evidence_ok(t, mode=mode)['record']
        assert rec.get('core_status') == 'PROVEN', rec
        assert rec.get('mode') == mode, rec
        # 复算：估算出场费 = exit_notional * 预期费率（_build_settlement_evidence 内部 _exit_rate）
        notional = 76500.0 * 0.002
        exp_fee = round(notional * expected_rate, 6)
        if 'exit_fee_estimate' in rec:
            assert abs(float(rec['exit_fee_estimate']) - exp_fee) < 1e-6, \
                f'{mode} 出场费应属 {expected_rate:.5%}: {rec}'


# ───────────────────────── T7：observed_qty 取真实成交 ─────────────────────────

def test_observed_qty_from_real_fill_not_constructed():
    """T7：finalizer 取 order.filled（真实成交）。
    - 精确成交 0.002 → PROVEN，v2 记录 amount == 真实成交（非 net_qty 伪造）；
    - 部分成交 0.0015 → 合法 DISPUTED（量差不可调和），但 v2 记录 amount 仍 == 真实
      0.0015，绝不把 observed 偷偷改成 expected 去伪造 PROVEN（项6）。"""
    # (a) 精确成交：PROVEN 且 amount 取真实填充
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    ok, msg = _finalize_limit(t, ex, 'L1',
                              dict(otype='LIMIT', amount=0.002, status='closed',
                                   filled=0.002, avg=76500.0))
    assert ok, msg
    tr = _settlement_records(t)
    assert len(tr) == 1 and tr[0].get('core_status') == 'PROVEN', tr
    assert tr[0].get('amount') == 0.002, \
        f'PROVEN amount 必须取真实成交 0.002: {tr[0]}'
    # (b) 部分成交：合法 DISPUTED，但 amount 取真实 0.0015（不伪造）
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    _state_write(t2, _single(b))
    ex2._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.0015, avg=76500.0)
    ex2.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long', 'positionSide': 'LONG'}]
    ok2, msg2 = t2._finalize_limit_full_fill(SYM, BID, 'L1')
    assert ok2 == trader_260725.SETTLE_MANUAL_REVIEW, \
        f'部分成交应 DISPUTED 终态（量差不可调和）: {ok2!r} / {msg2}'
    tr2 = _settlement_records(t2)
    assert len(tr2) == 1 and tr2[0].get('core_status') == 'DISPUTED', tr2
    assert tr2[0].get('amount') == 0.0015, \
        f'DISPUTED amount 必须取真实成交 0.0015，而非 net_qty 0.002: {tr2[0]}'


# ────── T8：§8.3 DISPUTED 是自动化终态（连续多轮 / 跨重启只记一次） ──────

def _mk_disputed_env(tmp=None, net=0.002):
    """构造必然走向 DISPUTED 的限价全平场景（入场引用为空 → entry_refs 缺失）。
    tmp 可指定，用于跨重启场景复用同一份磁盘账本与 stats 文件。"""
    t, ex = make_trader(tmp if tmp is not None
                        else tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=net, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    b['current_sl_id'] = 'S1'
    b['tp_order_id'] = 'T1'
    _state_write(t, _single(b))
    ex._mk('S1', otype='STOP_MARKET', amount=net, stop=75001.0, status='open')
    ex._mk('T1', otype='TAKE_PROFIT_MARKET', amount=net, stop=79000.0, status='open')
    ex._mk('L1', otype='LIMIT', amount=net, status='closed', filled=net,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    return t, ex


def test_disputed_terminal_idempotent_across_rounds():
    """§8.3（ChatGPT 裁定 2026-09-05）：DISPUTED 是自动化终态 / 人工核对态，
    **不是可重试失败**。连续多轮触发 finalizer 必须：
      ① 只记一次 settlement_dispute（dedup 幂等，绝不重复写 stats）；
      ② 不重建 outbox（stats_committed 恒 True、settlement_id 不变）；
      ③ 不调用交易所撤单 API（撤 SL/TP 计数为 0）；
      ④ 不 clear（批次仍 active，close_reason='settlement_disputed'）；
      ⑤ 每轮稳定返回 SETTLE_MANUAL_REVIEW（终态 → 调用方停止重试）。"""
    t, ex = _mk_disputed_env()
    # spy：BEGIN / evidence 构造次数——进入终态后必须完全不再发生（活锁根因）
    _calls = {'begin': 0, 'evidence': 0}
    _real_begin, _real_ev = t._atomic_outbox_begin, t._build_settlement_evidence

    def _spy_begin(*a, **k):
        _calls['begin'] += 1
        return _real_begin(*a, **k)

    def _spy_ev(*a, **k):
        _calls['evidence'] += 1
        return _real_ev(*a, **k)

    t._atomic_outbox_begin = _spy_begin
    t._build_settlement_evidence = _spy_ev
    with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
        st1, _ = t._finalize_limit_full_fill(SYM, BID, 'L1')
    assert st1 == trader_260725.SETTLE_MANUAL_REVIEW, st1
    n1 = len(_settlement_records(t))
    assert n1 == 1, f'首轮必须恰好 1 条争议记录: {_v2_trades(t)}'
    ob1 = _state_read(t)[SYM][BID]['pending_settlement']
    assert ob1.get('stats_committed') is True, ob1
    sid1 = ob1.get('settlement_id')

    # 连续 4 轮（模拟监控线程持续轮询接管）
    for _ in range(4):
        with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
            st, _ = t._finalize_limit_full_fill(SYM, BID, 'L1')
        assert st == trader_260725.SETTLE_MANUAL_REVIEW, \
            f'⑤ 终态必须稳定返回: {st!r}'

    assert len(_settlement_records(t)) == n1, \
        f'① DISPUTED 连续多轮必须只记一次: {_settlement_records(t)}'
    ob2 = _state_read(t)[SYM][BID]['pending_settlement']
    assert ob2.get('stats_committed') is True, '② stats_committed 不得被重置回 False'
    assert ob2.get('settlement_id') == sid1, '② outbox 不得被重建（settlement_id 变更）'
    assert ex.cancel_calls == [], f'③ 终态后不得再调用交易所撤单 API: {ex.cancel_calls}'
    bb = _state_read(t).get(SYM, {}).get(BID)
    assert bb is not None and bb.get('is_active'), '④ DISPUTED 不得 clear 批次'
    assert bb.get('close_reason') == 'settlement_disputed', bb
    # ⑥ 终态短路必须在 BEGIN 之前生效：后续轮次一次都不许再 BEGIN / 构造 evidence
    assert _calls['begin'] == 1, f'⑥ 终态后不得再调 BEGIN（会重建 outbox）: {_calls}'
    assert _calls['evidence'] == 1, f'⑥ 终态后不得再构造 evidence: {_calls}'


def test_atomic_begin_reuses_same_dedup_outbox():
    """§8.3：同 dedup 重复 BEGIN 必须**复用原事务**——绝不重建 outbox、绝不把
    stats_committed 重置回 False。这是「记一次争议 → 重置 → 再记」活锁的根因修复点。"""
    t, _ = _mk_disputed_env()
    _begin_kw = dict(batch_id=BID, symbol=SYM, mode='LIMIT', generation='L1',
                     exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
                     observed_qty=0.002, exit_price=76500.0,
                     expected_qty=0.002, net_cost=153.24, entry_fee_estimate=0.15)
    with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
        rec1 = t._atomic_outbox_begin(**_begin_kw)
    assert isinstance(rec1, dict) and rec1.get('core_status') == 'DISPUTED', rec1
    ob1 = _state_read(t)[SYM][BID]['pending_settlement']
    assert ob1.get('stats_committed') is False, ob1
    # 标记为已提交（模拟 DISPUTED 已记账的终态）
    _st = _state_read(t)
    _st[SYM][BID]['pending_settlement']['stats_committed'] = True
    _state_write(t, _st)
    # 同 dedup 再次 BEGIN → 必须复用，不得把 stats_committed 打回 False
    with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
        rec2 = t._atomic_outbox_begin(**_begin_kw)
    assert isinstance(rec2, dict), '同 dedup BEGIN 必须复用原事务并返回 record'
    ob2 = _state_read(t)[SYM][BID]['pending_settlement']
    assert ob2.get('stats_committed') is True, \
        '同 dedup 复用不得把 stats_committed 重置回 False（活锁根因）'
    assert ob2.get('settlement_id') == ob1.get('settlement_id'), 'outbox 未被重建'


def test_disputed_terminal_idempotent_across_restart():
    """§8.3：跨重启只记一次。重启后新实例从磁盘加载同一 outbox（stats_committed
    已 True、close_reason 已 settlement_disputed）必须：
      ① 不再追加 settlement_dispute；② 直接返回终态 SETTLE_MANUAL_REVIEW；
      ③ 不撤保护单、不 clear。"""
    tmp = tempfile.mkdtemp(prefix='v2a_')
    t, ex = _mk_disputed_env(tmp)
    with mock.patch.object(t, '_derive_entry_order_refs', return_value=[]):
        st1, _ = t._finalize_limit_full_fill(SYM, BID, 'L1')
    assert st1 == trader_260725.SETTLE_MANUAL_REVIEW, st1
    n1 = len(_settlement_records(t))
    assert n1 == 1, _v2_trades(t)

    # ==== 模拟重启：同一临时目录重建实例（磁盘账本与 stats 文件延续）====
    t2, ex2 = make_trader(tmp)
    ex2.orders.update(ex.orders)
    ex2.positions = list(ex.positions)
    with mock.patch.object(t2, '_derive_entry_order_refs', return_value=[]):
        st2, _ = t2._finalize_limit_full_fill(SYM, BID, 'L1')
    assert st2 == trader_260725.SETTLE_MANUAL_REVIEW, f'② 重启后仍是终态: {st2!r}'
    assert len(_settlement_records(t2)) == n1, \
        f'① 跨重启必须只记一次: {_settlement_records(t2)}'
    assert ex2.cancel_calls == [], f'③ 重启后终态不得撤保护单: {ex2.cancel_calls}'
    bb = _state_read(t2).get(SYM, {}).get(BID)
    assert bb is not None and bb.get('is_active'), '③ 跨重启后批次仍保留（未 clear）'
    assert bb.get('close_reason') == 'settlement_disputed', bb


# ────── T9：裁定 2026-09-05 收口反例（七项行为锁定） ──────

def test_market_begin_precedes_all_cancels():
    """反例①：MARKET 路径 BEGIN 必须**先于**所有撤单动作。
    旧实现把 BEGIN 放在撤 ENTRY / TP / SL / 限价平仓单**之后**——BEGIN 一旦失败
    （stats CORRUPT、持久化失败、代际冲突），保护与残单早已被移除，而报告却称
    「保留恢复能力」，事实与口径严重不符且撤单不可逆。
    裁定顺序：ExitConfirmed → AtomicOutboxBegin → ReconcileRemainingOrders。
    直接从生产源码 AST 提取函数体，按源码行号验证先后（不依赖文档）。"""
    import ast as _ast
    src = open(os.path.join(_PROD_DIR, 'trader_260725.py'), encoding='utf-8').read()
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == 'close_position_market')
    begin_lineno = None
    cancels = []
    for node in _ast.walk(fn):
        if not isinstance(node, _ast.Call):
            continue
        fname = (node.func.id if isinstance(node.func, _ast.Name)
                 else getattr(node.func, 'attr', None))
        if fname == '_atomic_outbox_begin':
            begin_lineno = node.lineno
        elif fname in ('_cancel_and_verify_entry_orders',
                       '_cancel_limit_close_order', 'cancel_order'):
            cancels.append((fname, node.lineno))
    assert begin_lineno is not None, 'close_position_market 未调用 _atomic_outbox_begin'
    assert cancels, 'close_position_market 未找到任何撤单调用'
    first = min(cancels, key=lambda x: x[1])
    assert begin_lineno < first[1], (
        f'BEGIN(L{begin_lineno}) 必须先于所有撤单；最早撤单 {first[0]} 在 L{first[1]}。'
        f' 全部撤单={cancels}')


def test_reconcile_before_stats_and_corrupt_stats_blocks_clear():
    """反例②：BEGIN 后崩溃恢复时，残单处理（pre-stats converge）必须发生在写
    stats **之前**；反例③：stats CORRUPT 不妨碍残单安全处理，但仍禁止 clear。

    若把 converge 放在写 stats 之后，进程在 BEGIN 后、撤单前崩溃且恢复时又遇
    stats CORRUPT，就会在「写 stats 失败」处返回，残余订单永远得不到安全处理。"""
    calls = []
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    _state_write(t, _single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    # 建立 PROVEN outbox（模拟「BEGIN 成功、撤单前」的崩溃点）
    rec = t._atomic_outbox_begin(
        batch_id=BID, symbol=SYM, mode='LIMIT', generation='L1',
        exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
        observed_qty=0.002, exit_price=76500.0,
        expected_qty=0.002, net_cost=153.24, entry_fee_estimate=0.15)
    assert isinstance(rec, dict) and rec.get('core_status') == 'PROVEN', rec

    _real_conv = t._converge_batch_orders_before_clear
    _real_stats = t._record_settlement_v2
    _real_clear = t.clear_batch_state

    def _spy_conv(s, bid):
        calls.append('converge')
        return {'scope': 'FULL', 'batch_id': bid, 'symbol': s,
                'position_zero': True, 'exchange_scan': 'zero',
                'state_ids_resolved': []}

    def _spy_stats(record=None, stats_file=None):
        calls.append('stats')
        return _real_stats(record=record, stats_file=stats_file)

    def _spy_clear(s, bid, proof=None, authorization=None):
        calls.append('clear')
        return _real_clear(s, bid, proof=proof, authorization=authorization)

    t._converge_batch_orders_before_clear = _spy_conv
    t._record_settlement_v2 = _spy_stats
    t.clear_batch_state = _spy_clear
    # 注入 stats CORRUPT
    with open(t._stats_file, 'w', encoding='utf-8') as f:
        f.write('{ 损坏JSON 不合法 ')
    st = t._try_finalize_outbox(BID, SYM)
    assert calls == ['converge', 'stats'], \
        f'② 残单处理必须先于 stats 写入，且 CORRUPT 时不得 clear: {calls}'
    assert st == trader_260725.SETTLE_PENDING_RETRY, st
    assert BID in _state_read(t).get(SYM, {}), '③ stats CORRUPT 必须禁止 clear'


def test_malformed_outbox_cannot_be_dropped_or_bypass_gate():
    """反例④：pending_settlement **字段存在即受保护**（UNKNOWN ≠ EMPTY）。
    (a) _merge_batch_state 不得静默删除畸形磁盘 outbox；
    (b) 畸形结构不得回落旧 P5h 四元门绕过 §9 财务授权门。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    for bad in ({}, {'base_dedup_key': ''}, {'base_dedup_key': None},
                {'base_dedup_key': 123}, 'not-a-dict', [], 123, None):
        bb = copy.deepcopy(b)
        bb['pending_settlement'] = bad
        # (a) 磁盘畸形 → 必须保留原值，不得凭空消失
        merged = t._merge_batch_state(copy.deepcopy(bb), {})
        assert 'pending_settlement' in merged, \
            f'(a) 畸形磁盘 outbox 不得被 merge 删除: bad={bad!r}'
        assert merged['pending_settlement'] == bad, \
            f'(a) 必须保留原值: bad={bad!r} -> {merged.get("pending_settlement")!r}'
        # (b) clear 门：给出**恰好能匹配旧 P5h 四元门**的授权也不得放行。
        #     这是本反例的真正判别点——旧实现会把畸形 outbox 当作「无事务」而
        #     落回 P5h 四元门，该门只比对 (close_op_id, close_reason, settled,
        #     limit_close_order_id)，完全不看 outbox，于是畸形状态下清批被放行。
        _state_write(t, _single(copy.deepcopy(bb)))
        _p5h_auth = (bb.get('close_op_id') or '',
                     bb.get('close_reason') or '',
                     bool(bb.get('settled_by_limit_close')),
                     bb.get('limit_close_order_id') or '')
        ok = t.clear_batch_state(SYM, BID, proof=_VALID_PROOF,
                                 authorization=_p5h_auth)
        assert not ok, \
            f'(b) 畸形 outbox 不得放行 clear（P5h 四元授权已匹配）: bad={bad!r}'
        assert BID in _state_read(t).get(SYM, {}), '批次必须保留'


def test_missing_side_is_disputed_never_defaults_buy():
    """反例⑤：side 缺失/非法绝不得被伪装成 BUY（零猜测）。
    结算证据链的 side 判定唯一来源：_atomic_outbox_begin → _build_settlement_evidence。
    缺失或非 BUY/SELL 必须 DISPUTED，且不得写权威 net_pnl。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    for bad in ('', None, 'LONG', 'SHORT', 'HOLD', 123):
        b['side'] = bad
        _state_write(t, _single(b))
        rec = t._atomic_outbox_begin(
            batch_id=BID, symbol=SYM, mode='LIMIT', generation='L1',
            exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
            observed_qty=0.002, exit_price=76500.0,
            expected_qty=0.002, net_cost=153.24, entry_fee_estimate=0.15)
        assert isinstance(rec, dict), f'side={bad!r} 应能构造记录'
        assert rec.get('core_status') == 'DISPUTED', \
            f'side={bad!r} 非法必须 DISPUTED，不得默认 BUY: {rec}'
        assert rec.get('side') != 'BUY', \
            f'side={bad!r} 不得被伪装成 BUY: {rec.get("side")!r}'
        assert rec.get('net_pnl_estimate') is None, 'DISPUTED 不得写权威 net_pnl'


def test_entry_order_refs_are_algo_not_regular():
    """反例⑥：ENTRY 引用必须落盘为 kind='algo'。
    生产 ENTRY 由 create_order(type='STOP_MARKET') 创建，是 Binance 条件单
    （registry 记 order_kind='conditional'、role='ENTRY'），查询/撤销须走 algo
    端点；写成 'regular' 会让 v2B 查错端点。禁止用 ID 长度/前缀猜测。"""
    t, _ = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    b['entry_orders'] = ['E1', 'E2']
    b['target_amounts'] = [0.001, 0.001]
    b['filled_details'] = [76620.0, 76620.0]
    b['last_filled_count'] = 2
    _state_write(t, _single(b))
    refs = t._derive_entry_order_refs(b)
    assert len(refs) == 2, refs
    for r in refs:
        assert r.get('kind') == 'algo', \
            f'ENTRY 引用必须是 algo（STOP_MARKET 条件单），实际 {r.get("kind")!r}'
    # 落盘校验：outbox.evidence.entry_order_refs 逐条为 algo
    rec = t._atomic_outbox_begin(
        batch_id=BID, symbol=SYM, mode='LIMIT', generation='L1',
        exit_order_ref={'kind': 'regular', 'order_id': 'L1'},
        observed_qty=0.002, exit_price=76500.0,
        expected_qty=0.002, net_cost=153.24, entry_fee_estimate=0.15)
    assert isinstance(rec, dict), rec
    ob = _state_read(t)[SYM][BID]['pending_settlement']
    ev_refs = ob['evidence']['entry_order_refs']
    assert ev_refs and all(r.get('kind') == 'algo' for r in ev_refs), \
        f'落盘 ENTRY 引用必须全部为 algo: {ev_refs}'


def test_limit_no_durable_phase2_without_outbox():
    """反例⑦：LIMIT 认领字段与 outbox 必须**同一次持久化**。
    注入 BEGIN 的持久化失败后，磁盘上绝不允许出现「close_phase=2 /
    settled_by_limit_close=True 但无 pending_settlement」的孤儿窗口——
    该窗口下恢复路径既认领不到事务，也无法重建证据。"""
    t, ex = make_trader(tempfile.mkdtemp(prefix='v2a_'))
    b = _lp_batch(net=0.002, reason='limit_pending_normal')
    b['close_op_id'] = OP
    b['limit_close_order_id'] = 'L1'
    assert not b.get('settled_by_limit_close'), '前置：本用例需未认领批次'
    _state_write(t, _single(b))
    ex._mk('L1', otype='LIMIT', amount=0.002, status='closed', filled=0.002,
           avg=76500.0)
    ex.positions = [{'symbol': SYM, 'contracts': 0.0, 'side': 'long',
                     'positionSide': 'LONG'}]
    # 正常路径：认领字段与 outbox 必须在**同一次** _persist_states 落盘。
    # 判据不是「全程只 persist 一次」（撤单登记/stats/墓碑/clear 本就各有一次），
    # 而是：任何一次落盘都不得出现「已认领（settled_by_limit_close / phase=2）
    # 但没有 pending_settlement」的快照——那正是孤儿窗口的定义。
    snaps = []
    _real_persist = t._persist_states

    def _spy_persist(all_states):
        snaps.append(copy.deepcopy(all_states))
        return _real_persist(all_states)

    t._persist_states = _spy_persist
    try:
        st_ok, _ = t._finalize_limit_full_fill(SYM, BID, 'L1')
    finally:
        t._persist_states = _real_persist
    assert st_ok == trader_260725.SETTLE_CLEARED, \
        f'正常路径应结算完成（entry_orders 在册 → PROVEN）: {st_ok}'
    assert snaps, '应至少发生一次持久化'
    for i, sn in enumerate(snaps):
        bb2 = (sn.get(SYM) or {}).get(BID)
        if not isinstance(bb2, dict):
            continue
        claimed = (bool(bb2.get('settled_by_limit_close'))
                   or int(bb2.get('close_phase', 0) or 0) == 2)
        assert not (claimed and bb2.get('pending_settlement') is None), \
            (f'第 {i + 1} 次持久化出现「已认领但无 outbox」的孤儿窗口: '
             f'phase={bb2.get("close_phase")!r} '
             f'settled={bb2.get("settled_by_limit_close")!r}')

    # 失败路径：BEGIN 内持久化失败 → 不得留下孤儿窗口
    _state_write(t, _single(copy.deepcopy(b)))
    t._persist_states = lambda all_states: False
    try:
        st, msg = t._finalize_limit_full_fill(SYM, BID, 'L1')
    finally:
        t._persist_states = _real_persist
    assert st == trader_260725.SETTLE_PENDING_RETRY, (st, msg)
    bb = _state_read(t).get(SYM, {}).get(BID)
    assert bb is not None
    assert not bb.get('settled_by_limit_close'), \
        f'BEGIN 失败后不得落 settled_by_limit_close（孤儿窗口）: {bb}'
    assert int(bb.get('close_phase', 0) or 0) != 2, \
        f'BEGIN 失败后不得落 durable close_phase=2（孤儿窗口）: {bb}'
    assert bb.get('pending_settlement') is None, bb


# ───────────────────────── 生产文件免疫 ─────────────────────────

def test_production_files_untouched():
    """r99：整套 v2A 行为测试跑完，生产账本零变化（hash + size + mtime_ns）。"""
    _now = _prod_snapshot()
    _diff = {k: (v, _now[k]) for k, v in _PROD_SNAP_AT_IMPORT.items() if _now[k] != v}
    assert not _diff, f'测试污染生产文件（导入前 → 现在）: {_diff}'


TESTS = [
    test_limit_close_writes_real_v2_stats,
    test_sl_close_writes_real_v2_stats,
    test_tp_close_writes_real_v2_stats,
    test_market_close_writes_real_v2_stats,
    test_disputed_evidence_matrix_no_silent_zero,
    test_proven_evidence_requires_all_fields,
    test_disputed_flow_keeps_batch_and_none_pnl,
    test_clear_rejected_without_stats_committed,
    test_clear_rejected_on_authorization_mismatch,
    test_clear_rejected_on_disputed,
    test_clear_allowed_when_proven_and_authorized,
    test_resume_pending_settlement_dedup_once,
    test_fee_mode_maker_for_limit_taker_for_others,
    test_observed_qty_from_real_fill_not_constructed,
    test_disputed_terminal_idempotent_across_rounds,
    test_atomic_begin_reuses_same_dedup_outbox,
    test_disputed_terminal_idempotent_across_restart,
    test_market_begin_precedes_all_cancels,
    test_reconcile_before_stats_and_corrupt_stats_blocks_clear,
    test_malformed_outbox_cannot_be_dropped_or_bypass_gate,
    test_missing_side_is_disputed_never_defaults_buy,
    test_entry_order_refs_are_algo_not_regular,
    test_limit_no_durable_phase2_without_outbox,
    test_production_files_untouched,
]


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
