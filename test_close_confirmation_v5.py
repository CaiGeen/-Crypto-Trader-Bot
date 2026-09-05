# -*- coding: utf-8 -*-
"""test_close_confirmation_v5.py — v5 atomic BEGIN / coverage 不变量 / TERMINAL_ZERO 收紧

被测对象：G:/tmp/new_helpers_v5.py（提议实现，ast 隔离提取，零 import 副作用）
负向对照：
  - G:/tmp/new_helpers_v4.py  —— 证明 v4 在【target 已 close_phase=1 的决定性例子】
                                 和【closed+filled=0 矛盾组合】上会给出错误放行/回滚资格
  - G:/tmp/new_helpers_v3.py  —— 保留 v3 的 not_filled 过宽与 ENTRY or [] 对照
  - G:/tmp/new_helpers_v3_entry.py —— v3 缺陷版 ENTRY 函数档案

对应 ChatGPT 对 v4 终审的 4 个上线阻断项：
  阻断1（§二+§三）atomic BEGIN/claim            → B1-B8
  阻断2（§一+§六）coverage 不变量含 target       → G3/G6/G7（G6-v4 负向）
  阻断3（§四）    ENTRY False 必须成 clear gate  → E1/E1-负向（+ 文档改动 2 的人工核）
  阻断4（§五）    TERMINAL_ZERO 收紧             → T1-T4（T1-v4/T2-v4 负向）
  非阻断（§八-1） _read_position_amt 非 list     → P1（P1-v4 负向）

判据纪律：每个场景同时给出 v5 期望与 v4 实测，断言必须能在回归时失败。
"""
import ast
import sys
import time
import uuid
import textwrap
import threading

import ccxt

sys.stdout.reconfigure(encoding='utf-8')

V5_PATH = 'G:/tmp/new_helpers_v5.py'
V4_PATH = 'G:/tmp/new_helpers_v4.py'
V3_PATH = 'G:/tmp/new_helpers_v3.py'

WANT_FUNCS = ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
              '_survey_same_side_batches', '_close_amount_guard',
              '_begin_close_request_if_active',
              '_rollback_close_request_if_current', '_verify_entry_order_terminal',
              '_cancel_and_verify_entry_orders']

SYM = 'BTC/USDT:USDT'


def extract_impl(path, want=None):
    """从磁盘源码 ast 提取目标函数（隔离执行：零 import 副作用、零网络）。
    want=None → 要求全集；传子集 → 只要求其中存在的函数（v3 负向对照用）。"""
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    impl = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in want and node.name not in impl:
            seg = ast.get_source_segment(src, node)
            body = textwrap.dedent(seg)
            ns = {'time': time, 'ccxt': ccxt, 'threading': threading, 'uuid': uuid}
            exec(compile(body, path, 'exec'), ns)
            impl[node.name] = ns[node.name]
    missing = [f for f in want if f not in impl]
    if missing:
        raise RuntimeError(f'{path} 缺函数: {missing}')
    return impl


def bind(fake, impl):
    for name, fn in impl.items():
        setattr(fake, name, fn.__get__(fake, type(fake)))


class FakeExchange:
    """fetch_positions / fetch_order 按脚本序列返回；用尽后重复最后一个值
    （避免失败原因被'序列耗尽异常'掩盖——v2 教训）。"""

    def __init__(self, pos_seq=None, order_seq=None, open_orders_seq=None):
        self.pos_seq = list(pos_seq if pos_seq is not None else [])
        self.order_seq = list(order_seq if order_seq is not None else [])
        self.oo_seq = list(open_orders_seq if open_orders_seq is not None else [])
        # 用尽后重复各自序列的最后一个值（不抛异常、不静默变 None），
        # 否则失败原因会被"序列耗尽"掩盖真实机制（v2 教训，务必言出必行）
        self._fallback = {'pos': self.pos_seq[-1] if self.pos_seq else [],
                          'order': self.order_seq[-1] if self.order_seq else None,
                          'oo': self.oo_seq[-1] if self.oo_seq else []}
        self.cancelled = []

    def _next(self, seq, key):
        v = seq.pop(0) if seq else self._fallback[key]
        if isinstance(v, Exception):
            raise v
        return v

    def fetch_positions(self, symbols=None):
        return self._next(self.pos_seq, 'pos')

    def fetch_order(self, order_id, symbol, params=None, retries=None):
        return self._next(self.order_seq, 'order')

    def fetch_open_orders(self, symbol, params=None):
        return self._next(self.oo_seq, 'oo')

    def cancel_order(self, order_id, symbol, params=None):
        self.cancelled.append(order_id)


class FakeSelf:
    def __init__(self, exchange, states=None, fail_load=False):
        self.exchange = exchange
        self._state_lock = threading.Lock()
        self._states = states if states is not None else {}
        self.persisted = []
        self._fail_load = fail_load
        self.tg_sent = []

    def _safe_api_call(self, func, *args, retries=5, delay=2, **kwargs):
        return func(*args, **kwargs)

    def load_all_states(self):
        if self._fail_load:
            raise RuntimeError('load_all_states failed (simulated)')
        return self._states

    def _persist_states(self, all_states):
        self.persisted.append({k: {b: dict(v) for b, v in s.items()}
                               for k, s in all_states.items()})

    def send_tg_notification(self, msg, level='info'):
        self.tg_sent.append((level, msg))


def _order(status, filled):
    return {'id': 'OID1', 'status': status, 'filled': filled, 'average': 77000.0}


def _order_nofilled(status):
    """filled 字段缺失（v5 §五：必须判 UNKNOWN，绝不给回滚资格）"""
    return {'id': 'OID1', 'status': status, 'average': 77000.0}


def _order_nullfilled(status):
    """filled 字段为 None"""
    return {'id': 'OID1', 'status': status, 'filled': None, 'average': 77000.0}


POS_LONG_001 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.001,
                 'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
POS_LONG_002 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.002,
                 'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
POS_LONG_0005 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.0005,
                  'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]

cases = []


def run_confirm(impl, order_seq, pos_seq=None, expected=0.001, pos_before=0.001, attempts=3):
    fx = FakeExchange(pos_seq=pos_seq or [POS_LONG_001], order_seq=order_seq)
    sf = FakeSelf(fx)
    bind(sf, impl)
    r = impl['_confirm_close_filled'](sf, SYM, 'BUY', True, 'OID1', expected,
                                      pos_before=pos_before, attempts=attempts, delay=0.0)
    # v3 返回二元组 (verdict, detail)；v4/v5 返回三元组 (verdict, detail, filled)
    if len(r) == 2:
        return r[0], r[1], None
    return r[0], r[1], r[2]


def run_amount(impl, pos_seq, ledger, states, target='batch_A', fail_load=False):
    fx = FakeExchange(pos_seq=pos_seq)
    sf = FakeSelf(fx, states=states, fail_load=fail_load)
    bind(sf, impl)
    return impl['_close_amount_guard'](sf, SYM, 'BUY', True, ledger, target)


def run_survey(impl, states, target='batch_A'):
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states)
    bind(sf, impl)
    return impl['_survey_same_side_batches'](sf, SYM, 'BUY', target)


def run_begin(impl, states, batch_id='batch_A', reason='market_confirming'):
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states)
    bind(sf, impl)
    r = impl['_begin_close_request_if_active'](sf, SYM, batch_id, reason)
    return sf, r[0], r[1], r[2]


def run_rollback(impl, states, op_id):
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states)
    bind(sf, impl)
    ok, reason = impl['_rollback_close_request_if_current'](sf, SYM, 'batch_A', op_id)
    return sf, ok, reason


def run_entry(impl, oo_seq, order_seq):
    fx = FakeExchange(open_orders_seq=oo_seq, order_seq=order_seq)
    sf = FakeSelf(fx)
    bind(sf, impl)
    b_data = {'entry_orders': ['E1', 'E2'], 'last_filled_count': 1}  # 只剩 E2 未成交
    return impl['_cancel_and_verify_entry_orders'](sf, SYM, 'batch_A', b_data, 1), sf


def _mk_batch(phase=0, pending=False, side='BUY', amt=0.001, settled=False,
              filled_count=1, active=True):
    return {'side': side, 'close_phase': phase, 'pending_close': pending,
            'target_amounts': [amt], 'last_filled_count': filled_count,
            'settled_by_limit_close': settled, 'is_active': active}


def main():
    print('=' * 68)
    print('一、六态确认器 + TERMINAL_ZERO 收紧（ChatGPT 终审 §五）')
    print('=' * 68)

    # C1 CONFIRMED_FULL：closed + filled 达标 → 放行
    v, d, f = run_confirm(IMPL, [_order('closed', 0.001)], pos_seq=[POS_LONG_001, []])
    cases.append(('C1 closed+filled达标 → CONFIRMED_FULL', v, 'CONFIRMED_FULL', f, 0.001, d))

    # C2 open ×3 → PENDING（不回滚）
    v, d, f = run_confirm(IMPL, [_order('open', 0)] * 3)
    cases.append(('C2 open×3 → PENDING（不回滚）', v, 'PENDING', f, None, d))

    # C3 partial → PARTIAL（不回滚）
    v, d, f = run_confirm(IMPL, [_order('closed', 0.0005)])
    cases.append(('C3 partial filled=0.0005 → PARTIAL（不回滚）', v, 'PARTIAL', f, 0.0005, d))

    # C4 canceled + 权威 filled=0 → TERMINAL_ZERO（唯一可回滚）
    v, d, f = run_confirm(IMPL, [_order('canceled', 0.0)])
    cases.append(('C4 canceled+权威filled=0 → TERMINAL_ZERO（可回滚）', v,
                  'TERMINAL_ZERO', f, 0.0, d))

    # C6 not_found（重试后）→ NOT_CONFIRMED（不回滚）
    v, d, f = run_confirm(IMPL, [ccxt.OrderNotFound('nf')] * 3, attempts=1)
    cases.append(('C6 create有ID但fetch不到 → NOT_CONFIRMED（不回滚）', v,
                  'NOT_CONFIRMED', f, None, d))

    # C7 查询异常 ×3 → UNKNOWN
    v, d, f = run_confirm(IMPL, [RuntimeError('net')] * 3)
    cases.append(('C7 查询异常×3 → UNKNOWN', v, 'UNKNOWN', f, None, d))

    # ── T1-T4：v5 TERMINAL_ZERO 收紧（§五）
    v, d, f = run_confirm(IMPL, [_order('closed', 0.0)])
    cases.append(('T1 closed+filled=0（矛盾组合）→ UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order_nofilled('canceled')])
    cases.append(('T2 canceled 但 filled 字段缺失 → UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order_nullfilled('expired')])
    cases.append(('T3 expired 但 filled=None → UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order('rejected', 0.0)])
    cases.append(('T4 rejected+权威filled=0 → TERMINAL_ZERO', v, 'TERMINAL_ZERO', f, 0.0, d))

    # T1/T2 的 v4 负向对照：v4 会给回滚资格（T1）/把缺失当 0（T2）
    v4, d4, _ = run_confirm(IMPL_V4, [_order('closed', 0.0)])
    cases.append(('T1-v4 负向: v4 判 TERMINAL_ZERO（错误给回滚资格）', v4,
                  'TERMINAL_ZERO', None, None, d4))
    v4, d4, _ = run_confirm(IMPL_V4, [_order_nofilled('canceled')])
    cases.append(('T2-v4 负向: v4 判 TERMINAL_ZERO（filled缺失被当成0）', v4,
                  'TERMINAL_ZERO', None, None, d4))

    # v3 负向对照（保留）：not_filled 过宽
    for tag, seq in [('open×3', [_order('open', 0)] * 3),
                     ('partial', [_order('closed', 0.0005)]),
                     ('not_found', [ccxt.OrderNotFound('nf')] * 3)]:
        r = run_confirm(IMPL_V3, seq, attempts=1 if 'not' in tag else 3)
        cases.append((f'N-{tag}: v3 判 {r[0]}（应为 not_filled，证明 v3 会错误回滚）',
                      r[0], 'not_filled', None, None, ''))

    print()
    print('=' * 68)
    print('二、atomic BEGIN/claim（ChatGPT 终审 §二 + §三 + §六）')
    print('=' * 68)

    # B1 首次 claim 成功
    st = {SYM: {'batch_A': _mk_batch()}}
    sf, ok, op_id, why = run_begin(IMPL, st)
    b = sf._states[SYM]['batch_A']
    cases.append(('B1 首次 claim → 成功', ok, True, None, None, why))
    cases.append(('B1b close_phase=1 已落盘', b.get('close_phase'), 1, None, None, ''))
    cases.append(('B1c close_op_id 非空且为 32 位 hex',
                  len(op_id) == 32 and all(c in '0123456789abcdef' for c in op_id),
                  True, None, None, op_id))
    cases.append(('B1d close_reason 已写入', b.get('close_reason'),
                  'market_confirming', None, None, ''))
    cases.append(('B1e 落盘恰 1 次（锁内原子）', len(sf.persisted), 1, None, None, ''))
    cases.append(('B1f is_programmatic_cancel 置 True',
                  b.get('is_programmatic_cancel'), True, None, None, ''))

    # B2 二次 claim（phase 已为 1）→ 拒绝，且不落盘
    sf2, ok2, op2, why2 = run_begin(IMPL, sf._states)
    cases.append(('B2 二次 claim（phase=1）→ 拒绝', ok2, False, None, None, why2))
    cases.append(('B2b 被拒绝时不落盘', len(sf2.persisted), 0, None, None, ''))
    cases.append(('B2c 被拒绝时不返回 op_id（无权下单）', op2, '', None, None, ''))

    # B3 同方向其他批次已在平仓 → 拒绝（§六 单飞）
    st3 = {SYM: {'batch_A': _mk_batch(),
                 'batch_B': _mk_batch(phase=1, pending=True)}}
    sf3, ok3, op3, why3 = run_begin(IMPL, st3)
    cases.append(('B3 同方向有在途平仓 → 拒绝（单飞）', ok3, False, None, None, why3))
    cases.append(('B3b 拒绝原因含 same_side_close_inflight',
                  'same_side_close_inflight' in why3, True, None, None, why3))
    cases.append(('B3c 拒绝时未修改任何批次状态',
                  sf3._states[SYM]['batch_A'].get('close_phase'), 0, None, None, ''))

    # B4 不同方向批次在平仓 → 不受影响（应放行）
    st4 = {SYM: {'batch_A': _mk_batch(side='BUY'),
                 'batch_B': _mk_batch(phase=1, side='SELL')}}
    sf4, ok4, op4, why4 = run_begin(IMPL, st4)
    cases.append(('B4 反方向有在途平仓 → 不拦截', ok4, True, None, None, why4))

    # B5 缺 close_reason → 拒绝
    sf5, ok5, op5, why5 = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}}, reason='')
    cases.append(('B5 缺 close_reason → 拒绝', ok5, False, None, None, why5))

    # B6 settled_by_limit_close 事实在 → 拒绝
    st6 = {SYM: {'batch_A': _mk_batch(settled=True)}}
    sf6, ok6, op6, why6 = run_begin(IMPL, st6)
    cases.append(('B6 settled 事实在 → 拒绝', ok6, False, None, None, why6))

    # B7 batch 不存在 → 拒绝
    sf7, ok7, op7, why7 = run_begin(IMPL, {SYM: {}}, batch_id='batch_X')
    cases.append(('B7 batch 不存在 → 拒绝', ok7, False, None, None, why7))

    # B8 uuid 唯一性：两次独立 claim 的 op_id 必须不同
    op_ids = []
    for _ in range(5):
        _s = {SYM: {'batch_A': _mk_batch()}}
        _, _ok, _op, _ = run_begin(IMPL, _s)
        op_ids.append(_op)
    cases.append(('B8 5 次独立 claim 的 op_id 互不相同', len(set(op_ids)), 5, None, None,
                  f'{op_ids[0][:8]}…'))

    # B9 BEGIN → 正常 CAS 回滚闭环（op_id 匹配）
    st9 = {SYM: {'batch_A': _mk_batch()}}
    sf9, ok9, op9, _ = run_begin(IMPL, st9)
    sfrb, okrb, whyrb = run_rollback(IMPL, sf9._states, op9)
    cases.append(('B9 BEGIN→CAS回滚成功（op_id 匹配）', (ok9, okrb), (True, True),
                  None, None, whyrb))

    # B10 假冒 op_id 无法回滚别人的事务
    sf10, ok10, op10, _ = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}})
    _, ok_fake, why_fake = run_rollback(IMPL, sf10._states, 'deadbeef' * 4)
    cases.append(('B10 假冒 op_id 无法回滚 → 拒绝', ok_fake, False, None, None, why_fake))

    print()
    print('=' * 68)
    print('三、coverage 不变量（ChatGPT 终审 §一 + §六）')
    print('=' * 68)

    # G1 actual >= ledger → 按台账平
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, {})
    cases.append(('G1 敞口0.002≥台账0.001 → 平0.001', amt, 0.001, None, None, d))

    # G2 actual < ledger 且单批次 → 按实测平（归因唯一成立）
    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {})
    cases.append(('G2 敞口0.0005<台账0.001 单批次 → 平0.0005', amt, 0.0005, None, None, d))

    # G3 多批次且 actual < 台账合计 → None
    states_multi = {SYM: {'batch_A': _mk_batch(), 'batch_B': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_001], 0.001, states_multi)
    cases.append(('G3 多批次且实际<台账合计 → None（禁止自动平）', amt, None, None, None, d))

    # G3b 多批次但台账合计 == 总实际 → 归属成立
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, states_multi)
    cases.append(('G3b 多批次且实际==台账合计 → 平0.001', amt, 0.001, None, None, d))

    # 🔴 G6 决定性例子·真实调用顺序版（target 已 BEGIN 置 close_phase=1）
    #    A(target) 0.001 phase=1 / B 0.001 phase=0 / actual 0.001
    #    v4 会把 A 排除 → sum_all=0.001 → 放行；v5 必须拦截
    states_g6 = {SYM: {'batch_A': _mk_batch(phase=1, pending=True),
                       'batch_B': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_001], 0.001, states_g6)
    cases.append(('G6 target已phase=1的决定性例子 → None（v5 拦截）', amt, None,
                  None, None, d))

    # survey 三元组直查：target 必须计入 sum_all
    others, sum_all, blocking = run_survey(IMPL, states_g6)
    cases.append(('G6b survey：sum_all 含 target = 0.002', round(sum_all, 6), 0.002,
                  None, None, f'others={others} blocking={blocking}'))
    cases.append(('G6c survey：blocking_count=0（其他批次未在平仓）', blocking, 0,
                  None, None, ''))

    # G6-v4 负向：v4 在同样状态下放行（决定性例子被漏过）
    amt_v4, d_v4 = run_amount(IMPL_V4, [POS_LONG_001], 0.001, states_g6)
    cases.append(('G6-v4 负向: v4 放行（返回 0.001，决定性例子漏过）', amt_v4, 0.001,
                  None, None, d_v4))

    # G7 blocking_count > 0（另一批次也在平仓，理论不可达但必须 Fail-Closed）
    states_g7 = {SYM: {'batch_A': _mk_batch(phase=1),
                       'batch_B': _mk_batch(phase=1),
                       'batch_C': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, states_g7, target='batch_A')
    cases.append(('G7 同方向另有在途平仓 → None（Fail-Closed）', amt, None, None, None, d))

    # G4 读取失败 → None（B-09 Fail-Closed）
    amt, d = run_amount(IMPL, [None], 0.001, {})
    cases.append(('G4 持仓读取失败 → None（不发单）', amt, None, None, None, d))

    # G5 load_all_states 失败 → None
    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {}, fail_load=True)
    cases.append(('G5 批次统计失败 → None（归因不可判定）', amt, None, None, None, d))

    print()
    print('=' * 68)
    print('四、_read_position_amt 严格 Fail-Closed（ChatGPT 终审 §八-1）')
    print('=' * 68)

    fx = FakeExchange(pos_seq=[{'unexpected': 'dict'}])
    sf = FakeSelf(fx)
    bind(sf, IMPL)
    v = IMPL['_read_position_amt'](sf, SYM, 'BUY', True)
    cases.append(('P1 fetch_positions 返回 dict → None', v, None, None, None, ''))

    fx4 = FakeExchange(pos_seq=[{'unexpected': 'dict'}])
    sf4p = FakeSelf(fx4)
    bind(sf4p, IMPL_V4)
    v4p = IMPL_V4['_read_position_amt'](sf4p, SYM, 'BUY', True)
    cases.append(('P1-v4 负向: v4 返回 0.0（UNKNOWN→ZERO 退化）', v4p, 0.0, None, None, ''))

    print()
    print('=' * 68)
    print('五、ENTRY 逐 ID 验证（ChatGPT 终审 §三 + §四 前门）')
    print('=' * 68)

    # E1 fetch_open_orders 返回 None → False
    ok, sf = run_entry(IMPL, [None], [_order('canceled', 0.0)])
    cases.append(('E1 快照返回 None → False（Fail-Closed）', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    ok_v3, sf_v3 = run_entry(IMPL_V3, [None], [])
    cases.append(('E1-负向: v3 在 None 上返回 True（假确认复现）', ok_v3, True,
                  None, None, ''))

    # E2 快照 [] + 逐 ID canceled → True
    ok, sf = run_entry(IMPL, [[]], [_order('canceled', 0.0)])
    cases.append(('E2 快照[]+ID终态canceled → True', ok, True, None, None, ''))

    # E3 逐 ID 仍 open → False
    ok, sf = run_entry(IMPL, [[]], [_order('open', 0.0)] * 3)
    cases.append(('E3 逐ID验证 open → False', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    # E4 逐 ID filled → False（ENTRY 在等待期间成交，仓位已变化）
    ok, sf = run_entry(IMPL, [[]], [_order('closed', 0.001)])
    cases.append(('E4 逐ID验证 filled → False', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    # ═══ 判定 ═══
    print()
    print('=' * 68)
    fails = 0
    for name, got, want, filled, wf, d in cases:
        ok = (got == want) and (filled is None or wf is None or filled == wf)
        mark = '✅' if ok else '❌'
        if not ok:
            fails += 1
        extra = f' filled={filled}' if filled is not None else ''
        print(f'  {mark} {name}{extra}')
        if not ok:
            print(f'       期望 {want!r} / 实得 {got!r}  | {d}')
    print('=' * 68)
    if fails:
        print(f'🚨 {fails}/{len(cases)} 项失败')
        return 1
    print(f'✅ {len(cases)}/{len(cases)} 全部通过（含 v4 / v3 负向对照）')
    return 0


IMPL = extract_impl(V5_PATH, WANT_FUNCS)
IMPL_V4 = extract_impl(V4_PATH, ['_read_position_amt', '_fetch_close_order_state',
                                 '_confirm_close_filled', '_survey_same_side_batches',
                                 '_close_amount_guard'])
IMPL_V3 = extract_impl(V3_PATH, ['_read_position_amt', '_fetch_close_order_state',
                                 '_confirm_close_filled'])


def extract_v3_entry():
    """v3 缺陷版 ENTRY 函数负向对照源（含 `or []`），固化为档案。"""
    src = open('G:/tmp/new_helpers_v3_entry.py', encoding='utf-8').read()
    ns = {'time': time, 'ccxt': ccxt}
    exec(compile(src, 'G:/tmp/new_helpers_v3_entry.py', 'exec'), ns)
    return ns['_cancel_and_verify_entry_orders']


IMPL_V3['_cancel_and_verify_entry_orders'] = extract_v3_entry()

if __name__ == '__main__':
    sys.exit(main())
