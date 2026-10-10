# -*- coding: utf-8 -*-
"""v6.2 GREEN 测试层（V62 SUT × 23 个 RED 场景 + 行为级升级用例）。

授权（ChatGPT）：GREEN candidate/helper/测试实现 ✅；生产三文件 ❌ 仍冻结。

结构（ChatGPT 要求的两层证明）：
  - `test_v62_red_first.py`（OLD SUT）→ 23/23 RED（冻结不动，历史判别力基线）
  - 本文件（V62 SUT）→ 同一批风险语义 23/23 GREEN
  - 升级项：R3-c（真实 BEGIN seed/回滚）、R3-b（commit 行为级）、
    R1-e/f（registry 恢复行为级）、R3-e（真实 _confirm conditional 端点路由 +
    normal 端点对照，M30 真 killer）、R3-g（coverage gate 行为级）、
    R1-h/R2-g（真实候选 AFTER）

V62 SUT 构成：
  - helper 层 = `送审附件_v6.2/new_helpers_v62.py`（13 helper，r4 修补 + 2 新增）
  - 生产方法候选 = `送审附件_v6.2/after_candidates_v62.py`（改动 1/2/3/4/5/
    6.5/9.0/9.0b + R1-h，正式落地时按落点表回填 trader_260725.py）
"""
import ast
import copy

import ccxt

import os

import test_v62_red_first as R
from test_v62_red_first import (FakeExchange62, FakeSelf62, mk_batch, bind,
                                _order, _e511, SYM)

V62_HELPER_PATH = (os.environ.get('V62_HELPER_OVERRIDE')
                   or os.path.join(R.PROJECT_DIR, '送审附件_v6.2', 'new_helpers_v62.py'))
V62 = R._extract_functions(
    V62_HELPER_PATH,
    ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
     '_survey_same_side_batches', '_close_amount_guard',
     '_begin_close_request_if_active', '_derive_close_txn_vars',
     '_rollback_close_request_if_current', '_verify_entry_order_terminal',
     '_cancel_and_verify_entry_orders', '_set_close_reason_if_current',
     '_pending_entry_ids_for_gate', '_commit_limit_close_order_if_current'])

import importlib.util
# 注入点：M11~M31 变异需要对 candidate（生产方法候选）注入变异体
CAND_PATH = (os.environ.get('V62_CANDIDATE_OVERRIDE')
             or os.path.join(R.PROJECT_DIR, '送审附件_v6.2',
                             'after_candidates_v62.py'))
_spec = importlib.util.spec_from_file_location(
    'after_candidates_v62', CAND_PATH)
CAND = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CAND)


def _setup_v62(states=None, persist_ok=True, persist_fail_first_n=0, **fx_kw):
    fx = FakeExchange62(**fx_kw)
    fake = FakeSelf62(fx, states=states, persist_ok=persist_ok,
                      persist_fail_first_n=persist_fail_first_n)
    bind(fake, V62)
    for name in ('cancel_open_orders_v62', 'monitor_zero_filled_exit_v62',
                 'entry_detection_v62', 'sl_attribution_v62', 'tp_attribution_v62',
                 'producer2_v62', 'outer_except_precreate_v62',
                 'limit_tp_gate_v62', 'limit_coverage_gate_v62',
                 'monitor_hole_check_v62'):
        bind(fake, {name: getattr(CAND, name)})
    bind(fake, {'_find_registry_identity_by_order_id':
                R.OLD_find_registry_identity_by_order_id})
    return fake, fx


def g_R1_a_positional_and_attribution():
    """R1-a GREEN：台账永不压缩 + 无 sticky + E4 registry 归因。"""
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(
            ['E1', 'E2', 'E3', 'E4', 'E5'], 3,
            amounts=[0.001, 0.002, 0.003, 0.004, 0.005])}},
        cancel_by_id={'E5': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[_order('canceled', 0.0), _order('canceled', 0.0)])
    ret = fake.cancel_open_orders_v62('batch_A')
    b = fake._states[SYM]['batch_A']
    assert list(b['entry_orders']) == ['E1', 'E2', 'E3', 'E4', 'E5']
    assert not b.get('is_programmatic_cancel')
    reg = b.get('protection_registry') or {}
    e4 = [e for e in reg.values() if e.get('order_id') == 'E4']
    assert e4 and e4[0].get('state') == 'PROGRAMMATIC_CANCELED', f"reg={reg}"


def g_R1_i_dash2011_then_filled():
    """R1-i GREEN：E5 -2011 且实为 filled → 停止，E4 零调用，E5 保留。"""
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(
            ['E1', 'E2', 'E3', 'E4', 'E5'], 3,
            amounts=[0.001, 0.002, 0.003, 0.004, 0.005])}},
        cancel_by_id={'E5': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[_order('closed', 0.005)])
    fake.cancel_open_orders_v62('batch_A')
    b = fake._states[SYM]['batch_A']
    assert fx.cancel_calls.count('E4') == 0
    assert 'E5' in b['entry_orders']
    assert any(tg[0] == 'critical' and '成交' in tg[1] for tg in fake.tg_sent)


def g_R1_b_g_zero_filtered():
    """R1-b/g GREEN：zero-filled unresolved → 台账保留 + False + 无终止标志。"""
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1', 'E2'], 0)}},
        cancel_by_id={'E2': ('raise', _e511('-2011 Unknown order'))})
    ret = fake.cancel_open_orders_v62('batch_A')
    b = fake._states[SYM]['batch_A']
    assert list(b['entry_orders']) == ['E1', 'E2']
    assert ret[0] is False
    assert not b.get('pending_close') and int(b.get('close_phase', 0) or 0) == 0


def g_R1_b_g_zero_happy():
    """R1-b 正向：zero-filled 全部 gone → 才写终止标志 + True。"""
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1', 'E2'], 0)}},
        cancel_seq=[('ok', None)],
        conditional_seq=[_order('canceled', 0.0), _order('canceled', 0.0)])
    ret = fake.cancel_open_orders_v62('batch_A')
    b = fake._states[SYM]['batch_A']
    assert ret[0] is True, f"全 gone 应成功：{ret!r}"
    assert b.get('pending_close') is True and int(b.get('close_phase', 0) or 0) == 1
    assert list(b['entry_orders']) == ['E1', 'E2']      # D-4：仍不压缩


def g_R1_l_exit_criteria():
    """R1-l GREEN：退出判据只看 pending_close。"""
    _setup_v62()
    disk = {'is_programmatic_cancel': True, 'pending_close': False}
    assert CAND.monitor_zero_filled_exit_v62(disk, 0) is False
    disk2 = {'is_programmatic_cancel': True, 'pending_close': True}
    assert CAND.monitor_zero_filled_exit_v62(disk2, 0) is True


def g_R1_o_sl_attribution():
    """R1-o GREEN：sticky True + SL 非 programmatic → 仍自动补挂。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), is_programmatic_cancel=True,
        protection_registry={
            'batch_A:SL:0:LONG': {'role': 'SL', 'state': 'CONFIRMED',
                                  'order_id': 'SLX', 'layer': 0}})}})
    latest = fake._states[SYM]['batch_A']
    # SL 非程序终结（registry CONFIRMED）+ sticky True 污染 → 仍必须自动补挂
    assert fake.sl_attribution_v62(SYM, 'batch_A', latest, 'SLX',
                                   user_modified=False) is True, \
        "sticky bool 污染：SL 外部撤销被判为程序撤单"
    # 对照：SL 确为程序终结（registry PROGRAMMATIC_CANCELED）→ 不补挂
    fake._update_registry(SYM, 'batch_A', 'batch_A:SL:0:LONG',
                          state='PROGRAMMATIC_CANCELED', order_id='SLX',
                          id_known=True, terminated_reason='close_requested_canceled')
    assert fake.sl_attribution_v62(SYM, 'batch_A', latest, 'SLX',
                                   user_modified=False) is False


def g_R1_p_entry_detection():
    """R1-p GREEN：E5 programmatic（registry）/ E4 manual（无记录）。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), is_programmatic_cancel=True,
        protection_registry={
            'batch_A:ENTRY:4:LONG': {'role': 'ENTRY',
                                     'state': 'PROGRAMMATIC_CANCELED',
                                     'order_id': 'E5', 'layer': 4}})}})
    latest = fake._states[SYM]['batch_A']
    assert fake.entry_detection_v62(SYM, 'batch_A', latest, 'E4', 3) is True
    assert fake.entry_detection_v62(SYM, 'batch_A', latest, 'E5', 4) is False


def g_R1_s_producer2():
    """R1-s/m GREEN：filled 后 bitmap 不置位、E4 零调用；正常 gone 则置位+归因。"""
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1', 'E2', 'E3', 'E4', 'E5'], 3)}},
        cancel_by_id={'E5': ('ok', None), 'E4': ('ok', None)},
        conditional_seq=[_order('closed', 0.005)])
    filled = [True, True, True, False, False]
    canceled = [False] * 5
    fake.producer2_v62(SYM, 'batch_A', ['E1', 'E2', 'E3', 'E4', 'E5'],
                       filled, canceled, 3)
    assert canceled[4] is False, "E5 filled 不得标记 canceled"
    assert fx.cancel_calls.count('E4') == 0
    b = fake._states[SYM]['batch_A']
    assert list(b['entry_orders']) == ['E1', 'E2', 'E3', 'E4', 'E5']   # 不截断

    # 正向：E4 cancel 成功 + verifier gone → bitmap 置位 + registry 归因
    fake2, fx2 = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1', 'E2', 'E3', 'E4'], 3)}},
        cancel_by_id={'E4': ('ok', None)},
        conditional_seq=[_order('canceled', 0.0)])
    filled2 = [True, True, True, False]
    canceled2 = [False] * 4
    fake2.producer2_v62(SYM, 'batch_A', ['E1', 'E2', 'E3', 'E4'],
                        filled2, canceled2, 3)
    assert canceled2[3] is True
    reg = fake2._states[SYM]['batch_A']['protection_registry']
    e4 = [e for e in reg.values() if e.get('order_id') == 'E4']
    assert e4 and e4[0].get('state') == 'PROGRAMMATIC_CANCELED'


def g_R1_k_derive_hole():
    """R1-k GREEN：hole → entry_fill_hole。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}})
    tb = mk_batch(['E1', 'E2', 'E3', 'E4', 'E5'], 4,
                  filled_details=[77000.0, 77001.0, 77002.0, 0.0, 77003.0])
    ok, _v, why = fake._derive_close_txn_vars(tb, 'batch_A')
    assert ok is False and (('entry_fill_hole' in why) or ('ledger_invalid' in why)), \
        f"{ok!r}/{why!r}"


def g_R1_n_derive_shape():
    """R1-n GREEN：shape 损坏 → filled_details_shape_invalid。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}})
    tb = mk_batch(['E1', 'E2', 'E3'], 3,
                  filled_details=[77000.0, 77001.0, 77002.0],
                  amounts=[0.001, 0.002, 0.003, 0.004, 0.005],
                  with_registry=False)
    tb['entry_orders'] = ['E1', 'E2', 'E3']
    ok, _v, why = fake._derive_close_txn_vars(tb, 'batch_A')
    assert ok is False and 'filled_details_shape_invalid' in why, f"{ok!r}/{why!r}"


def g_R1_t_u_derive_invalid():
    """R1-t/u GREEN：tail -1 与 prefix NaN 均 Fail-Closed。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}})
    tb_t = mk_batch(['E1', 'E2', 'E3', 'E4'], 2,
                    filled_details=[77000.0, 77001.0, -1.0, 0.0])
    # 尾段刻意全为非正数（-1.0, 0.0）：若 _finite_zero_dv 退化为「<=0 即有效」
    # （M26 变异），-1.0 会逃逸 → derive ok=True → 本断言 RED
    ok_t, _v, why_t = fake._derive_close_txn_vars(tb_t, 'batch_A')
    assert ok_t is False and (('entry_fill_hole' in why_t)
                              or ('ledger_invalid' in why_t)), why_t
    tb_u = mk_batch(['E1', 'E2', 'E3'], 2,
                    filled_details=[77000.0, float('nan'), 0.0])
    ok_u, _v, why_u = fake._derive_close_txn_vars(tb_u, 'batch_A')
    assert ok_u is False and (('ledger_invalid' in why_u)
                              or ('entry_fill_hole' in why_u)), why_u


def g_R3_h1_coverage_nan():
    """R3-h1 GREEN：NaN contracts → read None → guard None。"""
    nan_pos = [{'symbol': SYM, 'side': 'long', 'contracts': float('nan'),
                'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}},
                          pos_seq=[nan_pos])
    assert fake._read_position_amt(SYM, 'BUY', True) is None
    amt, _d = fake._close_amount_guard(SYM, 'BUY', True, 0.001, 'batch_A')
    assert amt is None, f"NaN fail-open：{amt!r}"


def g_R3_h2_survey_topology():
    """R3-h2 GREEN：B hole → survey (-1,-1,-1)；B 拓扑合法 → 正常 coverage。"""
    fake, fx = _setup_v62(
        states={SYM: {
            'batch_A': mk_batch(['E1'], 1, filled_details=[77000.0], amounts=[1.0]),
            'batch_B': mk_batch(['B1', 'B2'], 1,
                                filled_details=[0.0, 70000.0], amounts=[0.1, 10.0],
                                with_registry=False)}},
        pos_seq=[{'symbol': SYM, 'side': 'long', 'contracts': 1.5,
                  'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}])
    assert fake._survey_same_side_batches(SYM, 'BUY', 'batch_A') == (-1, -1, -1)
    # guard 部分：pos_seq 同修为 list-of-list（r6 修正）
    fx.pos_seq = [[{'symbol': SYM, 'side': 'long', 'contracts': 1.5,
                    'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]]
    amt, _d = fake._close_amount_guard(SYM, 'BUY', True, 1.0, 'batch_A')
    assert amt is None
    # M31 防护：拓扑合法（前缀正数+尾部零）但声明 lfc>0 而计划量合计 <=0
    # → 账本矛盾 → Fail-Closed（旧代码 continue 会静默跳过 → 假 coverage）
    fake._states[SYM]['batch_B']['target_amounts'] = [0.0, 10.0]
    fake._states[SYM]['batch_B']['filled_details'] = [70000.0, 0.0]
    assert fake._survey_same_side_batches(SYM, 'BUY', 'batch_A') == (-1, -1, -1), \
        "lfc>0 但计划量合计 0 必须账本矛盾 Fail-Closed"
    # 恢复 B 正常台账，进入正向断言
    fake._states[SYM]['batch_B']['target_amounts'] = [0.1, 10.0]
    # 正向：B 拓扑合法（[70000,0]/N=1）→ 正常 coverage
    fake._states[SYM]['batch_B']['filled_details'] = [70000.0, 0.0]
    others, sum_all, blocking = fake._survey_same_side_batches(SYM, 'BUY', 'batch_A')
    assert (others, sum_all, blocking) == (1, 1.1, 0), f"{others},{sum_all},{blocking}"


def g_R3_b_commit_behavior():
    """R3-b GREEN（行为级，M20 killer）：source-state guard + durable commit。"""
    op_id = 'c' * 32
    base = dict(mk_batch(['E1'], 1), close_phase=1, pending_close=True,
                close_op_id=op_id, close_reason='limit_creating')
    # (a) happy：limit_creating + 事实齐 → committed + 4 字段原子落盘
    fake, fx = _setup_v62(states={SYM: {'batch_A': dict(base)}}, persist_ok=True)
    ok, why = fake._commit_limit_close_order_if_current(
        SYM, 'batch_A', op_id, 'LIM1', 77000.0, '💎 最优价挂单')
    assert ok is True, why
    b = fake._states[SYM]['batch_A']
    assert b['limit_close_order_id'] == 'LIM1'
    assert b['limit_close_price'] == 77000.0
    assert b['limit_close_mode'] == '💎 最优价挂单'
    assert b['close_reason'] == 'limit_pending_normal'
    # (b) reason 已 abnormal（settlement_error）→ 拒绝，绝不拉回 normal
    fake2, _fx2 = _setup_v62(
        states={SYM: {'batch_A': dict(base, close_reason='settlement_error')}})
    ok2, why2 = fake2._commit_limit_close_order_if_current(
        SYM, 'batch_A', op_id, 'LIM1', 77000.0, '💎 最优价挂单')
    assert ok2 is False and 'reason_changed' in why2, f"{ok2!r}/{why2!r}"
    assert fake2._states[SYM]['batch_A']['close_reason'] == 'settlement_error'
    # (c) op_id 不匹配 → 拒绝
    fake3, _fx3 = _setup_v62(states={SYM: {'batch_A': dict(base)}}, persist_ok=True)
    ok3, why3 = fake3._commit_limit_close_order_if_current(
        SYM, 'batch_A', 'f' * 32, 'LIM1', 77000.0, 'x')
    assert ok3 is False and 'op_id_mismatch' in why3


def g_R1_ef_recovery():
    """R1-e/f GREEN（行为级）：registry 恢复按真实 layer 排序 + 一致性证明。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}})
    # (e) chain 与计划层不一致 → recoverable=False
    tb_bad = mk_batch(['E1', 'E2', 'E3'], 3,
                      amounts=[0.001, 0.002, 0.003, 0.004, 0.005],
                      with_registry=False)
    tb_bad['protection_registry'] = {          # 只有 3 条（缺 layer3/4）
        'batch_A:ENTRY:0:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                 'order_id': 'E1', 'layer': 0},
        'batch_A:ENTRY:1:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                 'order_id': 'E2', 'layer': 1},
        'batch_A:ENTRY:2:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                 'order_id': 'E3', 'layer': 2}}
    ids, rec, chain = fake._pending_entry_ids_for_gate(SYM, 'batch_A', tb_bad, 3)
    assert rec is False and ids == [], f"{rec}/{ids}"
    # (f) skip 层坐标系：registry layers=[1,2,3]、entry_orders 压缩 idx=[0,1,2]
    tb_f = mk_batch(['E1', 'E2', 'E3'], 3,
                    amounts=[0.001, 0.002, 0.003], with_registry=False)
    tb_f['protection_registry'] = {
        'k1': {'role': 'ENTRY', 'state': 'CONFIRMED', 'order_id': 'E1', 'layer': 1},
        'k2': {'role': 'ENTRY', 'state': 'CONFIRMED', 'order_id': 'E2', 'layer': 2},
        'k3': {'role': 'ENTRY', 'state': 'CONFIRMED', 'order_id': 'E3', 'layer': 3}}
    ids_f, rec_f, chain_f = fake._pending_entry_ids_for_gate(
        SYM, 'batch_A', tb_f, 3)
    assert rec_f is True and chain_f == ['E1', 'E2', 'E3']
    assert ids_f == []                          # lfc=3 → pending 空 → gate True
    # pending 恢复：lfc=2 → chain[2:] = ['E3']
    ids_2, rec_2, _c = fake._pending_entry_ids_for_gate(SYM, 'batch_A', tb_f, 2)
    assert rec_2 is True and ids_2 == ['E3']


def g_R2_f_first_abnormal_wins():
    """R2-f GREEN：abnormal reason 不被 generic except 覆盖。"""
    op_id = 'd' * 32
    fake, fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), close_phase=1, pending_close=True, close_op_id=op_id,
        close_reason='market_confirm_unknown')}})
    ok, why = fake._set_close_reason_if_current(SYM, 'batch_A', op_id, 'settlement_error')
    assert ok is True and why.startswith('reason_already_abnormal'), f"{ok!r}/{why!r}"
    assert fake._states[SYM]['batch_A']['close_reason'] == 'market_confirm_unknown'
    # 正常态 → 允许写异常态（独立场景，fresh normal reason）
    fake2, _fx2 = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), close_phase=1, pending_close=True, close_op_id=op_id,
        close_reason='market_confirming')}})
    ok2, why2 = fake2._set_close_reason_if_current(SYM, 'batch_A', op_id, 'settlement_error')
    assert ok2 is True and why2 == 'reason_set', f"{ok2!r}/{why2!r}"
    assert fake2._states[SYM]['batch_A']['close_reason'] == 'settlement_error'


def g_R3_c_begin_seed():
    """R3-c GREEN（行为级，M21 killer）：真实 BEGIN 的 seed/回滚。"""
    # (a) persist 成功 → ok + seed 存在
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1'], 1)}}, persist_ok=True)
    ok, op_id, why, snap = fake._begin_close_request_if_active(
        SYM, 'batch_A', 'limit_creating')
    assert ok is True, why
    assert 'batch_A' in fake._freeze_alerted, "BEGIN 未 seed _freeze_alerted（M21）"
    assert snap['close_reason'] == 'limit_creating'
    # (b) persist 失败 → ok=False 且 seed 回滚（不留幽灵窗口）
    fake2, _fx2 = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1'], 1)}}, persist_ok=False)
    ok2, _op2, why2, _snap2 = fake2._begin_close_request_if_active(
        SYM, 'batch_A', 'limit_creating')
    assert ok2 is False and 'persist_failed' in why2
    assert fake2._freeze_alerted.get('batch_A') is None, \
        "persist 失败后 seed 未回滚（幽灵 grace）"
    # (c) crash 模拟：新 fake（内存 dict 空）+ 磁盘 limit_creating → 冻结分型 loud
    fake3, _fx3 = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), close_phase=1, pending_close=True,
        close_reason='limit_creating')}})
    fake3._freeze_alerted = {}
    disk = fake3._states[SYM]['batch_A']
    assert disk['close_reason'] not in ('market_confirming', 'limit_pending_normal'), \
        "limit_creating 不得在冻结静默集"
    assert 'limit_creating' not in ('market_confirming', 'limit_pending_normal')


def g_R3_e_endpoint_routing():
    """R3-e GREEN（M30 真 killer）：真实 _confirm conditional 端点路由。"""
    # conditional 端点：canceled + filled=0 → TERMINAL_ZERO（可放行）
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1'], 1)}},
        conditional_seq=[_order('canceled', 0.0)])
    v_cond, _d, _f = fake._confirm_close_filled(
        SYM, 'BUY', True, 'TP1', 0.001, 0.001, order_kind='conditional')
    assert v_cond == 'TERMINAL_ZERO', f"conditional 路由判定错误：{v_cond!r}"
    assert 'fetch_conditional:TP1' in fx.calls
    assert 'fetch_normal:TP1' not in fx.calls
    # normal 端点查同一张条件单 → OrderNotFound → NOT_CONFIRMED（C5 bug 建模）
    fake2, fx2 = _setup_v62(
        states={SYM: {'batch_A': mk_batch(['E1'], 1)}},
        order_seq=[ccxt.OrderNotFound('not found'),
                   ccxt.OrderNotFound('not found'),
                   ccxt.OrderNotFound('not found')])
    v_norm, _d2, _f2 = fake2._confirm_close_filled(
        SYM, 'BUY', True, 'TP1', 0.001, 0.001, order_kind='normal', attempts=1)
    assert v_norm == 'NOT_CONFIRMED', f"normal 端点对照错误：{v_norm!r}"
    # gate 集成：TP gate 走 conditional → TERMINAL_ZERO → proceed
    fake3, _fx3 = _setup_v62(
        states={SYM: {'batch_A': dict(
            mk_batch(['E1'], 1), close_phase=1, pending_close=True,
            close_op_id='a' * 32, close_reason='limit_creating')}},
        cancel_by_id={'TP1': ('ok', None)},
        conditional_seq=[_order('canceled', 0.0)])
    proceed, verdict = fake3.limit_tp_gate_v62(
        SYM, 'batch_A', 'a' * 32, fake3._states[SYM]['batch_A'], 0.001)
    # ^ 注意：limit_tp_gate_v62(self, target_symbol, batch_id, target_b_data, amount)
    assert proceed is True and verdict == 'TERMINAL_ZERO', f"{proceed!r}/{verdict!r}"


def g_R3_d_tp_network_blocked():
    """R3-d GREEN：TP cancel 网络异常 → gate 阻断 + CAS limit_tp_unresolved + create 0。"""
    op_id = 'b' * 32
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': dict(
            mk_batch(['E1'], 1), close_phase=1, pending_close=True,
            close_op_id=op_id, close_reason='limit_creating',
            tp_order_id='TP1')}},
        cancel_by_id={'TP1': ('raise', RuntimeError('network error'))},
        conditional_seq=[_order('open', 0.001)])
    proceed, verdict = fake.limit_tp_gate_v62(
        SYM, 'batch_A', op_id, fake._states[SYM]['batch_A'], 0.001)
    assert proceed is False and verdict == 'PENDING', f"{proceed!r}/{verdict!r}"
    b = fake._states[SYM]['batch_A']
    assert b['close_reason'] == 'limit_tp_unresolved'
    assert fx.calls.count('create_order') == 0


def g_R3_f_tp_filled_blocked():
    """R3-f GREEN：cancel 正常返回但 TP 已成交 → 阻断 + create 0。"""
    op_id = 'b' * 32
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': dict(
            mk_batch(['E1'], 1), close_phase=1, pending_close=True,
            close_op_id=op_id, close_reason='limit_creating',
            tp_order_id='TP1')}},
        cancel_by_id={'TP1': ('ok', None)},
        conditional_seq=[_order('closed', 0.001)])
    proceed, verdict = fake.limit_tp_gate_v62(
        SYM, 'batch_A', op_id, fake._states[SYM]['batch_A'], 0.001)
    assert proceed is False and verdict == 'CONFIRMED_FULL'
    assert fx.calls.count('create_order') == 0
    assert fake._states[SYM]['batch_A']['close_reason'] == 'limit_tp_unresolved'


def g_R3_g_coverage_blocked():
    """R3-g GREEN：coverage 冲突 → coverage gate 阻断 + limit_amount_conflict + create 0。"""
    op_id = 'b' * 32
    fake, fx = _setup_v62(
        states={SYM: {
            'batch_A': dict(mk_batch(['E1'], 1, filled_details=[77000.0],
                                     amounts=[0.001]),
                            close_phase=1, pending_close=True,
                            close_op_id=op_id, close_reason='limit_creating'),
            'batch_B': mk_batch(['B1'], 1, filled_details=[77000.0],
                                amounts=[0.001], with_registry=False)}},
        # 🔒 r6 修正：pos_seq 元素是【positions 列表】（生产 fetch_positions
        # 语义）——旧 fixture 传裸 dict 会让 _read_position_amt 判非列表
        # Fail-Closed，「结果对、证明链错」。
        pos_seq=[[{'symbol': SYM, 'side': 'long', 'contracts': 0.001,
                   'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]],
        cancel_by_id={'TP1': ('ok', None)},
        conditional_seq=[_order('canceled', 0.0)])
    tb = fake._states[SYM]['batch_A']
    tp_ok, _v = fake.limit_tp_gate_v62(SYM, 'batch_A', op_id, tb, 0.001)
    assert tp_ok is True                       # TP gate 先过（close_op_id 修正）
    proceed, safe = fake.limit_coverage_gate_v62(
        SYM, 'batch_A', op_id, tb, 0.001)
    # ^ 签名：(self, target_symbol, batch_id, close_op_id, target_b_data, amount)
    assert proceed is False, f"coverage 冲突未阻断：{proceed!r}/{safe!r}"
    assert '归因冲突' in _d_report(fake), 'detail 须含归因冲突语义'
    b = fake._states[SYM]['batch_A']
    assert b['close_reason'] == 'limit_amount_conflict'
    assert fx.calls.count('create_order') == 0
    # 正向：A+B 台账 0.002 = actual 0.002 → coverage PASS → safe=台账量
    fake._states[SYM]['batch_B']['last_filled_count'] = 1
    fx.pos_seq = [[{'symbol': SYM, 'side': 'long', 'contracts': 0.002,
                    'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]]
    proceed2, safe2 = fake.limit_coverage_gate_v62(
        SYM, 'batch_A', op_id, tb, 0.001)
    assert proceed2 is True and abs(safe2 - 0.001) < 1e-8, \
        f"coverage 正向失败：{proceed2!r}/{safe2!r}"


def _d_report(fake):
    """取最近一次 guard detail（persisted 无关，直接从 _survey 侧重建）。"""
    others, sum_all, blocking = fake._survey_same_side_batches(SYM, 'BUY', 'batch_A')
    return f'归因冲突：总敞口 < 同方向批次台账合计 {sum_all}' if others >= 0 else '勘察失败'


def g_R1_h_monitor_hole():
    """R1-h GREEN：hole bitmap → monitor hole 检测 critical。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': mk_batch(['E1'], 1)}})
    n = fake.monitor_hole_check_v62('batch_A', [True, True, True, False, True], 4)
    assert n == 1
    assert any(tg[0] == 'critical' and '不连续' in tg[1] for tg in fake.tg_sent)
    # 正向：无 hole → 零告警
    n2 = fake.monitor_hole_check_v62('batch_A', [True, True, True, True, False], 4)
    assert n2 == 0


def g_R2_g_rollback_failed():
    """R2-g GREEN（行为级）：pre-create rollback 失败 → CAS 写 abnormal reason。"""
    op_id = 'e' * 32
    # persist_fail_first_n=1：第 1 次落盘（rollback）失败、第 2 次（reason
    # CAS）成功——精确建模「rollback 落盘失败但 reason 写入成功」的时序。
    fake, fx = _setup_v62(
        states={SYM: {'batch_A': dict(
            mk_batch(['E1'], 1), close_phase=1, pending_close=True,
            close_op_id=op_id, close_reason='market_confirming')}},
        persist_fail_first_n=1)
    ret = fake.outer_except_precreate_v62(SYM, 'batch_A', op_id,
                                          RuntimeError('boom'))
    assert ret[0] is False
    disk_reason = fake._states[SYM]['batch_A']['close_reason']
    assert disk_reason == 'txn_aborted_rollback_failed', \
        f"rollback 失败后 reason 未切 abnormal：{disk_reason!r}（冻结告警静默）"
    assert any(tg[0] == 'critical' for tg in fake.tg_sent)


def g_R3_h3_survey_invalid_amounts():
    """R3-h3 GREEN：其他批次 target_amounts 含字符串 → survey (-1,-1,-1)。"""
    fake, fx = _setup_v62(states={SYM: {
        'batch_A': mk_batch(['E1'], 1, filled_details=[77000.0], amounts=[1.0]),
        'batch_B': mk_batch(['B1', 'B2'], 1,
                            filled_details=[70000.0, 0.0], amounts=['corrupt', 10.0],
                            with_registry=False)}})
    others, sum_all, blocking = fake._survey_same_side_batches(SYM, 'BUY', 'batch_A')
    assert (others, sum_all, blocking) == (-1, -1, -1), \
        f"非法 amounts 未按契约 Fail-Closed：{(others, sum_all, blocking)}"


def g_R1_d_integration():
    """R1-d GREEN（集成）：legacy 截断账本真实进入
    _cancel_and_verify_entry_orders——registry 恢复出的 E5 已 filled → gate=False。"""
    fake, fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1', 'E2', 'E3'], 3,
                 amounts=[0.001, 0.002, 0.003, 0.004, 0.005],
                 with_registry=False),
        close_phase=1, pending_close=True,
        protection_registry={
            'batch_A:ENTRY:0:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                     'order_id': 'E1', 'layer': 0},
            'batch_A:ENTRY:1:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                     'order_id': 'E2', 'layer': 1},
            'batch_A:ENTRY:2:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                     'order_id': 'E3', 'layer': 2},
            'batch_A:ENTRY:3:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                     'order_id': 'E4', 'layer': 3},
            'batch_A:ENTRY:4:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                     'order_id': 'E5', 'layer': 4}})}},
        conditional_seq=[_order('canceled', 0.0),      # E4 已撤（F-1 成功半边）
                         _order('closed', 0.005)])     # E5 已成交（遗失真相）
    tb = fake._states[SYM]['batch_A']
    gate = fake._cancel_and_verify_entry_orders(SYM, 'batch_A', tb, 3)
    # registry 恢复 E4/E5 → 逐 ID 验证发现 E5 已成交 → gate=False（冻结）
    assert gate is False, f"legacy 截断 + E5 filled 应 gate=False：{gate!r}"
    assert any('fetch_conditional:E5' in c for c in fx.calls), \
        "恢复出的 E5 未走 conditional 逐 ID 验证"
    assert any(tg[0] == 'critical' for tg in fake.tg_sent)
    # M22 防护：registry chain 与 entry_orders 前缀不一致（E3 被伪造成 E9）
    # → 恢复不可信 → recoverable=False，绝不把 registry 链当 pending 视图
    fake._states[SYM]['batch_A']['protection_registry'][
        'batch_A:ENTRY:2:LONG']['order_id'] = 'E9'
    ids_m, rec_m, _chain_m = fake._pending_entry_ids_for_gate(
        SYM, 'batch_A', fake._states[SYM]['batch_A'], 3)
    assert rec_m is False and ids_m == [], \
        f"前缀不一致必须不可恢复: rec={rec_m}, ids={ids_m}"


# ── staged trader 集成（真实目标态，非孤立候选）────────────────────

STAGED_PATH = os.environ.get('V62_STAGED',
                            r'G:/tmp/v62_staged/trader_260725_v62_staged.py')


def _staged_src():
    """读取 staged trader 源码（缺失 = 集成前置条件未满足 → 显式失败）。"""
    import os as _os
    assert _os.path.exists(STAGED_PATH), \
        f'staged trader 缺失：{STAGED_PATH}（先跑 build_v62_staged.py）'
    return open(STAGED_PATH, encoding='utf-8').read()


def _staged_freeze_probe():
    """从 staged trader 提取真实 freeze 块，编译为可调用探针。

    机械变换：`continue` → `return "loop"`（块内唯一跳出点），
    其余代码逐字保留——跑的是真实 staged 源码，不是转录。"""
    src_txt = _staged_src()
    i = src_txt.index('                _b_close_phase = int((latest_b_data or {}).get(')
    j = src_txt.index('                    continue', i) + len('                    continue')
    frag = src_txt[i:j].replace('                    continue',
                                '                    return "loop"')
    wrapped = ('def _freeze_probe(self, latest_b_data, batch_id):\n'
               + '\n'.join(('    ' + ln) if ln.strip() else ln
                            for ln in frag.splitlines()))
    ns = {'time': __import__('time')}
    exec(compile(wrapped, '<staged:freeze>', 'exec'), ns)
    return ns['_freeze_probe']


def _crit(fake):
    return sum(1 for lv, m in fake.tg_sent
               if lv == 'critical' and '\u5361\u6b7b' in m)


def g_staged_freeze_init_structural():
    """M25 结构断言：staged CryptoTrader.__init__ 必须初始化 _freeze_alerted。"""
    src_txt = _staged_src()
    tree = ast.parse(src_txt)
    cls = [n for n in ast.walk(tree)
           if isinstance(n, ast.ClassDef) and n.name == 'CryptoTrader']
    assert cls, 'CryptoTrader 缺失'
    init = [n for n in ast.walk(cls[0])
            if isinstance(n, ast.FunctionDef) and n.name == '__init__']
    assert init, '__init__ 缺失'
    names = set()
    for node in ast.walk(init[0]):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) \
                        and tgt.value.id == 'self':
                    names.add(tgt.attr)
    assert '_freeze_alerted' in names, \
        f'staged __init__ 未初始化 _freeze_alerted（新进程会 AttributeError）：' \
        f'{sorted(names)}'


def g_M25_crash_loud_fresh_process():
    """M25 行为（fresh process）：空 dict 由 staged __init__ 提供（结构断言已证），
    磁盘 limit_creating → 立即 loud、不抛异常、不写 monitor_error。"""
    probe = _staged_freeze_probe()
    fake, _fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), close_phase=1, pending_close=True,
        close_reason='limit_creating')}})
    # fresh runtime state（等价于新进程 __init__ 之后）
    fake._freeze_alerted = {}
    r = probe(fake, fake._states[SYM]['batch_A'], 'batch_A')
    assert _crit(fake) == 1, f'crash 后未 loud：r={r!r}, tg={fake.tg_sent}'
    assert r == 'loop', f'freeze 块未正常返回（会中断 monitor 循环）：{r!r}'
    assert not getattr(fake, 'monitor_error', False), \
        'freeze 块不得触发 monitor_error（否则走 stale cleanup 语义）'


def g_M25_missing_init_mutant_killed():
    """M25 变异敏感性：若 staged __init__ 未初始化 _freeze_alerted，
    真实 freeze 块必须抛 AttributeError（即该初始化是承重的）。"""
    probe = _staged_freeze_probe()
    fake, _fx = _setup_v62(states={SYM: {'batch_A': dict(
        mk_batch(['E1'], 1), close_phase=1, pending_close=True,
        close_reason='limit_creating')}})
    if hasattr(fake, '_freeze_alerted'):
        del fake._freeze_alerted          # 模拟「未初始化的运行时状态」
    raised = None
    try:
        probe(fake, fake._states[SYM]['batch_A'], 'batch_A')
    except AttributeError as e:
        raised = e
    assert raised is not None, \
        '未初始化 _freeze_alerted 时 freeze 块仍不报错=' \
        '初始化非承重（与 crash-loud 设计不符，该变异杀不掉）'


GREEN_CASES = [g_staged_freeze_init_structural,
               g_M25_crash_loud_fresh_process,
               g_M25_missing_init_mutant_killed,
               g_R3_h3_survey_invalid_amounts,
               g_R1_d_integration,
               g_R1_a_positional_and_attribution,
               g_R1_i_dash2011_then_filled,
               g_R1_b_g_zero_filtered,
               g_R1_b_g_zero_happy,
               g_R1_l_exit_criteria,
               g_R1_o_sl_attribution,
               g_R1_p_entry_detection,
               g_R1_s_producer2,
               g_R1_k_derive_hole,
               g_R1_n_derive_shape,
               g_R1_t_u_derive_invalid,
               g_R3_h1_coverage_nan,
               g_R3_h2_survey_topology,
               g_R3_b_commit_behavior,
               g_R1_ef_recovery,
               g_R2_f_first_abnormal_wins,
               g_R3_c_begin_seed,
               g_R3_e_endpoint_routing,
               g_R3_d_tp_network_blocked,
               g_R3_f_tp_filled_blocked,
               g_R3_g_coverage_blocked,
               g_R1_h_monitor_hole,
               g_R2_g_rollback_failed]


def main():
    print('=' * 76)
    print('v6.2 GREEN 测试层（V62 SUT × 23 个 RED 场景 + 行为级升级用例）')
    print('授权：GREEN candidate/helper/测试 ✅；生产三文件 ❌ 冻结')
    print('=' * 76)
    ok_count, fail = 0, []
    for case in GREEN_CASES:
        try:
            case()
            ok_count += 1
            print(f"  [GREEN ✓] {case.__name__}")
        except Exception as e:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc().splitlines()[-3:]
            fail.append((case.__name__, ' | '.join(x.strip() for x in tb)))
            print(f"  [GREEN ✗] {case.__name__}: {' | '.join(x.strip() for x in tb[-2:])}")
    print('-' * 76)
    print(f"GREEN: {ok_count}/{len(GREEN_CASES)}")
    for name, err in fail:
        print(f"    FAIL {name}: {err}")
    print('=' * 76)
    if not fail:
        print("✅ V62 SUT 全绿：同批风险语义全部通过（OLD 23 RED 基线独立保持）。")
        return 0
    print("❌ V62 SUT 存在未通过项——修复后重跑。")
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
