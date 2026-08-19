# -*- coding: utf-8 -*-
"""
R3-v2 离线语义验收测试（不连交易所、不碰真实状态文件、不影响运行中的 Bot）

覆盖 4 个场景（ChatGPT 验收标准）：
  A: 0 个活跃批次        -> 返回 True, 接管 0 个
  B: 1 个活跃批次正常接管 -> 返回 True, 接管 1 个
  C: 恢复前健康检查失败    -> 返回 False
  D: 恢复过程中异常       -> 异常冒出（由 bot_runner 重试 3 次 + TG critical）

用法: .venv\\Scripts\\python.exe test_recover_semantics.py
跑完可删除本文件。
"""
import sys
import threading
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class FakeExchange:
    def __init__(self, fail_time=False, positions=None):
        self.fail_time = fail_time
        self.positions = positions or []
        self.set_leverage_calls = []

    def fetch_time(self):
        if self.fail_time:
            raise ConnectionError("模拟：交易所不可达")
        return 1234567890123

    def fetch_positions(self, symbols):
        return self.positions

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))


def make_fake_self(states, exchange):
    """构造最小 fake self，避免实例化 CryptoTrader（那会连真实交易所）"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.exchange = exchange
    fake.load_all_states = lambda: states
    fake.clear_batch_state = lambda symbol, batch_id: None
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitors = set()
    return fake


def make_batch(is_active=True, monitor_error=False):
    return {
        'is_active': is_active,
        'monitor_error': monitor_error,
        'symbol': SYMBOL,
        'entry_orders': [{'id': 'o1'}, {'id': 'o2'}],
        'last_filled_count': 1,          # 1 成交 + 1 挂单 -> has_pending_orders = True
        'current_sl_id': None,           # 跳过 SL 验证分支
        'tp_order_id': None,
        'stop_steps': [],
        'take_profit_price': 60000.0,
        'batch_total_amount': 0.01,
        'target_amounts': [0.005, 0.005],
        'params_base': {'leverage': 50},
        'is_hedge_mode': False,
        'side': 'BUY',
        'filled_details': None,
    }


def scenario_a():
    """A: 0 活跃批次 -> True"""
    fake = make_fake_self({}, FakeExchange())
    with mock.patch.object(trader_260725.time, 'sleep'), \
         mock.patch.object(trader_260725.threading, 'Thread'):
        ret = CryptoTrader.recover_active_batches(fake)
    report("场景A: 0 活跃批次 -> True", ret is True, f"(实际返回: {ret!r})")


def scenario_b():
    """B: 1 活跃批次正常接管 -> True + 接管 1 个"""
    states = {SYMBOL: {'batch_001': make_batch()}}
    ex = FakeExchange(positions=[{'symbol': SYMBOL, 'contracts': 0.005}])
    fake = make_fake_self(states, ex)
    started = []

    class FakeThread:
        def __init__(self, *a, **k):
            self.kwargs = k
        def start(self):
            started.append(self.kwargs['kwargs']['batch_id'] if 'kwargs' in self.kwargs else '?')

    with mock.patch.object(trader_260725.time, 'sleep'), \
         mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        ret = CryptoTrader.recover_active_batches(fake)
    ok = ret is True and len(started) == 1 and len(ex.set_leverage_calls) == 1
    report("场景B: 1 批次接管 -> True + 启动 1 监控线程", ok,
           f"(返回: {ret!r}, 线程: {started}, 杠杆调用: {len(ex.set_leverage_calls)})")


def scenario_c():
    """C: 健康检查失败 -> False"""
    ex = FakeExchange(fail_time=True)
    fake = make_fake_self({SYMBOL: {'batch_001': make_batch()}}, ex)
    with mock.patch.object(trader_260725.time, 'sleep') as ms, \
         mock.patch.object(trader_260725.threading, 'Thread'):
        ret = CryptoTrader.recover_active_batches(fake)
    ok = ret is False and ms.called  # 确认走了 60s 等待路径
    report("场景C: 健康检查失败 -> False", ok, f"(返回: {ret!r}, sleep 调用: {ms.called})")


def scenario_d():
    """D: 恢复过程中异常 -> 异常冒出（不能吞掉返回 True/False）"""
    fake = make_fake_self({}, FakeExchange())
    fake.load_all_states = mock.Mock(side_effect=RuntimeError("模拟：状态文件损坏"))
    raised = None
    with mock.patch.object(trader_260725.time, 'sleep'), \
         mock.patch.object(trader_260725.threading, 'Thread'):
        try:
            CryptoTrader.recover_active_batches(fake)
        except Exception as e:
            raised = e
    report("场景D: 恢复中异常 -> Exception 冒出", isinstance(raised, RuntimeError),
           f"(捕获: {raised!r})")


def scenario_partial():
    """+1: 部分批次恢复失败 -> 必须以异常中断，绝不能返回 True（部分成功≠完全成功）"""
    states = {SYMBOL: {
        'batch_ok': make_batch(),                      # 正常批次
        'batch_bad': make_batch(),                     # 缺 symbol 键 -> KeyError
    }}
    del states[SYMBOL]['batch_bad']['symbol']
    fake = make_fake_self(states, FakeExchange())

    class FakeThread:
        def __init__(self, *a, **k):
            pass
        def start(self):
            pass

    raised = None
    with mock.patch.object(trader_260725.time, 'sleep'), \
         mock.patch.object(trader_260725.threading, 'Thread', FakeThread):
        try:
            ret = CryptoTrader.recover_active_batches(fake)
        except Exception as e:
            raised = e
    ok = raised is not None
    report("+1: 部分批次失败 -> 异常中断（非 True）", ok,
           f"(返回值: {'异常 ' + type(raised).__name__ if raised else '未抛异常!'})")


if __name__ == '__main__':
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_partial()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过（A/B/C/D/+1 部分失败）")
    print("R3-v2 语义验收完成，可提交 GitHub 封板 Phase A")
