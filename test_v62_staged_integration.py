# -*- coding: utf-8 -*-
"""test_v62_staged_integration.py — 组装后 staged trader 的 MARKET/LIMIT 全流程集成验证。

被测对象：G:/tmp/v62_staged/trader_260725_v62_staged.py
  - close_position_market（改动 6/7 完整组装：BEGIN → derive → guard → create →
    六态确认 → ENTRY gate → TP/SL 归因 → 结算 → 六出口 reason）
  - close_position_limit（改动 9 完整组装：BEGIN limit_creating → ENTRY gate →
    TP factual gate → coverage guard → create → durable commit → B-lite → outer except）

方法提取复用 test_v62_red_first 的 _extract_functions 模式（零 import 副作用），
与 FakeSelf62 绑定（_persist_states->bool 契约 + registry Fake）。
"""
import ast
import copy
import os
import sys
import textwrap
import threading

import ccxt

sys.path.insert(0, r'G:/my-crypto-bot')
import test_v62_red_first as R
from test_v62_red_first import (FakeExchange62, FakeSelf62, mk_batch, bind,
                                _order, _e511, SYM)

STAGED_PATH = (os.environ.get('STAGED_TRADER_OVERRIDE')
               or r'G:/tmp/v62_staged/trader_260725_v62_staged.py')
# helper 注入点（变异检查用：指向改坏后的 new_helpers_v62 副本）
V62_HELPER_PATH = (os.environ.get('V62_HELPER_OVERRIDE')
                   or os.path.join(R.PROJECT_DIR, '送审附件_v6.2', 'new_helpers_v62.py'))
TAKER_FEE_RATE = 0.0005
MAKER_FEE_RATE = 0.0002

_NS = {'time': __import__('time'), 'ccxt': ccxt, 'threading': threading,
       'uuid': __import__('uuid'), 'TAKER_FEE_RATE': TAKER_FEE_RATE,
       'MAKER_FEE_RATE': MAKER_FEE_RATE}


def extract_from(path, names, class_name=None):
    """从 staged trader 提取方法源码并 exec（注入 TAKER/MAKER 常量）。"""
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    if class_name is None:
        scopes = [tree]
    else:
        scope = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node
                break
        if scope is None:
            raise LookupError(f'class {class_name} not found in {path}')
        scopes = [scope]
    out = {}
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in names and node.name not in out:
                seg = ast.get_source_segment(src, node)
                if seg is None:
                    continue
                body = textwrap.dedent(seg)
                ns = dict(_NS)
                exec(compile(body, path, 'exec'), ns)
                out[node.name] = ns[node.name]
    missing = [n for n in names if n not in out]
    if missing:
        raise LookupError(f'{path} 缺函数: {missing}')
    return out


# ── 组装后 staged 方法提取 ─────────────────────────────────────────
STAGED = extract_from(STAGED_PATH, ['close_position_market',
                                    'close_position_limit',
                                    'cancel_open_orders'],
                      class_name='CryptoTrader')
# staged 生产方法（仅 _cancel_limit_close_order 需要真实现；收敛/记账走桩）
PROD_METHODS = extract_from(STAGED_PATH, ['_cancel_limit_close_order'],
                            class_name='CryptoTrader')
# v6.2 helpers（已含 limit_creating seed grace）
V62 = R._extract_functions(
    V62_HELPER_PATH,
    ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
     '_survey_same_side_batches', '_close_amount_guard',
     '_begin_close_request_if_active', '_derive_close_txn_vars',
     '_rollback_close_request_if_current', '_verify_entry_order_terminal',
     '_cancel_and_verify_entry_orders', '_set_close_reason_if_current',
     '_pending_entry_ids_for_gate', '_commit_limit_close_order_if_current'])


class _PersistAtFake(FakeSelf62):
    """扩展：第 N 次 _persist_states 精确失败（BEGIN 成功但 commit 失败等时序）。"""

    def _persist_states(self, all_states):
        self.persisted.append(copy.deepcopy(all_states))
        if getattr(self, '_persist_fail_at', None) is not None:
            self._persist_call_count = getattr(self, '_persist_call_count', 0) + 1
            if self._persist_call_count == self._persist_fail_at:
                return False
        if self._persist_fail_left > 0:
            self._persist_fail_left -= 1
            return False
        if self.persist_ok:
            self._states = all_states
        return self.persist_ok


def _setup(states=None, persist_ok=True, persist_fail_first_n=0,
           persist_fail_at=None, **fx_kw):
    fx = FakeExchange62(**fx_kw)
    if persist_fail_at is not None:
        fake = _PersistAtFake(fx, states=states, persist_ok=persist_ok,
                              persist_fail_first_n=0)
        fake._persist_fail_at = int(persist_fail_at)
    else:
        fake = FakeSelf62(fx, states=states, persist_ok=persist_ok,
                          persist_fail_first_n=persist_fail_first_n)
    bind(fake, V62)
    bind(fake, STAGED)
    bind(fake, PROD_METHODS)
    bind(fake, {'_find_registry_identity_by_order_id':
                R.OLD_find_registry_identity_by_order_id})
    # ── 生产方法桩（收敛/记账路径 —— 集成测试聚焦平仓事务语义）──
    bind(fake, {
        '_converge_batch_orders_before_clear': lambda *a, **k: {'scope': 'FULL'},
        '_record_realized_pnl': _record_realized_pnl_stub,
        '_notify_snapshot': lambda *a, **k: None,
        'clear_batch_state': _clear_batch_state_stub,
        '_cancel_limit_close_order': _cancel_limit_close_order_stub,
        '_monitor_limit_close': lambda *a, **k: None,
    })
    fake.ticker_price = 77000.0

    # fetch_ticker / price_to_precision 桩
    def _fetch_ticker(symbol):
        fx.calls.append('fetch_ticker')
        return {'last': fake.ticker_price, 'close': fake.ticker_price,
                'bid': fake.ticker_price - 1.0, 'ask': fake.ticker_price + 1.0}
    fx.fetch_ticker = _fetch_ticker
    fx.price_to_precision = lambda symbol, price: float(price)
    return fake, fx


# 记账桩：仅记录调用，不落盘（生产侧 _record_realized_pnl 是静默失败路径）
_pnl_records = []


def _record_realized_pnl_stub(self, batch_id, symbol, side, amount, avg_price,
                              exit_price, net_pnl, mode, pnl_partial=False):
    _pnl_records.append(dict(batch_id=batch_id, symbol=symbol, side=side,
                             amount=amount, mode=mode, pnl_partial=pnl_partial))


def _clear_batch_state_stub(self, symbol, batch_id, proof=None):
    """集成测试桩：证明清理成功但保留批次（便于断言 registry 归因）。"""
    return True


def _cancel_limit_close_order_stub(self, symbol, batch_id):
    return None


def _mk_full_batch(**extra):
    """lfc=1 已成交 0.001、含 E2 未成交层 + TP/SL + registry 的完整批次。"""
    b = mk_batch(['E1', 'E2'], 1,
                 amounts=[0.001, 0.001],
                 filled_details=[77000.0, 0.0],
                 with_registry=True,
                 tp_order_id='TP1', current_sl_id='SL1',
                 **extra)
    # 补 TP/SL registry 条目（_find_registry_identity_by_order_id 按 order_id 反查）
    reg = b.setdefault('protection_registry', {})
    reg['batch_A:TP:L0:LONG'] = {
        'role': 'TP', 'state': 'CONFIRMED', 'layer': 0, 'side': 'LONG',
        'order_id': 'TP1', 'id_known': True, 'order_kind': 'conditional',
        'updated_at': 0.0}
    reg['batch_A:SL:L0:LONG'] = {
        'role': 'SL', 'state': 'CONFIRMED', 'layer': 0, 'side': 'LONG',
        'order_id': 'SL1', 'id_known': True, 'order_kind': 'conditional',
        'updated_at': 0.0}
    return b


# ══════════════════ MARKET 集成 ══════════════════

def t_market_happy_path():
    """市价 happy path：BEGIN → guard → create → CONFIRMED_FULL → ENTRY gate
    → 撤 TP/SL + 归因 → 结算 → True。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],
        conditional_seq=[_order('canceled', 0.0)],   # ENTRY E2 终态
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is True, f'happy path 应成功: {msg}'
    b = fake._states[SYM]['batch_A']
    # 结算后批次保留（clear 桩不删批次），registry 归因可见
    reg = b.get('protection_registry') or {}
    tp = [e for e in reg.values() if e.get('order_id') == 'TP1']
    sl = [e for e in reg.values() if e.get('order_id') == 'SL1']
    assert tp and tp[0].get('state') == 'PROGRAMMATIC_CANCELED', f'tp={tp}'
    assert sl and sl[0].get('state') == 'PROGRAMMATIC_CANCELED', f'sl={sl}'
    # create exactly once（非幂等）
    assert fx.calls.count('create_order') == 1, 'happy path 必须只 create 一次'
    return 'ok'


def t_market_r1q_sl_cancel_fail_no_attribution():
    """R1-q：撤 TP 成功、撤 SL 失败 → SL registry 绝不写 PROGRAMMATIC_CANCELED。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],
        cancel_by_id={'SL1': ('raise', _e511('-2011 Unknown order'))},
        conditional_seq=[_order('canceled', 0.0)],
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is True, f'应成功: {msg}'
    b = fake._states[SYM]['batch_A']
    reg = b.get('protection_registry') or {}
    tp = [e for e in reg.values() if e.get('order_id') == 'TP1']
    sl = [e for e in reg.values() if e.get('order_id') == 'SL1']
    assert tp and tp[0].get('state') == 'PROGRAMMATIC_CANCELED', f'tp={tp}'
    # SL cancel 抛异常 → 不写 PROGRAMMATIC_CANCELED
    assert sl and sl[0].get('state') != 'PROGRAMMATIC_CANCELED', f'sl={sl}'
    return 'ok'


def t_market_exit34_post_create_exception():
    """R2 出口③④：CONFIRMED_FULL + ENTRY gate 通过 + 结算期异常
    → outer except close_order_placed=True → reason=settlement_error + 冻结。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],
        conditional_seq=[_order('canceled', 0.0)],
    )
    orig = fake._record_realized_pnl
    fake._record_realized_pnl = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('settle crash'))
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert int(b.get('close_phase', 0) or 0) >= 1, '必须保持冻结'
    assert b.get('close_reason') == 'settlement_error', \
        f"reason={b.get('close_reason')}"
    assert any(tg[0] == 'critical' for tg in fake.tg_sent)
    return 'ok'


def t_market_exit2_confirm_unknown():
    """R2 出口②：六态非确认（UNKNOWN）→ reason=market_confirm_unknown。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_e511('network error')],   # fetch_order 异常 → UNKNOWN
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'market_confirm_unknown', f"reason={b.get('close_reason')}"
    return 'ok'


def t_market_exit3_terminal_zero_rollback_rejected():
    """R2 出口③⑦：TERMINAL_ZERO 但 rollback CAS persist 失败
    → reason=rollback_rejected。BEGIN persist=1 成功，rollback persist=2 失败。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('canceled', 0.0)],  # TERMINAL_ZERO
        persist_fail_at=2,
        persist_ok=True,
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'rollback_rejected', f"reason={b.get('close_reason')}"
    return 'ok'


def t_market_exit5_entry_gate_fail():
    """R2 出口⑤：ENTRY gate 返回 False → reason=market_entry_unknown。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],
        # gate 内 fetch_open_orders 返回 None → Fail-Closed
        open_orders_seq=[None],
        conditional_seq=[_order('canceled', 0.0)],
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'market_entry_unknown', f"reason={b.get('close_reason')}"
    assert '保留' in msg
    return 'ok'


def t_market_exit6_precreate_rollback_failed():
    """R2 出口⑥：BEGIN 后 create 前异常（_read_position_amt 失败）且 rollback
    persist 失败 → reason=txn_aborted_rollback_failed。BEGIN persist=1 成功，
    rollback persist=2 失败。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[None],                      # _read_position_amt 失败 → Fail-Closed
        persist_fail_at=2,
        persist_ok=True,
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'txn_aborted_rollback_failed', \
        f"reason={b.get('close_reason')}"
    return 'ok'


def t_market_frozen_batch_begin_rejected():
    """已冻结批次（close_phase=1 + abnormal reason）→ BEGIN 直接拒绝，
    绝不下单、绝不覆盖既有 reason。（注意：这只证明 BEGIN 层不覆盖，
    **不是** first-abnormal-wins 的 mid-flight 证明——后者见下个用例。）"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch(
            close_phase=1, pending_close=True, close_reason='market_confirm_unknown',
            close_op_id='OP1')}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],
        open_orders_seq=[_e511('network error')],
        conditional_seq=[_order('canceled', 0.0)],
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'market_confirm_unknown', 'abnormal reason 不得被覆盖'
    assert fx.calls.count('create_order') == 0, '冻结批次绝不下单'
    return 'ok'


def t_market_first_abnormal_wins_midflight():
    """**真正的 mid-flight first-abnormal-wins 集成**：

        BEGIN 成功（reason=market_confirming）
        → 六态 UNKNOWN → CAS 写 market_confirm_unknown（第一现场）
        → raise RuntimeError
        → generic outer except（close_order_placed=True）尝试 CAS settlement_error
        → `_set_close_reason_if_current` 见首因已 abnormal → 拒绝覆盖
        → 磁盘最终保留 market_confirm_unknown

    通过 hook 记录每一次 CAS 尝试，证明确实走到了 outer except 的
    settlement_error 分支（而不只是靠 BEGIN 拒绝绕过去）。
    """
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_e511('network error')],   # 六态 → UNKNOWN
    )
    cas_attempts = []
    orig_cas = fake._set_close_reason_if_current

    def _recording_cas(symbol, batch_id, close_op_id, reason):
        res = orig_cas(symbol, batch_id, close_op_id, reason)
        cas_attempts.append((reason, res))
        return res

    fake._set_close_reason_if_current = _recording_cas
    ok, msg = fake.close_position_market('batch_A')
    assert ok is False
    # ① 第一现场 CAS 成功写入 market_confirm_unknown
    first = [r for r in cas_attempts if r[0] == 'market_confirm_unknown']
    assert first and first[0][1][0] is True, f'首因 CAS 应成功: {cas_attempts}'
    # ② outer except 确实尝试过 settlement_error（真实调用链，非 BEGIN 短路）
    se = [r for r in cas_attempts if r[0] == 'settlement_error']
    assert se, f'outer except 必须尝试 settlement_error: {cas_attempts}'
    # ③ first-abnormal-wins：该尝试被拒（reason_already_abnormal）
    assert se[0][1][0] is True and 'reason_already_abnormal' in se[0][1][1], \
        f'settlement_error 应被拒: {se}'
    # ④ 磁盘最终保留首因
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'market_confirm_unknown', \
        f"磁盘应保留首因: {b.get('close_reason')}"
    return 'ok'


# ══════════════════ LIMIT 集成 ══════════════════

def _limit_pos_seq():
    return [[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]]


def t_limit_happy_path():
    """LIMIT happy path：BEGIN limit_creating → ENTRY gate → TP gate(TERMINAL_ZERO)
    → coverage guard → create → durable commit 4 字段 → limit_pending_normal → True。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=_limit_pos_seq(),
        conditional_seq=[_order('canceled', 0.0),      # ENTRY E2 终态
                         _order('canceled', 0.0)],     # TP1 六态 TERMINAL_ZERO
    )
    ok, msg = fake.close_position_limit('batch_A', price=77000.0)
    assert ok is True, f'LIMIT happy path 应成功: {msg}'
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'limit_pending_normal', \
        f"reason={b.get('close_reason')}"
    # durable commit 4 字段
    assert b.get('limit_close_order_id') == 'OID1', b.get('limit_close_order_id')
    assert b.get('limit_close_price') == 77000.0, b.get('limit_close_price')
    assert b.get('limit_close_mode') and '自定义' in b.get('limit_close_mode')
    assert int(b.get('close_phase', 0) or 0) == 1
    assert b.get('pending_close') is True
    # TP registry PROGRAMMATIC_CANCELED（N14）
    reg = b.get('protection_registry') or {}
    tp = [e for e in reg.values() if e.get('order_id') == 'TP1']
    assert tp and tp[0].get('state') == 'PROGRAMMATIC_CANCELED', f'tp={tp}'
    # create exactly once（非幂等）
    assert fx.calls.count('create_order') == 1, 'happy path 必须只 create 一次'
    return 'ok'


def t_limit_optimal_price_path():
    """LIMIT 最优价路径（price=None）：BEGIN → ENTRY gate → TP gate → coverage
    → create（price=ask）→ commit → limit_pending_normal。
    组装修正回归：v6.1 doc LIMIT BEGIN 只取 current_price，必须补 bid/ask
    （否则最优价 NameError）。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=_limit_pos_seq(),
        conditional_seq=[_order('canceled', 0.0),   # ENTRY E2
                         _order('canceled', 0.0)],  # TP1 TERMINAL_ZERO
    )
    ok, msg = fake.close_position_limit('batch_A', price=None)  # 最优价
    assert ok is True, f'最优价路径应成功: {msg}'
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'limit_pending_normal', \
        f"reason={b.get('close_reason')}"
    # ask = 77000.0 + 1.0（ticker 桩）
    assert abs(float(b.get('limit_close_price')) - 77001.0) < 1e-9, \
        f"最优价应为 ask=77001.0: {b.get('limit_close_price')}"
    assert b.get('limit_close_mode') and '最优价' in b.get('limit_close_mode')
    assert fx.calls.count('create_order') == 1, '最优价路径必须只 create 一次'
    return 'ok'


def t_limit_tp_gate_blocked():
    """TP gate 非 TERMINAL_ZERO（PENDING）→ 冻结 + reason=limit_tp_unresolved。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=_limit_pos_seq(),
        conditional_seq=[_order('canceled', 0.0),   # ENTRY E2
                         _order('open', 0.0)],      # TP1 PENDING
    )
    ok, msg = fake.close_position_limit('batch_A', price=77000.0)
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'limit_tp_unresolved', f"reason={b.get('close_reason')}"
    # gate 失败 → exchange create 必须 exactly zero（cancel≠create，不能拿 cancel 代替）
    assert fx.calls.count('create_order') == 0, \
        f"TP gate 阻断后不得 create LIMIT: {fx.calls}"
    return 'ok'


def t_limit_coverage_conflict():
    """coverage guard 冲突（aggregate 0.001 < 台账 0.001+0.001）→ limit_amount_conflict。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch(),
                      'batch_B': mk_batch(['F1'], 1, amounts=[0.001])}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        conditional_seq=[_order('canceled', 0.0),   # ENTRY E2
                         _order('canceled', 0.0)],  # TP1
    )
    ok, msg = fake.close_position_limit('batch_A', price=77000.0)
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'limit_amount_conflict', f"reason={b.get('close_reason')}"
    assert fx.calls.count('create_order') == 0, \
        f"coverage 冲突后不得 create LIMIT: {fx.calls}"
    return 'ok'


def t_limit_commit_fail_blite():
    """B-lite：create 成功但 durable commit persist 失败 → 撤单 + 六态确认 +
    reason=limit_persist_failed + 绝不 rollback ACTIVE。BEGIN persist=1 成功，
    commit persist=2 失败。

    🔒 GREEN 终审 P0（endpoint 一致性）：OID1 是 create_order(type='LIMIT') 建出的
    **普通限价单**，cancel / verify 都必须走 **normal endpoint**（不得 stop=True）。
    旧的 conditional 写法会让真单撤不掉，安全网被削弱——本用例锁定该修复。
    OID1 的终态因此必须放进 `order_seq`（normal），而不是 `conditional_seq`。
    """
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=_limit_pos_seq(),
        conditional_seq=[_order('canceled', 0.0),   # ENTRY E2（conditional）
                         _order('canceled', 0.0)],  # TP1（conditional）
        order_seq=[_order('canceled', 0.0)],        # OID1 终态（normal endpoint）
        persist_fail_at=2,   # BEGIN 成功，commit persist 失败
        persist_ok=True,
    )
    ok, msg = fake.close_position_limit('batch_A', price=77000.0)
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert b.get('close_reason') == 'limit_persist_failed', f"reason={b.get('close_reason')}"
    assert int(b.get('close_phase', 0) or 0) == 1, '绝不 rollback'
    assert b.get('pending_close') is True, '绝不 rollback'
    # ── endpoint routing：OID1 走 normal，绝不走 conditional ──
    cancels = [c for c in fx.calls if c.startswith('cancel')]
    fetches = [c for c in fx.calls if c.startswith('fetch')]
    assert fx.calls.count('cancel_normal:OID1') == 1, \
        f"OID1 必须走 normal 撤单: {cancels}"
    assert fx.calls.count('cancel_conditional:OID1') == 0, \
        f"OID1 不得走 conditional 撤单: {cancels}"
    assert fx.calls.count('fetch_normal:OID1') >= 1, \
        f"OID1 六态确认必须走 normal: {fetches}"
    assert fx.calls.count('fetch_conditional:OID1') == 0, \
        f"OID1 六态确认不得走 conditional: {fetches}"
    # 条件单仍走 conditional（对照：证明不是把端点一刀切成 normal）
    assert fx.calls.count('cancel_conditional:E2') >= 1, 'ENTRY 条件单须走 conditional'
    assert fx.calls.count('cancel_conditional:TP1') >= 1, 'TP 条件单须走 conditional'
    # create exactly once（非幂等）
    assert fx.calls.count('create_order') == 1, 'B-lite 前必须只 create 一次'
    return 'ok'


# ══════════════════ r6 三项 P1 的行为测试 ══════════════════

def _mk_batch_e2_terminal():
    """E2 已被程序终结（registry=PROGRAMMATIC_CANCELED），entry_orders 不压缩。"""
    b = _mk_full_batch()
    reg = b.setdefault('protection_registry', {})
    reg['batch_A:ENTRY:1:LONG'] = {
        'role': 'ENTRY', 'state': 'PROGRAMMATIC_CANCELED', 'layer': 1,
        'side': 'LONG', 'order_id': 'E2', 'id_known': True,
        'order_kind': 'conditional', 'updated_at': 0.0,
        'terminated_reason': 'cancel_open_orders'}
    return b


def t_r6a_second_cancel_no_false_failure():
    """负向 A：第一次 🗑️ → PROGRAMMATIC_CANCELED；**第二次 🗑️**
    → 不得再次 cancel/fetch 已知终态 ID，且不得报失败/critical。

    修复前：第二次 🗑️ 会重撤 E2 → Binance 对历史已撤单返回 -2011/OrderNotFound
    → verifier 判 unknown → unresolved_ids 非空 → 假失败。
    """
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        conditional_seq=[_order('canceled', 0.0)],   # E2 终态 → gone
    )
    # 第一次 🗑️
    ok1, msg1 = fake.cancel_open_orders('batch_A')
    assert ok1 is True, f'第一次 🗑️ 应成功: {msg1}'
    reg = fake._states[SYM]['batch_A'].get('protection_registry') or {}
    e2 = [e for e in reg.values() if e.get('order_id') == 'E2']
    assert e2 and e2[0].get('state') == 'PROGRAMMATIC_CANCELED', f'E2 归因: {e2}'

    # 第二次 🗑️（对已知终态 ID）
    fx.calls.clear()
    fake.tg_sent.clear()
    ok2, msg2 = fake.cancel_open_orders('batch_A')
    assert ok2 is True, f'第二次 🗑️ 不得报失败（已知终态应跳过）: {msg2}'
    # 不得再次 cancel / fetch 已确认终结的 E2
    assert fx.calls.count('cancel:E2') == 0, \
        f'不得重复撤已知终态 E2: {[c for c in fx.calls if c.startswith("cancel")]}'
    assert fx.calls.count('fetch_conditional:E2') == 0, \
        f'不得重复验证已知终态 E2: {[c for c in fx.calls if c.startswith("fetch")]}'
    # 不得发 critical（没有新的风险事实）
    assert not any(tg[0] == 'critical' for tg in fake.tg_sent), \
        f'已知终态跳过不得发 critical: {fake.tg_sent}'
    return 'ok'


def t_r6b_gate_skips_known_terminal():
    """负向 B：claimed batch 中 E2=PROGRAMMATIC_CANCELED，且交易所对 E2
    配置 **OrderNotFound**。close 的 ENTRY gate 仍应 True，
    且 E2 的 cancel/fetch 次数 == 0。

    修复前：E2 被重查成 unknown → gate=False → 正常平仓被自己的历史事实挡死。
    """
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_batch_e2_terminal()}},
        pos_seq=[[{'symbol': SYM, 'side': 'LONG', 'contracts': 0.001}]],
        order_seq=[_order('closed', 0.001)],        # MARKET 平仓单成交
        # E2 若被误查：conditional 端点抛 OrderNotFound → unknown
        conditional_seq=[ccxt.OrderNotFound('E2 not found')],
    )
    ok, msg = fake.close_position_market('batch_A')
    assert ok is True, f'已知终态不应挡住平仓: {msg}'
    # E2 完全没被撤、没被查
    assert fx.calls.count('cancel:E2') == 0, \
        f'E2 不得被撤: {[c for c in fx.calls if c.startswith("cancel")]}'
    assert fx.calls.count('fetch_conditional:E2') == 0, \
        f'E2 不得被查: {[c for c in fx.calls if c.startswith("fetch")]}'
    # 未发 ENTRY 相关的 critical
    assert not any('ENTRY' in tg[1] and tg[0] == 'critical' for tg in fake.tg_sent), \
        f'不得因已知终态发 ENTRY critical: {fake.tg_sent}'
    return 'ok'


def t_r6c_missing_batch_guard_no_exchange_call():
    """MARKET / LIMIT：批次不存在 → 纯本地拒绝，
    fetch_ticker / create_order / persist 全部 zero。

    修复前：guard 被组装删掉 → target_symbol=None → fetch_ticker(None)
    → ccxt 异常 → 错误分类被破坏（stale TG 按钮 / 已结束批次本应纯本地拒绝）。
    """
    for fn in (fake_close_market, fake_close_limit):
        fake, fx = _setup(states={SYM: {}})   # 无任何批次
        ok, msg = fn(fake)
        assert ok is False, f'{fn.__name__} 应拒绝: {msg}'
        assert '未找到处于活跃状态的批次号' in msg, \
            f'应返回批次未找到（而非网络错误）: {msg}'
        assert fx.calls.count('fetch_ticker') == 0, \
            f'批次不存在时零 API: {fx.calls}'
        assert fx.calls.count('create_order') == 0, f'不得下单: {fx.calls}'
        assert len(fake.persisted) == 0, \
            f'BEGIN 前不得落盘（persist={len(fake.persisted)}）'
    return 'ok'


def fake_close_market(fake):
    return fake.close_position_market('batch_X')


def fake_close_limit(fake):
    return fake.close_position_limit('batch_X', price=77000.0)


def t_r6e_absent_failed_not_skipped():
    """**动态负向**：ABSENT / FAILED 不得被 skip，必须仍走正常 cancel/verify 路径。

    只豁免 PROGRAMMATIC_CANCELED —— 只有它代表「本程序曾按 ID 确认安全终结」
    这一持久化事实。ABSENT/FAILED 没有这个事实，跳过它们等于凭 registry 状态
    猜交易所现实。用行为（是否真的去撤/查）而非解析实现细节来判，最牢。
    """
    for state in ('ABSENT', 'FAILED'):
        b = _mk_full_batch()
        reg = b.setdefault('protection_registry', {})
        assert 'batch_A:ENTRY:1:LONG' in reg, 'fixture 需含 E2 registry 条目'
        reg['batch_A:ENTRY:1:LONG']['state'] = state
        fake, fx = _setup(
            states={SYM: {'batch_A': b}},
            conditional_seq=[_order('canceled', 0.0)],
        )
        fake.cancel_open_orders('batch_A')
        assert fx.calls.count('cancel:E2') >= 1, \
            f'{state} 不得被 skip（应进入正常撤单/验证路径）: {fx.calls}'
    return 'ok'


def t_r6d_staged_helper_surface_exact13():
    """落地符号白名单：CryptoTrader 的 v6.2 helper 必须 exact 13，
    且嵌套闭包（_finite_* / _topology_ok）不得被提为 class method。"""
    tree = ast.parse(open(STAGED_PATH, encoding='utf-8').read())
    cls = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == 'CryptoTrader':
            cls = n
            break
    methods = {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
    expected = {
        '_begin_close_request_if_active', '_derive_close_txn_vars',
        '_rollback_close_request_if_current', '_set_close_reason_if_current',
        '_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
        '_survey_same_side_batches', '_close_amount_guard',
        '_verify_entry_order_terminal', '_cancel_and_verify_entry_orders',
        '_pending_entry_ids_for_gate', '_commit_limit_close_order_if_current',
    }
    nested = {'_finite_pos_dv', '_finite_zero_dv', '_topology_ok',
              '_finite_nonneg', '_finite_pos', '_finite_zero'}
    assert expected <= methods, f'缺 helper: {sorted(expected - methods)}'
    polluted = nested & methods
    assert not polluted, f'嵌套闭包污染 class surface: {sorted(polluted)}'
    return 'ok'


def t_limit_entry_gate_fail_rollback():
    """LIMIT ENTRY gate 失败 → 未挂单 + CAS 回滚成功 → 批次回 ACTIVE。"""
    fake, fx = _setup(
        states={SYM: {'batch_A': _mk_full_batch()}},
        pos_seq=_limit_pos_seq(),
        open_orders_seq=[None],   # gate 快照不可判定 → Fail-Closed
    )
    ok, msg = fake.close_position_limit('batch_A', price=77000.0)
    assert ok is False
    b = fake._states[SYM]['batch_A']
    assert int(b.get('close_phase', 0) or 0) == 0, 'ENTRY gate 失败应回滚'
    assert b.get('pending_close') is False
    assert fx.calls.count('create_order') == 0, \
        f"ENTRY gate 失败后不得 create LIMIT: {fx.calls}"
    return 'ok'


TESTS = [
    t_market_happy_path,
    t_market_r1q_sl_cancel_fail_no_attribution,
    t_market_exit34_post_create_exception,
    t_market_exit2_confirm_unknown,
    t_market_exit3_terminal_zero_rollback_rejected,
    t_market_exit5_entry_gate_fail,
    t_market_exit6_precreate_rollback_failed,
    t_market_frozen_batch_begin_rejected,
    t_market_first_abnormal_wins_midflight,
    t_limit_happy_path,
    t_limit_optimal_price_path,
    t_limit_tp_gate_blocked,
    t_limit_coverage_conflict,
    t_limit_commit_fail_blite,
    t_limit_entry_gate_fail_rollback,
    # r6 三项 P1
    t_r6a_second_cancel_no_false_failure,
    t_r6b_gate_skips_known_terminal,
    t_r6c_missing_batch_guard_no_exchange_call,
    t_r6d_staged_helper_surface_exact13,
    t_r6e_absent_failed_not_skipped,
]


def main():
    print('=' * 76)
    print('v6.2 staged 集成测试（组装后 close_position_market / close_position_limit）')
    print('=' * 76)
    passed = 0
    failed = 0
    for t in TESTS:
        try:
            r = t()
            print(f'  [✓] {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  [✗] {t.__name__}: {type(e).__name__}: {e}')
            failed += 1
    print('-' * 76)
    print(f'PASSED: {passed}/{len(TESTS)}')
    if failed:
        print(f'FAILED: {failed}')
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
