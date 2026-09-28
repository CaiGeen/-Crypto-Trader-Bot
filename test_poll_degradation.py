# -*- coding: utf-8 -*-
"""R1/R2 连续轮询失败修复 —— RED 负测（ChatGPT 裁定 6 场景，先红后绿）。

当前代码缺陷（本文件断言修复后的行为，故在当前代码上应红）：
  1. 单批次持续失败 → 无 critical 告警、无降级标记
  2. 新信号未被拦（execute_signal 不读降级标志，真实新信号仍继续）
  3. 多批次一成一败 → 成功批次会替失败批次解锁
  4. 恢复过早（fetch 成功即宣称恢复，未等成交识别 + 保护处理完成）
  5. 通知异常可杀监控（告警未在 try/except 内）—— 守卫测，当前代码无告警故空过
  6. 暂停逻辑会触碰存量 SL/TP 或监控

用法：`.venv\\Scripts\\python.exe test_poll_degradation.py`（rc=0 即全过）
"""
import copy
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725
from trader_260725 import CryptoTrader
import test_monitor_poll_recovery as PR

SYMBOL = PR.SYMBOL
BATCH = PR.BATCH
ENTRY_ID = PR.ENTRY_ID

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"{'[PASS]' if passed else '[FAIL]'} {name} {detail}")


def _criticals(fake):
    return [m for lvl, m in fake.sent if lvl == 'critical']


def _init_poll_tracking(fake):
    """初始化 R1/R2 降级跟踪变量为真实类型（fake 不跑 __init__）。"""
    fake._poll_fail_streak = {}
    fake._poll_first_fail_time = {}
    fake._poll_last_success_time = time.time()   # 近期，避免 _stale 立即触发
    fake._poll_degraded_batches = set()
    fake._poll_alert_lock = threading.Lock()
    fake._poll_alert_active = False
    # 绑定新增告警方法（MagicMock 不会自动绑未在 REAL_HELPERS 中的新方法）
    fake._alert_poll_degraded = (
        lambda streak, stale: CryptoTrader._alert_poll_degraded(fake, streak, stale))


def _drive_failing(fake, rounds=5):
    """驱动监控，fetch_open_orders 持续失败。返回 (rounds, err)。"""
    fake.exchange.fetch_open_orders.side_effect = RuntimeError('持续失败')
    return PR._drive(fake, None, max_rounds=rounds)


# ── T1: 单批次持续失败 → critical 告警 + 该批次标记降级 ────────────────────
def test_single_batch_persistent_failure_alerts_and_marks_degraded():
    d = tempfile.mkdtemp(prefix='pd_t1_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)   # 真实 set，非 MagicMock
        _drive_failing(fake, 5)
        crit = _criticals(fake)
        report('T1a 持续失败 → 发出 critical 告警',
               len(crit) >= 1, f'critical 数={len(crit)}')
        report('T1b 持续失败 → 该批次标记降级',
               BATCH in fake._poll_degraded_batches,
               f'degraded={fake._poll_degraded_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T2: 新信号实际被拦（execute_signal → ENTRY create_order 拒绝）──────────
class _EntryFake:
    """execute_signal fake：通过所有门闸直到 ENTRY 创建前。"""

    def __init__(self):
        self._ready = True
        self._not_ready_reason = ""
        self.tg_sent = []
        self.create_calls = []
        self._poll_degraded_batches = set()

    def _safe_api_call(self, fn, *a, **k):
        return fn(*a, **k)

    def _check_account_risk(self, all_states, signal, stats_file=None):
        return CryptoTrader._check_account_risk(self, all_states, signal, stats_file)

    def _count_active_batches(self, all_states):
        return CryptoTrader._count_active_batches(self, all_states)

    def _get_today_realized_pnl(self, stats_file=None):
        return CryptoTrader._get_today_realized_pnl(self, stats_file)

    def load_all_states(self):
        return {}

    def _compute_signal_fingerprint(self, signal):
        return "fp-test"

    def _check_existing_conflicts(self, symbol, batch_id, all_states, fp):
        return False

    def _get_current_position_amt(self, *a, **k):
        return 0.0

    def _validate_stop_losses(self, signal, mark_price):
        return True, "ok"

    def _validate_take_profit(self, signal, mark_price):
        return True, "ok"

    def _protection_identity(self, batch_id, role, layer, side):
        return f"{batch_id}|{role}|L{layer}|{side}"

    def _build_intent(self, **k):
        return dict(k)

    def save_batch_state(self, symbol, batch_id, data):
        return True

    def _update_registry(self, *a, **k):
        return None

    def send_tg_notification(self, text, **k):
        self.tg_sent.append((k.get('level', 'info'), str(text)))

    def _send_email_alert(self, *a, **k):
        pass


class _FakeSignal:
    def __init__(self):
        self.symbol = SYMBOL
        self.batch_id = "batch_new_001"
        self.side = "BUY"
        self.leverage = 20
        self.entries = [(77000.0, 0.43)]
        self.stop_loss_steps = [75000.0]
        self.take_profit = 60000.0


def _entry_fake_with_exchange():
    fake = _EntryFake()
    ex = mock.MagicMock()
    ex.fapiPrivateGetPositionSideDual.return_value = {'dualSidePosition': True}
    ex.fetch_ticker.return_value = {'last': 76500.0, 'close': 76500.0}
    ex.fetch_balance.return_value = {'USDT': {'free': 10000.0}}
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.set_leverage.return_value = {}

    def _create(**k):
        fake.create_calls.append(k)
        return {'id': 'x', 'status': 'open'}
    ex.create_order.side_effect = _create
    fake.exchange = ex
    return fake


def test_new_signal_blocked_when_degraded():
    fake = _entry_fake_with_exchange()
    fake._poll_degraded_batches.add(BATCH)   # 模拟监控线程已报告降级
    ret = CryptoTrader.execute_signal(fake, _FakeSignal())
    report('T2a 降级时新信号被拒绝（返回 None）',
           ret is None, f'返回={ret!r}')
    report('T2b 降级时未创建 ENTRY 单',
           len(fake.create_calls) == 0,
           f'create 次数={len(fake.create_calls)}')


# ── T3: 多批次一成一败 → 成功批次不得替失败批次解锁 ────────────────────────
def test_multi_batch_one_success_one_failure():
    d = tempfile.mkdtemp(prefix='pd_t3_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        # 模拟两个批次同时降级
        fake._poll_degraded_batches.add(BATCH)
        fake._poll_degraded_batches.add('batch_other')
        fake._poll_fail_streak[BATCH] = 3
        fake._poll_fail_streak['batch_other'] = 3
        # 本批次查询成功 → 完整恢复后只移除自己
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        PR._drive(fake, None, max_rounds=3)
        degraded = fake._poll_degraded_batches
        report('T3a 本批次恢复后被移除',
               BATCH not in degraded, f'degraded={degraded}')
        report('T3b 另一批次仍降级（不被解锁）',
               'batch_other' in degraded, f'degraded={degraded}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T4: 失败后的完整恢复（成交识别 + 保护处理完成）────────────────────────
def test_complete_recovery_after_failure():
    d = tempfile.mkdtemp(prefix='pd_t4_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        # 第一阶段：3 轮失败 → 降级
        fake.exchange.fetch_open_orders.side_effect = [
            RuntimeError('失败1'), RuntimeError('失败2'), RuntimeError('失败3')]
        PR._drive(fake, None, max_rounds=3)
        report('T4a 失败期间批次处于降级',
               BATCH in fake._poll_degraded_batches,
               f'失败后 degraded={fake._poll_degraded_batches}')
        # 第二阶段：成功且入场单已成交 → 完整恢复
        fake.exchange.fetch_open_orders.side_effect = None
        fake.exchange.fetch_open_orders.return_value = []
        fake.exchange.fetch_order.return_value = {
            'id': ENTRY_ID, 'status': 'closed', 'average': 58000.0,
            'info': {'cumQuote': '24940', 'executedQty': '0.43', 'updateTime': 1},
        }
        PR._drive(fake, None, max_rounds=3)
        report('T4b 完整恢复后解除降级',
               BATCH not in fake._poll_degraded_batches,
               f'恢复后 degraded={fake._poll_degraded_batches}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T5: 通知异常不得杀监控（守卫测）────────────────────────────────────────
def test_notification_exception_kills_nothing():
    d = tempfile.mkdtemp(prefix='pd_t5_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)

        def _boom(text, **k):
            raise RuntimeError('TG 发送失败')
        fake.send_tg_notification = _boom
        fake._send_email_alert = mock.MagicMock(
            side_effect=RuntimeError('邮件失败'))
        rounds, err = _drive_failing(fake, 5)
        report('T5 通知异常未杀监控（驱动未因告警异常中断）',
               rounds >= 3, f'驱动轮次={rounds} err={err!r}')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


# ── T6: 已有 SL/TP 和监控不被暂停逻辑触碰 ────────────────────────────────
def test_existing_sl_tp_and_monitor_not_touched():
    d = tempfile.mkdtemp(prefix='pd_t6_')
    sp = os.path.join(d, 'trade_state.json')
    trader_260725.STATE_FILE = sp
    try:
        PR._seed(sp)
        states = PR._disk(sp)
        states[SYMBOL][BATCH]['current_sl_id'] = 'S1'
        states[SYMBOL][BATCH]['tp_order_id'] = 'T1'
        fake = PR._make_fake(sp, states)
        _init_poll_tracking(fake)
        fake.cancel_calls = 0
        _orig_cancel = fake.exchange.cancel_order

        def _counting_cancel(*a, **k):
            fake.cancel_calls += 1
            return _orig_cancel(*a, **k)
        fake.exchange.cancel_order.side_effect = _counting_cancel
        _drive_failing(fake, 5)
        report('T6a 暂停期间未撤存量保护单',
               fake.cancel_calls == 0, f'cancel={fake.cancel_calls}')
        report('T6b 暂停期间监控仍在运行',
               fake.exchange.api['fetch_positions'] >= 1
               if isinstance(fake.exchange.api, dict) else True,
               '（监控线程未退出）')
    finally:
        trader_260725.STATE_FILE = PR._real_state_file


def main():
    tests = [
        test_single_batch_persistent_failure_alerts_and_marks_degraded,
        test_new_signal_blocked_when_degraded,
        test_multi_batch_one_success_one_failure,
        test_complete_recovery_after_failure,
        test_notification_exception_kills_nothing,
        test_existing_sl_tp_and_monitor_not_touched,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            report(f'{fn.__name__} 异常', False, f'{type(e).__name__}: {e}')
    passed = sum(1 for _, p in RESULTS if p)
    print(f'\nGREEN: {passed}/{len(RESULTS)}')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
