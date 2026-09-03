# -*- coding: utf-8 -*-
"""v6.4-P0 决定性测试（5 个封顶，RED-first）——返工版（ChatGPT landing audit P0-1~P0-4）。

与首版的关键差异：全部经真实生产 helper/调用链验证，不再用宽松桩遮蔽契约——
- 确认链用真实 _confirm_close_filled 六态（订单存在 ≠ 成交；PENDING/UNKNOWN 不 COMMIT）
- resize 复用 crash-safe Create 链（PENDING_CREATE intent → create → verify → G3b owner）
- consumer 用真实 _survey_same_side_batches 行为证明 + 全库结构换点证明
- restart 经真实生产调用点 _handle_partial_close_on_recovery 触发
- detector 经真实生产调用点 _maybe_report_conservation_conflict 触发（含 3 次告警去重）
"""
import ast
import copy
import importlib.util
import textwrap
import threading
import types

TRADER_PATH = r'G:\my-crypto-bot\trader_260725.py'
HELPER_PATH = r'G:\my-crypto-bot\送审附件_v6.2\new_helpers_v62.py'
BOTRUNNER_PATH = r'G:\my-crypto-bot\bot_runner.py'

SRC = open(TRADER_PATH, encoding='utf-8').read()
HLP = open(HELPER_PATH, encoding='utf-8').read()
TREE = ast.parse(SRC)
HTREE = ast.parse(HLP)

SYM = 'BTC/USDT:USDT'


def _extract(tree, src, name, ns):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(textwrap.dedent(ast.get_source_segment(src, node)), ns)
            return ns[name]
    return None


def _module_assigns(tree, src, prefixes):
    """提取模块级常量（_MERGE_* / owner 判据等），供提取函数的全局名解析。"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets and \
                isinstance(node.targets[0], ast.Name) and \
                node.targets[0].id.startswith(prefixes):
            try:
                out[node.targets[0].id] = eval(compile(ast.Expression(node.value), '<m>', 'eval'))
            except Exception:
                pass
    return out


# 共享命名空间：函数间经 global 名互相解析（与生产模块语义一致）
NS = {'uuid': __import__('uuid'), 'time': __import__('time'), 'math': __import__('math')}
NS.update(_module_assigns(TREE, SRC, ('_MERGE', '_PARTIAL', '_partial_resize', 'TAKER', 'MAKER', 'CONSERVATION')))
OWNER_FN = _extract(TREE, SRC, '_partial_resize_owner_ok', NS)
NS['_partial_resize_owner_ok'] = OWNER_FN


def ex_t(name):
    return _extract(TREE, SRC, name, NS)


def ex_h(name):
    return _extract(HTREE, HLP, name, NS)


F = {n: ex_t(n) for n in (
    '_batch_net_position', '_execute_partial_close', '_resize_protection_after_partial',
    '_resume_partial_resize', '_handle_partial_close_on_recovery',
    '_maybe_report_conservation_conflict', '_check_conservation_conflict',
    '_is_valid_inflight_close_txn',
    '_final_pre_create_check', '_assert_create_allowed', '_commit_protection_with_g3',
    '_update_registry', '_verify_and_update_registry', '_verify_order_created',
    '_build_intent', '_protection_identity', '_close_amount_guard',
    '_try_acquire_resize_inflight', '_release_resize_inflight',
    '_maybe_runtime_resume_partial')}
H = {n: ex_h(n) for n in (
    '_confirm_close_filled', '_fetch_close_order_state', '_set_close_reason_if_current',
    '_begin_close_request_if_active', '_survey_same_side_batches',
    '_rollback_close_request_if_current')}

BINDS = {**F, **H}
MISSING = [k for k, v in BINDS.items() if v is None]


class StubExchange:
    """订单状态机桩：market 单即成 closed/filled=amount；条件单 open 可撤（撤后 canceled）。"""

    def __init__(self, lazy_market=False):
        self.orders = {}
        self.create_calls = []
        self.cancel_calls = []
        self._next_id = 1000
        self.lazy_market = lazy_market  # True: market 单已接受但未成交（open/filled=0）

    def _gen(self):
        self._next_id += 1
        return str(self._next_id)

    def price_to_precision(self, symbol, v):
        return float(round(float(v), 1))

    def amount_to_precision(self, symbol, v):
        return float(round(float(v), 3))

    def create_order(self, symbol, otype, side, amount, price=None, params=None, **k):
        oid = self._gen()
        self.create_calls.append((otype, side, round(float(amount), 6)))
        if otype == 'market' and not self.lazy_market:
            o = {'id': oid, 'status': 'closed', 'filled': float(amount), 'amount': float(amount)}
        else:
            o = {'id': oid, 'status': 'open', 'filled': 0.0, 'amount': float(amount),
                 'stopPrice': float((params or {}).get('stopPrice') or 0),
                 'type': otype}
        self.orders[oid] = o
        return dict(o)

    def cancel_order(self, order_id, symbol, params=None, **k):
        self.cancel_calls.append(str(order_id))
        o = self.orders.get(str(order_id))
        if o is None:
            raise Exception(f'binanceusdm -2011 Unknown order sent')
        o['status'] = 'canceled'
        return {'id': str(order_id), 'status': 'canceled'}

    def fetch_order(self, order_id, symbol, params=None, **k):
        o = self.orders.get(str(order_id))
        if o is None:
            raise Exception('binanceusdm -2011 Unknown order sent')
        return dict(o)

    def fetch_open_orders(self, symbol, params=None, **k):
        return []

    def fetch_ticker(self, symbol):
        return {'last': 78000.0, 'close': 78000.0}

    def set_leverage(self, *a, **k):
        return True


class StubTrader:
    pass


def make_trader(states, actual_pos=2.0, lazy_market=False):
    t = StubTrader()
    t.exchange = StubExchange(lazy_market=lazy_market)
    t._states = states
    t._lock = threading.RLock()
    t._state_lock = t._lock
    t._criticals = []
    # 🔥 v6.4-P6：守恒分级观察器事件存储（v1.3 契约）
    t._conservation_events = {}
    t._conservation_event_lock = threading.Lock()
    t._persist_ok = True
    t._stub_actual = actual_pos

    def load_all_states():
        return copy.deepcopy(t._states)

    def _persist_states(all_states):
        if not t._persist_ok:
            return False
        t._states.clear()
        t._states.update(all_states)
        return True

    def send_tg_notification(msg, level='info'):
        if level == 'critical':
            t._criticals.append(msg)

    def _safe_api_call(fn, *a, **k):
        return fn(*a, **k)

    def _read_position_amt(symbol, side, is_hedge_mode):
        return t._stub_actual

    def _get_current_position_amt(symbol, is_hedge_mode, side='BUY', **k):
        return t._stub_actual

    t.load_all_states = load_all_states
    t._persist_states = _persist_states
    t.send_tg_notification = send_tg_notification
    t._safe_api_call = _safe_api_call
    t._read_position_amt = _read_position_amt
    t._get_current_position_amt = _get_current_position_amt
    # 闸门告警簿记桩（真实实现为计数告警，与本测试判据无关）
    t._gate_alert_counts = {}
    t._gate_alert_lock = threading.Lock()
    # 🔥 v6.4-P1：resize 在途互斥簿记（_try/_release_resize_inflight 消费）
    t._resize_inflight = set()
    t._resize_inflight_lock = threading.Lock()
    t._partial_resume_state = {}
    t._gate_alert_notify = lambda identity, reason, msg, level='warning': print(f'  [gate] {msg}')
    t._gate_alert_clear = lambda identity: None
    for name, fn in BINDS.items():
        setattr(t, name, types.MethodType(fn, t))
    return t


def _batch(ta=(1.0,), fd=(100.0,), lfc=1, **extra):
    b = {
        'is_active': True, 'batch_id': 'batch_A', 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'target_amounts': list(ta), 'filled_details': list(fd),
        'last_filled_count': lfc, 'total_entry_fee': 0.0,
        # 🔥 v6.4-P1：durable 取价契约——生产批次必有这两字段（trade_state 实证），
        # resize 取价只认 durable，拒绝从旧交易所订单恢复
        'stop_steps': [75001.0], 'take_profit_price': 80000.0,
        'entry_orders': ['E1'], 'current_sl_id': 'S1', 'tp_order_id': 'T1',
        'close_phase': 0, 'pending_close': False, 'is_programmatic_cancel': False,
        'close_reason': '', 'close_op_id': '',
        'protection_registry': {
            f'batch_A|SL|L{lfc - 1}|LONG': {'role': 'SL', 'state': 'CONFIRMED',
                                            'order_id': 'S1', 'layer': lfc - 1, 'side': 'LONG'},
            f'batch_A|TP|L{lfc - 1}|LONG': {'role': 'TP', 'state': 'CONFIRMED',
                                            'order_id': 'T1', 'layer': lfc - 1, 'side': 'LONG'},
        },
    }
    b.update(extra)
    return b


def _seed_protection_orders(t):
    """S1/T1 旧保护单在场（open 条件单，带 stopPrice）。"""
    t.exchange.orders['S1'] = {'id': 'S1', 'status': 'open', 'filled': 0.0,
                               'amount': 1.0, 'stopPrice': 75001.0, 'type': 'STOP_MARKET'}
    t.exchange.orders['T1'] = {'id': 'T1', 'status': 'open', 'filled': 0.0,
                               'amount': 1.0, 'stopPrice': 80000.0, 'type': 'TAKE_PROFIT_MARKET'}


# ── ① PARTIAL 事务全链 + 真实六态确认拒绝未成交 ──────────────────────────
def t01_partial_commit_full_chain_and_no_fill_rejection():
    # 场景 B（先证拒绝）：MARKET 已接受但 filled=0（status=open）→ 绝不 COMMIT
    t = make_trader({SYM: {'batch_A': _batch()}}, actual_pos=1.0, lazy_market=True)
    _seed_protection_orders(t)
    ok, msg = F['_execute_partial_close'](t, SYM, 'batch_A', 0.5)
    assert ok is False, (ok, msg)
    assert 'PENDING' in msg or 'UNKNOWN' in msg or 'NOT_CONFIRMED' in msg, msg
    b = t._states[SYM]['batch_A']
    assert b.get('close_phase') == 1 and b.get('close_reason') == 'partial_closing'
    assert 'realized_reduce_amount' not in b, '未成交绝不写账本'
    assert not any(c[0] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET') for c in t.exchange.create_calls), \
        '未成交绝不 resize'
    assert len(t._criticals) >= 1, '未成交必须 loud'

    # 场景 A（happy chain）：真实六态 CONFIRMED_FULL → COMMIT → resize → ACTIVE
    t2 = make_trader({SYM: {'batch_A': _batch()}}, actual_pos=1.0)
    _seed_protection_orders(t2)
    ok, msg = F['_execute_partial_close'](t2, SYM, 'batch_A', 0.5)
    assert ok is True, msg
    b2 = t2._states[SYM]['batch_A']
    assert abs(b2['realized_reduce_amount'] - 0.5) < 1e-9
    assert abs(b2['realized_reduce_cost'] - 50.0) < 1e-6, b2['realized_reduce_cost']
    assert b2['close_phase'] == 0 and b2['pending_close'] is False
    assert b2['is_programmatic_cancel'] is False and b2['close_reason'] == ''
    # resize 用 commit 后净量 0.5（非旧 gross 1.0）
    assert ('STOP_MARKET', 'sell', 0.5) in t2.exchange.create_calls, t2.exchange.create_calls
    assert ('TAKE_PROFIT_MARKET', 'sell', 0.5) in t2.exchange.create_calls
    assert 'S1' in t2.exchange.cancel_calls and 'T1' in t2.exchange.cancel_calls
    assert b2['current_sl_id'] != 'S1' and b2['tp_order_id'] != 'T1'
    reg = b2['protection_registry']
    assert any(e.get('order_id') == b2['current_sl_id'] and e.get('state') == 'CONFIRMED'
               for e in reg.values()), '新 SL 必须 verify 后才 CONFIRMED'

    # 场景 C（三审 P0-1）：多批次 actual < Σnet（归属冲突）→ guard Fail-Closed，
    # BEGIN 被 CAS 回滚，零 MARKET——绝不带冲突继续减 aggregate 重演 wrong-close
    t3 = make_trader({
        SYM: {'batch_A': _batch(),
              'batch_B': _batch(batch_id='batch_B')}}, actual_pos=1.5)  # Σnet 2.0 > 1.5
    ok, msg = F['_execute_partial_close'](t3, SYM, 'batch_A', 0.5)
    assert ok is False and 'partial_guard_rejected' in msg, (ok, msg)
    assert not t3.exchange.create_calls, '冲突时零 MARKET'
    ba = t3._states[SYM]['batch_A']
    assert ba['close_phase'] == 0 and ba['pending_close'] is False \
        and ba['is_programmatic_cancel'] is False, '无副作用阶段必须 CAS 回滚 BEGIN'
    # close_reason/close_op_id 保留作取证痕迹（rollback 契约如此）

    # 场景 D（三审 P0-2）：amount == net_qty → 入口拒绝并提示 /close，零 MARKET
    t4 = make_trader({SYM: {'batch_A': _batch()}}, actual_pos=1.0)
    ok, msg = F['_execute_partial_close'](t4, SYM, 'batch_A', 1.0)
    assert ok is False and '/close' in msg, (ok, msg)
    assert not t4.exchange.create_calls


# ── ② net ledger 真正接入生产：survey 行为证明 + 全库结构换点证明 ────────
def t02_consumers_use_net_ledger():
    # 行为证明：真实 survey 对 partial 后批次按净量计入 coverage
    t = make_trader({
        SYM: {'batch_A': _batch(realized_reduce_amount=0.5),   # gross 1.0 → net 0.5
              'batch_B': _batch(batch_id='batch_B', ta=(1.0,), fd=(100.0,))}},  # net 1.0
        actual_pos=1.5)
    actual, sum_all, blocking = t._survey_same_side_batches(SYM, 'BUY', 'batch_A')
    assert abs(sum_all - 1.5) < 1e-9, f'survey Σtracked 必须按净仓位 1.5（gross 会得 2.0）: {sum_all}'

    # 结构证明：trader/bot_runner 旧净读点已全部换 helper（仅 fee 分摊分母/净算式保留 gross）
    banned = 'current_filled_amount = sum(target_amounts[:last_filled_count])'
    assert banned not in SRC, 'trader 仍残留旧 gross 赋值'
    assert SRC.count('_batch_net_position(') >= 12, SRC.count('_batch_net_position(')
    br = open(BOTRUNNER_PATH, encoding='utf-8').read()
    assert 'current_filled_amount = sum(target_amounts[:last_filled_count])' not in br
    assert br.count('trader._batch_net_position(') >= 5, br.count('trader._batch_net_position(')
    # helper 两处（derive/survey）已 net 化
    assert HLP.count('realized_reduce_amount') >= 2, HLP.count('realized_reduce_amount')
    # 成本点：6 处均价公式不再用完整 total_entry_fee 直接除净仓位
    assert '(total_cost + total_entry_fee) / current_filled_amount' not in SRC
    assert '(total_cost + total_entry_fee) / current_filled_amount' not in br
    # fee 口径：必须按 cost 比例（fee ∝ notional；qty 比例在 partial→不同价新层后低估）
    assert '/ _gross_filled' not in SRC and '/ gross_filled' not in SRC, '残留 qty 比例 fee'
    fee_cost_based = (SRC.count('net_cost / _gross_cost')
                      + SRC.count('_net_cost_m / _gross_cost_m')
                      + SRC.count('_net_cost_l / _gross_cost_l'))
    assert fee_cost_based >= 4, fee_cost_based


# ── ③ 重启分型经真实生产调用点 + gate owner exception 窄度 ───────────────
def t03_restart_split_via_production_hook():
    # 场景 1：partial_resize_pending（账本已 CAS、resize 前崩溃）→ 续跑且不重发 MARKET
    t = make_trader({SYM: {'batch_A': _batch(
        realized_reduce_amount=0.5, realized_reduce_cost=50.0,
        close_phase=1, pending_close=True, is_programmatic_cancel=True,
        close_reason='partial_resize_pending', close_op_id='OPX')}}, actual_pos=1.0)
    _seed_protection_orders(t)
    F['_handle_partial_close_on_recovery'](t, SYM, 'batch_A')
    b = t._states[SYM]['batch_A']
    assert b['close_phase'] == 0 and b['close_reason'] == '', b['close_reason']
    assert not any(c[0] == 'market' for c in t.exchange.create_calls), '续跑绝不重发 MARKET'
    assert ('STOP_MARKET', 'sell', 0.5) in t.exchange.create_calls

    # 场景 2：partial_closing（MARKET 副作用未知）→ loud 拒续跑
    t2 = make_trader({SYM: {'batch_A': _batch(
        close_phase=1, pending_close=True, is_programmatic_cancel=True,
        close_reason='partial_closing', close_op_id='OPY')}}, actual_pos=1.0)
    F['_handle_partial_close_on_recovery'](t2, SYM, 'batch_A')
    assert len(t2._criticals) >= 1 and not t2.exchange.create_calls
    assert t2._states[SYM]['batch_A']['close_reason'] == 'partial_closing'

    # 场景 3：owner exception 窄度——非 partial reason / op 不匹配 → 一律拒绝
    for reason, op in (('limit_pending_normal', 'OPZ'), ('partial_resize_pending', 'WRONG')):
        t3 = make_trader({SYM: {'batch_A': _batch(close_phase=1, pending_close=True,
                                                  close_reason=reason, close_op_id=op)}})
        ok, r = F['_final_pre_create_check'](t3, SYM, 'batch_A', 'batch_A|SL|L0|LONG',
                                             desc='x', owner_op_id='OPZ')
        assert ok is False, (reason, op, r)


# ── ④ partial → 新层不同价成交 → 再 partial（净均价恒定，钉死成本基准）──
def t04_partial_new_fill_partial_avg_stable():
    t = make_trader({SYM: {'batch_A': _batch()}}, actual_pos=2.0)
    _seed_protection_orders(t)
    ok, msg = F['_execute_partial_close'](t, SYM, 'batch_A', 0.5)
    assert ok is True, msg
    b = t._states[SYM]['batch_A']  # persist 整体替换 → 重新取引用
    # 新层成交：L2 1.0@200 入账（fill 路径零改动）
    b['target_amounts'] = [1.0, 1.0]
    b['filled_details'] = [100.0, 200.0]
    b['last_filled_count'] = 2
    nq, nc = F['_batch_net_position'](t, b)
    assert abs(nq - 1.5) < 1e-9 and abs(nc - 250.0) < 1e-6, (nq, nc)
    assert abs(nc / nq - 166.6666667) < 1e-3, nc / nq  # 166.67（gross avg 会得 200 ❌）
    ok, msg = F['_execute_partial_close'](t, SYM, 'batch_A', 0.5)
    assert ok is True, msg
    b = t._states[SYM]['batch_A']  # 重新取引用
    nq2, nc2 = F['_batch_net_position'](t, b)
    assert abs(nq2 - 1.0) < 1e-9, nq2
    assert abs(nc2 / nq2 - 166.6666667) < 1e-3, f'二次 partial 净均价必须仍 166.67: {nc2 / nq2}'


# ── ⑤ detector 经真实生产调用点（v6.4-P6 分级语义：事件内 ≤3 critical 上限）──
def t05_detector_via_wiring_with_dedup():
    t = make_trader({
        SYM: {'batch_A': _batch(), 'batch_B': _batch(batch_id='batch_B')}},
        actual_pos=1.5)  # 同方向 Σnet 2.0 > actual 1.5 → conflict（无有效在途 → 立即 critical）
    for i in range(5):
        F['_maybe_report_conservation_conflict'](t, SYM, 'BUY', 1.5)
    assert len(t._criticals) == 3, f'告警必须存在且单事件 3 次封顶: {len(t._criticals)}'
    # 守恒恢复 → 静默 + 事件记录整份删除
    t2 = make_trader({
        SYM: {'batch_A': _batch(), 'batch_B': _batch(batch_id='batch_B')}},
        actual_pos=2.0)
    F['_maybe_report_conservation_conflict'](t2, SYM, 'BUY', 2.0)
    assert t2._criticals == []
    assert t2._conservation_events == {}, f'收敛必须删除事件记录: {t2._conservation_events}'


TESTS = [t01_partial_commit_full_chain_and_no_fill_rejection,
         t02_consumers_use_net_ledger,
         t03_restart_split_via_production_hook,
         t04_partial_new_fill_partial_avg_stable,
         t05_detector_via_wiring_with_dedup]


def main():
    if MISSING:
        print(f'RED 0/{len(TESTS)}（未实现: {MISSING}）')
        return 1
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
