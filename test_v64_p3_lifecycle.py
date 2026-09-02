# -*- coding: utf-8 -*-
"""v6.4-P3 决定性测试（R1-R6/R5b，RED-first，ChatGPT 冻结规格）——monitor 生命周期守卫。

事故（2026-09-02 18:45）：/auth_reset 对账清理批次后，旧监控线程（内存生命周期仍 ACTIVE）
进入僵尸循环——对已墓碑批次反复发假结算报告（均价 0.00/数量 gross 0.002/假盈亏 +152.85）、
反复收敛失败、疑似重挂幽灵保护单。根因：磁盘生命周期 CLOSED 后，内存线程未失去资格。

冻结规格（ChatGPT 终审，两轮收敛）：
- G1/G2/G3 三态生命周期守卫 `_monitor_lifecycle_check`：
    账本可信 + active → 'ok'；账本可信 + missing/inactive → 'exit'；
    _state_corrupted → 'unknown'（UNKNOWN ≠ EMPTY，绝不解释为「已清理」）。
  G1=每轮苏醒后、任何 API 前；G2=持仓归零分支加载后、结算/撤单/converge 前；
  G3=保护单维护重载点。
- settlement 原子认领 `_claim_settlement_reported`（_state_lock 内 CAS）：
  锁内重读 batch 仍 active + 未被认领 → 写 settlement_reported=True + durable persist
  → 唯一 owner 才发结算报告；persist 失败/已认领/批次消失 → 不发送（at-most-once）。
- 不新增线程停止抽象（_active_monitors.discard ≠ stop，Fix② 已撤回）。

全部为运行行为测试：真跑 `_start_monitoring` 线程 + 全端点计数桩。
"""
import copy
import threading
import time
import traceback
import types
from collections import Counter

import test_v64_partial_close as H

SYM = H.SYM

# 提取 _start_monitoring 所需的模块级名字（异常类/常量 shim——测试桩不会走到其真实分支）
class _DummyAuthBlocked(Exception):
    pass
H.NS.setdefault('AuthBlockedError', _DummyAuthBlocked)
H.NS.setdefault('AUTH_BLIND_SLEEP_SECONDS', 300.0)
class _CCXTShim:
    class OrderNotFound(Exception):
        pass
H.NS.setdefault('ccxt', _CCXTShim)


class _FakeTime:
    """可控 sleep（每代握手）：hold_now() 生成新 release 事件并挂起线程，
    main 收到 _blocked 即可确信线程被冻结在 sleep 内（不可能产生任何 API）；
    release() 唤醒本代。下一轮 hold_now 重新生成事件，避免旧 set 状态破坏握手。"""

    def __init__(self):
        self.hold = False
        self._release = threading.Event()
        self._blocked = threading.Event()
        self.entered = 0

    def time(self):
        return time.time()

    def sleep(self, s):
        self.entered += 1
        if self.hold:
            self._blocked.set()
            self._release.wait(5)
            return
        time.sleep(min(s, 0.05))  # 保留真实节流，防测试忙循环刷爆输出

    def hold_now(self):
        self._release = threading.Event()  # 本代新事件（旧 set 状态作废）
        self.hold = True
        return self._blocked

    def release(self):
        self.hold = False
        self._release.set()

    def reset(self):
        self.hold = False
        self._release = threading.Event()
        self._blocked = threading.Event()
        self.entered = 0


_H_FAKE_TIME = _FakeTime()
H.NS['time'] = _H_FAKE_TIME


def _bind_fn(t, fn):
    t.__dict__[fn.__name__] = types.MethodType(fn, t)


# ── 可计数交易所桩 ──────────────────────────────────────────────────────────
class CountingExchange(H.StubExchange):
    """全端点计数 + 可编程：fetch_positions 钩子（模拟并发 clear）/ 持仓值 / fetch_order 结果。"""

    def __init__(self, on_fetch_positions=None, fetch_order_result='open'):
        super().__init__()
        self.api = Counter()
        self.on_fetch_positions = on_fetch_positions
        self.fetch_order_result = fetch_order_result
        self.position_getter = lambda: 0.0

    def fetch_positions(self, symbols=None, params=None, **k):
        self.api['fetch_positions'] += 1
        if self.on_fetch_positions:
            self.on_fetch_positions()
        v = self.position_getter()
        if v:
            return [{'symbol': SYM, 'side': 'long', 'contracts': v,
                     'info': {'symbol': 'BTCUSDT'}}]
        return []

    def fetch_open_orders(self, symbol, params=None, **k):
        self.api['fetch_open_orders'] += 1
        return []

    def fetch_order(self, order_id, symbol, params=None, **k):
        self.api['fetch_order'] += 1
        return {'id': str(order_id), 'status': self.fetch_order_result,
                'stopPrice': 75001.0, 'amount': 0.002}

    def fetch_ticker(self, symbol):
        self.api['fetch_ticker'] += 1
        return {'last': 76500.0, 'close': 76500.0}

    def cancel_order(self, order_id, symbol, params=None, **k):
        self.api['cancel_order'] += 1
        raise Exception('binanceusdm -2011 Unknown order sent')

    def create_order(self, symbol, otype, side, amount, price=None, params=None, **k):
        self.api['create_order'] += 1
        oid = self._gen()
        self.orders[oid] = {'id': oid, 'status': 'open', 'filled': 0.0,
                            'amount': float(amount), 'stopPrice': 75001.0, 'type': otype}
        return dict(self.orders[oid])


class AutoStubTrader(H.StubTrader):
    """未显式 stub 的 trader 方法 → 计数 no-op（防 AttributeError 干扰行为断言）。"""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        counter = self.__dict__.setdefault('_auto_stub_calls', Counter())

        def _noop(*a, **k):
            counter[name] += 1
            return None
        self.__dict__[name] = _noop
        return _noop


def _disk_batch(**extra):
    """磁盘上的正常 active 批次（1 层 0.002 已成交，SL/TP 在账）。"""
    b = {
        'is_active': True, 'batch_id': 'batch_A', 'symbol': SYM, 'side': 'BUY',
        'is_hedge_mode': True, 'params_base': {'positionSide': 'LONG', 'leverage': 100},
        'target_amounts': [0.002], 'filled_details': [76690.0],
        'last_filled_count': 1, 'total_entry_fee': 0.15,
        'entry_orders': ['E1'], 'current_sl_id': 'S1', 'tp_order_id': 'T1',
        'close_phase': 0, 'pending_close': False, 'is_programmatic_cancel': False,
        'close_reason': '', 'close_op_id': '',
        'stop_steps': [75001.0], 'take_profit_price': 80000.0,
        'batch_total_amount': 0.002, 'settlement_reported': False,
        'protection_registry': {
            'batch_A|SL|L0|LONG': {'role': 'SL', 'state': 'CONFIRMED', 'order_id': 'S1',
                                   'layer': 0, 'side': 'LONG'},
            'batch_A|TP|L0|LONG': {'role': 'TP', 'state': 'CONFIRMED', 'order_id': 'T1',
                                   'layer': 0, 'side': 'LONG'},
        },
    }
    b.update(extra)
    return b


def _make_runner(states, position=0.002, fetch_order_result='open',
                 on_fetch_positions=None, converge_result=None):
    """构建可真跑 _start_monitoring 的 stub trader，返回 (t, controls)。"""
    t = AutoStubTrader()
    t.exchange = CountingExchange(on_fetch_positions=on_fetch_positions,
                                  fetch_order_result=fetch_order_result)
    t._states = states
    t._force_corrupt = False
    t._lock = threading.RLock()
    t._state_lock = t._lock
    t._active_monitors = set()
    t._active_monitors_lock = threading.Lock()
    t._criticals = []
    t.tg_msgs = []
    t.converge_calls = Counter()
    t.clear_calls = Counter()
    t.sync_calls = Counter()
    t.converge_result = converge_result  # None = UNKNOWN（未收敛）
    t._position_value = position

    def load_all_states():
        if t._force_corrupt:
            t._state_corrupted = True
            return {}
        t._state_corrupted = False
        return copy.deepcopy(t._states)

    def _persist_states(all_states):
        t._states.clear()
        t._states.update(all_states)
        return True

    def save_batch_state(symbol, batch_id, data):
        t._states.setdefault(symbol, {})[batch_id] = copy.deepcopy(data)
        return True

    def send_tg_notification(msg, level='info'):
        t.tg_msgs.append((level, str(msg)))

    def _calculate_monitoring_interval():
        return 0.05

    def _sync_time_if_needed():
        t.sync_calls['sync'] += 1

    def _converge_batch_orders_before_clear(symbol, batch_id):
        t.converge_calls['converge'] += 1
        return t.converge_result

    def clear_batch_state(symbol, batch_id, proof=None):
        t.clear_calls['clear'] += 1
        if t.clear_result:
            t._states.get(symbol, {}).pop(batch_id, None)
        return True

    def _safe_api_call(fn, *a, **k):
        return fn(*a, **k)

    t._safe_api_call = _safe_api_call
    # 真实 save_batch_state / _merge_batch_state（R5c 需生产级 merge 棘轮语义）
    _sb = H.ex_t('save_batch_state')
    _mb = H.ex_t('_merge_batch_state')
    if _sb is not None and _mb is not None:
        _bind_fn(t, _sb)
        _bind_fn(t, _mb)
        t._load_tombstones = lambda: {}
    t.load_all_states = load_all_states
    t._persist_states = _persist_states
    # save_batch_state 用上面绑定的真实现（含 _merge_batch_state 棘轮语义，R5c 依赖）
    t.send_tg_notification = send_tg_notification
    t._calculate_monitoring_interval = _calculate_monitoring_interval
    t._sync_time_if_needed = _sync_time_if_needed
    t._converge_batch_orders_before_clear = _converge_batch_orders_before_clear
    t.clear_batch_state = clear_batch_state
    t.registry_self_heal_interval = 9999.0
    t.last_ip_check_time = time.time()
    t.IP_CHECK_INTERVAL = 99999.0

    for name, fn in H.BINDS.items():
        _bind_fn(t, fn)
    # 真实持仓读取（经 exchange.fetch_positions → 计数生效）
    pos_fn = H.ex_t('_get_current_position_amt')
    if pos_fn is not None:
        _bind_fn(t, pos_fn)
    t.exchange.position_getter = lambda: t._position_value
    # 新守卫/认领 helper（RED 阶段尚不存在 → 交给 AutoStubTrader 兜底）
    for name in ('_monitor_lifecycle_check', '_claim_settlement_reported'):
        fn = H.ex_t(name)
        if fn is not None:
            _bind_fn(t, fn)
    return t


def _start(t, **kw):
    args = dict(entry_orders=['E1'], stop_steps=[75001.0], take_profit_price=80000.0,
                current_sl_id='S1', tp_order_id='T1', batch_total_amount=0.002,
                target_amounts=[0.002], params_base={'positionSide': 'LONG', 'leverage': 100},
                is_hedge_mode=True, side='BUY', last_filled_count=1,
                filled_details=[76690.0], total_entry_fee=0.15)
    args.update(kw)
    th = threading.Thread(target=t._start_monitoring,
                          args=(SYM, 'batch_A'), kwargs=args, daemon=True)
    th.start()
    global _LAST_THREAD
    _LAST_THREAD = th
    return th


_LAST_THREAD = None


def _force_stop(th):
    """测试兜底：终止仍在运行的监控线程（RED 阶段僵尸不退出会污染后续测试与 stdout）。"""
    if th is None or not th.is_alive():
        return
    import ctypes
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(th.ident), ctypes.py_object(SystemExit))
    th.join(timeout=2)


def _settle_tg(t):
    return [m for lvl, m in t.tg_msgs if '[平仓结算]' in m]


# ── R1：睡眠窗口内 batch 被 clear → 全端点零 API 退出（确定性 Event 同步）─────
def r1_clear_during_sleep_zero_side_effect():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.002)
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)

    def _wait(cond, timeout=5):
        dl = time.time() + timeout
        while time.time() < dl and not cond():
            time.sleep(0.02)
        return cond()

    assert _wait(lambda: t.exchange.api['fetch_positions'] >= 2), '应完成至少 2 个周期'
    blocked = _H_FAKE_TIME.hold_now()
    assert _wait(blocked.is_set, timeout=5), '线程应挂起在 sleep 窗口内'
    time.sleep(0.05)                     # 确保已阻塞在 sleep 窗口内
    api0 = dict(t.exchange.api)          # 快照：此刻起任何 API 都算违规
    api0_sum = sum(api0.values())
    t._states[SYM].pop('batch_A')        # 睡眠窗口内 /auth_reset 对账归档
    _H_FAKE_TIME.release()               # 释放线程
    th.join(timeout=5)
    assert not th.is_alive(), '批次消失后监控线程必须退出'
    api1 = dict(t.exchange.api)
    diff = {k: v - api0.get(k, 0) for k, v in api1.items() if v - api0.get(k, 0) != 0}
    assert sum(api1.values()) == api0_sum, \
        f'clear 后必须全端点零 API，diff={diff}'
    assert not _settle_tg(t), '不得发出结算报告'
    assert t.exchange.api['create_order'] == 0 and t.exchange.api['cancel_order'] == 0
    assert t.clear_calls['clear'] == 0, '不得重复 clear'


# ── R2：G1 通过后并发 clear → G2 命中 → 零结算/零撤单/零收敛 ────────────────
def r2_toctou_second_guard():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.0)

    def _concurrent_clear():
        t._states[SYM].pop('batch_A', None)  # 本周期中途被 /auth_reset 清掉
    t.exchange.on_fetch_positions = _concurrent_clear
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)
    th.join(timeout=5)
    assert not th.is_alive(), 'G2 必须命中 exit'
    assert len(_settle_tg(t)) == 0, f'不得发假结算: {len(_settle_tg(t))}'
    assert t.exchange.api['cancel_order'] == 0, '不得撤单'
    assert t.exchange.api['create_order'] == 0, '不得补挂'
    assert t.converge_calls['converge'] == 0, '不得 converge'
    assert t.clear_calls['clear'] == 0, '不得重复 clear'
    # 判别力：线程必须经生命周期守卫正常退出，而非异常死亡（W1 monitor_error 标记）
    assert not (t._states[SYM].get('batch_A') or {}).get('monitor_error'), \
        '线程经 W1 异常退出 = 非 G2 生命周期退出'


# ── R3：本批次已 clear、sibling 仍有实际 LONG → 不得进入保护单补挂 ──────────
def r3_zombie_no_protection_repair():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.001)

    def _clear_batch_keep_sibling():
        t._states[SYM].pop('batch_A', None)
        t._states[SYM]['batch_B'] = _disk_batch(batch_id='batch_B')
    t.exchange.on_fetch_positions = _clear_batch_keep_sibling
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)
    th.join(timeout=5)
    assert not th.is_alive(), 'G3 必须命中 exit'
    assert t.exchange.api['create_order'] == 0, \
        f'sibling 有仓时僵尸绝不得补挂保护单: {t.exchange.api}'
    assert t.exchange.api['cancel_order'] == 0
    # 判别力：G3 必须在进入 repair 路径（F3 adjudicate）之前拦截
    assert t._auto_stub_calls.get('_adjudicate_recreate_before_repair', 0) == 0, \
        'G3 必须在 F3 补挂裁决之前拦截'
    assert not (t._states[SYM].get('batch_A') or {}).get('monitor_error'), \
        '线程经 W1 异常退出 = 非 G3 生命周期退出'


# ── R4：账本损坏 UNKNOWN ≠ EMPTY → 不退出、当轮零交易所调用 ─────────────────
def r4_corrupted_is_not_empty():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.002)
    ft = _H_FAKE_TIME
    ft.reset()
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)

    def _wait(cond, timeout=5):
        dl = time.time() + timeout
        while time.time() < dl and not cond():
            time.sleep(0.02)
        return cond()

    assert _wait(lambda: t.exchange.api['fetch_positions'] >= 2), '应完成至少 2 个周期'
    # 冻结在 sleep 边界 → 快照 → 开启损坏窗口 → 再冻结 → 快照比对
    blocked = ft.hold_now()
    assert _wait(blocked.is_set, timeout=5)
    time.sleep(0.05)
    api0 = sum(t.exchange.api.values())
    t._force_corrupt = True  # D-009：损坏 → load 返回 {} + _state_corrupted=True
    ft.release()             # 损坏窗口：G1 unknown → continue，零交易所调用
    time.sleep(0.3)
    blocked = ft.hold_now()
    assert _wait(blocked.is_set, timeout=5)
    time.sleep(0.05)
    api1 = sum(t.exchange.api.values())
    assert th.is_alive(), 'UNKNOWN 绝不能被解释为「已清理」而退出'
    assert api1 == api0, f'损坏窗口内必须零交易所调用: {api0} -> {api1}'
    assert not _settle_tg(t)
    t._force_corrupt = False  # 恢复 → 线程继续正常工作
    ft.release()
    time.sleep(0.2)
    assert th.is_alive(), '恢复后线程应继续运行'
    api2 = sum(t.exchange.api.values())
    assert api2 > api1, '恢复后应恢复轮询'
    t._states[SYM].pop('batch_A')
    ft.release()
    th.join(timeout=5)
    assert not th.is_alive()


# ── R5：active + actual=0 + converge 连续 UNKNOWN → 结算报告恰好一次 ────────
def r5_settlement_report_exactly_once():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.0,
                     converge_result=None)
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)
    time.sleep(0.6)  # 跑 >8 轮
    n = len(_settle_tg(t))
    assert n == 1, f'converge UNKNOWN 重试期间结算报告必须恰好 1 次: {n}'
    assert th.is_alive(), '未收敛应继续重试而非退出'
    assert t.converge_calls['converge'] >= 2, '应持续重试收敛'
    t.converge_result = {'ok': True}  # 人工收敛成功 → clear → 退出
    th.join(timeout=5)
    assert not th.is_alive()
    assert len(_settle_tg(t)) == 1


# ── R5b：并发 settlement claim → 恰好一个 owner ─────────────────────────────
def r5b_atomic_settlement_claim():
    claim_fn = H.ex_t('_claim_settlement_reported')
    assert claim_fn is not None, '缺 _claim_settlement_reported'
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.0)
    _bind_fn(t, claim_fn)
    results = []
    lock = threading.Lock()

    def _claim():
        r = t._claim_settlement_reported(SYM, 'batch_A')
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for x in threads:
        x.start()
    for x in threads:
        x.join(timeout=5)
    assert sum(1 for r in results if r is True) == 1, \
        f'原子认领必须恰好一个 owner: {results}'
    assert t._states[SYM]['batch_A'].get('settlement_reported') is True
    assert t._claim_settlement_reported(SYM, 'batch_A') is False  # 已认领 → False
    t._states[SYM].pop('batch_A')
    assert t._claim_settlement_reported(SYM, 'batch_A') is False  # 批次消失 → False


# ── R6：正常 active 批次行为零变化（回归）────────────────────────────────────
def r6_normal_batch_unchanged():
    t = _make_runner({SYM: {'batch_A': _disk_batch()}}, position=0.002)
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)
    time.sleep(0.35)
    assert th.is_alive(), '正常批次监控必须继续运行'
    assert t.exchange.api['fetch_positions'] >= 3, '正常轮询必须继续'
    assert len(_settle_tg(t)) == 0, '有仓不得发结算'
    assert t.exchange.api['create_order'] == 0, '正常轮询不得 create'
    t._states[SYM].pop('batch_A')
    th.join(timeout=5)
    assert not th.is_alive()


# ── R5c：settlement_reported 不得被陈旧快照降级（durable ratchet）────────────
def r5c_settlement_reported_ratchet():
    disk = _disk_batch()
    disk['settlement_reported'] = True
    t = _make_runner({SYM: {'batch_A': disk}}, position=0.0)
    stale = _disk_batch()
    stale['settlement_reported'] = False  # 旧内存快照（僵尸线程视角）
    t.save_batch_state(SYM, 'batch_A', stale)
    b = t._states[SYM]['batch_A']
    assert b.get('settlement_reported') is True, \
        f'durable True 不得被陈旧快照 False 覆盖: {b.get("settlement_reported")}'
    assert t._claim_settlement_reported(SYM, 'batch_A') is False, \
        '被降级后重新认领 = 重复发送通道重新打开'


# ── R7：partial 后外部全平 → 结算数量/盈亏必须用 durable 净量 ────────────────
def r7_settlement_uses_net_qty_not_gross():
    # gross 0.002 → /partial 0.001（realized_reduce_amount=0.001）→ App 一键全平
    disk = _disk_batch(realized_reduce_amount=0.001, realized_reduce_cost=76.69)
    t = _make_runner({SYM: {'batch_A': disk}}, position=0.0, converge_result=None)
    _bind_fn(t, H.ex_t('_start_monitoring'))
    th = _start(t)
    time.sleep(0.5)
    msgs = _settle_tg(t)
    assert len(msgs) >= 1, '应发出结算报告'
    msg = msgs[0]
    assert '🔢 **平仓数量**：`0.001`' in msg, \
        f'结算数量必须用 durable 净量 0.001，不得用内存 gross 0.002:\n{msg}'
    assert '`+152' not in msg, f'PnL 不得按 gross 0.002 计算（假盈利 +152）:\n{msg}'
    t.converge_result = {'ok': True}  # 收敛成功 → clear → 退出
    th.join(timeout=5)
    assert not th.is_alive()


TESTS = [r1_clear_during_sleep_zero_side_effect,
         r2_toctou_second_guard,
         r3_zombie_no_protection_repair,
         r4_corrupted_is_not_empty,
         r5_settlement_report_exactly_once,
         r5b_atomic_settlement_claim,
         r5c_settlement_reported_ratchet,
         r7_settlement_uses_net_qty_not_gross,
         r6_normal_batch_unchanged]


def main():
    import sys
    only = sys.argv[1] if len(sys.argv) > 1 else None
    tests = [fn for fn in TESTS if only is None or fn.__name__ == only or fn.__name__.startswith(only)]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {e}')
            traceback.print_exc()
        finally:
            _force_stop(_LAST_THREAD)
    print(f'\nGREEN: {passed}/{len(tests)}')
    return 0 if passed == len(tests) else 1


if __name__ == '__main__':
    raise SystemExit(main())
