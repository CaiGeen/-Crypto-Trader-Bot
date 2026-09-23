#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过渡期安全收口（2026-09-22）定点测试——离线零交易所连接。

被测：trader_260725.py（frozen-archive，等待 batchtrader 上线前的最小安全收口）。
ast 提取方法源码 + Stub 绑定（test_v63_entry_idempotency 同款范式）。

覆盖：
  T1  手工/外部挂单 → _check_existing_conflicts 阻断 + 零 cancel_order + critical 告警
  T2  程序遗留单（registry intent 匹配）→ 同样阻断零撤单，告警文案区分标注
  T3  known_order_ids 完整性：limit_close_order_id / registry order_id 不误判孤儿
  T4  _validate_stop_losses 阶梯单调性（BUY：倒退拒绝 / 逐层不降放行含相等）
  T5  _validate_stop_losses 阶梯单调性（SELL 镜像：上升拒绝 / 逐层不升放行）
  T6  SG2 方向过滤：LONG+SHORT 批次并存时台账只累计同方向（Hedge 模式不误拒）
  T7  SG2 side 必填：缺 side → ValueError（拒绝静默双方向求和）
  T8  源码锚点：持仓模式检测先于持仓查询 / 无"默认单向持仓"Fail-Open 回退 /
      冲突检查函数体内零 cancel_order

用法: python test_transition_safety_gate.py
"""
import ast
import sys
import textwrap
import types

SRC_PATH = 'trader_260725.py'
SYM = 'BTC/USDT:USDT'
RESULTS = []


def report(name, passed, detail=''):
    RESULTS.append((name, bool(passed)))
    print(f"{'✅' if passed else '❌'} {name} {detail}")


def extract_fn(name):
    with open(SRC_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {}
            exec(textwrap.dedent(ast.get_source_segment(src, node)), ns)
            return ns[name]
    raise AssertionError(f'未找到函数 {name}')


class StubExchange:
    """双通道 fetch_open_orders + cancel_order 计数桩。"""

    def __init__(self, normal=None, stop=None):
        self.normal = list(normal or [])
        self.stop = list(stop or [])
        self.api_calls = []

    def price_to_precision(self, symbol, v):
        return str(round(float(v), 1))

    def amount_to_precision(self, symbol, v):
        return str(float(v))

    def fetch_open_orders(self, symbol, params=None, **k):
        return list(self.stop) if (params or {}).get('stop') else list(self.normal)

    def cancel_order(self, *a, **k):
        self.api_calls.append('cancel_order')
        return {}


class StubTrader:
    pass


def make_trader(ex, with_matcher=True):
    t = StubTrader()
    t.exchange = ex
    t._tg = []  # [(level, text)]

    def _safe(fn, *a, **k):
        ex.api_calls.append(getattr(fn, '__name__', str(fn)))
        return fn(*a, **k)

    t._safe_api_call = _safe
    t.send_tg_notification = lambda text, **k: t._tg.append((k.get('level'), text))
    if with_matcher:
        t._order_matches_intent = types.MethodType(extract_fn('_order_matches_intent'), t)
    return t


class Sig:
    def __init__(self, side, entries, stops, init_sl):
        self.side = side
        self.entries = entries
        self.stop_loss_steps = stops
        self.initial_stop_loss = init_sl


# ── T1: 手工/外部挂单 → 阻断 + 零撤单 ────────────────────────────

def t1_manual_orders_block_no_cancel():
    CHECK = extract_fn('_check_existing_conflicts')
    manual_limit = {'id': 'M1', 'symbol': SYM, 'side': 'buy', 'type': 'LIMIT',
                    'amount': 0.5, 'price': 70000.0, 'stopPrice': None,
                    'info': {'type': 'LIMIT'}}
    manual_sl = {'id': 'M2', 'symbol': SYM, 'side': 'sell', 'type': 'market',
                 'amount': 0.5, 'stopPrice': 60000.0,
                 'info': {'type': 'STOP_MARKET', 'stopPrice': '60000.0'}}
    ex = StubExchange(normal=[manual_limit], stop=[manual_sl])
    t = make_trader(ex)
    blocked = types.MethodType(CHECK, t)(SYM, 'batch_new', {SYM: {}}, 'fp_x')
    crit = [m for lv, m in t._tg if lv == 'critical']
    ok = (blocked is True
          and ex.api_calls.count('cancel_order') == 0
          and len(crit) == 1 and '外部/手工' in crit[0]
          and '未撤销/修改任何订单' in crit[0])
    report('T1 手工/外部挂单 → 阻断 + 零撤单 + critical 告警', ok,
           f'(blocked={blocked}, cancel={ex.api_calls.count("cancel_order")}, crit_TG={len(crit)})')


# ── T2: 程序遗留单（intent 匹配）→ 阻断零撤单 + 区分标注 ─────────

def t2_program_orphan_block_no_cancel():
    CHECK = extract_fn('_check_existing_conflicts')
    orphan = {'id': 'X1', 'symbol': SYM, 'side': 'buy', 'type': 'market',
              'amount': 0.001, 'stopPrice': 75000.0,
              'info': {'type': 'STOP_MARKET', 'stopPrice': '75000.0'}}
    states = {SYM: {
        'batch_dead': {  # 已终结批次残留：registry intent 仍参与来源解释
            'is_active': False,
            'protection_registry': {
                'ident_sl0': {'state': 'PENDING_CREATE', 'intent': {
                    'symbol': SYM, 'side': 'buy', 'qty': 0.001,
                    'order_type': 'STOP_MARKET', 'stop_price': 75000.0,
                    'reduce_only': None}}},
        }}}
    ex = StubExchange(stop=[orphan])
    t = make_trader(ex)
    blocked = types.MethodType(CHECK, t)(SYM, 'batch_new', states, 'fp_x')
    crit = [m for lv, m in t._tg if lv == 'critical']
    ok = (blocked is True
          and ex.api_calls.count('cancel_order') == 0
          and len(crit) == 1 and '程序遗留' in crit[0] and 'batch_dead' in crit[0])
    report('T2 程序遗留单（intent 匹配）→ 阻断零撤单 + 区分标注来源批次', ok,
           f'(blocked={blocked}, cancel={ex.api_calls.count("cancel_order")}, crit_TG={len(crit)})')


# ── T3: known_order_ids 完整性（limit_close/registry 不误判孤儿）──

def t3_known_ids_complete_no_false_orphan():
    CHECK = extract_fn('_check_existing_conflicts')
    states = {SYM: {
        'batch_live': {
            'is_active': True,
            'entry_orders': ['E1'],
            'tp_order_id': 'TP1',
            'current_sl_id': 'SL1',
            'limit_close_order_id': 'LC1',   # 改动前漏此项 → 程序自己的 LIMIT 平仓单会被误判
            'protection_registry': {
                'ident_tp0': {'state': 'CONFIRMED', 'order_id': 'RG1',  # 改动前漏此项
                              'intent': {'symbol': SYM, 'side': 'sell', 'qty': 0.01,
                                         'order_type': 'TAKE_PROFIT_MARKET',
                                         'stop_price': 90000.0, 'reduce_only': True}}},
        }}}
    own_orders = [{'id': oid, 'symbol': SYM, 'side': 'sell', 'type': 'LIMIT',
                   'amount': 0.01, 'price': 1.0, 'stopPrice': None, 'info': {}}
                  for oid in ('E1', 'TP1', 'LC1', 'RG1')]
    own_stops = [{'id': 'SL1', 'symbol': SYM, 'side': 'sell', 'type': 'market',
                  'amount': 0.01, 'stopPrice': 50000.0, 'info': {}}]
    ex = StubExchange(normal=own_orders, stop=own_stops)
    t = make_trader(ex)
    blocked = types.MethodType(CHECK, t)(SYM, 'batch_new', states, 'fp_new')
    ok = (blocked is False and not t._tg
          and ex.api_calls.count('cancel_order') == 0
          and ex.api_calls.count('fetch_open_orders') == 2)  # 双通道扫描仍完整
    report('T3 known 集合完整：程序自有单（limit_close/registry）不误判孤儿', ok,
           f'(blocked={blocked}, TG={len(t._tg)}, 通道数={ex.api_calls.count("fetch_open_orders")})')


# ── T4/T5: 阶梯单调性 ────────────────────────────────────────────

def t4_buy_ladder_monotonic():
    V = extract_fn('_validate_stop_losses')
    t = StubTrader()
    entries = [(100.0, 0.1), (110.0, 0.1), (120.0, 0.1)]
    ok_pass, _ = V(t, Sig('BUY', entries, [90.0, 95.0, 100.0], 90.0), 99.0)
    ok_rej, msg_rej = V(t, Sig('BUY', entries, [90.0, 80.0, 100.0], 90.0), 99.0)
    ok_flat, _ = V(t, Sig('BUY', entries, [90.0, 90.0, 90.0], 90.0), 99.0)
    passed = (ok_pass is True
              and ok_rej is False and '倒退' in msg_rej
              and ok_flat is True)
    report('T4 BUY 阶梯：逐层不降放行（含相等）/ 倒退拒绝', passed,
           f'(递增={ok_pass}, 倒退拦截={ok_rej is False}, 持平={ok_flat})')


def t5_sell_ladder_monotonic():
    V = extract_fn('_validate_stop_losses')
    t = StubTrader()
    entries = [(100.0, 0.1), (90.0, 0.1), (80.0, 0.1)]
    ok_pass, _ = V(t, Sig('SELL', entries, [110.0, 105.0, 100.0], 110.0), 99.0)
    ok_rej, msg_rej = V(t, Sig('SELL', entries, [110.0, 120.0, 100.0], 110.0), 99.0)
    ok_flat, _ = V(t, Sig('SELL', entries, [110.0, 110.0, 100.0], 110.0), 99.0)
    passed = (ok_pass is True
              and ok_rej is False and '倒退' in msg_rej
              and ok_flat is True)
    report('T5 SELL 阶梯：逐层不升放行（含相等）/ 上升拒绝', passed,
           f'(递减={ok_pass}, 上升拦截={ok_rej is False}, 持平={ok_flat})')


# ── T6/T7: SG2 方向过滤 ─────────────────────────────────────────

def _sg2_trader(ex):
    t = StubTrader()
    t.exchange = ex
    t._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    t._batch_net_position = types.MethodType(extract_fn('_batch_net_position'), t)
    return t


def _mk_batch(side, sl_id):
    return {'is_active': True, 'side': side, 'target_amounts': [0.01],
            'filled_details': [50000.0], 'last_filled_count': 1,
            'current_sl_id': sl_id, 'realized_reduce_amount': 0.0}


def t6_sg2_side_filter():
    cov = extract_fn('_check_sl_coverage')
    states = {SYM: {'b_long': _mk_batch('BUY', 'SL1'), 'b_short': _mk_batch('SELL', 'SL2')}}
    # LONG 视角：交易所 LONG 仓 0.01、SL1 在条件单通道 → 放行（b_short 不计入台账，
    # 旧行为会把 0.02 台账 vs 0.01 持仓判为「台账>交易所」误拒）
    t = _sg2_trader(StubExchange(stop=[{'id': 'SL1'}]))
    ok_long, msg_long = types.MethodType(cov, t)(SYM, states, 0.01, 'BUY')
    # 反向对照：SHORT 视角只看 b_short，SL2 不在场 → 拒绝
    ok_short, msg_short = types.MethodType(cov, t)(SYM, states, 0.01, 'SELL')
    passed = (ok_long is True
              and ok_short is False and '缺少有效止损单' in msg_short)
    report('T6 SG2 方向过滤：双方向并存只累计同方向台账', passed,
           f'(LONG: ok={ok_long} msg={msg_long!r} | SHORT: ok={ok_short} msg={msg_short!r})')


def t7_sg2_side_required():
    cov = extract_fn('_check_sl_coverage')
    t = _sg2_trader(StubExchange())
    passed = False
    try:
        types.MethodType(cov, t)(SYM, {SYM: {}}, 0.0, None)
    except ValueError:
        passed = True
    report('T7 SG2 缺 side → ValueError（拒绝静默双方向求和）', passed)


# ── T8: 源码锚点（execute_signal 顺序 + 零撤单不变量）────────────

def t8_source_anchors():
    with open(SRC_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    seg_exec = seg_conf = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == 'execute_signal':
                seg_exec = ast.get_source_segment(src, node)
            elif node.name == '_check_existing_conflicts':
                seg_conf = ast.get_source_segment(src, node)
    i_detect = seg_exec.find('fapiPrivateGetPositionSideDual')
    i_pos = seg_exec.find('_get_current_position_amt')
    ok = (seg_exec.count('fapiPrivateGetPositionSideDual') == 1   # 下单段旧检测已删除
          and 0 < i_detect < i_pos                                 # 检测先于持仓查询
          and 'is_hedge_mode=is_hedge_mode' in seg_exec            # 持仓查询使用真实检测结果
          and '获取持仓模式状态失败' not in seg_exec                # 旧 Fail-Open 回退已移除
          and 'self.exchange.cancel_order' not in seg_conf          # 冲突检查零撤单
          and '_order_matches_intent' in seg_conf)                  # 遗留单分类在场
    report('T8 源码锚点：模式检测先于持仓查询 / 无默认单向回退 / 冲突检查零撤单', ok)


if __name__ == '__main__':
    for fn in (t1_manual_orders_block_no_cancel, t2_program_orphan_block_no_cancel,
               t3_known_ids_complete_no_false_orphan, t4_buy_ladder_monotonic,
               t5_sell_ladder_monotonic, t6_sg2_side_filter, t7_sg2_side_required,
               t8_source_anchors):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc(limit=2)
            report(fn.__name__, False, f'异常: {type(e).__name__}: {e}')
    failed = [n for n, p in RESULTS if not p]
    print(f"\n{'❌ 失败: ' + str(failed) if failed else f'✅ 全部 {len(RESULTS)} 项通过'}")
    sys.exit(1 if failed else 0)
