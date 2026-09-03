# -*- coding: utf-8 -*-
"""
B2/SG1 READY 门控离线验收测试（不连交易所、不碰真实状态文件、不影响运行中的 Bot）

覆盖 7 个场景（ChatGPT 最终验收标准）：
  1: NOT_READY 直接调用 execute_signal（绕过 bot_runner）-> 拒绝，0 次交易所调用
  2: READY=True -> 通过闸门进入后续逻辑（fetch_positions 探针被调用）
  3: recover 返回 True（含 0 批次）-> _ready=True（R3-v2 语义衔接）
  4: recover 返回 False -> 永久 NOT_READY + reason 更新
  5: recover 异常 x3 -> 永久 NOT_READY + reason 更新
  6: NOT_READY + 保护操作（SL/TP 预挂）-> 不受 READY Gate 影响
  7: 临时 trader（无恢复）-> 默认 NOT_READY，Gate 拦截（含 __init__ 默认值源码断言）

用法: .venv\\Scripts\\python.exe test_sg1_ready_gate.py
"""
import asyncio
import inspect
import sys
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_test_001"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class FakeSignal:
    def __init__(self):
        self.symbol = SYMBOL
        self.batch_id = BATCH
        self.side = "BUY"
        self.leverage = 20  # D-006（2026-08-28）：execute_signal 新增账户风控闸门需读取杠杆


class GateFake:
    """execute_signal 闸门测试 fake：记录是否进入后续逻辑（探针 = 持仓查询）"""

    def __init__(self, ready):
        self._ready = ready
        self._not_ready_reason = "启动恢复中（历史批次接管未完成）"
        self.gate_passed = False
        self.conflict_check_called = False
        self.tg_sent = []

    # D-006（2026-08-28）：绑定真实账户风控闸门三件套（防假回归；
    # helper 不依赖 self 状态，委托 CryptoTrader 实实现即可）
    def _check_account_risk(self, all_states, signal, stats_file=None):
        return CryptoTrader._check_account_risk(self, all_states, signal, stats_file)

    def _count_active_batches(self, all_states):
        return CryptoTrader._count_active_batches(self, all_states)

    def _get_today_realized_pnl(self, stats_file=None):
        return CryptoTrader._get_today_realized_pnl(self, stats_file)

    # 以下全部是"闸门之后"才会触发的依赖 —— 被调用即证明已过闸
    def load_all_states(self):
        return {}

    def _check_existing_conflicts(self, symbol, batch_id, all_states):
        self.conflict_check_called = True
        return False

    def _get_current_position_amt(self, *a, **k):
        self.gate_passed = True  # 探针：闸门已通过，进入持仓查询
        return None              # 触发"无法查询持仓" return None 分支

    def send_tg_notification(self, text, **k):
        self.tg_sent.append(text)


def scenario_1():
    """1: NOT_READY 直接 execute_signal -> 拒绝，0 次后续调用，无 TG 告警风暴"""
    fake = GateFake(ready=False)
    ret = CryptoTrader.execute_signal(fake, FakeSignal())
    ok = (ret is None
          and fake.gate_passed is False        # 未进入持仓查询（0 次 API）
          and fake.conflict_check_called is False
          and len(fake.tg_sent) == 0)           # C 层 Gate 只 print，不发 TG（防告警风暴）
    report("场景1: NOT_READY 直接调用 -> 0 交易所调用/0 TG", ok,
           f"(返回: {ret!r}, 过闸: {fake.gate_passed}, TG: {len(fake.tg_sent)})")


def scenario_2():
    """2: READY=True -> 确实进入后续执行逻辑（探针被调用）"""
    fake = GateFake(ready=True)
    ret = CryptoTrader.execute_signal(fake, FakeSignal())
    ok = fake.gate_passed and fake.conflict_check_called  # 过闸 + 冲突检查已执行
    report("场景2: READY=True -> 进入后续逻辑", ok,
           f"(过闸: {fake.gate_passed}, 冲突检查: {fake.conflict_check_called})")


def scenario_6():
    """6: NOT_READY 时保护操作（SL/TP 预挂）不受 READY Gate 影响"""
    fake = GateFake(ready=False)  # NOT_READY！
    fake.states = {SYMBOL: {BATCH: {'current_sl_id': None, 'tp_order_id': None,
                                    'pending_sl_orders': [0]}}}
    fake.load_all_states = lambda: fake.states
    fake.save_batch_state = lambda s, b, d: None
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)

    calls = []

    class FakeExchange:
        @staticmethod
        def create_order(**kw):
            calls.append(kw)
            return {'id': f'ord_{len(calls)}'}

    fake.exchange = FakeExchange()
    # B2-3/B2-4 闸门接入后，预挂路径需 B2 helper（纯桩；闸门/registry 语义由 B2 套件覆盖）
    fake._api_cooldown_until = 0
    fake._protection_identity = lambda batch_id, role, layer, side: f'{batch_id}|{role}|L{layer}|{side}'
    if hasattr(CryptoTrader, '_assert_create_allowed'):
        fake._assert_create_allowed = (
            lambda s, b, i, **k: CryptoTrader._assert_create_allowed(fake, s, b, i, **k))
    # 运行时安全补丁：gate 告警去重 helper 桩（_assert_create_allowed True 路径会调用 _gate_alert_clear）
    fake._gate_alert_counts = {}
    fake._gate_alert_clear = lambda identity: None
    fake._gate_alert_notify = lambda *a, **k: None
    fake._classify_create_exception = lambda e: 'not_found'
    fake._build_intent = lambda **k: {'stub': True}
    fake._update_registry = lambda *a, **k: None
    fake._verify_order_created = lambda oid, sym, kind: 'success'
    # P0 Batch A（2026-08-28）：_place_prepared 路径新增 G2 紧前复核（L5774/5934 解包
    # ` _g2_ok, _g2_reason = self._final_pre_create_check(...)`）+ G3b 原子提交
    # （L5842/6002）——未绑定时 MagicMock 迭代为空 → "not enough values to unpack"
    # 被 except 吞 → 下单中断（同 _assert_create_allowed 既有坑，绑定真实语义）
    import threading as _th
    fake._state_lock = _th.Lock()          # 生产同款非重入锁
    fake._persist_states = lambda all_s: None   # states 共享引用，无需落盘
    for _n in ('_final_pre_create_check', '_commit_protection_with_g3',
               '_g3a_converge_race_order', '_g3_cancel_race_order',
               '_g3_log_position_recheck', '_find_registry_identity_by_order_id'):
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n,
                    (lambda _n=_n: lambda *a, **k: getattr(CryptoTrader, _n)(fake, *a, **k))())
    # R2/R3（ChatGPT 终审 2026-08-20）：预生成挂单段新增 TP 补挂预检 helper
    #（本场景只测 READY Gate 独立性，TP 可行性校验语义由 test_sg4/sg3_p1 覆盖 → 纯桩放行）
    fake._tp_invalid_alerted = {}
    fake._tp_update_blocked = lambda *a, **k: False
    fake._mark_tp_param_invalid = lambda *a, **k: None
    fake._clear_tp_param_invalid = lambda *a, **k: None
    fake._check_tp_viability = lambda *a, **k: (True, '')
    layer_sl_params = [{'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'sell',
                        'amount': 0.01, 'params': {'stopPrice': 100.0}}]
    prepared_tp_params = {'symbol': SYMBOL, 'type': 'TAKE_PROFIT_MARKET', 'side': 'sell',
                          'amount': 0.01, 'params': {'stopPrice': 110.0}}
    CryptoTrader._place_prepared_orders_immediately(
        fake, SYMBOL, BATCH, 0, 0.01,
        prepared_tp_params, layer_sl_params, False, {}, [100.0])
    types = [c.get('type') for c in calls]
    ok = 'STOP_MARKET' in types and 'TAKE_PROFIT_MARKET' in types  # SL+TP 照常挂出
    report("场景6: NOT_READY + SL/TP 预挂 -> 保护操作不受影响", ok,
           f"(下单: {types}, _ready: {fake._ready})")


def scenario_7():
    """7: 临时 trader（无恢复）-> 默认 NOT_READY，Gate 拦截
    __init__ 真实执行需连交易所，故用源码断言锁死默认值 + 行为断言"""
    src = inspect.getsource(CryptoTrader.__init__)
    init_has_default = 'self._ready = False' in src
    # 行为断言：按 __init__ 默认值构造（模拟 bot_runner 临时实例的初始状态）
    temp = GateFake(ready=False)  # 即 __init__ 默认 self._ready = False
    ret = CryptoTrader.execute_signal(temp, FakeSignal())
    ok = init_has_default and ret is None and temp.gate_passed is False
    report("场景7: 临时 trader -> 默认 NOT_READY 被拦", ok,
           f"(__init__含默认False: {init_has_default}, 返回: {ret!r}, 过闸: {temp.gate_passed})")


async def _run_recovery_scenarios():
    """场景 3/4/5：bot_runner.run_trader_recovery_on_startup 的置位/保持语义
    （三个场景共用同一事件循环，避免 TRADER_LOCK 跨 loop 绑定问题）"""
    import bot_runner

    async def _nosleep(*a, **k):
        pass

    with mock.patch.object(bot_runner.asyncio, 'sleep', _nosleep):
        # 场景3: recover 返回 True（含 0 批次语义）-> READY
        mt = mock.MagicMock()
        mt.recover_active_batches.return_value = True
        await bot_runner.run_trader_recovery_on_startup(mt)
        ok3 = mt._ready is True and mt._not_ready_reason == ""
        report("场景3: recover=True(含0批次) -> READY=True", ok3,
               f"(_ready: {mt._ready}, reason: {mt._not_ready_reason!r})")

        # 场景4: recover 返回 False -> 永久 NOT_READY + reason 更新
        mt4 = mock.MagicMock()
        mt4._ready = False
        mt4._not_ready_reason = "启动恢复中（历史批次接管未完成）"
        mt4.recover_active_batches.return_value = False
        await bot_runner.run_trader_recovery_on_startup(mt4)
        ok4 = (mt4._ready is False
               and isinstance(mt4._not_ready_reason, str)
               and "恢复失败" in mt4._not_ready_reason
               and mt4.send_tg_notification.called)
        report("场景4: recover=False -> 永久 NOT_READY + 告警", ok4,
               f"(_ready: {mt4._ready}, reason: {mt4._not_ready_reason!r})")

        # 场景5: recover 异常 x3 -> 永久 NOT_READY + reason 更新
        mt5 = mock.MagicMock()
        mt5._ready = False
        mt5._not_ready_reason = "启动恢复中（历史批次接管未完成）"
        mt5.recover_active_batches.side_effect = RuntimeError("模拟恢复异常")
        await bot_runner.run_trader_recovery_on_startup(mt5)
        ok5 = (mt5._ready is False
               and isinstance(mt5._not_ready_reason, str)
               and mt5._not_ready_reason.startswith("恢复异常"))
        report("场景5: recover异常x3 -> 永久 NOT_READY", ok5,
               f"(_ready: {mt5._ready}, reason: {mt5._not_ready_reason!r})")


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_6()
    scenario_7()
    asyncio.run(_run_recovery_scenarios())
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("B2/SG1 READY 门控语义验收完成")
