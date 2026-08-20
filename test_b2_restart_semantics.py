#!/usr/bin/env python3
"""B2-7 TDD：测试方法论补强（ChatGPT-④）——真实 ccxt 异常对象 + registry 持久化 → 新实例重建 → 重启恢复语义

核心升级：区别于此前 MagicMock fake（直接构造内存 states），本套件用【真实 CryptoTrader 实例】+
【真实 trade_state.json 文件 I/O】（模块级 STATE_FILE 重定向到临时目录）模拟真实"崩溃+重启"：
  崩溃后磁盘遗留状态（write_state 直接落盘）→ 新实例（make_trader 全新构造，重新读同一文件）
  → 恢复路径 → 断言语义。不变量#15：重启不清零安全状态。

覆盖规格 §6 重启恢复表 + §5.4/§5.5 硬锁重启语义：
  R1 PENDING_VERIFY 连续重启幂等：N 次重启状态不变，闸门仍拒绝（禁降级为可重试；verify 网络异常维持原状态不 FAILED）
  R2 HARD_LOCK 重启不清零：FAILED fail_count=5 → 启动校验补置硬锁 → 再重启仍锁定；非法解锁回滚 / 合法解锁不干预
  R3 PENDING_CREATE 重启 → 真实文件身份匹配收编 CONFIRMED + entry_orders 重建（Case B/C/F 真实版）
  R4 恢复路径零副作用：零 create / 零 cancel（恢复总原则 §6）
  R5 FAILED 计数跨重启保留：fail_count 延续，<5 不锁可重试，≥5 补锁

真实 ccxt 异常对象（ccxt 层次：OrderNotFound extends ExchangeError；
NetworkError/RequestTimeout 为独立分支，均非 ExchangeError 子类）：
  fetch_order 抛 ccxt.NetworkError（R1）/ ccxt.OrderNotFound（R3 查询表缺省）
"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt
import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = 'BTCUSDT'
BATCH = 'batch_b2_7'
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


def sl_identity(layer=0):
    return f'{BATCH}|SL|L{layer}|LONG'


def intent_stop(price):
    return {'symbol': SYMBOL, 'side': 'buy', 'qty': 0.01, 'order_type': 'STOP_MARKET',
            'stop_price': price, 'reduce_only': None}


def open_order(oid, stop_price):
    return {'id': oid, 'symbol': SYMBOL, 'side': 'buy', 'type': 'STOP_MARKET',
            'amount': 0.01, 'stopPrice': stop_price}


def make_trader(tmp, configure=None):
    """全新真实 CryptoTrader 实例（=一次"重启"）。
    - STATE_FILE 模块级重定向（load_all_states/save_batch_state 引用模块全局，patch 作用域外会失效 → 直接赋值）
    - ccxt.binanceusdm → MagicMock（防 __init__ 联网；load_markets/fetch_time 等返回空值）
    - _daily_report_loop → 空函数（防后台线程干扰）
    - 构造后 _min_api_interval=0（加速 _safe_api_call）
    - send_tg_notification 实例级收集（_sent 列表）
    configure(ex) 供测试精确配置交易所行为。"""
    state_file = os.path.join(str(tmp), 'trade_state.json')
    trader_260725.STATE_FILE = state_file
    with mock.patch.object(CryptoTrader, '_daily_report_loop', lambda self: None):
        with mock.patch.object(trader_260725.ccxt, 'binanceusdm') as mk:
            ex = mock.MagicMock()
            ex.load_time_difference.return_value = None
            ex.load_markets.return_value = {}
            ex.fetch_time.return_value = 1234567890.0
            ex.fetch_positions.return_value = []
            ex.set_leverage = lambda *a, **k: None
            mk.return_value = ex
            t = CryptoTrader('k', 's')
    t._min_api_interval = 0
    if configure:
        configure(ex)
    t._sent = []
    t.send_tg_notification = lambda text, **k: t._sent.append(str(text))
    return t, ex


def write_state(tmp, data):
    """模拟崩溃后磁盘遗留状态：直接落盘 trade_state.json（不经过实例）"""
    path = os.path.join(str(tmp), 'trade_state.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def base_batch(reg):
    return {SYMBOL: {BATCH: {
        'is_active': True, 'batch_id': BATCH, 'symbol': SYMBOL, 'side': 'BUY',
        'entry_orders': [], 'last_filled_count': 0,
        'stop_steps': [], 'take_profit_price': 60000.0,
        'target_amounts': [], 'batch_total_amount': 0.0,
        'params_base': {'leverage': 50}, 'is_hedge_mode': False,
        'entry_layers': [0, 1, 2], 'entry_stop_steps': [54000.0, 55000.0, 56000.0],
        'protection_registry': reg,
    }}}


def scenario_r1_restart_idempotent(tmp):
    """R1：PENDING_VERIFY(id_known) 连续 5 次重启——状态不变（不变量#15）、闸门始终拒绝、
    verify 持续网络异常 → 维持 PENDING_VERIFY 不 FAILED 不计数（禁降级为可重试）"""
    ident = sl_identity(0)
    reg = {ident: {'state': 'PENDING_VERIFY', 'order_id': 'sl_1', 'id_known': True,
                   'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
                   'intent': intent_stop(54000.0), 'updated_at': time.time()}}
    write_state(tmp, base_batch(reg))
    all_ok = True
    details = []
    for i in range(5):
        t, ex = make_trader(tmp)
        ex.fetch_order.side_effect = ccxt.NetworkError(f'net down #{i}')  # 真实 ccxt 异常对象
        b = t.load_all_states()[SYMBOL][BATCH]
        e = b['protection_registry'][ident]
        st_ok = e.get('state') == 'PENDING_VERIFY' and e.get('order_id') == 'sl_1' \
            and e.get('id_known') is True and e.get('fail_count', 0) == 0
        allowed, reason = t._assert_create_allowed(SYMBOL, BATCH, ident)
        gate_ok = (not allowed) and ('PENDING_VERIFY' in reason)
        # 每轮同时跑一次自愈（模拟 verify 重查）：NetworkError → 维持原状态，不 FAILED
        t._recheck_registry_self_heal(SYMBOL, BATCH)
        b2 = t.load_all_states()[SYMBOL][BATCH]
        e2 = b2['protection_registry'][ident]
        heal_ok = e2.get('state') == 'PENDING_VERIFY' and e2.get('fail_count', 0) == 0
        if not (st_ok and gate_ok and heal_ok):
            all_ok = False
        details.append(f"#{i}:{e2.get('state')},fc={e2.get('fail_count', 0)}")
    report('R1/PENDING_VERIFY 连续5次重启幂等+闸门拒绝+自愈不降级', all_ok,
           f"({'; '.join(details)})")


def scenario_r2_hardlock_persist(tmp):
    """R2a/2b：FAILED fail_count=5 → 启动校验补置硬锁 → 再重启仍锁定；非法解锁回滚；合法解锁不干预"""
    ident = sl_identity(0)
    reg = {ident: {'state': 'FAILED', 'fail_count': 5, 'id_known': True,
                   'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
                   'intent': intent_stop(54000.0), 'updated_at': time.time()}}
    write_state(tmp, base_batch(reg))
    # 第一次重启：启动校验补置硬锁
    t1, _ = make_trader(tmp)
    rolled1, alerted1 = t1._validate_registry_locks_on_startup()
    disk1 = json.load(open(os.path.join(str(tmp), 'trade_state.json'), encoding='utf-8'))
    e1 = disk1[SYMBOL][BATCH]['protection_registry'][ident]
    ok1 = rolled1 == 1 and e1.get('state') == 'HARD_LOCK' and e1.get('hard_locked') is True
    report('R2a/FAILED≥5启动补置硬锁落盘', ok1,
           f"(rolled={rolled1}, state={e1.get('state')}, hard_locked={e1.get('hard_locked')})")
    # 第二次重启：仍锁定，闸门拒绝（reason 前缀 HARD_LOCK）
    t2, _ = make_trader(tmp)
    rolled2, _ = t2._validate_registry_locks_on_startup()
    allowed, reason = t2._assert_create_allowed(SYMBOL, BATCH, ident)
    ok2 = rolled2 == 0 and (not allowed) and reason.startswith('HARD_LOCK')
    report('R2b/再重启HARD_LOCK仍锁定+闸门拦截', ok2, f"(allowed={allowed}, reason={reason[:40]})")
    # 非法解锁（HARD_LOCK + hard_locked=false 无审计三字段）→ 回滚 + critical
    reg2 = {ident: {'state': 'HARD_LOCK', 'fail_count': 5, 'hard_locked': False,
                    'id_known': True, 'order_kind': 'conditional', 'role': 'SL',
                    'layer': 0, 'side': 'LONG', 'intent': intent_stop(54000.0),
                    'updated_at': time.time()}}
    write_state(tmp, base_batch(reg2))
    t3, _ = make_trader(tmp)
    rolled3, alerted3 = t3._validate_registry_locks_on_startup()
    disk3 = json.load(open(os.path.join(str(tmp), 'trade_state.json'), encoding='utf-8'))
    e3 = disk3[SYMBOL][BATCH]['protection_registry'][ident]
    ok3 = rolled3 == 1 and e3.get('hard_locked') is True and len(t3._sent) == 1
    report('R2c/非法解锁回滚+critical', ok3,
           f"(rolled={rolled3}, hard_locked={e3.get('hard_locked')}, alerts={len(t3._sent)})")
    # 合法解锁（有审计三字段）→ 不干预
    reg3 = {ident: {'state': 'PENDING_VERIFY', 'fail_count': 5, 'hard_locked': False,
                    'unlock_reason': '人工核实无单', 'unlock_time': time.time(),
                    'unlock_operator': 'user', 'id_known': True, 'order_kind': 'conditional',
                    'role': 'SL', 'layer': 0, 'side': 'LONG', 'intent': intent_stop(54000.0),
                    'updated_at': time.time()}}
    write_state(tmp, base_batch(reg3))
    t4, _ = make_trader(tmp)
    rolled4, _ = t4._validate_registry_locks_on_startup()
    report('R2d/合法解锁(审计三字段)不干预', rolled4 == 0, f"(rolled={rolled4})")


def scenario_r3_restart_heal(tmp):
    """R3+R4：骨架批次（L0/L1=PENDING_VERIFY+order_id，L2=PENDING_CREATE）真实文件重启 →
    完整恢复路径（recover_active_batches）→ L0/L1 身份匹配收编 CONFIRMED、L2 快照无单
    NOT_CONFIRMED（人工裁决）、entry_orders 重建接管、零 Create 零 Cancel"""
    reg = {
        entry_identity(0): {'state': 'PENDING_VERIFY', 'order_id': 'entry_1', 'id_known': True,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 0,
                            'side': 'LONG', 'intent': intent_stop(55000.0), 'updated_at': time.time()},
        entry_identity(1): {'state': 'PENDING_VERIFY', 'order_id': 'entry_2', 'id_known': True,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 1,
                            'side': 'LONG', 'intent': intent_stop(56000.0), 'updated_at': time.time()},
        entry_identity(2): {'state': 'PENDING_CREATE', 'id_known': False,
                            'order_kind': 'conditional', 'role': 'ENTRY', 'layer': 2,
                            'side': 'LONG', 'intent': intent_stop(57000.0), 'updated_at': time.time()},
    }
    write_state(tmp, base_batch(reg))
    creates, cancels = [], []

    def _fetch_open(symbol=None, params=None, **k):
        if params and params.get('stop'):
            return [open_order('entry_1', 55000.0), open_order('entry_2', 56000.0)]
        return []

    def _fetch_order(order_id, symbol=None, params=None, **k):
        if order_id in ('entry_1', 'entry_2'):
            return open_order(order_id, 55000.0 if order_id == 'entry_1' else 56000.0)
        raise ccxt.OrderNotFound(f'Order {order_id} not found')

    def _create(**kw):
        creates.append(kw.get('amount'))
        return {'id': f'new_{len(creates)}'}

    def _cancel(*a, **k):
        cancels.append(a)
        return {}

    t, ex = make_trader(tmp, configure=lambda e: None)
    ex.fetch_open_orders.side_effect = _fetch_open
    ex.fetch_order.side_effect = _fetch_order
    ex.create_order.side_effect = _create
    ex.cancel_order.side_effect = _cancel
    ex.fetch_positions.return_value = []
    t.recover_active_batches()
    b = t.load_all_states()[SYMBOL][BATCH]
    reg2 = b.get('protection_registry', {})
    ok1 = reg2.get(entry_identity(0), {}).get('state') == 'CONFIRMED' \
        and reg2.get(entry_identity(1), {}).get('state') == 'CONFIRMED'
    ok2 = reg2.get(entry_identity(2), {}).get('state') == 'NOT_CONFIRMED'
    ok3 = b.get('entry_orders') == ['entry_1', 'entry_2']
    ok4 = not creates and not cancels
    report('R3/重启身份匹配收编L0L1+L2人工裁决', ok1 and ok2,
           f"(L0={reg2.get(entry_identity(0), {}).get('state')}, "
           f"L1={reg2.get(entry_identity(1), {}).get('state')}, "
           f"L2={reg2.get(entry_identity(2), {}).get('state')})")
    report('R3/entry_orders重建接管', ok3, f"(entry_orders={b.get('entry_orders')})")
    report('R4/恢复路径零Create零Cancel', ok4, f"(creates={len(creates)}, cancels={len(cancels)})")


def scenario_r5_fail_count_persist(tmp):
    """R5：FAILED 计数跨重启保留——fail_count=2（<5）不锁、允许重试；新一次确定失败 → 3 →
    再重启仍 3 且可重试；fail_count 是重启不清零的持久化安全状态"""
    ident = sl_identity(0)
    reg = {ident: {'state': 'FAILED', 'fail_count': 2, 'id_known': True,
                   'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
                   'intent': intent_stop(54000.0), 'updated_at': time.time()}}
    write_state(tmp, base_batch(reg))
    t1, _ = make_trader(tmp)
    rolled1, _ = t1._validate_registry_locks_on_startup()
    allowed1, _ = t1._assert_create_allowed(SYMBOL, BATCH, ident)
    ok1 = rolled1 == 0 and allowed1  # <5 不锁且 FAILED 允许重试
    # 模拟一次新的确定失败（真实 ccxt.ExchangeError）
    t1._update_registry(SYMBOL, BATCH, ident, state='FAILED',
                        fail_count_incr=1)  # 真实落盘
    t2, _ = make_trader(tmp)  # 重启
    e2 = t2.load_all_states()[SYMBOL][BATCH]['protection_registry'][ident]
    allowed2, _ = t2._assert_create_allowed(SYMBOL, BATCH, ident)
    ok2 = e2.get('fail_count') == 3 and e2.get('state') == 'FAILED' and allowed2
    report('R5/FAILED计数跨重启保留且可重试', ok1 and ok2,
           f"(rolled1={rolled1}, allowed1={allowed1}, fc_after_restart={e2.get('fail_count')}, "
           f"allowed2={allowed2})")


def main():
    print("=" * 60)
    print("B2-7 重启恢复语义（真实实例 + 真实文件持久化，ChatGPT-④）")
    print("=" * 60)
    tmp = tempfile.mkdtemp(prefix='b2_7_restart_')
    try:
        scenario_r1_restart_idempotent(tmp)
        scenario_r2_hardlock_persist(tmp)
        scenario_r3_restart_heal(tmp)
        scenario_r5_fail_count_persist(tmp)
    finally:
        trader_260725.STATE_FILE = 'trade_state.json'
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
