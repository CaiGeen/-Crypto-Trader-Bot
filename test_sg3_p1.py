# -*- coding: utf-8 -*-
"""
C4/SG3-P1 SL/TP 保护单有效性校验 —— TDD 测试（红阶段）

规格（ChatGPT APPROVED，v2 两处必修已落实）：
  A. helper 纯语义矩阵（11 场景）—— _check_protection_order_validity
     ① 方向 side  ② 保护语义 reduceOnly/closePosition(单向)/positionSide(hedge)  ③ 数量≥已成交量(0.1%容差)
     必修1: hedge = side 匹配 且 positionSide 匹配（LONG+BUY=加仓非保护，方向条件不可省略）
  B. 零 API AST 断言 —— helper 内不得出现 fetch_*/create_order/cancel_order 调用
  C. SL 集成 —— invalid + user_modified=False → need_recover_sl=True → 既有恢复链(撤旧→挂新)被触发
  D. user_modified —— 必修2: 不改变 validity 判定(仍 invalid)，只改变动作(告警、不自动恢复、零下单)
  E. TP 对称 —— TP invalid → need_recover_tp=True → 恢复链触发（防只测 SL 漏 TP）

TDD 红阶段预期：helper 未实现 → A/B 组 AttributeError/断言失败；插入点未实现 → C/D/E 组恢复链不触发。
用法: .venv\\Scripts\\python.exe test_sg3_p1.py
"""
import ast
import inspect
import sys
import time
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_sg3p1_001"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


class ProbeReached(Exception):
    """第二轮轮询探针：time.sleep 第二次调用抛出 → 触发监控循环 L3029 异常捕获路径 → 测试驱动结束"""


# =====================================================================
# A 组：helper 纯语义矩阵（未绑定调用，红阶段 AttributeError=预期失败）
# =====================================================================

def _ord(side='SELL', amount=0.01, reduceOnly='true', closePosition='false', positionSide='BOTH'):
    """构造 ccxt 解析后的订单 dict（模拟 open_orders_map 中的值）"""
    return {
        'id': 'o1',
        'side': side,
        'amount': amount,
        'info': {
            'reduceOnly': reduceOnly,
            'closePosition': closePosition,
            'positionSide': positionSide,
        },
    }


def _call_helper(ord, expected_side='sell', is_hedge_mode=False, position_side='BOTH', required=0.01):
    """绑定调用 helper（helper 是纯判断器，self 不需要任何属性，传 None）"""
    fn = getattr(CryptoTrader, '_check_protection_order_validity')
    return fn(None, ord, expected_side, is_hedge_mode, position_side, required)


def _expect_valid(name, ord, **kw):
    try:
        valid, reason = _call_helper(ord, **kw)
    except AttributeError as e:
        report(f"A/{name}", False, f"[TDD红] helper 未实现: {e}")
        return
    report(f"A/{name}", valid is True, f"(reason={reason!r})")


def _expect_invalid(name, ord, **kw):
    try:
        valid, reason = _call_helper(ord, **kw)
    except AttributeError as e:
        report(f"A/{name}", False, f"[TDD红] helper 未实现: {e}")
        return
    report(f"A/{name}", valid is False, f"(reason={reason!r})")


def scenario_helper_matrix():
    """A 组：11 场景矩阵（ChatGPT 锁定）"""
    # 1: 正确 side + reduceOnly + 足量 → valid
    _expect_valid("正确side+reduceOnly+足量", _ord())
    # 2: 错 side → invalid
    _expect_invalid("错side", _ord(side='BUY'))
    # 3: 无 reduceOnly / closePosition → invalid
    _expect_invalid("无保护语义", _ord(reduceOnly='false', closePosition='false'))
    # 4: 数量不足 → invalid
    _expect_invalid("数量不足", _ord(amount=0.005), required=0.01)
    # 5: 数量在 0.1% 容差内 → valid（0.01*0.999 恰在容差线上）
    _expect_valid("数量容差边界", _ord(amount=0.01 * 0.999), required=0.01)
    # 6: 数量超过要求 → valid
    _expect_valid("数量超额", _ord(amount=0.02), required=0.01)
    # 7: amount=None + closePosition=true → valid（全仓平语义，跳过数量校验）
    _expect_valid("closePosition数量豁免", _ord(amount=None, closePosition='true'))
    # 8a: amount=None + 无 closePosition + 无 reduceOnly → invalid（保护语义先决，不因 None 判 fail）
    _expect_invalid("None且无保护语义", _ord(amount=None, reduceOnly='false', closePosition='false'))
    # 8b: amount=None + reduceOnly=true → valid（保护语义过，数量 None 跳过）
    _expect_valid("None+reduceOnly", _ord(amount=None))
    # 9: hedge 正确 side + 正确 positionSide → valid
    _expect_valid("hedge双向正确", _ord(positionSide='LONG'),
                  is_hedge_mode=True, position_side='LONG')
    # 10: hedge 错误 side + 正确 positionSide → invalid（必修1：LONG+BUY=加仓非保护）
    _expect_invalid("hedge错side对positionSide", _ord(side='BUY', positionSide='LONG'),
                    is_hedge_mode=True, position_side='LONG')
    # 11: hedge 正确 side + 错误 positionSide → invalid
    _expect_invalid("hedge对side错positionSide", _ord(side='SELL', positionSide='SHORT'),
                    is_hedge_mode=True, position_side='LONG')


# =====================================================================
# B 组：零 API AST 断言
# =====================================================================

def scenario_ast_zero_api():
    """B 组：helper 存在 + 函数体内不得出现 fetch_*/create_order/cancel_order 调用"""
    try:
        src = inspect.getsource(CryptoTrader)
    except Exception as e:
        report("B/helper定义", False, f"无法读取源码: {e}")
        return

    tree = ast.parse(src)
    helper_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_check_protection_order_validity':
            helper_node = node
            break
    report("B/helper定义存在", helper_node is not None,
           "(未找到 → [TDD红] 实现缺失)")

    if helper_node is not None:
        bad_calls = []
        for n in ast.walk(helper_node):
            if isinstance(n, ast.Call):
                func = n.func
                name = None
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                if name and (name.startswith('fetch_') or name in ('create_order', 'cancel_order')):
                    bad_calls.append(name)
        report("B/helper零API调用", not bad_calls, f"(违规调用: {bad_calls})")

    # 插入点结构断言：helper 调用必须出现在源码中（SL/TP 各至少一处）
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == '_check_protection_order_validity']
    report("B/插入点helper调用≥1", len(calls) >= 1,
           f"(调用点: {len(calls)} → [TDD红] 插入点缺失)")


# =====================================================================
# C/D/E 组：集成测试（真实驱动 _start_monitoring 第一轮）
# =====================================================================

def _make_sl_order(**kw):
    base = {'id': 'sl_1', 'side': 'SELL', 'amount': 0.01,
            'info': {'reduceOnly': 'true', 'closePosition': 'false', 'positionSide': 'BOTH'}}
    base.update(kw)
    # 同步 info 子字段：ccxt 实测 reduceOnly/closePosition/positionSide 在 info 中（顶层读不到），
    # 测试意图覆盖 info 子字段时 base.update(kw) 只会落在顶层，必须显式同步
    for k in ('reduceOnly', 'closePosition', 'positionSide'):
        if k in kw:
            base['info'][k] = kw[k]
    return base


def _make_tp_order(**kw):
    base = {'id': 'tp_1', 'side': 'SELL', 'amount': 0.01,
            'info': {'reduceOnly': 'true', 'closePosition': 'false', 'positionSide': 'BOTH'}}
    base.update(kw)
    # 同步 info 子字段（同 _make_sl_order）
    for k in ('reduceOnly', 'closePosition', 'positionSide'):
        if k in kw:
            base['info'][k] = kw[k]
    return base


def make_fake(states, open_orders):
    """MagicMock 基座 + 显式 stub 监控循环依赖（第一轮最小路径）"""
    fake = mock.MagicMock()
    fake.load_all_states = lambda: states
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)

    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_open_orders.return_value = open_orders
    ex.fetch_positions.return_value = []
    ex.fetch_ticker.return_value = {'last': 100.0}
    ex.cancel_order.return_value = {}
    ex.create_order.return_value = {'id': 'new_order'}
    fake.exchange = ex

    # 监控循环依赖 stub（None 持仓 → 跳过归零/部分减仓/持仓覆盖分支）
    fake._get_current_position_amt = lambda *a, **k: None
    fake._calculate_monitoring_interval = lambda: 60.0
    fake._sync_time_if_needed = lambda: None
    fake._check_ip_periodically = lambda: None
    fake.last_ip_check_time = time.time()
    fake.IP_CHECK_INTERVAL = 300.0
    fake._active_monitors_lock = mock.MagicMock()
    fake._active_monitors = set()
    fake._sg3_alerted = set()  # SG3-P1 告警节流集合（实现后使用）
    fake._api_cooldown_until = 0  # B2-3 gate 需真实数值（MagicMock getattr 陷阱）

    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake._cancel_remaining_entries = lambda *a, **k: None
    fake._cancel_limit_close_order = lambda *a, **k: None
    fake.clear_batch_state = lambda *a, **k: None
    fake._record_realized_pnl = lambda *a, **k: None
    fake._notify_snapshot = lambda *a, **k: None
    # 显式绑定 helper：MagicMock 会自动创建任意属性，不绑定会返回 MagicMock →
    # `valid, reason = ...` 解包抛 ValueError 被监控循环外层 except 吞掉（SG2 同款坑）
    if hasattr(CryptoTrader, '_assert_create_allowed'):
        fake._assert_create_allowed = (
            lambda s, b, i, **k: CryptoTrader._assert_create_allowed(fake, s, b, i, **k))
    fake._check_protection_order_validity = (
        lambda ord, expected_side, is_hedge_mode, position_side, required_amount:
        CryptoTrader._check_protection_order_validity(
            fake, ord, expected_side, is_hedge_mode, position_side, required_amount))
    return fake


def make_states(user_modified=False):
    return {
        SYMBOL: {
            BATCH: {
                'is_active': True,
                'side': 'BUY',
                'current_sl_id': 'sl_1',
                'tp_order_id': 'tp_1',
                'user_modified': user_modified,
                'stop_steps': [55000.0],
                'take_profit_price': 60000.0,
                'pending_sl_orders': [],
            }
        }
    }


def run_monitor(fake):
    """驱动 _start_monitoring：第一轮 sleep 正常，第二轮 sleep 抛探针结束驱动。
    探针被 L3029 except 捕获转 critical TG，函数经 finally 正常返回。"""
    with mock.patch.object(trader_260725.time, 'sleep', side_effect=[None, ProbeReached()]):
        CryptoTrader._start_monitoring(
            fake, SYMBOL, BATCH,
            entry_orders=['entry_1'],
            stop_steps=[55000.0], take_profit_price=60000.0,
            current_sl_id='sl_1', tp_order_id='tp_1',
            batch_total_amount=0.01, target_amounts=[0.01],
            params_base={}, is_hedge_mode=False, side='BUY',
            last_filled_count=1, filled_details=[100.0],
            total_entry_fee=0.0, pending_sl_orders=[],
            prepared_tp_params=None, layer_sl_params=None,
        )


def _sg3_alerts(fake):
    return [t for lv, t in fake.sent if 'SG3' in t]


def scenario_sl_invalid_recover():
    """C 组：SL invalid（缺 reduceOnly）+ user_modified=False → 恢复链触发（撤旧→挂新）"""
    states = make_states(user_modified=False)
    orders = [
        _make_sl_order(reduceOnly='false'),          # SL 无效
        _make_tp_order(),                            # TP 有效
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    cancel_n = fake.exchange.cancel_order.call_count
    create_n = fake.exchange.create_order.call_count
    sg3 = _sg3_alerts(fake)
    report("C/SL无效触发恢复(撤旧)", cancel_n >= 1, f"(cancel_order: {cancel_n} → [TDD红] 恢复链未触发)")
    report("C/SL无效触发恢复(挂新)", create_n >= 1, f"(create_order: {create_n} → [TDD红] 恢复链未触发)")

    types = [c.kwargs.get('type') for c in fake.exchange.create_order.call_args_list]
    report("C/恢复创建STOP_MARKET", 'STOP_MARKET' in types, f"(创建类型: {types})")
    report("C/SG3告警已发送", len(sg3) >= 1, f"(SG3告警: {len(sg3)} → [TDD红] 无告警)")


def scenario_sl_invalid_user_modified():
    """D 组（必修2）：SL invalid + user_modified=True → 告警 + 零 cancel/create"""
    states = make_states(user_modified=True)
    orders = [
        _make_sl_order(reduceOnly='false'),          # SL 无效
        _make_tp_order(),                            # TP 有效
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    cancel_n = fake.exchange.cancel_order.call_count
    create_n = fake.exchange.create_order.call_count
    sg3 = _sg3_alerts(fake)
    report("D/user_modified零撤单", cancel_n == 0, f"(cancel_order: {cancel_n} → 应零撤单)")
    report("D/user_modified零下单", create_n == 0, f"(create_order: {create_n} → 应零下单)")
    report("D/user_modified仍告警", len(sg3) >= 1, f"(SG3告警: {len(sg3)} → [TDD红] 豁免修复≠豁免告警)")


def scenario_tp_invalid_recover():
    """E 组：TP invalid（方向错）+ SL valid → need_recover_tp → 恢复链触发 TAKE_PROFIT_MARKET"""
    states = make_states(user_modified=False)
    orders = [
        _make_sl_order(),                            # SL 有效
        _make_tp_order(side='BUY'),                  # TP 方向反 → 无效
    ]
    fake = make_fake(states, orders)
    run_monitor(fake)

    create_n = fake.exchange.create_order.call_count
    cancel_n = fake.exchange.cancel_order.call_count
    types = [c.kwargs.get('type') for c in fake.exchange.create_order.call_args_list]
    sg3 = _sg3_alerts(fake)
    report("E/TP无效触发恢复", create_n >= 1 and cancel_n >= 1,
           f"(cancel: {cancel_n}, create: {create_n} → [TDD红] TP 恢复链未触发)")
    report("E/恢复创建TAKE_PROFIT_MARKET", 'TAKE_PROFIT_MARKET' in types, f"(创建类型: {types})")
    report("E/SG3告警已发送", len(sg3) >= 1, f"(SG3告警: {len(sg3)} → [TDD红] 无告警)")


def scenario_all_valid_noop():
    """C 组补充：SL/TP 均有效 → 零恢复、零 SG3 告警（不误报）"""
    states = make_states(user_modified=False)
    orders = [_make_sl_order(), _make_tp_order()]
    fake = make_fake(states, orders)
    run_monitor(fake)

    cancel_n = fake.exchange.cancel_order.call_count
    create_n = fake.exchange.create_order.call_count
    sg3 = _sg3_alerts(fake)
    report("C/全有效零撤单", cancel_n == 0, f"(cancel_order: {cancel_n})")
    report("C/全有效零下单", create_n == 0, f"(create_order: {create_n})")
    report("C/全有效零告警", len(sg3) == 0, f"(SG3告警: {len(sg3)})")


if __name__ == '__main__':
    print("#" * 60)
    print("C4/SG3-P1 保护单有效性校验测试（TDD）")
    print("运行基线: commit 973b901（插入点/helper 未实现 → 预期全红）")
    print("#" * 60)

    scenario_helper_matrix()
    scenario_ast_zero_api()
    scenario_sl_invalid_recover()
    scenario_sl_invalid_user_modified()
    scenario_tp_invalid_recover()
    scenario_all_valid_noop()

    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("C4/SG3-P1 保护单有效性校验验收完成")
