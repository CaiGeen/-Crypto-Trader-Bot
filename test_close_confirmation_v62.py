# -*- coding: utf-8 -*-
"""test_close_confirmation_v62.py — v6.2 canonical 变体（fixture 按生产形态补齐） — v6 claimed snapshot 绑定 / 市价事务顺序 / ENTRY OrderNotFound 收紧

被测对象：送审附件_v6.1/new_helpers_v6.py（提议实现，ast 隔离提取，零 import 副作用）
文档被测：事故_市价平仓-4061_精确diff_送审ChatGPT.md「改动 1」的 AFTER 代码块
负向对照（同目录 送审附件_v6.1/，v6.1 起随项目版本化，不再依赖 G:/tmp）：
  - new_helpers_v5.py          —— v5 helper（BEGIN 无快照 / OrderNotFound=gone）
  - v5_after_market_close.py   —— v5 改动 1 AFTER 存档（撤 SL/TP 在 ENTRY gate 之前）
  - new_helpers_v4.py          —— v4 的 coverage 漏过 / TERMINAL_ZERO 过宽
  - new_helpers_v3.py          —— v3 的 not_filled 过宽
  - new_helpers_v60.py         —— v6.0 归档（v6.1 的负向对照基线）
  - new_helpers_v3_entry.py    —— v3 缺陷版 ENTRY 函数（含 `or []`）

对应 ChatGPT 对 v5 复审的 2 个事务边界 + 1 个语义收紧：
  边界1（§一） claim 与 transaction snapshot 绑定   → D0-D5 + D-neg
  边界2（§二） ENTRY gate 必须在撤 SL/TP 之前       → S1（静态 AST）+ S2（运行时序列）+ S1-v5/S2-v5 负向
  收紧 （§二） ENTRY OrderNotFound → unknown        → O1-O3 + O1-v5/O2-v5 负向

v5 的 52 场景全部保留（C/T/N/B/G/P/E 组）。

v6.1（ChatGPT 交叉审核 v6：3 P0 + 2 修正；第二轮：测试桩/checker 假绿）：
  R2-①  FakeSelf._persist_states 建模 -> bool 契约（persist_ok 参数），
         新增 B11/B12（persist=False → BEGIN/rollback 拒绝）+ v6.0 负向
  R2-②  D3c target_amounts_short / D3d side_invalid（+ v6.0 负向）
  R2-③  D9 完整集成链（监控更新→真 BEGIN→真 derive→rebind→文档 AFTER）
         + D9-neg 敏感性对照（漏 rebind → E3 漏撤必须被检出）
  R2-④  D6-keys：derive 返回 exactly 11 键（10 raw + 1 derived）
  R2-⑤  L 组 L1-L5：限价改动 1d AFTER 块事务级测试
  R2-⑥  S2e/S2f：gate 失败后落盘 close_reason == market_entry_unknown
  R2-⑦  D6c/D6d：有未成交层时 entry_orders 缺失/不足 → 拒绝（+ v6.0 负向）
  R2-⑧⑨ M 组：check_doc_helper_parity.py 主检查 + --self-test（mutation）

判据纪律：每个新场景同时给出 v6.1 期望与 v6.0 实测，断言必须能在回归时失败。
"""
import ast
import os
import subprocess
import sys
import time
import uuid
import textwrap
import threading

import ccxt

sys.stdout.reconfigure(encoding='utf-8')

# ── 路径解析（v6.1：helper 附件从 G:/tmp 移入项目内 `送审附件_v6.1/`） ──────────
# 为什么不能只用 __file__ 所在目录：run_mutation_checks_v61.py 会把本文件**复制**
# 到 G:/tmp/mut61_auto/ 下逐变异体运行，那时 __file__ 指向副本、而附件仍在原项目。
# 故留两个注入点（仅变异检查使用，常规运行不设）：
#   HELPER_PROJECT_DIR —— 原项目根目录（附件与送审稿都在那里）
#   V6_HELPER_OVERRIDE —— 该变异体改坏后的 helper，取代 ASSETS 里的正本
# 缺省时全部回落到「本文件所在目录」，即直接运行时的常规行为。
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.environ.get('HELPER_PROJECT_DIR') or HERE
ASSETS = os.path.join(PROJECT_DIR, '送审附件_v6.1')

V6_PATH = os.environ.get('V6_HELPER_OVERRIDE') or os.path.join(
    PROJECT_DIR, '送审附件_v6.2', 'new_helpers_v62.py')
V60_PATH = os.path.join(ASSETS, 'new_helpers_v60.py')   # v6.0 归档（v6.1 负向对照基线）
V5_PATH = os.path.join(ASSETS, 'new_helpers_v5.py')
V4_PATH = os.path.join(ASSETS, 'new_helpers_v4.py')
V3_PATH = os.path.join(ASSETS, 'new_helpers_v3.py')
V5_AFTER = os.path.join(ASSETS, 'v5_after_market_close.py')
V3_ENTRY = os.path.join(ASSETS, 'new_helpers_v3_entry.py')
DOC_PATH = os.path.join(PROJECT_DIR, '事故_市价平仓-4061_精确diff_送审ChatGPT.md')

WANT_FUNCS = ['_read_position_amt', '_fetch_close_order_state', '_confirm_close_filled',
              '_survey_same_side_batches', '_close_amount_guard',
              '_begin_close_request_if_active', '_derive_close_txn_vars',
              '_rollback_close_request_if_current', '_verify_entry_order_terminal',
              '_cancel_and_verify_entry_orders',
              '_set_close_reason_if_current']          # v6.1 第 11 个 helper

# v6.2 新增 helper（registry 恢复视图 / LIMIT durable commit）
WANT_FUNCS_V62 = WANT_FUNCS + ['_pending_entry_ids_for_gate',
                               '_commit_limit_close_order_if_current']
WANT_FUNCS_V60 = [f for f in WANT_FUNCS if f != '_set_close_reason_if_current']
WANT_FUNCS_V5 = [f for f in WANT_FUNCS_V60 if f != '_derive_close_txn_vars']

SYM = 'BTC/USDT:USDT'


def extract_impl(path, want=None):
    """从磁盘源码 ast 提取目标函数（隔离执行：零 import 副作用、零网络）。"""
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
    （避免失败原因被'序列耗尽异常'掩盖——v2 教训）。
    calls 记录每一次交易所调用，供 S2 的顺序断言使用。"""

    def __init__(self, pos_seq=None, order_seq=None, open_orders_seq=None):
        self.pos_seq = list(pos_seq if pos_seq is not None else [])
        self.order_seq = list(order_seq if order_seq is not None else [])
        self.oo_seq = list(open_orders_seq if open_orders_seq is not None else [])
        self._fallback = {'pos': self.pos_seq[-1] if self.pos_seq else [],
                          'order': self.order_seq[-1] if self.order_seq else None,
                          'oo': self.oo_seq[-1] if self.oo_seq else []}
        self.cancelled = []
        self.calls = []

    def _next(self, seq, key):
        v = seq.pop(0) if seq else self._fallback[key]
        if isinstance(v, Exception):
            raise v
        return v

    def fetch_positions(self, symbols=None):
        self.calls.append('fetch_positions')
        return self._next(self.pos_seq, 'pos')

    def fetch_order(self, order_id, symbol, params=None, retries=None):
        self.calls.append(f'fetch_order:{order_id}')
        return self._next(self.order_seq, 'order')

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append('fetch_open_orders')
        return self._next(self.oo_seq, 'oo')

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(f'cancel:{order_id}')
        self.cancelled.append(order_id)

    def create_order(self, symbol=None, type=None, side=None, amount=None,
                     price=None, params=None, retries=None):
        self.calls.append('create_order')
        return {'id': 'OID1', 'status': 'closed', 'filled': amount, 'average': 77000.0}


class FakeSelf:
    def __init__(self, exchange, states=None, fail_load=False, persist_ok=True):
        self.exchange = exchange
        self._state_lock = threading.Lock()
        self._states = states if states is not None else {}
        self.persisted = []
        self.saved = []
        self._fail_load = fail_load
        # 🔒 v6.1（R2-①）：建模生产 _persist_states -> bool 契约（L1340）。
        # 旧桩无 return（永远 None）恰好掩盖 P0-1 —— 必须显式返回。
        self.persist_ok = persist_ok
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
        return self.persist_ok   # 生产契约 -> bool；False = 写盘失败

    def save_batch_state(self, symbol, batch_id, data):
        self.saved.append((symbol, batch_id,
                           dict(data) if isinstance(data, dict) else data))

    def send_tg_notification(self, msg, level='info'):
        self.tg_sent.append((level, msg))


def _order(status, filled):
    return {'id': 'OID1', 'status': status, 'filled': filled, 'average': 77000.0}


def _order_nofilled(status):
    """filled 字段缺失（v5 §五：必须判 UNKNOWN，绝不给回滚资格）"""
    return {'id': 'OID1', 'status': status, 'average': 77000.0}


def _order_nullfilled(status):
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
    if len(r) == 2:      # v3 返回二元组
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


def run_begin(impl, states, batch_id='batch_A', reason='market_confirming',
              persist_ok=True):
    """v6 BEGIN 返回四元组；v5 返回三元组（snapshot=None）—— 这正是负向对照的基础。"""
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states, persist_ok=persist_ok)
    bind(sf, impl)
    r = impl['_begin_close_request_if_active'](sf, SYM, batch_id, reason)
    snap = r[3] if len(r) >= 4 else None
    return sf, r[0], r[1], r[2], snap


def run_rollback(impl, states, op_id, persist_ok=True):
    fx = FakeExchange()
    sf = FakeSelf(fx, states=states, persist_ok=persist_ok)
    bind(sf, impl)
    ok, reason = impl['_rollback_close_request_if_current'](sf, SYM, 'batch_A', op_id)
    return sf, ok, reason


def run_entry(impl, oo_seq, order_seq):
    """直接调用 ENTRY gate。

    ⚠️ 一律捕获异常：gate 内部一旦抛出（例如把 `remaining is None or not
    isinstance(...)` 改回 `or []` 后 `for o in None` 抛 TypeError），未捕获会
    炸穿整个套件、抹掉所有后续用例的结果；捕获后变成 `(False, '<EXC …>')`，
    由用例断言区分「干净拦截」与「异常兜底」。
    """
    fx = FakeExchange(open_orders_seq=oo_seq, order_seq=order_seq)
    sf = FakeSelf(fx)
    bind(sf, impl)
    b_data = {'entry_orders': ['E1', 'E2'], 'last_filled_count': 1}  # 只剩 E2 未成交
    try:
        ok = impl['_cancel_and_verify_entry_orders'](sf, SYM, 'batch_A', b_data, 1)
    except Exception as e:  # noqa: BLE001 - 目的就是把崩溃转成可断言的失败值
        # 返回**非空字符串**而非 False：gate 正常返回 bool，异常若也返回 False
        # 就与干净 Fail-Closed 无法区分；非空串对 `is False` / `== False` /
        # `not ok` 三种写法都判否，任何既有断言都会干净失败。
        ok = f'<EXC {type(e).__name__}: {e}>'
    return ok, sf


NO_RETURN = (None, '<NO_RETURN：AFTER 块未显式返回>')
"""哨兵：AFTER 块走完却未显式 return（gate 通过时本就应落后续生产段）。

不能直接留 None —— 下游 `ret[0]` 会抛 TypeError，一旦变异体改动控制流，
测试就崩溃而非干净失败，诊断信息全丢。
"""


def seq_index(calls, name):
    """调用序列中断言用下标；缺失返回 None（禁止裸 list.index）。

    理由：变异体一旦移除某个 API 调用，list.index 抛 ValueError → 测试崩溃，
    真实失败原因被 traceback 掩盖，只剩「rc=1」没有诊断信息。缺失必须表现为
    「断言失败」（下标 None → 比较不成立），而不是异常。
    """
    try:
        return list(calls).index(name)
    except ValueError:
        return None


def _mk_batch(phase=0, pending=False, side='BUY', amt=0.001, settled=False,
              filled_count=1, active=True):
    # v6.2：filled_details 必须与 target_amounts 等长（生产 L3250/L3311/L4563
    # 初始化即 [0.0] * len(entry_orders)），前缀成交价 + 尾段 exact 0。
    return {'side': side, 'close_phase': phase, 'pending_close': pending,
            'target_amounts': [amt], 'last_filled_count': filled_count,
            'filled_details': ([77000.0] * filled_count
                               + [0.0] * max(len([amt]) - filled_count, 0)),
            'settled_by_limit_close': settled, 'is_active': active}


# ══════════════ S 组：市价事务顺序 ══════════════

def extract_doc_after(doc_path=DOC_PATH):
    """从送审文档提取「改动 1」的 AFTER 代码块（被测对象是文档里的真实代码）。"""
    doc = open(doc_path, encoding='utf-8').read()
    i = doc.index('### 改动 1：`close_position_market`')
    j = doc.index('#### AFTER', i)
    k = doc.index('```python\n', j) + len('```python\n')
    m = doc.index('\n```', k)
    blk = doc[k:m]
    assert 'close_position_confirmed' in blk, 'AFTER 块定位错误'
    return blk


def static_order(block):
    """静态 AST 顺序断言：返回 (confirm_lines, gate_lines, cancel_lines)。"""
    tree = ast.parse(textwrap.dedent(block))
    confirm, gate, cancel = [], [], []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        if not isinstance(fn, ast.Attribute):
            continue
        if fn.attr == '_confirm_close_filled':
            confirm.append(n.lineno)
        elif fn.attr == '_cancel_and_verify_entry_orders':
            gate.append(n.lineno)
        elif fn.attr == '_safe_api_call' and n.args \
                and isinstance(n.args[0], ast.Attribute) \
                and n.args[0].attr == 'cancel_order':
            cancel.append(n.lineno)
    return confirm, gate, cancel


def static_ordered(block):
    con, gate, cancel = static_order(block)
    if not con or not gate or len(cancel) < 2:
        return False, f'调用点不足 (confirm={con} gate={gate} cancel={cancel})'
    ok = max(con) < min(gate) < min(cancel)
    return ok, f'confirm={con} gate={gate} cancel={cancel}'


def build_txn_fn(block):
    """把 AFTER 代码块包装成可执行函数（零 import 副作用）。

    块内引用的外部名字（target_symbol / batch_id / target_b_data / ...）
    通过 globals 注入，不改动被测代码本身。
    """
    body = textwrap.dedent(block)
    wrapped = 'def _txn(self):\n' + '\n'.join(
        ('    ' + ln if ln.strip() else '') for ln in body.split('\n'))
    return wrapped


def run_txn(impl, block, order_seq, oo_seq=None, pos_seq=None, tb=None,
            txn_vars=None, catch=False, sf=None, close_op_id='deadbeef' * 4,
            states=None, last_filled_count=1, current_filled_amount=0.001):
    """执行文档 AFTER 块。

    v6.1 变更：默认磁盘批次改为「BEGIN 已落盘」形态（close_phase=1 +
    pending_close + 匹配注入 close_op_id + close_reason='market_confirming'）
    —— _set_close_reason_if_current 的 CAS（op_id 匹配 + phase>=1）由此可
    真实生效，S2e/S2f 才能断言落盘 reason。guard/survey 不受影响
    （target 计入 sum_all、单批 others=0）。
    传入 sf 时复用调用方已构造/已 BEGIN 的 FakeSelf（D9 集成链）。
    """
    if sf is None:
        fx = FakeExchange(pos_seq=pos_seq if pos_seq is not None else
                          [POS_LONG_001, POS_LONG_001, []],
                          order_seq=order_seq,
                          open_orders_seq=oo_seq if oo_seq is not None else [[]])
        st = states if states is not None else {
            SYM: {'batch_A': dict(_mk_batch(), close_phase=1, pending_close=True,
                                  is_programmatic_cancel=True,
                                  close_op_id=close_op_id,
                                  close_reason='market_confirming')}}
        sf = FakeSelf(fx, states=st)
        bind(sf, impl)
    fx = sf.exchange
    tb = tb if tb is not None else {
        'entry_orders': ['E1', 'E2'], 'last_filled_count': 1,
        'tp_order_id': 'TP1', 'current_sl_id': 'SL1',
        'params_base': {}, 'is_hedge_mode': True,
        'target_amounts': [0.001, 0.001], 'filled_details': [77000.0, 0.0],
        'total_entry_fee': 0.01, 'side': 'BUY'}
    # v6：`_txn_vars` 由 claimed 快照派生（默认与 tb 同源 = 正常情形）。
    # v5：根本没有 _derive_close_txn_vars → txn_vars=None（v5 只读 target_b_data）。
    if txn_vars is None:
        _dv = impl.get('_derive_close_txn_vars')
        if _dv is not None:
            _ok, _v, _why = _dv(FakeSelf(FakeExchange()), tb, 'batch_A')
            txn_vars = _v if _ok else None
    ns = {'time': time, 'ccxt': ccxt, 'uuid': uuid,
          'target_symbol': SYM, 'batch_id': 'batch_A',
          'close_op_id': close_op_id,
          'target_b_data': tb, '_txn_vars': txn_vars,
          'last_filled_count': last_filled_count,
          'current_filled_amount': current_filled_amount, 'side': 'BUY'}
    exec(compile(build_txn_fn(block), '<after>', 'exec'), ns)
    try:
        ret = ns['_txn'](sf)
    except Exception as e:  # noqa: BLE001 - 只用于「调用序列已发生」类断言
        if not catch:
            raise
        ret = (False, f'<EXC {type(e).__name__}: {e}>')
    # ⚠️ 断言纪律：AFTER 块若未显式返回（变异体改动控制流时会发生），下游
    # `ret[0]` 抛 TypeError → 测试崩溃、诊断信息全丢。必须在源头兜成干净的
    # 断言失败值，让「行为变了」表现为 ❌ 而不是 traceback。
    if ret is None:
        ret = NO_RETURN
    return ret, fx, sf


def extract_doc_after_limit(doc_path=DOC_PATH):
    """从送审文档提取「改动 1d」的 AFTER 代码块（限价 ENTRY gate，v6.1 P0-3）。"""
    doc = open(doc_path, encoding='utf-8').read()
    i = doc.index('#### 改动 1d')
    j = doc.index('#### AFTER', i)
    k = doc.index('```python\n', j) + len('```python\n')
    m = doc.index('\n```', k)
    blk = doc[k:m]
    assert '_cancel_and_verify_entry_orders' in blk, '1d AFTER 块定位错误'
    assert 'limit_entry_unknown' in blk, '1d AFTER 块缺异常 reason 分支'
    return blk


def static_tpsl_source(block):
    """静态断言：撤 TP/SL 时 order id 的**取值来源**。

    返回 [(对象名, 键名), ...]，例如 v6 应为
    [('_txn_vars', 'tp_order_id'), ('_txn_vars', 'current_sl_id')]，
    而 v5 为 [('target_b_data', ...), ...]。
    只收 Subscript 形式（ENTRY 撤单用的是 Name `order_id`，不在此列）。
    """
    tree = ast.parse(textwrap.dedent(block))
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        if not isinstance(fn, ast.Attribute) or fn.attr != '_safe_api_call':
            continue
        if len(n.args) < 2:
            continue
        a0 = n.args[0]
        if not (isinstance(a0, ast.Attribute) and a0.attr == 'cancel_order'):
            continue
        a1 = n.args[1]
        if isinstance(a1, ast.Subscript) and isinstance(a1.value, ast.Name):
            key = a1.slice
            kn = key.value if isinstance(key, ast.Constant) else '<dynamic>'
            out.append((a1.value.id, kn))
    return out


def main():
    print('=' * 68)
    print('一、六态确认器 + TERMINAL_ZERO 收紧（ChatGPT 终审 §五，v6 沿用）')
    print('=' * 68)

    v, d, f = run_confirm(IMPL, [_order('closed', 0.001)], pos_seq=[POS_LONG_001, []])
    cases.append(('C1 closed+filled达标 → CONFIRMED_FULL', v, 'CONFIRMED_FULL', f, 0.001, d))

    v, d, f = run_confirm(IMPL, [_order('open', 0)] * 3)
    cases.append(('C2 open×3 → PENDING（不回滚）', v, 'PENDING', f, None, d))

    v, d, f = run_confirm(IMPL, [_order('closed', 0.0005)])
    cases.append(('C3 partial filled=0.0005 → PARTIAL（不回滚）', v, 'PARTIAL', f, 0.0005, d))

    v, d, f = run_confirm(IMPL, [_order('canceled', 0.0)])
    cases.append(('C4 canceled+权威filled=0 → TERMINAL_ZERO（可回滚）', v,
                  'TERMINAL_ZERO', f, 0.0, d))

    v, d, f = run_confirm(IMPL, [ccxt.OrderNotFound('nf')] * 3, attempts=1)
    cases.append(('C6 create有ID但fetch不到 → NOT_CONFIRMED（不回滚）', v,
                  'NOT_CONFIRMED', f, None, d))

    v, d, f = run_confirm(IMPL, [RuntimeError('net')] * 3)
    cases.append(('C7 查询异常×3 → UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order('closed', 0.0)])
    cases.append(('T1 closed+filled=0（矛盾组合）→ UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order_nofilled('canceled')])
    cases.append(('T2 canceled 但 filled 字段缺失 → UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order_nullfilled('expired')])
    cases.append(('T3 expired 但 filled=None → UNKNOWN', v, 'UNKNOWN', f, None, d))

    v, d, f = run_confirm(IMPL, [_order('rejected', 0.0)])
    cases.append(('T4 rejected+权威filled=0 → TERMINAL_ZERO', v, 'TERMINAL_ZERO', f, 0.0, d))

    v4, d4, _ = run_confirm(IMPL_V4, [_order('closed', 0.0)])
    cases.append(('T1-v4 负向: v4 判 TERMINAL_ZERO（错误给回滚资格）', v4,
                  'TERMINAL_ZERO', None, None, d4))
    v4, d4, _ = run_confirm(IMPL_V4, [_order_nofilled('canceled')])
    cases.append(('T2-v4 负向: v4 判 TERMINAL_ZERO（filled缺失被当成0）', v4,
                  'TERMINAL_ZERO', None, None, d4))

    for tag, seq in [('open×3', [_order('open', 0)] * 3),
                     ('partial', [_order('closed', 0.0005)]),
                     ('not_found', [ccxt.OrderNotFound('nf')] * 3)]:
        r = run_confirm(IMPL_V3, seq, attempts=1 if 'not' in tag else 3)
        cases.append((f'N-{tag}: v3 判 {r[0]}（应为 not_filled，证明 v3 会错误回滚）',
                      r[0], 'not_filled', None, None, ''))

    print()
    print('=' * 68)
    print('二、atomic BEGIN/claim（§二 + §三 + §六）')
    print('=' * 68)

    st = {SYM: {'batch_A': _mk_batch()}}
    sf, ok, op_id, why, snap = run_begin(IMPL, st)
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

    sf2, ok2, op2, why2, snap2 = run_begin(IMPL, sf._states)
    cases.append(('B2 二次 claim（phase=1）→ 拒绝', ok2, False, None, None, why2))
    cases.append(('B2b 被拒绝时不落盘', len(sf2.persisted), 0, None, None, ''))
    cases.append(('B2c 被拒绝时不返回 op_id（无权下单）', op2, '', None, None, ''))

    st3 = {SYM: {'batch_A': _mk_batch(),
                 'batch_B': _mk_batch(phase=1, pending=True)}}
    sf3, ok3, op3, why3, _ = run_begin(IMPL, st3)
    cases.append(('B3 同方向有在途平仓 → 拒绝（单飞）', ok3, False, None, None, why3))
    cases.append(('B3b 拒绝原因含 same_side_close_inflight',
                  'same_side_close_inflight' in why3, True, None, None, why3))
    cases.append(('B3c 拒绝时未修改任何批次状态',
                  sf3._states[SYM]['batch_A'].get('close_phase'), 0, None, None, ''))

    st4 = {SYM: {'batch_A': _mk_batch(side='BUY'),
                 'batch_B': _mk_batch(phase=1, side='SELL')}}
    sf4, ok4, op4, why4, _ = run_begin(IMPL, st4)
    cases.append(('B4 反方向有在途平仓 → 不拦截', ok4, True, None, None, why4))

    sf5, ok5, op5, why5, _ = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}}, reason='')
    cases.append(('B5 缺 close_reason → 拒绝', ok5, False, None, None, why5))

    st6 = {SYM: {'batch_A': _mk_batch(settled=True)}}
    sf6, ok6, op6, why6, _ = run_begin(IMPL, st6)
    cases.append(('B6 settled 事实在 → 拒绝', ok6, False, None, None, why6))

    sf7, ok7, op7, why7, _ = run_begin(IMPL, {SYM: {}}, batch_id='batch_X')
    cases.append(('B7 batch 不存在 → 拒绝', ok7, False, None, None, why7))

    op_ids = []
    for _ in range(5):
        _s = {SYM: {'batch_A': _mk_batch()}}
        _, _ok, _op, _, _ = run_begin(IMPL, _s)
        op_ids.append(_op)
    cases.append(('B8 5 次独立 claim 的 op_id 互不相同', len(set(op_ids)), 5, None, None,
                  f'{op_ids[0][:8]}…'))

    st9 = {SYM: {'batch_A': _mk_batch()}}
    sf9, ok9, op9, _, _ = run_begin(IMPL, st9)
    sfrb, okrb, whyrb = run_rollback(IMPL, sf9._states, op9)
    cases.append(('B9 BEGIN→CAS回滚成功（op_id 匹配）', (ok9, okrb), (True, True),
                  None, None, whyrb))

    sf10, ok10, op10, _, _ = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}})
    _, ok_fake, why_fake = run_rollback(IMPL, sf10._states, 'deadbeef' * 4)
    cases.append(('B10 假冒 op_id 无法回滚 → 拒绝', ok_fake, False, None, None, why_fake))

    # ── B11/B12（v6.1 P0-1）：persist 契约纳入事务原子性 ───────────────
    # 生产 _persist_states -> bool（L1340：账本损坏/写盘异常主动 False）。
    # 「写盘成功」必须是「claim 成功」的一部分，否则锁内 claim 成功但磁盘
    # 仍 phase=0 → 第二个线程重读再 claim → 双 MARKET。
    sf11, ok11, op11, why11, _ = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}},
                                           persist_ok=False)
    cases.append(('B11 写盘失败 → BEGIN 拒绝（不写盘=未取得所有权）', ok11, False,
                  None, None, why11))
    cases.append(('B11b 写盘失败时不返回 op_id（无权下单）', op11, '', None, None, ''))
    cases.append(('B11c 原因含 claim_persist_failed',
                  'claim_persist_failed' in (why11 or ''), True, None, None, why11))
    _, ok11v, op11v, why11v, _ = run_begin(IMPL_V60, {SYM: {'batch_A': _mk_batch()}},
                                           persist_ok=False)
    cases.append(('B11-v60 负向: v6.0 忽略写盘失败仍发 op_id（双 MARKET 窗口实证）',
                  (ok11v, op11v != ''), (True, True), None, None, why11v))

    sf12, ok12, op12, _, _ = run_begin(IMPL, {SYM: {'batch_A': _mk_batch()}})
    _, ok12r, why12r = run_rollback(IMPL, sf12._states, op12, persist_ok=False)
    cases.append(('B12 回滚写盘失败 → 绝不报告已回滚', ok12r, False, None, None, why12r))
    cases.append(('B12b 原因含 rollback_persist_failed（磁盘仍 phase=1，如实上报）',
                  'rollback_persist_failed' in (why12r or ''), True, None, None, why12r))
    # v6.1 rollback 在 persist 失败前已就地把内存副本改回 phase=0（FakeSelf
    # load_all_states 返回同一对象）→ v6.0 负向必须用独立状态，否则被
    # phase_changed 拦截、掩盖了「谎称 rolled_back」这个真正要证的缺陷。
    st12v = {SYM: {'batch_A': dict(_mk_batch(), close_phase=1, pending_close=True,
                                   is_programmatic_cancel=True,
                                   close_op_id='op_b12',
                                   close_reason='market_confirming')}}
    _, ok12v, why12v = run_rollback(IMPL_V60, st12v, 'op_b12', persist_ok=False)
    cases.append(('B12-v60 负向: v6.0 写盘失败仍谎称 rolled_back（TG 假恢复）',
                  (ok12v, why12v), (True, 'rolled_back'), None, None, ''))

    # ── B13（C 路存活变异体）：_set_close_reason_if_current 的 persist 分支 ──
    # P0-1 同型：删掉该 helper 的 persist 检查，126 场景曾全绿（零覆盖）。
    # 本用例把它锁死：写盘失败必须 ok=False，绝不谎称 reason 已切换
    # （否则 TG 谎称监控恢复，磁盘仍停在 market_confirming → fail-silent）。
    st13 = {SYM: {'batch_A': dict(_mk_batch(), close_phase=1, pending_close=True,
                                  is_programmatic_cancel=True,
                                  close_op_id='op_b13',
                                  close_reason='market_confirming')}}
    fx13 = FakeExchange()
    sf13 = FakeSelf(fx13, states=st13, persist_ok=False)
    bind(sf13, IMPL)
    ok13, why13 = IMPL['_set_close_reason_if_current'](
        sf13, SYM, 'batch_A', 'op_b13', 'market_entry_unknown')
    cases.append(('B13 reason 切换写盘失败 → ok=False（绝不谎称已切换）', ok13, False,
                  None, None, why13))
    cases.append(('B13b 原因含 persist_failed',
                  'persist_failed' in (why13 or ''), True, None, None, why13))

    print()
    print('=' * 68)
    print('三、🆕 D 组：claim 与 transaction snapshot 绑定（v6 §一）')
    print('=' * 68)

    # D0：v5 根本没有这个能力（负向对照成立的前提）
    v5_src = open(V5_PATH, encoding='utf-8').read()
    cases.append(('D0 v5 无 _derive_close_txn_vars（能力缺失，负向前提）',
                  '_derive_close_txn_vars' in v5_src, False, None, None, ''))

    # D1：正常派生（last_filled_count=2 → 0.002）
    snap1 = {'last_filled_count': 2, 'target_amounts': [0.001, 0.001],
             'filled_details': [77000.0, 77100.0], 'total_entry_fee': 0.02,
             'side': 'BUY', 'params_base': {'x': 1}, 'is_hedge_mode': True}
    ok_d1, vars1, why1 = IMPL['_derive_close_txn_vars'](FakeSelf(FakeExchange()), snap1, 'batch_A')
    cases.append(('D1 derive 正常 → ok=True', ok_d1, True, None, None, why1))
    cases.append(('D1b current_filled_amount=0.002（两层）',
                  round(vars1['current_filled_amount'], 6), 0.002, None, None, ''))
    cases.append(('D1c last_filled_count=2', vars1['last_filled_count'], 2, None, None, ''))
    cases.append(('D1d side/params_base/is_hedge_mode 同源',
                  (vars1['side'], vars1['params_base'], vars1['is_hedge_mode']),
                  ('BUY', {'x': 1}, True), None, None, ''))

    # D2：BEGIN 返回的 snapshot 反映磁盘最新（监控线程已更新为 2）
    disk2 = {SYM: {'batch_A': dict(snap1, is_active=True)}}
    sf_d2, ok_d2, _, _, snap_d2 = run_begin(IMPL, disk2)
    cases.append(('D2 BEGIN 返回的 snapshot 是磁盘最新',
                  (ok_d2, snap_d2.get('last_filled_count')), (True, 2), None, None, ''))
    cases.append(('D2b snapshot 含 BEGIN 写入的 close_phase=1',
                  snap_d2.get('close_phase'), 1, None, None, ''))
    ok_d2b, vars2b, _ = IMPL['_derive_close_txn_vars'](sf_d2, snap_d2, 'batch_A')
    cases.append(('D2c claimed 快照派生出 0.002',
                  round(vars2b['current_filled_amount'], 6), 0.002, None, None, ''))

    # D3：filled_details 比 last_filled_count 短 → 拒绝（防 IndexError）
    snap3 = dict(snap1, filled_details=[77000.0])
    ok_d3, vars3, why3d = IMPL['_derive_close_txn_vars'](FakeSelf(FakeExchange()), snap3, 'batch_A')
    cases.append(('D3 filled_details 缺 1 层 → ok=False', ok_d3, False, None, None, why3d))
    cases.append(('D3b 原因含 filled_details_short',
                  'filled_details_short' in (why3d or ''), True, None, None, why3d))

    # ── D3c/D3d（v6.1 P0-2）：对称长度校验 + side 严格校验 ─────────────
    # Python 切片不因长度不足报错：last_filled_count=2 配 [0.001] 静默派生
    # 0.001 → 少平 → 按单确认自己下的量必然通过 → ENTRY gate 假通过 →
    # 撤保护 → 残留裸仓。必须 Fail-Closed。
    snap3c = {'last_filled_count': 2, 'target_amounts': [0.001],
              'filled_details': [77000.0, 77100.0], 'total_entry_fee': 0.02,
              'side': 'BUY'}
    ok_d3c, _, why3c = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap3c, 'batch_A')
    cases.append(('D3c target_amounts 短于已成交层 → ok=False', ok_d3c, False,
                  None, None, why3c))
    cases.append(('D3cb 原因含 target_amounts_short',
                  'target_amounts_short' in (why3c or ''), True, None, None, why3c))
    ok_d3cv, vars3cv, _ = IMPL_V60['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap3c, 'batch_A')
    cases.append(('D3c-v60 负向: v6.0 静默派生 0.001（少平一半，gate 假通过链实证）',
                  (ok_d3cv, round(vars3cv['current_filled_amount'], 6)
                   if vars3cv else None), (True, 0.001),  # C-2：归档污染时记 ❌ 而非崩溃
                  None, None, '切片不报错，只少加'))

    snap3d = dict(snap1, side='HOLD')
    ok_d3d, _, why3dd = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap3d, 'batch_A')
    cases.append(('D3d side 非法值 → ok=False（反向开仓风险）', ok_d3d, False,
                  None, None, why3dd))
    cases.append(('D3db 原因含 side_invalid',
                  'side_invalid' in (why3dd or ''), True, None, None, why3dd))
    ok_d3dv, vars3dv, _ = IMPL_V60['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap3d, 'batch_A')
    cases.append(('D3d-v60 负向: v6.0 原样放行非法 side（零校验）',
                  (ok_d3dv, vars3dv['side'] if vars3dv else None), (True, 'HOLD'),
                  None, None, ''))

    # D4：无需平仓
    snap4 = dict(snap1, last_filled_count=0)
    ok_d4, _, why4d = IMPL['_derive_close_txn_vars'](FakeSelf(FakeExchange()), snap4, 'batch_A')
    cases.append(('D4 last_filled_count=0 → no_filled_amount', why4d,
                  'no_filled_amount（claimed 快照显示无需平仓）', None, None, ''))

    # D5：账本类型损坏
    snap5 = dict(snap1, target_amounts=['bad', 'worse'])
    ok_d5, _, why5d = IMPL['_derive_close_txn_vars'](FakeSelf(FakeExchange()), snap5, 'batch_A')
    cases.append(('D5 target_amounts 非数值 → ok=False', ok_d5, False, None, None, why5d))
    cases.append(('D5b 原因含 ledger_broken',
                  'ledger_broken' in (why5d or ''), True, None, None, why5d))

    # 🔴 D-neg：v5 调用范式复现（入口读旧快照 → BEGIN → 沿用旧变量）
    entry_snapshot = {'last_filled_count': 1, 'target_amounts': [0.001, 0.001],
                      'filled_details': [77000.0, 0.0], 'side': 'BUY',
                      'total_entry_fee': 0.01, 'is_active': True}
    # 监控线程在 BEGIN 之前把 last_filled_count 更新为 2 并落盘
    disk_neg = {SYM: {'batch_A': dict(entry_snapshot, last_filled_count=2,
                                      filled_details=[77000.0, 77100.0])}}
    sf_neg, ok_neg, _, _, snap_neg = run_begin(IMPL, disk_neg)
    v5_amount = sum(entry_snapshot['target_amounts'][:entry_snapshot['last_filled_count']])
    _, vars_neg, _ = IMPL['_derive_close_txn_vars'](sf_neg, snap_neg, 'batch_A')
    v6_amount = vars_neg['current_filled_amount']
    cases.append(('D-neg 负向: v5 范式沿用旧快照 → 只平 0.001（少平 0.001）',
                  round(v5_amount, 6), 0.001, None, None, 'BEGIN 前的入口变量'))
    cases.append(('D-neg v6 用 claimed 快照 → 平 0.002（正确）',
                  round(v6_amount, 6), 0.002, None, None, 'BEGIN 返回的快照'))

    # ── D6/D7/D8：自查补齐的 3 个字段（entry_orders / tp_order_id / current_sl_id）──
    # 监控线程那次落盘是一整个 update 块（生产 L6231-6254），一次性写 8 个字段；
    # 这 3 个不参与「算平多少」而参与「撤哪些单」，漏掉 → 孤儿保护单。
    snap6 = dict(snap1, entry_orders=['E1', 'E2', 'E3'],
                 tp_order_id='TP2', current_sl_id='SL2')
    ok_d6, vars6, why_d6 = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6, 'batch_A')
    cases.append(('D6 derive 含 3 个「撤哪些单」字段（v6 契约完整）',
                  (ok_d6,
                   vars6.get('entry_orders'),
                   vars6.get('tp_order_id'),
                   vars6.get('current_sl_id')),
                  (True, ['E1', 'E2', 'E3'], 'TP2', 'SL2'), None, None, why_d6))

    # D6-keys（v6.1 R2-④）：exactly 11 键（10 raw + 1 derived），锁 exact set
    WANT_KEYS = {'last_filled_count', 'target_amounts', 'current_filled_amount',
                 'filled_details', 'total_entry_fee', 'side', 'params_base',
                 'is_hedge_mode', 'entry_orders', 'tp_order_id', 'current_sl_id'}
    cases.append(('D6-keys derive 返回 exactly 11 键（10 raw + 1 derived）',
                  set(vars6.keys()), WANT_KEYS, None, None, f'{sorted(vars6)}'))

    ok_d6b, vars6b, _ = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap1, 'batch_A')  # 无这三个字段的快照
    cases.append(('D6b 缺保护单字段（且无未成交层）→ 不报错，归零值（ok 仍为 True）',
                  (ok_d6b, vars6b.get('tp_order_id'), vars6b.get('current_sl_id'),
                   vars6b.get('entry_orders')),
                  (True, None, None, []), None, None,
                  '安全情形：last_filled=2 == len(target)=2，无单可撤'))

    # ── D6c/D6d（v6.1 R2-⑦ D6b 收窄）：有未成交计划层时缺失/不足 → 拒绝 ──
    # 否则 missing → [] → pending_ids=[] → ENTRY gate 恒 True（UNKNOWN→EMPTY 同型）。
    snap6c = {'last_filled_count': 1, 'target_amounts': [0.001, 0.001],
              'filled_details': [77000.0, 0.0], 'total_entry_fee': 0.01, 'side': 'BUY'}
    ok_d6c, _, why6c = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6c, 'batch_A')
    cases.append(('D6c 有未成交层但 entry_orders 缺失 → ok=False', ok_d6c, False,
                  None, None, why6c))
    cases.append(('D6cb 原因含 entry_orders_missing',
                  'entry_orders_missing' in (why6c or ''), True, None, None, why6c))
    ok_d6cv, vars6cv, _ = IMPL_V60['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6c, 'batch_A')
    cases.append(('D6c-v60 负向: v6.0 缺失归零 [] → pending_ids=[] → gate 恒 True',
                  (ok_d6cv, vars6cv['entry_orders'] if vars6cv else None), (True, []),
                  None, None, 'UNKNOWN→EMPTY 同型'))

    # D6d（自审 F-1 后调整为「部分截断」区间：last < len(_eo) < len(target)，
    # 无生产路径产生 = 可疑中间态，必须拦）
    snap6d = dict(snap6c, target_amounts=[0.001, 0.001, 0.001],
                  entry_orders=['E1', 'E2'],
                  filled_details=[77000.0, 0.0, 0.0])
    ok_d6d, _, why6dd = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6d, 'batch_A')
    cases.append(('D6d entry_orders 部分截断（last<len<target）→ ok=False', ok_d6d, False,
                  None, None, why6dd))
    cases.append(('D6db 原因含 entry_orders_short',
                  'entry_orders_short' in (why6dd or ''), True, None, None, why6dd))
    ok_d6dv, vars6dv, _ = IMPL_V60['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6d, 'batch_A')
    cases.append(('D6d-v60 负向: v6.0 放行（E3 层无 ID 可撤也不拦）',
                  (ok_d6dv, vars6dv['entry_orders'] if vars6dv else None),
                  (True, ['E1', 'E2']), None, None, ''))

    # ── D6e/D6f（送审前交叉自审 F-1）：🗑️ 精确截断签名必须放行 ─────────
    # 生产 cancel_open_orders（L6896-6897）只截断 entry_orders 到
    # last_filled_count、不动 target_amounts → len(_eo)==last_filled_count
    # 是生产自己创造的合法状态（未成交层已被有意移除，pending_ids 恒空，
    # gate 无单可撤自然通过）。v6.1 初版把它永久挡死 = 新引入回归。
    snap6e = {'last_filled_count': 2, 'target_amounts': [0.001] * 5,
              'filled_details': [77000.0, 77100.0, 0.0, 0.0, 0.0],
              'total_entry_fee': 0.02,
              'side': 'BUY', 'entry_orders': ['E1', 'E2']}  # 🗑️ 截断后形态
    ok_d6e, vars6e, why6e = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6e, 'batch_A')
    cases.append(('D6e 🗑️ 截断签名（len==last==2 < target 5 层）→ 放行',
                  (ok_d6e, round(vars6e['current_filled_amount'], 6)
                   if vars6e else None, vars6e['entry_orders'] if vars6e else None),
                  (True, 0.002, ['E1', 'E2']), None, None,
                  f'F-1 回归锁：生产 L6896-6897 合法状态 why={why6e}'))
    # 已成交层 ID 都丢失（len < last_filled_count）= 账本损坏，与 🗑️ 签名
    # 形似但性质不同，维持 Fail-Closed
    snap6f = dict(snap6e, target_amounts=[0.001] * 3, entry_orders=['E1'],
                  filled_details=[77000.0, 77100.0, 0.0])
    ok_d6f, _, why6f = IMPL['_derive_close_txn_vars'](
        FakeSelf(FakeExchange()), snap6f, 'batch_A')
    cases.append(('D6f 已成交层 ID 丢失（len 1 < last 2）→ 仍拦',
                  (ok_d6f, 'entry_orders_short' in (why6f or '')),
                  (False, True), None, None, why6f))

    # ── D6g（B 路 F-1 处置要求）：🗑️ 批次必须能走完 BEGIN → derive → gate ──
    # 截断形态批次（5 层计划、成交 2 层、🗑️ 撤掉 3 层未成交挂单）：
    # BEGIN claim → derive 放行 → gate 从 registry 恢复出被截断的 E3/E4/E5
    # → 撤销并逐 ID 验证终态（**v6.2 改动 8 语义**：不再是 pending_ids 恒空 /
    #   零撤单；v6.1 的「恒空」假设会让被截断但仍活着的 ENTRY 成为漏网之鱼）。
    _reg6g = {f'batch_A:ENTRY:{i}:LONG': {'role': 'ENTRY', 'state': 'CONFIRMED',
                                          'layer': i, 'side': 'LONG',
                                          'order_id': f'E{i + 1}', 'id_known': True,
                                          'order_kind': 'conditional',
                                          'updated_at': 0.0}
              for i in range(5)}   # 5 层计划全在 registry（含被 🗑️ 截断的 E3/E4/E5）
    st6g = {SYM: {'batch_A': dict(_mk_batch(), target_amounts=[0.001] * 5,
                                  last_filled_count=2,
                                  filled_details=[77000.0, 77100.0, 0.0, 0.0, 0.0],
                                  total_entry_fee=0.02,
                                  entry_orders=['E1', 'E2'],
                                  protection_registry=_reg6g)}}
    sf6g, ok6g, op6g, why6g, claimed6g = run_begin(IMPL, st6g)
    # v6.2 改动 8：gate 会撤掉 registry 恢复出的被截断层并逐 ID 验证终态
    sf6g.exchange.order_seq = [_order('canceled', 0.0)] * 9
    sf6g.exchange._last_order = _order('canceled', 0.0)
    ok6g2, vars6g, why6g2 = IMPL['_derive_close_txn_vars'](sf6g, claimed6g, 'batch_A') \
        if ok6g else (False, None, f'BEGIN 失败: {why6g}')
    gate6g = IMPL['_cancel_and_verify_entry_orders'](
        sf6g, SYM, 'batch_A', claimed6g, vars6g['last_filled_count']) \
        if ok6g2 else None
    cancels6g = [c for c in sf6g.exchange.calls if str(c).startswith('cancel')]
    cases.append(('D6g 🗑️ 批次完整链 BEGIN→derive→gate 全通过',
                  (ok6g, ok6g2, gate6g), (True, True, True), None, None,
                  f'BEGIN={why6g} derive={why6g2}'))
    # ⚠️ 断言纪律（变异检查器 M7-f1-wide 抓到的装饰品断言）：
    # 只断言 cancels == [] 是**假绿**——上游 derive 失败时 gate 被跳过，
    # 撤单数同样是 0，缺陷版反而通过。必须把「链已成功」作为前置条件一起断言。
    cases.append(('D6gb 链成功后 gate 撤掉 registry 恢复出的被截断层'
                  '（v6.2 改动 8 语义再基线：不再是零撤单；含前置条件防假绿）',
                  (ok6g2, gate6g, sorted(cancels6g)),
                  (True, True, ['cancel:E3', 'cancel:E4', 'cancel:E5']), None, None,
                  f'calls={sf6g.exchange.calls}'))

    # D7 静态：v6 AFTER 撤 TP/SL 的 id 必须取自 _txn_vars（= claimed 快照）
    after_v6_for_d7 = extract_doc_after()
    src_v6 = static_tpsl_source(after_v6_for_d7)
    cases.append(('D7 静态: v6 撤 TP/SL 的 id 取自 _txn_vars', src_v6,
                  [('_txn_vars', 'tp_order_id'), ('_txn_vars', 'current_sl_id')],
                  None, None, f'src={src_v6}'))

    after_v5_for_d7 = open(V5_AFTER, encoding='utf-8').read()
    src_v5_d7 = static_tpsl_source(after_v5_for_d7)
    cases.append(('D7-v5 负向: v5 撤 TP/SL 的 id 取自 target_b_data', src_v5_d7,
                  [('target_b_data', 'tp_order_id'), ('target_b_data', 'current_sl_id')],
                  None, None, f'src={src_v5_d7}'))

    # ── D8 运行时：stale vs claimed ────────────────────────────────
    # 场景重现：/close 入口读 current_sl_id=SL1；监控线程滚动止损移 SL → SL2 并落盘；
    # BEGIN claim 到 SL2。v6 撤 SL2（正确）；v5 撤 SL1（早已被监控撤掉 → 静默吞掉
    # → SL2 成为孤儿单，clear_batch_state 后永久无主）。
    TB_STALE = {'entry_orders': ['E1', 'E2'], 'last_filled_count': 1,
                'tp_order_id': 'TP1', 'current_sl_id': 'SL1',   # BEGIN 之前的入口快照
                'params_base': {}, 'is_hedge_mode': True,
                'target_amounts': [0.001, 0.001], 'filled_details': [77000.0, 0.0],
                'total_entry_fee': 0.01, 'side': 'BUY'}
    TXN_CLAIMED = {'entry_orders': ['E1', 'E2'], 'last_filled_count': 1,
                   'current_filled_amount': 0.001, 'filled_details': [77000.0],
                   'total_entry_fee': 0.01, 'side': 'BUY',
                   'params_base': {}, 'is_hedge_mode': True,
                   'tp_order_id': 'TP2', 'current_sl_id': 'SL2'}  # ← 监控已移 SL
    # gate 必须**通过**（E2 = canceled+0）才能走到撤 TP/SL
    SEQ_GATE_OK = [_order('closed', 0.001), _order('canceled', 0.0)]

    _, fx_d8, _ = run_txn(IMPL, after_v6_for_d7, order_seq=SEQ_GATE_OK, oo_seq=[[]],
                          tb=TB_STALE, txn_vars=TXN_CLAIMED, catch=True)
    c_d8 = fx_d8.calls
    cases.append(('D8 运行时: v6 撤的是 claimed 的 SL2（不是 stale 的 SL1）',
                  ('cancel:SL2' in c_d8, 'cancel:SL1' in c_d8), (True, False),
                  None, None, f'calls={c_d8}'))
    cases.append(('D8b 运行时: v6 撤的是 claimed 的 TP2',
                  ('cancel:TP2' in c_d8, 'cancel:TP1' in c_d8), (True, False),
                  None, None, ''))

    _, fx_d8v5, _ = run_txn(IMPL_V5, after_v5_for_d7, order_seq=SEQ_GATE_OK, oo_seq=[[]],
                            tb=TB_STALE, catch=True)
    c_d8v5 = fx_d8v5.calls
    cases.append(('D8-v5 负向: v5 撤 stale 的 SL1/TP1，漏掉 SL2（孤儿单）',
                  ('cancel:SL1' in c_d8v5, 'cancel:TP1' in c_d8v5,
                   'cancel:SL2' in c_d8v5), (True, True, False),
                  None, None, f'calls={c_d8v5}'))

    # ── D9 完整集成链（v6.1，R2-③）：stale 磁盘 → 监控更新落盘 → 真 BEGIN →
    # 真 derive → rebind → 跑文档 AFTER。D8 是人工注入两个独立对象，只证明
    # 使用点用了 _txn_vars；D9 证明「target_b_data = _claimed」在完整链路上
    # 真的生效 —— ENTRY gate 读的是 target_b_data，rebind 漏掉则 E3 漏撤。
    STALE9 = {'side': 'BUY', 'close_phase': 0, 'pending_close': False,
              'is_active': True, 'settled_by_limit_close': False,
              'entry_orders': ['E1', 'E2'], 'last_filled_count': 1,
              'tp_order_id': 'TP1', 'current_sl_id': 'SL1',
              'params_base': {}, 'is_hedge_mode': True,
              'target_amounts': [0.001, 0.001], 'filled_details': [77000.0, 0.0],
              'total_entry_fee': 0.01}
    fx_d9 = FakeExchange(pos_seq=[POS_LONG_001, POS_LONG_001, []],
                         order_seq=[_order('closed', 0.001),   # OID1 平仓单成交
                                    _order('canceled', 0.0),   # E2 终态
                                    _order('canceled', 0.0)],  # E3 终态
                         open_orders_seq=[[]])
    sf_d9 = FakeSelf(fx_d9, states={SYM: {'batch_A': dict(STALE9)}})
    bind(sf_d9, IMPL)
    # 监控线程在 BEGIN 之前落盘：新层 E3 已挂出（未成交）、SL1→SL2、TP1→TP2
    sf_d9._states[SYM]['batch_A'].update(
        entry_orders=['E1', 'E2', 'E3'], tp_order_id='TP2', current_sl_id='SL2',
        target_amounts=[0.001, 0.001, 0.001],
        filled_details=[77000.0, 0.0, 0.0])
    ok_d9, op_d9, why_d9, claimed_d9 = IMPL['_begin_close_request_if_active'](
        sf_d9, SYM, 'batch_A', 'market_confirming')
    cases.append(('D9 前置: 真 BEGIN claim 到监控更新后的快照（E3/TP2/SL2）',
                  (ok_d9, claimed_d9['entry_orders'], claimed_d9['current_sl_id']),
                  (True, ['E1', 'E2', 'E3'], 'SL2'), None, None, why_d9))
    ok_d9d, txn_d9, why_d9d = IMPL['_derive_close_txn_vars'](sf_d9, claimed_d9, 'batch_A')
    cases.append(('D9b 前置: 真 derive 通过（v6.1 entry_orders 全长校验满足）',
                  ok_d9d, True, None, None, why_d9d))
    _, fx_d9r, _ = run_txn(IMPL, after_v6_for_d7, None,
                           tb=claimed_d9, txn_vars=txn_d9, catch=True,
                           sf=sf_d9, close_op_id=op_d9)
    c_d9 = fx_d9r.calls
    cases.append(('D9 运行时: gate 撤的是 claimed 完整 pending 集（E2 且 E3）',
                  ('cancel:E2' in c_d9, 'cancel:E3' in c_d9), (True, True),
                  None, None, f'calls={c_d9}'))
    cases.append(('D9c 运行时: 撤 claimed 的 TP2/SL2，不碰 stale 的 TP1/SL1',
                  ('cancel:TP2' in c_d9, 'cancel:SL2' in c_d9,
                   'cancel:TP1' in c_d9, 'cancel:SL1' in c_d9),
                  (True, True, False, False), None, None, ''))

    # D9-neg 敏感性对照：模拟整合漏掉 rebind（stale target_b_data 喂 gate）
    # → E3 必漏撤。本断言必须能检出，证明 D9 不是「恒绿」。
    fx_d9n = FakeExchange(pos_seq=[POS_LONG_001, POS_LONG_001, []],
                          order_seq=[_order('closed', 0.001), _order('canceled', 0.0)],
                          open_orders_seq=[[]])
    sf_d9n = FakeSelf(fx_d9n, states={SYM: {'batch_A': dict(STALE9)}})
    bind(sf_d9n, IMPL)
    sf_d9n._states[SYM]['batch_A'].update(
        entry_orders=['E1', 'E2', 'E3'], tp_order_id='TP2', current_sl_id='SL2',
        target_amounts=[0.001, 0.001, 0.001],
        filled_details=[77000.0, 0.0, 0.0])
    _, op_d9n, _, claimed_d9n = IMPL['_begin_close_request_if_active'](
        sf_d9n, SYM, 'batch_A', 'market_confirming')
    _, txn_d9n, _ = IMPL['_derive_close_txn_vars'](sf_d9n, claimed_d9n, 'batch_A')
    _, fx_d9nr, _ = run_txn(IMPL, after_v6_for_d7, None,
                            tb=dict(STALE9),       # ← 漏 rebind：stale 快照
                            txn_vars=txn_d9n, catch=True,
                            sf=sf_d9n, close_op_id=op_d9n)
    c_d9n = fx_d9nr.calls
    cases.append(('D9-neg 敏感性: 漏 rebind → E3 必漏撤（本测试能检出该缺陷）',
                  ('cancel:E2' in c_d9n, 'cancel:E3' in c_d9n), (True, False),
                  None, None, f'calls={c_d9n}'))

    print()
    print('=' * 68)
    print('四、🆕 O 组：ENTRY OrderNotFound 收紧（v6 §二 小点）')
    print('=' * 68)

    # O1：_verify_entry_order_terminal 遇 OrderNotFound
    fx_o1 = FakeExchange(order_seq=[ccxt.OrderNotFound('nf')])
    sf_o1 = FakeSelf(fx_o1)
    bind(sf_o1, IMPL)
    v_o1, _ = IMPL['_verify_entry_order_terminal'](sf_o1, 'E1', SYM, attempts=1, delay=0.0)
    cases.append(('O1 fetch_order(ENTRY) OrderNotFound → unknown', v_o1, 'unknown',
                  None, None, '不存在 ≠ 未成交'))

    fx_o1v5 = FakeExchange(order_seq=[ccxt.OrderNotFound('nf')])
    sf_o1v5 = FakeSelf(fx_o1v5)
    bind(sf_o1v5, IMPL_V5)
    v_o1v5, _ = IMPL_V5['_verify_entry_order_terminal'](sf_o1v5, 'E1', SYM, attempts=1, delay=0.0)
    cases.append(('O1-v5 负向: v5 判 gone（把"查不到"当成"没成交"）', v_o1v5, 'gone',
                  None, None, ''))

    # O2：整体 gate
    ok_o2, sf_o2 = run_entry(IMPL, [[]], [ccxt.OrderNotFound('nf')])
    cases.append(('O2 快照[] + fetch_order OrderNotFound → gate=False', ok_o2, False,
                  None, None, f'tg={len(sf_o2.tg_sent)}'))

    ok_o2v5, _ = run_entry(IMPL_V5, [[]], [ccxt.OrderNotFound('nf')])
    cases.append(('O2-v5 负向: v5 返回 True（放行 → 会撤 SL/TP）', ok_o2v5, True,
                  None, None, ''))

    # O3：正常路径（canceled）不受影响 —— 生产 L4151 实证支撑
    ok_o3, _ = run_entry(IMPL, [[]], [_order('canceled', 0.0)])
    ok_o3v5, _ = run_entry(IMPL_V5, [[]], [_order('canceled', 0.0)])
    cases.append(('O3 正常 canceled → v6/v5 都 True（正常路径未受影响）',
                  (ok_o3, ok_o3v5), (True, True), None, None, ''))

    print()
    print('=' * 68)
    print('五、🆕 S 组：市价事务顺序（v6 §二 核心）')
    print('=' * 68)

    # S1 静态 AST 顺序断言（对送审文档里改动 1 的 AFTER 块）
    after_v6 = extract_doc_after()
    ok_s1, det_s1 = static_ordered(after_v6)
    cases.append(('S1 静态: confirm < ENTRY gate < 撤 SL/TP', ok_s1, True, None, None, det_s1))

    # S1-v5 负向：v5 的 AFTER 存档必须不满足同一断言
    after_v5 = open(V5_AFTER, encoding='utf-8').read()
    ok_s1v5, det_s1v5 = static_ordered(after_v5)
    cases.append(('S1-v5 负向: v5 AFTER 顺序断言失败（对照有效）', ok_s1v5, False,
                  None, None, det_s1v5))

    # S2 运行时调用序列断言：ENTRY 在 MARKET 期间成交 → 撤 TP/SL 次数必须为 0
    ret_s2, fx_s2, sf_s2 = run_txn(
        IMPL, after_v6,
        order_seq=[_order('closed', 0.001), _order('closed', 0.001)],  # OID1 成交 / E2 已成交
        oo_seq=[[]])
    calls_s2 = fx_s2.calls
    cases.append(('S2 运行时: 撤 TP 调用次数 == 0', calls_s2.count('cancel:TP1'), 0,
                  None, None, f'calls={calls_s2}'))
    cases.append(('S2b 运行时: 撤 SL 调用次数 == 0', calls_s2.count('cancel:SL1'), 0,
                  None, None, ''))
    # ⚠️ 断言纪律：禁止直接用 list.index() 做调用序列断言 —— 变异体一旦移除
    # 某个 API 调用，index() 抛 ValueError，测试以**崩溃**收场而非干净失败，
    # 诊断信息全丢（M8-verify-always-gone 变异体实测暴露）。
    _p_s2 = [seq_index(calls_s2, n) for n in
             ('create_order', 'fetch_order:OID1', 'cancel:E2', 'fetch_order:E2')]
    cases.append(('S2c 运行时: create_order < fetch_order(OID1) < cancel(E2) < fetch_order(E2)',
                  (all(x is not None for x in _p_s2)
                   and _p_s2 == sorted(_p_s2)
                   and len(set(_p_s2)) == len(_p_s2)), True, None, None,
                  f'pos={_p_s2} calls={calls_s2}'))
    cases.append(('S2d 运行时: 返回 False（不进 clear）+ 发 critical',
                  (ret_s2[0], any(lv == 'critical' for lv, _ in sf_s2.tg_sent)),
                  (False, True), None, None, str(ret_s2[1])[:60]))

    # S2e/S2f（v6.1 修正2，R2-⑥）：gate 失败后 close_reason 必须切成异常态，
    # 否则批次永停 BEGIN 写入的 market_confirming → 冻结监控（改动 4 白名单）
    # 只 print 不再周期 critical —— fail-silent。
    b_s2 = sf_s2._states[SYM]['batch_A']
    cases.append(('S2e 运行时: 落盘 close_reason == market_entry_unknown（CAS 生效）',
                  b_s2.get('close_reason'), 'market_entry_unknown', None, None,
                  f'persist 次数={len(sf_s2.persisted)}'))
    cases.append(('S2f 运行时: 不再停在 market_confirming（fail-silent 已堵）',
                  b_s2.get('close_reason') != 'market_confirming', True, None, None, ''))

    # S2-v5 负向：v5 存档在同样输入下撤了 TP/SL，且发生在 verify ENTRY 之前
    ret_s2v5, fx_s2v5, _ = run_txn(
        IMPL_V5, after_v5,
        order_seq=[_order('closed', 0.001), _order('closed', 0.001)],
        oo_seq=[[]])
    calls_v5 = fx_s2v5.calls
    tp_v5 = calls_v5.count('cancel:TP1')
    sl_v5 = calls_v5.count('cancel:SL1')
    cases.append(('S2-v5 负向: v5 撤 TP/SL 各 1 次（保护单已丢）', (tp_v5, sl_v5), (1, 1),
                  None, None, f'calls={calls_v5}'))
    _i_tp5 = seq_index(calls_v5, 'cancel:TP1')
    _i_fe5 = seq_index(calls_v5, 'fetch_order:E2')
    cases.append(('S2-v5b 负向: v5 撤 TP 早于 verify ENTRY',
                  (_i_tp5 is not None and _i_fe5 is not None and _i_tp5 < _i_fe5),
                  True, None, None, f'pos=({_i_tp5}, {_i_fe5}) calls={calls_v5}'))

    print()
    print('=' * 68)
    print('五-b、🆕 L 组：限价 ENTRY gate（改动 1d，v6.1 P0-3）')
    print('=' * 68)

    # 被测对象 = 送审文档「改动 1d」的 AFTER 块本身（生产 L7541-7549 的替换段）。
    # 与市价的关键区别：gate 失败时平仓单还没挂、仓位零变化 → 优先 CAS 回滚
    # 让监控恢复；回滚失败才落 limit_entry_unknown 冻结。
    after_1d = extract_doc_after_limit()

    def _limit_states(op_id='deadbeef' * 4):
        return {SYM: {'batch_A': dict(_mk_batch(), close_phase=1, pending_close=True,
                                      is_programmatic_cancel=True, close_op_id=op_id,
                                      close_reason='limit_pending_normal')}}

    # L1：gate 通过 → 放行（块结束 return None，落生产 L7551 撤 TP / L7584 挂 LIMIT）
    ret_l1, fx_l1, sf_l1 = run_txn(IMPL, after_1d, [_order('canceled', 0.0)],
                                   oo_seq=[[]], states=_limit_states())
    b_l1 = sf_l1._states[SYM]['batch_A']
    cases.append(('L1 限价 gate 通过 → 块内不放回（落后续生产段）', ret_l1, NO_RETURN,
                  None, None, f'calls={fx_l1.calls}'))
    cases.append(('L1b gate 通过时绝不回滚/切 reason（phase 保持 1，零 persist）',
                  (b_l1.get('close_phase'), b_l1.get('close_reason'),
                   len(sf_l1.persisted)),
                  (1, 'limit_pending_normal', 0), None, None, ''))

    # L2：ENTRY 逐 ID 验证 OrderNotFound → gate=False → CAS 回滚成功
    ret_l2, _, sf_l2 = run_txn(IMPL, after_1d, [ccxt.OrderNotFound('nf')],
                               oo_seq=[[]], states=_limit_states())
    b_l2 = sf_l2._states[SYM]['batch_A']
    cases.append(('L2 ENTRY 查不到 → gate=False → 返回 (False,…)（不撤 TP 不挂 LIMIT）',
                  (isinstance(ret_l2, tuple) and ret_l2[0] is False), True,
                  None, None, str(ret_l2[1])[:50] if isinstance(ret_l2, tuple)
                  else repr(ret_l2)))
    cases.append(('L2b CAS 回滚成功 → phase=0/pending=False（监控恢复）',
                  (b_l2.get('close_phase'), b_l2.get('pending_close')), (0, False),
                  None, None, ''))
    cases.append(('L2c 回滚成功 → reason 不切异常 + 发 critical',
                  (b_l2.get('close_reason'),
                   any(lv == 'critical' for lv, _ in sf_l2.tg_sent)),
                  ('limit_pending_normal', True), None, None, ''))

    # L3：open_orders 快照 None → gate=False（UNKNOWN≠EMPTY 前门票）
    ret_l3, _, sf_l3 = run_txn(IMPL, after_1d, [], oo_seq=[None],
                               states=_limit_states(), catch=True)
    b_l3 = sf_l3._states[SYM]['batch_A']
    cases.append(('L3 快照 None → gate=False → 回滚（phase=0）',
                  (isinstance(ret_l3, tuple) and ret_l3[0] is False,
                   b_l3.get('close_phase')), (True, 0), None, None, ''))
    # ⚠️ L3c（变异检查器 M9 抓到的盲区）：只断言「返回 False」是不够的 ——
    # 若把 gate 的 `remaining is None or not isinstance(...)` 改回 `or []`
    # （正是事故原型的 UNKNOWN→EMPTY），`for o in None` 抛 TypeError，
    # 在 catch=False 下会直接炸穿整个套件（L3b 那种写法根本没机会执行），
    # 在 catch=True 下则伪装成 (False, '<EXC...>')，与干净拦截无法区分。
    # 因此这里用 catch 模式专门断言：**必须是 gate 干净拦截，而非异常兜底**。
    ret_l3c, _, _ = run_txn(IMPL, after_1d, [], oo_seq=[None],
                            states=_limit_states(), catch=True)
    _l3c_msg = ret_l3c[1] if isinstance(ret_l3c, tuple) and len(ret_l3c) > 1 else ''
    cases.append(('L3c 快照 None 必须 gate 干净拦截（不得依赖异常兜底，防 `or []` 回归）',
                  '<EXC' not in str(_l3c_msg), True, None, None, str(_l3c_msg)[:70]))

    # L4：ENTRY 在等待期成交 → gate=False（filled 单列一档）→ 回滚
    ret_l4, _, sf_l4 = run_txn(IMPL, after_1d, [_order('closed', 0.001)],
                               oo_seq=[[]], states=_limit_states())
    b_l4 = sf_l4._states[SYM]['batch_A']
    cases.append(('L4 ENTRY 等待期成交 → gate=False → 回滚（仓位变化由监控接管）',
                  (isinstance(ret_l4, tuple) and ret_l4[0] is False,
                   b_l4.get('close_phase')), (True, 0), None, None, ''))

    # L5：回滚被拒（op_id 不匹配 = 状态已被接管）→ reason 切换同样被拒（CAS 纪律）
    _, _, sf_l5 = run_txn(IMPL, after_1d, [ccxt.OrderNotFound('nf')],
                          oo_seq=[[]], states=_limit_states(op_id='other_op'))
    b_l5 = sf_l5._states[SYM]['batch_A']
    cases.append(('L5 回滚被拒（op_id 不匹配）→ reason 切换同样被拒（不覆盖他人状态）',
                  (b_l5.get('close_phase'), b_l5.get('close_reason')),
                  (1, 'limit_pending_normal'), None, None, ''))
    cases.append(('L5b 回滚被拒仍发 critical（fail-noisy）',
                  any(lv == 'critical' for lv, _ in sf_l5.tg_sent), True,
                  None, None, ''))

    # L5c：回滚被拒（phase 已被推进）+ op_id 匹配 → reason 切 limit_entry_unknown
    st_l5c = _limit_states()
    st_l5c[SYM]['batch_A']['close_phase'] = 2
    _, _, sf_l5c = run_txn(IMPL, after_1d, [ccxt.OrderNotFound('nf')],
                           oo_seq=[[]], states=st_l5c)
    b_l5c = sf_l5c._states[SYM]['batch_A']
    cases.append(('L5c 回滚被拒（phase 已推进）→ reason 切 limit_entry_unknown 兜底',
                  b_l5c.get('close_reason'), 'limit_entry_unknown', None, None, ''))

    print()
    print('=' * 68)
    print('六、coverage 不变量（§一 + §六，v6 沿用）')
    print('=' * 68)

    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, {})
    cases.append(('G1 敞口0.002≥台账0.001 → 平0.001', amt, 0.001, None, None, d))

    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {})
    cases.append(('G2 敞口0.0005<台账0.001 单批次 → 平0.0005', amt, 0.0005, None, None, d))

    states_multi = {SYM: {'batch_A': _mk_batch(), 'batch_B': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_001], 0.001, states_multi)
    cases.append(('G3 多批次且实际<台账合计 → None（禁止自动平）', amt, None, None, None, d))

    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, states_multi)
    cases.append(('G3b 多批次且实际==台账合计 → 平0.001', amt, 0.001, None, None, d))

    states_g6 = {SYM: {'batch_A': _mk_batch(phase=1, pending=True),
                       'batch_B': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_001], 0.001, states_g6)
    cases.append(('G6 target已phase=1的决定性例子 → None（拦截）', amt, None, None, None, d))

    others, sum_all, blocking = run_survey(IMPL, states_g6)
    cases.append(('G6b survey：sum_all 含 target = 0.002', round(sum_all, 6), 0.002,
                  None, None, f'others={others} blocking={blocking}'))
    cases.append(('G6c survey：blocking_count=0', blocking, 0, None, None, ''))

    amt_v4, d_v4 = run_amount(IMPL_V4, [POS_LONG_001], 0.001, states_g6)
    cases.append(('G6-v4 负向: v4 放行（返回 0.001，决定性例子漏过）', amt_v4, 0.001,
                  None, None, d_v4))

    states_g7 = {SYM: {'batch_A': _mk_batch(phase=1),
                       'batch_B': _mk_batch(phase=1),
                       'batch_C': _mk_batch()}}
    amt, d = run_amount(IMPL, [POS_LONG_002], 0.001, states_g7, target='batch_A')
    cases.append(('G7 同方向另有在途平仓 → None（Fail-Closed）', amt, None, None, None, d))

    amt, d = run_amount(IMPL, [None], 0.001, {})
    cases.append(('G4 持仓读取失败 → None（不发单）', amt, None, None, None, d))

    amt, d = run_amount(IMPL, [POS_LONG_0005], 0.001, {}, fail_load=True)
    cases.append(('G5 批次统计失败 → None（归因不可判定）', amt, None, None, None, d))

    print()
    print('=' * 68)
    print('七、_read_position_amt 严格 Fail-Closed（§八-1，v6 沿用）')
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
    print('八、ENTRY 逐 ID 验证（§三 + §四 前门）')
    print('=' * 68)

    ok, sf = run_entry(IMPL, [None], [_order('canceled', 0.0)])
    cases.append(('E1 快照返回 None → False（Fail-Closed）', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    ok_v3, sf_v3 = run_entry(IMPL_V3, [None], [])
    cases.append(('E1-负向: v3 在 None 上返回 True（假确认复现）', ok_v3, True, None, None, ''))

    ok, sf = run_entry(IMPL, [[]], [_order('canceled', 0.0)])
    cases.append(('E2 快照[]+ID终态canceled → True', ok, True, None, None, ''))

    ok, sf = run_entry(IMPL, [[]], [_order('open', 0.0)] * 3)
    cases.append(('E3 逐ID验证 open → False', ok, False, None, None, f'tg={len(sf.tg_sent)}'))

    ok, sf = run_entry(IMPL, [[]], [_order('closed', 0.001)])
    cases.append(('E4 逐ID验证 filled → False（仓位已变化）', ok, False, None, None,
                  f'tg={len(sf.tg_sent)}'))

    print()
    print('=' * 68)
    print('九、🆕 M 组：parity checker 严格判据 + mutation 自测（v6.1 R2-⑧⑨）')
    print('=' * 68)

    # 用 PROJECT_DIR 拼绝对路径，不依赖 CWD：变异检查会把本文件复制到
    # G:/tmp/mut61_auto/ 运行，相对路径会找不到 checker（依赖 CWD 同属「路径脆弱」）。
    _parity = os.path.join(PROJECT_DIR, 'check_doc_helper_parity.py')
    r_m1 = subprocess.run([sys.executable, _parity],
                          capture_output=True, text=True)
    cases.append(('M1 离线: parity 主检查 rc=0（11 helper 严格判据全过）',
                  r_m1.returncode, 0, None, None,
                  (r_m1.stdout or '').strip().splitlines()[-1][:60]
                  if r_m1.stdout else (r_m1.stderr or '')[-60:]))
    r_m2 = subprocess.run([sys.executable, _parity, '--self-test'],
                          capture_output=True, text=True)
    cases.append(('M2 离线: parity --self-test rc=0（M1~M5 mutation 全符合预期）',
                  r_m2.returncode, 0, None, None,
                  (r_m2.stdout or '').strip().splitlines()[-1][:60]
                  if r_m2.stdout else (r_m2.stderr or '')[-60:]))

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
    print(f'✅ {len(cases)}/{len(cases)} 全部通过（含 v6.0 / v5 / v4 / v3 负向对照）')
    return 0


IMPL = extract_impl(V6_PATH, WANT_FUNCS_V62)
IMPL_V60 = extract_impl(V60_PATH, WANT_FUNCS_V60)   # v6.0 归档：v6.1 负向对照
IMPL_V5 = extract_impl(V5_PATH, WANT_FUNCS_V5)
IMPL_V4 = extract_impl(V4_PATH, ['_read_position_amt', '_fetch_close_order_state',
                                 '_confirm_close_filled', '_survey_same_side_batches',
                                 '_close_amount_guard'])
IMPL_V3 = extract_impl(V3_PATH, ['_read_position_amt', '_fetch_close_order_state',
                                 '_confirm_close_filled'])


def extract_v3_entry():
    """v3 缺陷版 ENTRY 函数负向对照源（含 `or []`），固化为档案。"""
    src = open(V3_ENTRY, encoding='utf-8').read()
    ns = {'time': time, 'ccxt': ccxt}
    exec(compile(src, V3_ENTRY, 'exec'), ns)
    return ns['_cancel_and_verify_entry_orders']


IMPL_V3['_cancel_and_verify_entry_orders'] = extract_v3_entry()

if __name__ == '__main__':
    sys.exit(main())
