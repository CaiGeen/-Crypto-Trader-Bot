# -*- coding: utf-8 -*-
"""
B1（P0-2/P0-3）状态机语义 + verify order_kind 路由 + registry —— TDD 测试（红阶段）

规格：P0最终规格_状态机与Create仲裁_v2_送审ChatGPT.md
  §3.2 FAILED 精确分类（ExchangeError→failed / NetworkError→unknown）
  §5.1 幂等键 batch_id|role|L{layer}|side
  §5.2 registry 持久化结构与字段
  §6.3 身份匹配/重查自愈（只补 Commit 不新建）
  §13 规格攻击测试三问（场景②③④⑦⑨）

TDD：本文件先红（新语义/新 helper 未实现）→ 实施 B1 后全绿。
"""
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b1_001"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_base_fake():
    """通用 fake：真实 registry/verify/classify helper 绑定，交易所调用透传"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    ex.fetch_order.return_value = {'id': 'o1', 'status': 'NEW'}
    ex.create_order.return_value = {'id': 'o_new'}
    fake.exchange = ex

    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    return fake


def _bind_helpers(fake, states):
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._recheck_registry_self_heal = lambda s, b: CryptoTrader._recheck_registry_self_heal(fake, s, b)
    # ⚠️ MagicMock 坑（同 test_sg4）：fake 是 MagicMock，未显式绑定的方法会退化为自动 mock
    # （调用返回 MagicMock，不执行真实语义 → verify 恒 success / classify 恒 failed）。
    fake._verify_order_created = lambda oid, sym, kind='conditional': CryptoTrader._verify_order_created(fake, oid, sym, kind)
    fake._classify_create_exception = lambda e: CryptoTrader._classify_create_exception(fake, e)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': None,
        'tp_order_id': 'tp_1',
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'pending_sl_orders': [],
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


# =====================================================================
# T1/T2: verify order_kind 路由（P0-3）——conditional → params={'stop': True}
# =====================================================================

def _verify_kind(kind):
    fake = _make_base_fake()
    fn = getattr(CryptoTrader, '_verify_order_created')
    return fn(fake, 'o1', SYMBOL, kind), fake.exchange.fetch_order


def scenario_verify_kind_routing():
    try:
        res, fetch = _verify_kind('conditional')
    except TypeError as e:
        report("T1/conditional带stop=True", False, f"[TDD红] _verify_order_created 无 order_kind 参数: {e}")
        return
    got = fetch.call_args.kwargs.get('params') if fetch.call_args else None
    report("T1/conditional带stop=True", res == 'success' and got == {'stop': True},
           f"(结果={res!r}, params={got!r} → 必须 {{'stop': True}}，否则查错端点=假阴性)")

    try:
        res2, fetch2 = _verify_kind('normal')
    except TypeError as e:
        report("T2/normal不带stop参数", False, f"[TDD红] {e}")
        return
    got2 = fetch2.call_args.kwargs.get('params') if fetch2.call_args else None
    report("T2/normal不带stop参数", res2 == 'success' and got2 is None,
           f"(params={got2!r} → normal 通道不得带 stop)")


# =====================================================================
# T3: create 异常分类（§3.2 FAILED 精确分类）
# =====================================================================

def _classify(e):
    return CryptoTrader._classify_create_exception(mock.MagicMock(), e)


def scenario_create_exception_classify():
    try:
        r_ex = _classify(ccxt.ExchangeError("rejected"))
        r_net = _classify(ccxt.NetworkError("net down"))
        r_to = _classify(ccxt.RequestTimeout("timeout"))
        r_418 = _classify(ccxt.RateLimitExceeded("418"))
        r_other = _classify(KeyError("boom"))
    except AttributeError as e:
        report("T3/异常分类", False, f"[TDD红] _classify_create_exception 未实现: {e}")
        return
    report("T3/异常分类", r_ex == 'failed' and r_net == 'unknown' and r_to == 'unknown'
           and r_418 == 'unknown' and r_other == 'unknown',
           f"(ExchangeError={r_ex!r}, NetworkError={r_net!r}, RequestTimeout={r_to!r}, "
           f"RateLimit={r_418!r}, 其他={r_other!r})")


# =====================================================================
# T4: 幂等键格式（§5.1）
# =====================================================================

def scenario_identity_format():
    try:
        got = CryptoTrader._protection_identity(mock.MagicMock(), 'batch_1', 'SL', 2, 'LONG')
    except AttributeError as e:
        report("T4/幂等键格式", False, f"[TDD红] _protection_identity 未实现: {e}")
        return
    report("T4/幂等键格式", got == 'batch_1|SL|L2|LONG',
           f"(实际={got!r} → 必须 batch_id|role|L{'{layer}'}|side，旧/新批次同层不互认)")


# =====================================================================
# T5: registry 落盘（§5.2）
# =====================================================================

def scenario_registry_persist():
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        CryptoTrader._update_registry(
            fake, SYMBOL, BATCH, 'batch_1|SL|L0|LONG',
            state='PENDING_VERIFY', order_id='o_sl', id_known=True,
            order_kind='conditional', role='SL', layer=0, side='LONG')
    except AttributeError as e:
        report("T5/registry落盘", False, f"[TDD红] _update_registry 未实现: {e}")
        return
    reg = states.get(SYMBOL, {}).get(BATCH, {}).get('protection_registry', {})
    entry = reg.get('batch_1|SL|L0|LONG', {})
    ok = (entry.get('state') == 'PENDING_VERIFY' and entry.get('order_id') == 'o_sl'
          and entry.get('id_known') is True and entry.get('order_kind') == 'conditional'
          and 'updated_at' in entry)
    report("T5/registry落盘", ok, f"(entry={entry})")


# =====================================================================
# T6: 预生成 SL verify not_found → NOT_CONFIRMED（§13 场景④⑤，禁重试禁补单）
# =====================================================================

def _run_pregen_sl(fake):
    layer_sl_params = [{
        'symbol': SYMBOL, 'type': 'STOP_MARKET', 'side': 'sell',
        'amount': 0.01, 'params': {'stopPrice': 55000.0},
    }]
    CryptoTrader._place_prepared_orders_immediately(
        fake, SYMBOL, BATCH, idx=0, batch_filled_amount=0.01,
        prepared_tp_params=None, layer_sl_params=layer_sl_params,
        is_hedge_mode=False, params_base={}, stop_steps=[55000.0])


def scenario_pregen_sl_not_found():
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.side_effect = ccxt.OrderNotFound("gone")
    try:
        _run_pregen_sl(fake)
    except Exception as e:
        report("T6/not_found→NOT_CONFIRMED", False,
               f"[TDD红] not_found 仍走异常重试路径（raise→fail_count++）: {type(e).__name__}: {e}")
        return
    b = states.get(SYMBOL, {}).get(BATCH, {})
    reg = b.get('protection_registry', {})
    entry = next((e for e in reg.values() if e.get('state') == 'NOT_CONFIRMED'), None)
    alerts = [t for lv, t in fake.sent if lv == 'critical']
    ok = (entry is not None
          and b.get('sl_fail_count') is None          # 不计数（NOT_CONFIRMED 非 FAILED）
          and b.get('current_sl_id') is None           # 不 Commit
          and fake.exchange.create_order.call_count == 1  # 不二次 Create
          and any('NOT_CONFIRMED' in t or '不会自动补单' in t for t in alerts))
    report("T6/not_found→NOT_CONFIRMED", ok,
           f"(registry: {entry}, sl_fail_count: {b.get('sl_fail_count')}, "
           f"create次数: {fake.exchange.create_order.call_count}, critical: {len(alerts)})")


# =====================================================================
# T7/T8: 重查自愈（只补 Commit 不新建，§6.3 / §13 场景⑦）
# =====================================================================

def scenario_recheck_self_heal():
    # T7: PENDING_VERIFY(id_known) + fetch 成功 → CONFIRMED + 补 Commit
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'] = {
        'b1|SL|L0|LONG': {'state': 'PENDING_VERIFY', 'order_id': 'o_sl', 'id_known': True,
                          'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG'}
    }
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.fetch_order.return_value = {'id': 'o_sl', 'status': 'NEW'}
    try:
        CryptoTrader._recheck_registry_self_heal(fake, SYMBOL, BATCH)
    except AttributeError as e:
        report("T7/重查自愈CONFIRMED", False, f"[TDD红] _recheck_registry_self_heal 未实现: {e}")
        return
    b = states.get(SYMBOL, {}).get(BATCH, {})
    entry = b.get('protection_registry', {}).get('b1|SL|L0|LONG', {})
    ok = (entry.get('state') == 'CONFIRMED' and b.get('current_sl_id') == 'o_sl'
          and fake.exchange.create_order.call_count == 0)   # 收编已存在订单，绝不新建
    report("T7/重查自愈CONFIRMED", ok,
           f"(state={entry.get('state')}, current_sl_id={b.get('current_sl_id')}, create={fake.exchange.create_order.call_count})")

    # T8: NOT_CONFIRMED 再查不到 → 维持 + 零 Create + 静默（不重复告警）
    states2 = _state_batch()
    states2[SYMBOL][BATCH]['protection_registry'] = {
        'b1|SL|L0|LONG': {'state': 'NOT_CONFIRMED', 'order_id': 'o_sl', 'id_known': True,
                          'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG'}
    }
    fake2 = _bind_helpers(_make_base_fake(), states2)
    fake2.exchange.fetch_order.side_effect = ccxt.OrderNotFound("gone")
    CryptoTrader._recheck_registry_self_heal(fake2, SYMBOL, BATCH)
    b2 = states2.get(SYMBOL, {}).get(BATCH, {})
    e2 = b2.get('protection_registry', {}).get('b1|SL|L0|LONG', {})
    ok2 = (e2.get('state') == 'NOT_CONFIRMED' and fake2.exchange.create_order.call_count == 0
           and b2.get('current_sl_id') is None)
    report("T8/重查NOT_CONFIRMED维持", ok2,
           f"(state={e2.get('state')}, create={fake2.exchange.create_order.call_count}, "
           f"current_sl_id={b2.get('current_sl_id')})")


# =====================================================================
# T9: create NetworkError → PENDING_VERIFY(id_unknown)，禁计数禁补单（§3.2 / §13 场景⑥）
# =====================================================================

def scenario_pregen_sl_network_error():
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake.exchange.create_order.side_effect = ccxt.NetworkError("timeout")
    try:
        _run_pregen_sl(fake)
    except Exception as e:
        report("T9/NetworkError→PENDING_VERIFY", False,
               f"[TDD红] create 网络异常仍按普通失败处理: {type(e).__name__}: {e}")
        return
    b = states.get(SYMBOL, {}).get(BATCH, {})
    reg = b.get('protection_registry', {})
    entry = next((e for e in reg.values() if e.get('state') == 'PENDING_VERIFY'), None)
    alerts = [t for lv, t in fake.sent if lv == 'critical']
    ok = (entry is not None and entry.get('id_known') is False
          and b.get('sl_fail_count') is None           # 结果未知 ≠ 失败，不计数
          and fake.exchange.create_order.call_count == 1  # 不二次 Create
          and any('结果未知' in t or 'UNKNOWN' in t for t in alerts))
    report("T9/NetworkError→PENDING_VERIFY", ok,
           f"(registry: {entry}, sl_fail_count: {b.get('sl_fail_count')}, "
           f"create次数: {fake.exchange.create_order.call_count}, critical: {len(alerts)})")


if __name__ == '__main__':
    print("#" * 60)
    print("B1 状态机语义 + verify order_kind + registry TDD 测试")
    print("状态: 红阶段（新语义/新 helper 未实现 → 应 FAIL）")
    print("#" * 60)

    scenario_verify_kind_routing()       # T1/T2
    scenario_create_exception_classify()  # T3
    scenario_identity_format()           # T4
    scenario_registry_persist()          # T5
    scenario_pregen_sl_not_found()       # T6
    scenario_recheck_self_heal()         # T7/T8
    scenario_pregen_sl_network_error()   # T9

    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    passed = [n for n, p in RESULTS if p]
    print(f"❌ FAIL {len(failed)}/{len(RESULTS)}: {failed}")
    print(f"✅ PASS {len(passed)}/{len(RESULTS)}: {passed}")
    if failed:
        print("→ 红阶段成立：新语义未实现，可进入 B1 实施")
        sys.exit(1)
    print("⚠️ 无 FAIL —— 红阶段不成立：新语义已存在，需复核测试是否锁错规格")
