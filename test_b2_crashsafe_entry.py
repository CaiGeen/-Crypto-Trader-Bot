#!/usr/bin/env python3
"""B2-5 TDD：开仓循环崩溃安全前置落盘（规格 §5.6 + Case F）

核心规则（§5.6 修正规则）：信号接受后、进开仓循环**前**，先把批次骨架 + 全部 ENTRY 的
PENDING_CREATE 记录落盘（每层一条 identity，意图先落盘）；循环内逐单 T2c 更新
（create 成功 → PENDING_VERIFY + order_id + id_known=true）；循环结束完整批次状态落盘时
全部 ENTRY → CONFIRMED（六步 T4 业务 Commit）；跳过层（价格过滤/-2021）不残留 PENDING_CREATE。

identity 编号约定：ENTRY L{idx} 为 0 基（idx 为 signal.entries 的 enumerate 下标），
与既有 SL/TP 的 L0 约定一致（规格 §5.1 示例 L1 仅为示意，实现遵循代码库 0 基惯例）。

附带恢复护栏：recover_active_batches 遇到 registry 存在未决 ENTRY（PENDING_CREATE/
PENDING_VERIFY/NOT_CONFIRMED/HARD_LOCK）的骨架批次 → 保留证据待对账，不清理不接管
（旧行为会因 entry_orders=[] 且无持仓而自动清理 → 证据被毁，前置落盘失去意义）。
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
BATCH = 'batch_b2_5'
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
    """threading.Thread 替换：记录 kwargs，不真正启动（监控线程语义由 _start_monitoring 桩覆盖）"""

    def __init__(self, *a, **k):
        self.kwargs = k

    def start(self):
        pass


def make_fake(create_results=None):
    """MagicMock 基座 + 显式 stub execute_signal 前置依赖 + 真实 B2 helper 绑定。
    create_results：None=全部成功；list=逐层结果（dict 返回 / Exception 抛出）。"""
    fake = mock.MagicMock()
    fake.states = {}
    fake.events = []   # ('save'|'create', ...) 顺序日志
    fake.save_snapshots = []  # 每次 save 的 data 深拷贝（T1 检查骨架快照用）
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

    # B2 helper 真实绑定（MagicMock 陷阱：不绑定则属性自动 mock → 解包崩溃被吞）
    for name in ('_update_registry', '_protection_identity', '_build_intent',
                 '_order_matches_intent', '_assert_create_allowed', '_verify_order_created',
                 '_registry_has_unresolved_entries'):
        if hasattr(CryptoTrader, name):
            setattr(fake, name,
                    (lambda n: (lambda *a, **k: getattr(CryptoTrader, n)(fake, *a, **k)))(name))
    return fake


def run_signal(fake, sig=None):
    with mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        return CryptoTrader.execute_signal(fake, sig or FakeSignal())


def scenario_pre_persist_before_create():
    """T1: 前置落盘先于任何 create + 骨架含全部 ENTRY PENDING_CREATE（§5.6 意图先落盘）"""
    fake = make_fake()
    ret = run_signal(fake)
    creates = [i for i, e in enumerate(fake.events) if e[0] == 'create']
    saves = [i for i, e in enumerate(fake.events) if e[0] == 'save']
    first_create = creates[0] if creates else -1
    first_save = saves[0] if saves else -1
    ok1 = (first_save >= 0 and first_create >= 0 and first_save < first_create)
    report('T1/前置落盘先于create', ok1,
           f"(首次save@{first_save} 首次create@{first_create} → {fake.events[:6]})")

    reg = fake.states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    ids = [entry_identity(i) for i in range(3)]
    ok2 = all(i in reg for i in ids) and len(reg) == 3
    # 注意：断言对象是**骨架快照**（首次 save 的 data），而非最终 registry——
    # 全部成功场景循环结束会合并置 CONFIRMED（T6 验证），PENDING_CREATE 只在骨架阶段存在
    snap_reg = (fake.save_snapshots[0].get('protection_registry', {})
                if fake.save_snapshots else {})
    ok3 = all((snap_reg.get(i) or {}).get('state') == 'PENDING_CREATE'
              and (snap_reg.get(i) or {}).get('role') == 'ENTRY'
              and (snap_reg.get(i) or {}).get('order_kind') == 'conditional'
              and (snap_reg.get(i) or {}).get('id_known') is False
              and (snap_reg.get(i) or {}).get('layer') == idx
              and (snap_reg.get(i) or {}).get('side') == 'LONG'
              for idx, i in enumerate(ids))
    report('T1/骨架含全部ENTRY PENDING_CREATE', ok2 and ok3,
           f"(骨架快照keys={sorted(snap_reg.keys())}, 完成={ret is not None})")


def scenario_intent_snapshot():
    """T2: 每层 ENTRY 意图含参数快照（symbol/side/qty/order_type/stop_price）"""
    fake = make_fake()
    run_signal(fake)
    reg = fake.states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    intents = []
    for i in range(3):
        intent = reg.get(entry_identity(i), {}).get('intent') or {}
        intents.append(intent)
    ok = all(
        intents[i].get('symbol') == SYMBOL
        and str(intents[i].get('side', '')).lower() == 'buy'
        and intents[i].get('qty') == 0.01
        and str(intents[i].get('order_type', '')).upper() == 'STOP_MARKET'
        and intents[i].get('stop_price') == ENTRIES[i][0]
        for i in range(3)
    )
    report('T2/ENTRY意图参数快照', ok, f"(intents={intents})")


def scenario_crash_window():
    """T3: 第 3 层 create 时 SystemExit（模拟进程崩溃）→ registry 状态自描述：
    L0/L1=PENDING_VERIFY(order_id,id_known) L2=PENDING_CREATE（未尝试）"""
    fake = make_fake(create_results=[{'id': 'entry_1'}, {'id': 'entry_2'}, SystemExit('模拟崩溃')])
    crashed = False
    try:
        run_signal(fake)
    except SystemExit:
        crashed = True
    reg = fake.states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    l0 = reg.get(entry_identity(0), {})
    l1 = reg.get(entry_identity(1), {})
    l2 = reg.get(entry_identity(2), {})
    report('T3/SystemExit传播', crashed, '')
    report('T3/L0已创建→PENDING_VERIFY+order_id', l0.get('state') == 'PENDING_VERIFY'
           and l0.get('order_id') == 'entry_1' and l0.get('id_known') is True,
           f"(L0={l0})")
    report('T3/L1已创建→PENDING_VERIFY', l1.get('state') == 'PENDING_VERIFY'
           and l1.get('order_id') == 'entry_2', '')
    report('T3/L2未尝试→PENDING_CREATE', l2.get('state') == 'PENDING_CREATE'
           and l2.get('order_id') is None, f"(L2={l2})")


def scenario_skip_layer_not_persisted():
    """T4: 价格过滤跳过层（L1 触发价 45000 < 市价 50000）→ 不预挂、不残留 PENDING_CREATE；
    成功层 L0/L2 循环结束 → CONFIRMED"""
    entries = [(55000.0, 0.01), (45000.0, 0.01), (57000.0, 0.01)]
    fake = make_fake()
    run_signal(fake, FakeSignal(entries=entries))
    reg = fake.states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    ids = sorted(reg.keys())
    ok1 = ids == [entry_identity(0), entry_identity(2)]
    ok2 = reg.get(entry_identity(0), {}).get('state') == 'CONFIRMED'
    ok3 = reg.get(entry_identity(2), {}).get('state') == 'CONFIRMED'
    report('T4/跳过层不预挂不残留', ok1, f"(registry={ids})")
    report('T4/成功层CONFIRMED', ok2 and ok3,
           f"(L0={reg.get(entry_identity(0), {}).get('state')}, "
           f"L2={reg.get(entry_identity(2), {}).get('state')})")


def scenario_minus2021_absent():
    """T5: 第 2 层 create 抛 -2021（触发价不合规，ExchangeError 确定拒绝）→ 该层 ABSENT，
    其余层 CONFIRMED（-2021 是正常跳过，不残留 PENDING_CREATE 也不计数 FAILED）"""
    fake = make_fake(create_results=[{'id': 'entry_1'},
                                     ccxt.ExchangeError('-2021 触发价不符合逻辑'),
                                     {'id': 'entry_3'}])
    run_signal(fake)
    reg = fake.states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    l0 = reg.get(entry_identity(0), {})
    l1 = reg.get(entry_identity(1), {})
    l2 = reg.get(entry_identity(2), {})
    report('T5/-2021层→ABSENT', l1.get('state') == 'ABSENT', f"(L1={l1})")
    report('T5/其余层CONFIRMED', l0.get('state') == 'CONFIRMED' and l2.get('state') == 'CONFIRMED',
           f"(L0={l0.get('state')}, L2={l2.get('state')})")


def scenario_full_success():
    """T6: 全部成功 → 完整批次状态（entry_orders 3 条）+ 全部 ENTRY CONFIRMED（业务 Commit）"""
    fake = make_fake()
    ret = run_signal(fake)
    b = fake.states.get(SYMBOL, {}).get(BATCH, {})
    ok1 = len(b.get('entry_orders', [])) == 3 and len(b.get('target_amounts', [])) == 3
    reg = b.get('protection_registry', {})
    ok2 = all(reg.get(entry_identity(i), {}).get('state') == 'CONFIRMED' for i in range(3))
    report('T6/完整批次状态落盘', ok1 and ret == BATCH,
           f"(entry_orders={b.get('entry_orders')}, ret={ret})")
    report('T6/全部ENTRY CONFIRMED', ok2, f"(states={[reg.get(entry_identity(i), {}).get('state') for i in range(3)]})")


def _recovery_fake(states, cleared):
    fake = make_fake()
    fake.states = copy.deepcopy(states)
    fake.clear_batch_state = lambda s, b: (cleared.append(b), fake.states.get(s, {}).pop(b, None))
    return fake


def scenario_recovery_guard_keeps_skeleton():
    """T7: 恢复护栏——骨架批次（entry_orders=[] 但 registry 有未决 ENTRY PENDING_CREATE）
    不得被自动清理（保留证据待对账），也不得被接管（无 entry_orders 的监控无意义）"""
    states = {SYMBOL: {BATCH: {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': [], 'last_filled_count': 0,
        'protection_registry': {entry_identity(0): {'state': 'PENDING_CREATE', 'role': 'ENTRY'},
                                entry_identity(1): {'state': 'PENDING_VERIFY', 'role': 'ENTRY'}},
    }}}
    cleared = []
    fake = _recovery_fake(states, cleared)
    with mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        CryptoTrader.recover_active_batches(fake)
    kept = BATCH in fake.states.get(SYMBOL, {})
    report('T7/骨架批次保留待对账', BATCH not in cleared and kept,
           f"(cleared={cleared}, kept={kept})")


def scenario_recovery_guard_clears_terminal():
    """T8: 负向对照——registry 全部终态（CONFIRMED）+ entry_orders=[] → 照常清理（无回归）"""
    states = {SYMBOL: {BATCH: {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': [], 'last_filled_count': 0,
        'protection_registry': {entry_identity(0): {'state': 'CONFIRMED', 'role': 'ENTRY'},
                                entry_identity(1): {'state': 'ABSENT', 'role': 'ENTRY'}},
    }}}
    cleared = []
    fake = _recovery_fake(states, cleared)
    with mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        CryptoTrader.recover_active_batches(fake)
    report('T8/终态骨架批次照常清理', BATCH in cleared,
           f"(cleared={cleared})")


def main():
    print("=" * 60)
    print("B2-5 开仓循环崩溃安全前置落盘 TDD（§5.6 + Case F）")
    print("=" * 60)
    scenario_pre_persist_before_create()
    scenario_intent_snapshot()
    scenario_crash_window()
    scenario_skip_layer_not_persisted()
    scenario_minus2021_absent()
    scenario_full_success()
    scenario_recovery_guard_keeps_skeleton()
    scenario_recovery_guard_clears_terminal()
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
