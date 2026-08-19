# -*- coding: utf-8 -*-
"""
B3/SG2 加仓前风险闸门离线验收测试（不连交易所、不碰真实状态文件、不影响运行中的 Bot）

覆盖 10 个核心场景 + 1 个零下单硬断言（ChatGPT 最终验收标准）：
   1: 首仓 current_pos=0            -> 放行，SG2 不调用 SL API
   2: 加仓，台账=交易所，全部 SL 有效  -> 放行
   3: 加仓，某已成交批次 sl_id=None   -> 拒绝
   4: 加仓，SL 不在 open_orders      -> 拒绝
   5: fetch_open_orders 异常         -> 拒绝（UNKNOWN Fail-Closed）
   6: delta > eps（未归属手工仓位）   -> 拒绝（方案 A 核心）
   7: delta < -eps（台账>交易所）     -> 拒绝（方案 A 核心）
   8: 多批次，其一 SL 无效           -> 拒绝
   9: 多批次，仅未成交批次无 SL       -> 放行（未成交≠裸仓）
  10: |delta| <= 1e-9 浮点噪声       -> 按相等处理放行
  11: 拒绝后零下单（硬断言）          -> return None + create_order==0 + TG 告知

用法: .venv\\Scripts\\python.exe test_sg2_risk_gate.py
"""
import sys
from unittest import mock

from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class ProbeReached(Exception):
    """探针异常：execute_signal 到达 fetch_ticker 即证明 SG1/SG2 已放行（被尾部 except 吸收）"""


class FakeExchange:
    def __init__(self, open_orders=None, fail_open_orders=False):
        self.open_orders = open_orders if open_orders is not None else []
        self.fail_open_orders = fail_open_orders
        self.calls = {'set_leverage': 0, 'create_order': 0, 'fetch_open_orders': 0}

    def fetch_open_orders(self, symbol):
        self.calls['fetch_open_orders'] += 1
        if self.fail_open_orders:
            raise ConnectionError("模拟：查询挂单失败")
        return self.open_orders

    def set_leverage(self, leverage, symbol):
        self.calls['set_leverage'] += 1

    def fetch_ticker(self, symbol):
        raise ProbeReached()  # 到这里 = 已过全部闸门

    def create_order(self, **kw):
        self.calls['create_order'] += 1
        return {'id': 'new_order'}


class HelperFake:
    """helper 单测最小 fake：只需 exchange + _safe_api_call 直通；
    _check_sl_coverage 绑定真实实现（fake 不继承 CryptoTrader）"""

    def __init__(self, exchange):
        self.exchange = exchange
        self._safe_api_call = lambda fn, *a, **k: fn(*a, **k)

    def _check_sl_coverage(self, symbol, all_states, current_pos):
        return CryptoTrader._check_sl_coverage(self, symbol, all_states, current_pos)


class ExecFake(HelperFake):
    """execute_signal 集成 fake：_ready=True（SG1 放行），注入持仓/状态"""

    def __init__(self, exchange, current_pos, states):
        super().__init__(exchange)
        self._ready = True
        self._not_ready_reason = ""
        self._current_pos = current_pos
        self._states = states
        self.tg_sent = []

    def load_all_states(self):
        return self._states

    def _check_existing_conflicts(self, symbol, batch_id, all_states):
        return False  # stub：不真正调 fetch_open_orders，保证调用计数只含 SG2

    def _get_current_position_amt(self, *a, **k):
        return self._current_pos

    def send_tg_notification(self, text, **k):
        self.tg_sent.append(text)


class FakeSignal:
    symbol = SYMBOL
    batch_id = "batch_new_001"
    side = "BUY"
    leverage = 50


def make_batch(last_filled, amounts, sl_id):
    return {'is_active': True, 'last_filled_count': last_filled,
            'target_amounts': amounts, 'current_sl_id': sl_id}


def helper(symbol, states, current_pos, open_orders=None, fail_open=False):
    ex = FakeExchange(open_orders=open_orders, fail_open_orders=fail_open)
    return CryptoTrader._check_sl_coverage(HelperFake(ex), symbol, states, current_pos), ex


# ---------------- helper 单元测试（场景 2-10） ----------------

def scenario_2_helper():
    states = {SYMBOL: {'b1': make_batch(2, [0.01, 0.01], 'sl1')}}
    (ok, reason), ex = helper(SYMBOL, states, 0.02, open_orders=[{'id': 'sl1'}])
    report("场景2: 台账=交易所+SL有效 -> 放行", ok is True and reason == "",
           f"(ok: {ok}, reason: {reason!r})")


def scenario_3():
    states = {SYMBOL: {'b1': make_batch(1, [0.01], None)}}
    (ok, reason), _ = helper(SYMBOL, states, 0.01, open_orders=[])
    report("场景3: 已成交批次 sl_id=None -> 拒绝", ok is False and "缺少有效止损" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_4():
    states = {SYMBOL: {'b1': make_batch(1, [0.01], 'sl_old')}}
    (ok, reason), _ = helper(SYMBOL, states, 0.01, open_orders=[{'id': 'sl_other'}])
    report("场景4: SL 不在 open_orders -> 拒绝", ok is False and "缺少有效止损" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_5():
    states = {SYMBOL: {'b1': make_batch(1, [0.01], 'sl1')}}
    (ok, reason), _ = helper(SYMBOL, states, 0.01, fail_open=True)
    report("场景5: fetch_open_orders 异常 -> 拒绝", ok is False and "查询失败" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_6():
    states = {SYMBOL: {'b1': make_batch(2, [0.01, 0.01], 'sl1')}}
    (ok, reason), _ = helper(SYMBOL, states, 0.03, open_orders=[{'id': 'sl1'}])  # delta=+0.01
    report("场景6: delta>eps 未归属手工仓 -> 拒绝", ok is False and "未归属" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_7():
    states = {SYMBOL: {'b1': make_batch(2, [0.01, 0.01], 'sl1')}}
    (ok, reason), _ = helper(SYMBOL, states, 0.01, open_orders=[{'id': 'sl1'}])  # delta=-0.01
    report("场景7: delta<-eps 台账>交易所 -> 拒绝", ok is False and "不一致" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_8():
    states = {SYMBOL: {
        'b1': make_batch(1, [0.01], 'sl1'),
        'b2': make_batch(1, [0.01], 'sl_gone'),
    }}
    (ok, reason), _ = helper(SYMBOL, states, 0.02, open_orders=[{'id': 'sl1'}])
    report("场景8: 多批次其一SL无效 -> 拒绝", ok is False and "b2" in reason,
           f"(ok: {ok}, reason: {reason!r})")


def scenario_9():
    states = {SYMBOL: {
        'b1': make_batch(1, [0.01], 'sl1'),
        'b2': make_batch(0, [0.01, 0.01], None),   # 未成交批次，无 SL 不算裸仓
    }}
    (ok, reason), _ = helper(SYMBOL, states, 0.01, open_orders=[{'id': 'sl1'}])
    report("场景9: 仅未成交批次无SL -> 放行", ok is True and reason == "",
           f"(ok: {ok}, reason: {reason!r})")


def scenario_10():
    states = {SYMBOL: {'b1': make_batch(1, [0.01], 'sl1')}}
    (ok, reason), _ = helper(SYMBOL, states, 0.01 + 5e-10, open_orders=[{'id': 'sl1'}])
    report("场景10: |delta|<=1e-9 浮点噪声 -> 按相等放行", ok is True,
           f"(ok: {ok}, reason: {reason!r})")


# ---------------- execute_signal 集成测试（场景 1、11） ----------------

def scenario_1():
    """1: 首仓 current_pos=0 -> 放行且 SG2 不触发任何 SL 查询"""
    ex = FakeExchange(open_orders=[])
    fake = ExecFake(ex, current_pos=0.0, states={SYMBOL: {}})
    CryptoTrader.execute_signal(fake, FakeSignal())
    ok = (ex.calls['fetch_open_orders'] == 0   # SG2 未调用（首仓零成本）
          and ex.calls['set_leverage'] == 1)   # 已进入后续执行（过闸）
    report("场景1: 首仓放行，SG2 零 API 调用", ok,
           f"(open_orders查询: {ex.calls['fetch_open_orders']}, set_leverage: {ex.calls['set_leverage']})")


def scenario_11():
    """11: 拒绝后零下单硬断言（delta>0 场景走完整 execute_signal）"""
    states = {SYMBOL: {'b1': make_batch(2, [0.01, 0.01], 'sl1')}}
    ex = FakeExchange(open_orders=[{'id': 'sl1'}])
    fake = ExecFake(ex, current_pos=0.03, states=states)  # delta=+0.01 手工仓
    ret = CryptoTrader.execute_signal(fake, FakeSignal())
    ok = (ret is None
          and ex.calls['create_order'] == 0     # 硬断言：入场单零创建
          and ex.calls['set_leverage'] == 0     # SG2 拒绝先于 set_leverage
          and len(fake.tg_sent) == 1            # 一次用户可见告知
          and "未归属" in fake.tg_sent[0])
    report("场景11: 拒绝后零下单+TG告知", ok,
           f"(返回: {ret!r}, 下单: {ex.calls['create_order']}, TG: {len(fake.tg_sent)})")


if __name__ == '__main__':
    scenario_1()
    scenario_2_helper()
    scenario_3()
    scenario_4()
    scenario_5()
    scenario_6()
    scenario_7()
    scenario_8()
    scenario_9()
    scenario_10()
    scenario_11()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("B3/SG2 加仓前风险闸门语义验收完成")
