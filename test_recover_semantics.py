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
import copy
import unittest
from unittest import mock

import ccxt
import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class FakeExchange:
    def __init__(self, fail_time=False, positions=None, open_orders=None):
        self.fail_time = fail_time
        self.positions = positions or []
        self.set_leverage_calls = []
        # 第十一轮归因修正：候选新增「启动重证」（_reverify_ok）要逐条核对
        # 未结 ENTRY 与持仓方向的 SL 实况，只实现 fetch_time/fetch_positions
        # 的旧夹具会让场景B 停在"未结订单查询异常"→ 返回 False。
        # Fail-Closed 行为本身是对的，缺的是**交易所读接口**。
        self.open_orders = list(open_orders or [])

    def fetch_time(self):
        if self.fail_time:
            raise ConnectionError("模拟：交易所不可达")
        return 1234567890123

    def fetch_positions(self, symbols):
        return self.positions

    def set_leverage(self, leverage, symbol):
        self.set_leverage_calls.append((leverage, symbol))

    def fetch_open_orders(self, symbol, params=None):
        # params={'stop': True} 是重证查条件单的用法；夹具按场景给同一份实况。
        return list(self.open_orders)

    def fetch_order(self, order_id, symbol=None, **kw):
        # **kw：ccxt 风格的 params= 调用（接管期 SL 校验就这么调）。
        # 在未结列表里的单返回 status=open（它们本就是 open order）；
        # 不在列表里的 ENTRY 返回 closed → 重证判明已成交/完结即可继续。
        for _o in self.open_orders:
            if str(_o.get('id')) == str(order_id):
                return dict(_o, status='open')
        return {'id': order_id, 'status': 'closed'}


def make_fake_self(states, exchange):
    """构造最小 fake self，避免实例化 CryptoTrader（那会连真实交易所）"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.exchange = exchange
    fake.load_all_states = lambda: states
    fake.clear_batch_state = lambda symbol, batch_id: None
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitors = set()
    # P5i：恢复改为方向感知仓位查询——未绑定时 MagicMock 参与 `> 0` 比较抛
    # TypeError（同 _assert_create_allowed / _finally_cleanup_decision 坑）
    fake._get_current_position_amt = (
        lambda symbol, is_hedge_mode, side='BUY', retries=3:
        CryptoTrader._get_current_position_amt(fake, symbol, is_hedge_mode, side,
                                               retries))
    # 第十一轮归因修正：候选新增的「启动重证」会调用
    # self._registry_has_unresolved_entries(bd)。未绑定时是 MagicMock → 恒真 →
    # 假"未决 ENTRY 意图" → 场景B 必然返回 False（同 :57 与归档 fake 的既有坑）。
    fake._registry_has_unresolved_entries = (
        lambda bd: CryptoTrader._registry_has_unresolved_entries(fake, bd))
    return fake


def make_batch(is_active=True, monitor_error=False):
    return {
        'is_active': is_active,
        'monitor_error': monitor_error,
        'symbol': SYMBOL,
        'entry_orders': [{'id': 'o1'}, {'id': 'o2'}],
        'last_filled_count': 1,          # 1 成交 + 1 挂单 -> has_pending_orders = True
        'current_sl_id': None,           # 默认无 SL 锚点：本文件默认场景无持仓，
                                        # 重证 ③ 不触发；场景B 显式给出锚点
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
    # 第十一轮归因修正：重证要求"有持仓 → 必须有本地 SL 锚点，且该 SL 仍在
    # 交易所（类型/方向/覆盖量逐项核对）"。旧夹具是 current_sl_id=None 且
    # FakeExchange 没有 fetch_open_orders → 场景B 必然返回 False。
    # 这里给出可通过判据的交易所真相：两笔已完结 ENTRY + 一张在场的止损。
    states[SYMBOL]['batch_001']['current_sl_id'] = 'sl1'
    ex = FakeExchange(
        positions=[{'symbol': SYMBOL, 'contracts': 0.005}],
        open_orders=[{'id': 'o1', 'type': 'LIMIT', 'side': 'BUY',
                      'amount': 0.005},
                     {'id': 'o2', 'type': 'LIMIT', 'side': 'BUY',
                      'amount': 0.005},
                     {'id': 'sl1', 'type': 'STOP_MARKET', 'side': 'SELL',
                      'amount': 0.01}])
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


class EntryChannelRecoveryTests(unittest.TestCase):
    """Startup must observe both order namespaces without guessing UNKNOWN away."""

    def recover(self, normal, conditional, normal_detail=None, conditional_detail=None,
                position=0.0):
        ex = FakeExchange()
        ex.create_order = mock.Mock()
        ex.cancel_order = mock.Mock()
        ex.open_calls = []
        ex.detail_calls = []

        def response(value):
            if isinstance(value, Exception):
                raise value
            return value

        def open_orders(symbol, params=None):
            channel = bool((params or {}).get('stop'))
            ex.open_calls.append(channel)
            return response(conditional if channel else normal)

        def fetch_order(order_id, symbol, params=None):
            channel = bool((params or {}).get('stop'))
            ex.detail_calls.append((order_id, channel))
            return response(conditional_detail if channel else normal_detail)

        ex.fetch_open_orders = open_orders
        ex.fetch_order = fetch_order
        batch = make_batch()
        batch.update(entry_orders=['entry'], last_filled_count=0,
                     protection_registry={}, pending_sl_orders=[0])
        states = {SYMBOL: {'batch_001': batch}}
        before = copy.deepcopy(states)
        fake = make_fake_self(states, ex)
        fake.clear_batch_state = mock.Mock()
        fake._ready = True
        fake._unresolved_intent_batches = set()
        fake._get_current_position_amt = lambda *a, **k: position
        with mock.patch.object(trader_260725.threading, 'Thread'), \
             mock.patch.object(trader_260725.time, 'sleep'):
            ok = CryptoTrader.recover_active_batches(fake)
        self.assertEqual(states, before)
        ex.create_order.assert_not_called()
        ex.cancel_order.assert_not_called()
        fake.clear_batch_state.assert_not_called()
        if not ok:
            self.assertIs(fake._ready, False)
            self.assertTrue(any(c.kwargs.get('level') == 'critical'
                                for c in fake.send_tg_notification.call_args_list))
        return ok, ex

    def test_conditional_entry_visible_only_in_algo_channel(self):
        ok, ex = self.recover([], [{'id': 'entry', 'status': 'open'}])
        self.assertIs(ok, True)
        self.assertEqual(ex.open_calls, [False, True])
        self.assertEqual(ex.detail_calls, [])

    def test_normal_entry_and_duplicate_id_remain_supported(self):
        for conditional in ([], [{'id': 'entry', 'status': 'open'}]):
            with self.subTest(conditional=conditional):
                ok, ex = self.recover([{'id': 'entry', 'status': 'open'}], conditional)
                self.assertIs(ok, True)
                self.assertEqual(ex.open_calls, [False, True])
                self.assertEqual(ex.detail_calls, [])

    def test_either_snapshot_failure_blocks_even_if_other_has_entry(self):
        for failure in (ccxt.NetworkError('offline'), None, {}, 'invalid',
                        [None], [{}]):
            for channel in (False, True):
                with self.subTest(failure=repr(failure), channel=channel):
                    known = [{'id': 'entry', 'status': 'open'}]
                    normal, conditional = (known, failure) if channel else (failure, known)
                    ok, ex = self.recover(normal, conditional)
                    self.assertIs(ok, False)
                    self.assertEqual(ex.detail_calls, [])

    def test_missing_normal_entry_can_be_proven_terminal(self):
        for status in ('closed', 'filled', 'canceled', 'expired', 'rejected'):
            with self.subTest(status=status):
                ok, ex = self.recover([], [], normal_detail={'id': 'entry', 'status': status})
                self.assertIs(ok, True)
                self.assertEqual(ex.detail_calls, [('entry', False)])

    def test_normal_not_found_routes_terminal_query_to_algo(self):
        for status in ('closed', 'filled', 'canceled', 'expired', 'rejected'):
            with self.subTest(status=status):
                ok, ex = self.recover([], [], ccxt.OrderNotFound('normal absent'),
                                      {'id': 'entry', 'status': status})
                self.assertIs(ok, True)
                self.assertEqual(ex.detail_calls, [('entry', False), ('entry', True)])

    def test_not_found_in_both_channels_is_unknown_not_terminal(self):
        ok, ex = self.recover([], [], ccxt.OrderNotFound('normal absent'),
                              ccxt.OrderNotFound('algo absent'))
        self.assertIs(ok, False)
        self.assertEqual(ex.detail_calls, [('entry', False), ('entry', True)])

    def test_normal_unknown_does_not_fall_through_to_algo_success(self):
        for detail in (ccxt.NetworkError('offline'), None, {}, {'status': 'open'}):
            with self.subTest(detail=repr(detail)):
                ok, ex = self.recover([], [], detail, {'id': 'entry', 'status': 'closed'})
                self.assertIs(ok, False)
                self.assertEqual(ex.detail_calls, [('entry', False)])

    def test_algo_unknown_or_open_still_blocks(self):
        for detail in (ccxt.NetworkError('offline'), None, {},
                       {'id': 'entry', 'status': 'open'}):
            with self.subTest(detail=repr(detail)):
                ok, _ = self.recover([], [], ccxt.OrderNotFound('normal absent'), detail)
                self.assertIs(ok, False)

    def test_entry_observed_but_position_unknown_or_unprotected_blocks(self):
        for position in (None, 0.01):
            with self.subTest(position=position):
                ok, _ = self.recover([], [{'id': 'entry', 'status': 'open'}], position=position)
                self.assertIs(ok, False)


if __name__ == '__main__':
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_partial()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EntryChannelRecoveryTests)
    if not unittest.TextTestRunner().run(suite).wasSuccessful():
        sys.exit(1)
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过（A/B/C/D/+1 部分失败）")
    print("R3-v2 语义验收完成，可提交 GitHub 封板 Phase A")
