# -*- coding: utf-8 -*-
"""test_close_confirmation_v4.py — v4 六态确认器 / 原子回滚 CAS / 归因守卫 / ENTRY 逐 ID 验证

被测对象：G:/tmp/new_helpers_v4.py（提议实现，ast 隔离提取，零 import 副作用）
负向对照：G:/tmp/new_helpers_v3.py（v3 实现，证明其在 PARTIAL/NOT_CONFIRMED 上会错误回滚）

对应 ChatGPT 对 v3 终审的四个 P0：
  §一 not_filled 过宽   → C2/C3/C4/C6：open→PENDING、partial→PARTIAL、
                          not_found→NOT_CONFIRMED，全部**不可回滚**
  §二 stale snapshot    → R1-R5：close_op_id CAS + 锁内重读
  §三 None→[]           → E1：fetch_open_orders None 必须 False（v3 会假确认）
  §四 归因冲突          → G3：actual<ledger 且多批次 → 拒绝自动平仓

判据纪律：每个场景同时给出 v4 期望与 v3 实测，断言必须能在回归时失败。
"""
import ast
import sys
import time
import textwrap
import threading

import ccxt

sys.stdout.reconfigure(encoding='utf-8')

V4_PATH = 'G:/tmp/new_helpers_v4.py'
V3_PATH = 'G:/tmp/new_helpers_v3.py'

WANT_FUNCS = ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
              '_survey_same_side_batches', '_close_amount_guard',
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
            ns = {'time': time, 'ccxt': ccxt, 'threading': threading}
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


POS_LONG_001 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.001, 'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
POS_LONG_002 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.002, 'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]
POS_LONG_0005 = [{'symbol': SYM, 'side': 'long', 'contracts': 0.0005, 'info': {'symbol': 'BTCUSDT', 'positionSide': 'LONG'}}]

cases = []


def run_confirm(impl, order_seq, pos_seq=None, expected=0.001, pos_before=0.001, attempts=3):
    fx = FakeExchange(pos_seq=pos_seq or [POS_LONG_001], order_seq=order_seq)
    sf = FakeSelf(fx)
    bind(sf, impl)
    return impl['_confirm_close_filled'](sf, SYM, 'BUY', True, 'OID1', expected,
                                         pos_before=pos_before, attempts=attempts, delay=0.0)


def run_amount(impl, pos_seq, ledger, states, fail_load=False):
    fx = FakeExchange(pos_seq=pos_seq)
    sf = FakeSelf(fx, states=states, fail_load=fail_load)
    bind(sf, impl)
    return impl['_close_amount_guard'](sf, SYM, 'BUY', True, ledger, 'batch_A')


def run_rollback(states, op_id):
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states)
    bind(sf, IMPL)
    ok, reason = impl_rollback(sf, 'BTC/USDT:USDT', 'batch_A', op_id)
    return sf, ok, reason


def run_entry(impl, oo_seq, order_seq):
    fx = FakeExchange(open_orders_seq=oo_seq, order_seq=order_seq)
    sf = FakeSelf(fx)
    bind(sf, impl)
    b_data = {'entry_orders': ['E1', 'E2'], 'last_filled_count': 1}  # 只剩 E2 未成交
    return impl['_cancel_and_verify_entry_orders'](sf, SYM, 'batch_A', b_data, 1), sf


def main():
    print('=' * 68)
    print('v4 六态确认器（ChatGPT 终审 §一）')
    print('=' * 68)

    # C1 CONFIRMED_FULL：closed + filled 达标 → 放行
    v, d, f = run_confirm(IMPL, [_order('closed', 0.001)], pos_seq=[POS_LONG_001, []])
    cases.append(('C1 closed+filled达标 → CONFIRMED_FULL', v, 'CONFIRMED_FULL', f, 0.001, d))

    # C2 open ×3 → PENDING（v3 会给 not_filled → 错误回滚）
    v, d, f = run_confirm(IMPL, [_order('open', 0)] * 3)
    cases.append(('C2 open×3 → PENDING（不回滚）', v, 'PENDING', f, None, d))

    # C3 partial → PARTIAL（v3 会给 not_filled → 错误回滚）
    v, d, f = run_confirm(IMPL, [_order('closed', 0.0005)])
    cases.append(('C3 partial filled=0.0005 → PARTIAL（不回滚）', v, 'PARTIAL', f, 0.0005, d))

    # C4 canceled+filled=0 → TERMINAL_ZERO（唯一可回滚）
    v, d, f = run_confirm(IMPL, [_order('canceled', 0.0)])
    cases.append(('C4 canceled+filled=0 → TERMINAL_ZERO（可回滚）', v, 'TERMINAL_ZERO', f, 0.0, d))

    # C5 closed 但 filled=0 → TERMINAL_ZERO
    v, d, f = run_confirm(IMPL, [_order('closed', 0.0)])
    cases.append(('C5 closed+filled=0 → TERMINAL_ZERO', v, 'TERMINAL_ZERO', f, 0.0, d))

    # C6 not_found（重试后）→ NOT_CONFIRMED（v3 会给 not_filled → 错误回滚）
    v, d, f = run_confirm(IMPL, [ccxt.OrderNotFound('nf')] * 3, attempts=1)
    cases.append(('C6 create有ID但fetch不到 → NOT_CONFIRMED（不回滚）', v, 'NOT_CONFIRMED', f, None, d))

    # C7 查询异常 ×3 → UNKNOWN
    v, d, f = run_confirm(IMPL, [RuntimeError('net')] * 3)
    cases.append(('C7 查询异常×3 → UNKNOWN', v, 'UNKNOWN', f, None, d))

    print()
    print('=' * 68)
    print('负向对照：v3 在 C2/C3/C6 上判 not_filled（错误回滚行为复现）')
    print('=' * 68)
    for tag, seq in [('open×3', [_order('open', 0)] * 3),
                     ('partial', [_order('closed', 0.0005)]),
                     ('not_found', [ccxt.OrderNotFound('nf')] * 3)]:
        r = run_confirm(IMPL_V3, seq, attempts=1 if 'not' in tag else 3)
        v3 = r[0]  # v3 返回二元组，v4 返回三元组，取 verdict 即可
        expect_v3 = 'not_filled'
        ok = (v3 == expect_v3)
        cases.append((f'N-{tag}: v3 判 {v3}（应为 not_filled，证明 v3 会错误回滚）',
                      v3, expect_v3, None, None, ''))
        if not ok:
            print(f'  ⚠️ v3 对照不符: {tag} → {v3}')

    print()
    print('=' * 68)
    print('归因守卫 _close_amount_guard（ChatGPT 终审 §四）')
    print('=' * 68)

    # G1 actual >= ledger → 按台账平
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, {})
    cases.append(('G1 敞口0.002≥台账0.001 → 平0.001', amt, 0.001, None, None, d))

    # G2 actual < ledger 且单批次 → 按实测平（归因唯一成立）
    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {})
    cases.append(('G2 敞口0.0005<台账0.001 单批次 → 平0.0005', amt, 0.0005, None, None, d))

    # G3 ChatGPT §四 决定性例子：A/B 各台账 0.001、总实际 0.001（A 已被手动平但台账未同步）
    # actual == ledger_A，单看本批台账发现不了漂移；只有台账合计 0.002 > 实际 0.001 可检
    states_multi = {SYM: {'batch_A': {'side': 'BUY', 'close_phase': 0,
                                      'target_amounts': [0.001], 'last_filled_count': 1},
                          'batch_B': {'side': 'BUY', 'close_phase': 0,
                                      'target_amounts': [0.001], 'last_filled_count': 1}}}
    amt, d = run_amount(IMPL, [POS_LONG_001], 0.001, states_multi)
    cases.append(('G3 多批次且实际<台账合计 → None（禁止自动平）', amt, None, None, None, d))

    # G3b 多批次但台账合计 == 总实际 → 归属成立，按台账平
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, states_multi)
    cases.append(('G3b 多批次且实际==台账合计 → 平0.001', amt, 0.001, None, None, d))

    # G4 读取失败 → None（B-09 Fail-Closed）
    amt, d = run_amount(IMPL, [None], 0.001, {})
    cases.append(('G4 持仓读取失败 → None（不发单）', amt, None, None, None, d))

    # G5 load_all_states 失败（归因不可判定）→ None
    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {}, fail_load=True)
    cases.append(('G5 批次统计失败 → None（归因不可判定）', amt, None, None, None, d))

    print()
    print('=' * 68)
    print('原子回滚 CAS _rollback_close_request_if_current（ChatGPT 终审 §二）')
    print('=' * 68)

    def mk_states(phase=1, op='OP1', settled=False, pending=True):
        return {SYM: {'batch_A': {'close_phase': phase, 'close_op_id': op,
                                  'settled_by_limit_close': settled,
                                  'pending_close': pending,
                                  'is_programmatic_cancel': True}}}

    sf, ok, reason = run_rollback(mk_states(), 'OP1')
    got = sf._states[SYM]['batch_A'].get('close_phase')
    cases.append(('R1 op_id匹配+phase1 → 回滚成功', (ok, got), (True, 0), None, None, reason))
    cases.append(('R1b 回滚已落盘（persist 恰 1 次）', len(sf.persisted), 1, None, None, ''))

    sf, ok, reason = run_rollback(mk_states(op='OP_OTHER'), 'OP1')
    cases.append(('R2 op_id 不匹配 → 拒绝', ok, False, None, None, reason))

    sf, ok, reason = run_rollback(mk_states(settled=True), 'OP1')
    cases.append(('R3 settled 事实在 → 拒绝（绝不降级）', ok, False, None, None, reason))

    sf, ok, reason = run_rollback(mk_states(phase=2), 'OP1')
    cases.append(('R4 phase 已推进到 2 → 拒绝', ok, False, None, None, reason))

    sf, ok, reason = run_rollback({SYM: {}}, 'OP1')
    cases.append(('R5 batch 消失 → 拒绝', ok, False, None, None, reason))

    print()
    print('=' * 68)
    print('ENTRY 逐 ID 验证（ChatGPT 终审 §三）')
    print('=' * 68)

    # E1 fetch_open_orders 返回 None → False（v3 会 or [] 假确认通过！）
    ok, sf = run_entry(IMPL, [None], [_order('canceled', 0.0)])
    cases.append(('E1 快照返回 None → False（Fail-Closed）', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    ok_v3, sf_v3 = run_entry(IMPL_V3, [None], [])
    cases.append(('E1-负向: v3 在 None 上返回 True（假确认复现）', ok_v3, True, None, None, ''))

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
    print(f'✅ {len(cases)}/{len(cases)} 全部通过（含负向对照）')
    return 0


IMPL = extract_impl(V4_PATH, WANT_FUNCS)


def extract_v3_entry_from_doc(doc_path):
    """v3 缺陷版 ENTRY 函数负向对照源。

    v4 文档重写后 v3 改动 2 的代码块已从送审文档移除，缺陷源码固化为档案
    G:/tmp/new_helpers_v3_entry.py（含头注说明），从那里 ast 提取。
    保留 doc_path 参数是为了将来档案再漂移时能扩展多源查找。"""
    src = open('G:/tmp/new_helpers_v3_entry.py', encoding='utf-8').read()
    ns = {'time': time, 'ccxt': ccxt}
    exec(compile(src, 'G:/tmp/new_helpers_v3_entry.py', 'exec'), ns)
    return ns['_cancel_and_verify_entry_orders']


IMPL_V3 = extract_impl(V3_PATH, ['_read_position_amt', '_fetch_close_order_state',
                                 '_confirm_close_filled'])
IMPL_V3['_cancel_and_verify_entry_orders'] = extract_v3_entry_from_doc(
    'G:/my-crypto-bot/事故_市价平仓-4061_精确diff_送审ChatGPT.md')
impl_rollback = IMPL['_rollback_close_request_if_current']

if __name__ == '__main__':
    sys.exit(main())
