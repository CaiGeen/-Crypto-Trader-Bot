#!/usr/bin/env python3
"""B2-6 TDD：Case A-F crash injection 测试矩阵（规格 §7 + §6 重启恢复表）

六场景三件套断言：
  ① 恢复后同一 identity 未发生第二次真实 Create（mock create 计数 = 首次或 0）
  ② 恢复路径零副作用（零 create / 零 cancel / 不改交易参数）
  ③ 状态与告警符合转移表（§6 重启恢复表）

模拟"崩溃+重启"：直接构造崩溃后的持久化 states（fake.states 即 trade_state.json），
用新 fake 实例 + 恢复 helper（_recheck_registry_self_heal / _self_heal_no_id /
recover_active_batches）验证恢复行为。

身份签名匹配（§6.3）：intent 即签名（B2-2 已落盘 6 字段），快照两通道
（normal + conditional 合并）逐单 _order_matches_intent 比对：
  命中唯一 → CONFIRMED + order_id 收编（SL→current_sl_id / TP→tp_order_id / ENTRY→重建 entry_orders）
  命中多条 → NOT_CONFIRMED + critical（人工裁决）
  未命中（快照 VALID）→ NOT_CONFIRMED（缺席≠从未存在：单可能已触发终结）
  快照 INVALID → 维持 PENDING_VERIFY（结果未知，静默下轮）
"""
import copy
import os
import sys
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt
import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = 'BTCUSDT'
BATCH = 'batch_b2_6'
MARKET = 50000.0
ENTRIES = [(55000.0, 0.01), (56000.0, 0.01), (57000.0, 0.01)]
SLS = [54000.0, 55000.0, 56000.0]
TAKE_PROFIT = 60000.0
PASS, FAIL = 0, 0
RESULTS = []


def report(name, passed, detail=''):
    global PASS, FAIL
    if passed:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((passed, name, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")


def entry_identity(layer):
    return f'{BATCH}|ENTRY|L{layer}|LONG'


def intent(i):
    """与 B2-5 骨架 _build_intent 一致的意图指纹（即 §6.3 签名）"""
    return {'symbol': SYMBOL, 'side': 'buy', 'qty': 0.01, 'order_type': 'STOP_MARKET',
            'stop_price': ENTRIES[i][0], 'reduce_only': None}


def open_order(oid, stop_price, side='buy', amount=0.01):
    return {'id': oid, 'symbol': SYMBOL, 'side': side, 'type': 'STOP_MARKET',
            'amount': amount, 'stopPrice': stop_price}


class FakeSignal:
    def __init__(self, entries=ENTRIES, stop_loss_steps=SLS):
        self.symbol = SYMBOL
        self.batch_id = BATCH
        self.side = 'BUY'
        self.leverage = 50
        self.entries = entries
        self.stop_loss_steps = stop_loss_steps
        self.take_profit = TAKE_PROFIT


class FakeThread:
    def __init__(self, *a, **k):
        self.kwargs = k

    def start(self):
        pass


def make_fake(create_results=None, open_normal=None, open_conditional=None,
              orders=None, fetch_behavior='normal'):
    """MagicMock 基座 + 显式 stub + 真实 B2 helper 绑定。
    open_normal/open_conditional：两通道 open orders 快照（None → 抛 NetworkError = INVALID）
    orders：fetch_order 查询表 {id: order}；fetch_behavior='network' → fetch_order 抛 NetworkError
    create_results：None=全部成功；list=逐层结果（dict / 抛异常）"""
    fake = mock.MagicMock()
    fake.states = {}
    fake.events = []
    fake.save_snapshots = []
    fake.sent = []
    fake._ready = True
    fake._api_cooldown_until = 0
    fake._create_n = 0

    def _load():
        return copy.deepcopy(fake.states)

    def _save(symbol, batch_id, data):
        fake.events.append(('save', batch_id))
        fake.save_snapshots.append(copy.deepcopy(data))
        fake.states.setdefault(symbol, {})[batch_id] = copy.deepcopy(data)

    fake.load_all_states = _load
    fake.save_batch_state = _save
    fake.clear_batch_state = lambda s, b: fake.states.get(s, {}).pop(b, None)
    fake._check_existing_conflicts = lambda s, b, all_states: False
    fake._get_current_position_amt = lambda s, is_hedge_mode=False, side=None: 0.0
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake._validate_stop_losses = lambda signal, price: (True, '止损校验通过')
    # R1（ChatGPT 终审 2026-08-20）：execute_signal 新增止盈价方向校验，需显式桩
    #（MagicMock 属性自动 mock → `tp_is_valid, tp_msg = ...` 解包抛异常 → execute_signal 首 save 前中断）
    fake._validate_take_profit = lambda signal, price: (True, '止盈校验通过')
    # D-006（2026-08-28）：execute_signal 新增账户风控闸门——绑定真实实现三件套
    #（MagicMock 未绑定 helper → 解包/比较假回归坑，3 次实证；必须显式绑定）
    fake._check_account_risk = lambda st, sig, stats_file=None: CryptoTrader._check_account_risk(fake, st, sig, stats_file)
    fake._count_active_batches = lambda st: CryptoTrader._count_active_batches(fake, st)
    fake._get_today_realized_pnl = lambda stats_file=None: CryptoTrader._get_today_realized_pnl(fake, stats_file)
    fake.send_tg_notification = lambda text, **k: fake.sent.append((k.get('level', 'info'), str(text)))
    fake._start_monitoring = lambda *a, **k: None

    ex = mock.MagicMock()
    ex.set_leverage = lambda l, s: None
    ex.fetch_ticker = lambda s=None: {'last': MARKET, 'close': MARKET}
    ex.fapiPrivateGetPositionSideDual = lambda: {}
    ex.amount_to_precision = lambda s, v: v
    ex.price_to_precision = lambda s, v: v
    ex.fetch_balance = lambda: {'USDT': {'free': 10000.0}}
    ex.load_time_difference = lambda: None
    ex.fetch_time = lambda: 1234567890.0
    ex.fetch_positions = lambda s=None: []

    # 两通道 open orders 快照
    def _fetch_open(symbol=None, params=None, **k):
        src = open_conditional if (params or {}).get('stop') else open_normal
        if src is None:
            raise ccxt.NetworkError('模拟快照通道失败')
        return copy.deepcopy(src)
    ex.fetch_open_orders = _fetch_open

    # fetch_order 查询表（verify 自愈用）
    def _fetch_order(order_id, symbol=None, params=None, **k):
        if fetch_behavior == 'network':
            raise ccxt.NetworkError('模拟 verify 网络异常')
        if orders is not None and order_id in orders:
            return copy.deepcopy(orders[order_id])
        raise ccxt.OrderNotFound(f'Order {order_id} not found')
    ex.fetch_order = _fetch_order

    if create_results is None:
        def _create_ok(**kw):
            fake.events.append(('create', kw.get('amount')))
            fake._create_n += 1
            return {'id': f'entry_{fake._create_n}'}
        ex.create_order = _create_ok
    else:
        seq = iter(create_results)

        def _create_seq(**kw):
            fake.events.append(('create', kw.get('amount')))
            item = next(seq)
            if isinstance(item, BaseException):
                raise item
            return item
        ex.create_order = _create_seq
    fake.exchange = ex

    for name in ('_update_registry', '_protection_identity', '_build_intent',
                 '_order_matches_intent', '_assert_create_allowed', '_verify_order_created',
                 '_registry_has_unresolved_entries', '_self_heal_no_id',
                 '_recheck_registry_self_heal', '_verify_and_update_registry',
                 '_rebuild_entry_orders_from_registry'):
        if hasattr(CryptoTrader, name):
            setattr(fake, name,
                    (lambda n: (lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k)))(name))
    return fake


def run_signal(fake, sig=None):
    with mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        return CryptoTrader.execute_signal(fake, sig or FakeSignal())


def _skeleton_state(entry_orders=None, reg=None):
    """B2-5 崩溃后骨架批次（entry_orders 空 + registry 未决）"""
    return {SYMBOL: {BATCH: {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': entry_orders or [], 'last_filled_count': 0,
        'stop_steps': [], 'take_profit_price': TAKE_PROFIT,
        'target_amounts': [], 'batch_total_amount': 0.0,
        'params_base': {'leverage': 50}, 'is_hedge_mode': False,
        'entry_layers': [0, 1, 2], 'entry_stop_steps': list(SLS),
        'protection_registry': reg or {},
    }}}


def _pending_create_reg():
    """全层 PENDING_CREATE（Case B/C：T0 已落盘，create 未确认）"""
    reg = {}
    for i in range(3):
        reg[entry_identity(i)] = {
            'state': 'PENDING_CREATE', 'id_known': False, 'order_kind': 'conditional',
            'role': 'ENTRY', 'layer': i, 'side': 'LONG', 'intent': intent(i),
            'updated_at': time.time(),
        }
    return reg


def scenario_a_clean_create():
    """Case A：T0 落盘前崩溃 → 无记录 → 重启后可正常 Create（干净态）"""
    fake = make_fake()
    ret = run_signal(fake)
    b = fake.states.get(SYMBOL, {}).get(BATCH, {})
    creates = [e for e in fake.events if e[0] == 'create']
    reg = b.get('protection_registry', {})
    ok1 = ret == BATCH and len(b.get('entry_orders', [])) == 3 and len(creates) == 3
    ok2 = all(reg.get(entry_identity(i), {}).get('state') == 'CONFIRMED' for i in range(3))
    report('A/干净态正常Create', ok1, f"(entry_orders={b.get('entry_orders')}, creates={len(creates)})")
    report('A/全部ENTRY CONFIRMED', ok2,
           f"(states={[reg.get(entry_identity(i), {}).get('state') for i in range(3)]})")


def scenario_b1_heal_found():
    """Case B1：PENDING_CREATE + 快照有唯一匹配单 → CONFIRMED + order_id 收编，零 Create"""
    fake = make_fake(open_normal=[], open_conditional=[open_order('ex_0', 55000.0)])
    fake.states = _skeleton_state(reg=_pending_create_reg())
    CryptoTrader._self_heal_no_id(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    e0 = reg.get(entry_identity(0), {})
    creates = [e for e in fake.events if e[0] == 'create']
    report('B1/快照唯一匹配→CONFIRMED+收编', e0.get('state') == 'CONFIRMED'
           and e0.get('order_id') == 'ex_0' and e0.get('id_known') is True,
           f"(L0={e0})")
    report('B1/零Create零副作用', not creates, f"(creates={len(creates)})")


def scenario_b2_heal_absent():
    """Case B2：PENDING_CREATE + 快照 VALID 无单 → NOT_CONFIRMED（缺席≠从未存在）"""
    fake = make_fake(open_normal=[], open_conditional=[])
    fake.states = _skeleton_state(reg=_pending_create_reg())
    CryptoTrader._self_heal_no_id(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    states = [reg.get(entry_identity(i), {}).get('state') for i in range(3)]
    creates = [e for e in fake.events if e[0] == 'create']
    report('B2/快照VALID无单→NOT_CONFIRMED', all(s == 'NOT_CONFIRMED' for s in states),
           f"(states={states})")
    report('B2/零Create', not creates, '')


def scenario_b3_heal_invalid():
    """Case B3：快照 INVALID（normal 通道失败）→ 维持 PENDING_VERIFY(id_unknown)，不误判无单"""
    fake = make_fake(open_normal=None, open_conditional=[])
    fake.states = _skeleton_state(reg=_pending_create_reg())
    CryptoTrader._self_heal_no_id(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    states = [reg.get(entry_identity(i), {}).get('state') for i in range(3)]
    id_knowns = [reg.get(entry_identity(i), {}).get('id_known') for i in range(3)]
    creates = [e for e in fake.events if e[0] == 'create']
    report('B3/快照INVALID→维持PENDING_VERIFY(id_unknown)',
           all(s == 'PENDING_VERIFY' for s in states) and all(k is False for k in id_knowns),
           f"(states={states}, id_known={id_knowns})")
    report('B3/零Create零告警', not creates and not fake.sent, f"(creates={len(creates)}, alerts={len(fake.sent)})")


def scenario_c_orphan_guard():
    """Case C：T2c 返回 ID 后落盘前崩溃 → 本地 PENDING_CREATE + 交易所已有真实单
    → 身份匹配收编 CONFIRMED（24 孤儿单事故通用防线：绝不二次 Create）"""
    fake = make_fake(open_normal=[], open_conditional=[open_order('ex_c', 55000.0)])
    fake.states = _skeleton_state(reg=_pending_create_reg())
    CryptoTrader._self_heal_no_id(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    e0 = reg.get(entry_identity(0), {})
    creates = [e for e in fake.events if e[0] == 'create']
    report('C/真实单收编CONFIRMED+order_id', e0.get('state') == 'CONFIRMED'
           and e0.get('order_id') == 'ex_c', f"(L0={e0.get('state')}, oid={e0.get('order_id')})")
    report('C/零二次Create（孤儿防线）', not creates, f"(creates={len(creates)})")


def _pending_verify_reg(order_id, layer=0):
    """PENDING_VERIFY(id_known=True) + order_id（Case D/E：T2c 已落盘）"""
    i = layer
    return {entry_identity(i): {
        'state': 'PENDING_VERIFY', 'order_id': order_id, 'id_known': True,
        'order_kind': 'conditional', 'role': 'ENTRY', 'layer': i, 'side': 'LONG',
        'intent': intent(i), 'updated_at': time.time(),
    }}


def scenario_d_verify_heal():
    """Case D：PENDING_VERIFY(id_known) → 重启 verify 自愈 → CONFIRMED"""
    fake = make_fake(open_normal=[], open_conditional=[],
                     orders={'ex_d': open_order('ex_d', 55000.0)})
    fake.states = _skeleton_state(reg=_pending_verify_reg('ex_d', 0))
    CryptoTrader._recheck_registry_self_heal(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    e0 = reg.get(entry_identity(0), {})
    creates = [e for e in fake.events if e[0] == 'create']
    report('D/verify自愈→CONFIRMED', e0.get('state') == 'CONFIRMED'
           and e0.get('order_id') == 'ex_d', f"(L0={e0.get('state')})")
    report('D/零Create', not creates, '')


def scenario_e_verify_exceptions():
    """Case E：verify 查询异常分类——OrderNotFound→NOT_CONFIRMED；NetworkError→维持 PENDING_VERIFY（均不 FAILED 不计数）"""
    fake = make_fake(open_normal=[], open_conditional=[], orders={})
    fake.states = _skeleton_state(reg=_pending_verify_reg('ex_e', 0))
    CryptoTrader._recheck_registry_self_heal(fake, SYMBOL, BATCH)
    reg = fake.states[SYMBOL][BATCH]['protection_registry']
    e0 = reg.get(entry_identity(0), {})
    creates = [e for e in fake.events if e[0] == 'create']
    report('E1/OrderNotFound→NOT_CONFIRMED', e0.get('state') == 'NOT_CONFIRMED', f"(L0={e0.get('state')})")
    report('E1/零Create零计数', not creates and e0.get('fail_count', 0) == 0, '')

    fake2 = make_fake(open_normal=[], open_conditional=[], orders={}, fetch_behavior='network')
    fake2.states = _skeleton_state(reg=_pending_verify_reg('ex_e2', 0))
    CryptoTrader._recheck_registry_self_heal(fake2, SYMBOL, BATCH)
    reg2 = fake2.states[SYMBOL][BATCH]['protection_registry']
    e2 = reg2.get(entry_identity(0), {})
    creates2 = [e for e in fake2.events if e[0] == 'create']
    report('E2/NetworkError→维持PENDING_VERIFY', e2.get('state') == 'PENDING_VERIFY',
           f"(L0={e2.get('state')})")
    report('E2/零Create零计数', not creates2 and e2.get('fail_count', 0) == 0, '')


def scenario_f_crash_mid_loop():
    """Case F：开仓循环第 3 层崩溃 → 骨架（L0/L1=PENDING_VERIFY+order_id，L2=PENDING_CREATE）
    → 恢复：L0/L1 身份匹配收编 CONFIRMED（重建 entry_orders）、L2 快照无单 → NOT_CONFIRMED（缺失层人工裁决）；
    禁止补挂任何层（零 Create）"""
    reg = {
        entry_identity(0): {'state': 'PENDING_VERIFY', 'order_id': 'entry_1', 'id_known': True,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 0, 'side': 'LONG',
                            'intent': intent(0), 'updated_at': time.time()},
        entry_identity(1): {'state': 'PENDING_VERIFY', 'order_id': 'entry_2', 'id_known': True,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 1, 'side': 'LONG',
                            'intent': intent(1), 'updated_at': time.time()},
        entry_identity(2): {'state': 'PENDING_CREATE', 'id_known': False,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 2, 'side': 'LONG',
                            'intent': intent(2), 'updated_at': time.time()},
    }
    fake = make_fake(open_normal=[], open_conditional=[open_order('entry_1', 55000.0),
                                                       open_order('entry_2', 56000.0)],
                     orders={'entry_1': open_order('entry_1', 55000.0),
                             'entry_2': open_order('entry_2', 56000.0)})
    fake.states = _skeleton_state(reg=reg)
    # 完整恢复路径：recover_active_batches（护栏分支 → 身份匹配 → 收编重建 → 接管）
    with mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        CryptoTrader.recover_active_batches(fake)
    b = fake.states[SYMBOL][BATCH]
    reg2 = b.get('protection_registry', {})
    creates = [e for e in fake.events if e[0] == 'create']
    report('F/L0L1身份匹配收编CONFIRMED', reg2.get(entry_identity(0), {}).get('state') == 'CONFIRMED'
           and reg2.get(entry_identity(1), {}).get('state') == 'CONFIRMED',
           f"(L0={reg2.get(entry_identity(0), {}).get('state')}, L1={reg2.get(entry_identity(1), {}).get('state')})")
    report('F/L2缺失层NOT_CONFIRMED(人工裁决)', reg2.get(entry_identity(2), {}).get('state') == 'NOT_CONFIRMED',
           f"(L2={reg2.get(entry_identity(2), {}).get('state')})")
    report('F/entry_orders重建接管', b.get('entry_orders') == ['entry_1', 'entry_2'],
           f"(entry_orders={b.get('entry_orders')})")
    report('F/零补挂任何层(禁二次Create)', not creates, f"(creates={len(creates)})")


def main():
    print("=" * 60)
    print("B2-6 Case A-F crash injection 测试矩阵（§7 + §6 重启恢复表）")
    print("=" * 60)
    scenario_a_clean_create()
    scenario_b1_heal_found()
    scenario_b2_heal_absent()
    scenario_b3_heal_invalid()
    scenario_c_orphan_guard()
    scenario_d_verify_heal()
    scenario_e_verify_exceptions()
    scenario_f_crash_mid_loop()
    print("=" * 60)
    print(f"✅ PASS {PASS}  ❌ FAIL {FAIL}")
    if FAIL:
        for p, n, d in RESULTS:
            if not p:
                print(f"  ❌ {n} {d}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
