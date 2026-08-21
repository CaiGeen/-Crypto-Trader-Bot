# -*- coding: utf-8 -*-
"""
B2-8: 收编 5 处绕过仲裁的 Create 入口（规格 §5.7 验收：grep 仅剩 1+2 处）—— TDD 测试

背景（ChatGPT 终审 4 攻击点核查，2026-08-20）：
§5.7 要求 12 处收编 + 2 处平仓不收编；实际只收编 6 处自动保护单 + ENTRY（B2-3/B2-5/6）。
以下 5 处仍为裸 create（无 _assert_create_allowed、无 registry 意图落盘）→ 可绕过孤儿闭环：
  #1 L1101 用户修改 TP 换挂（撤旧→挂新）
  #2 L1220 用户修改 SL 换挂（撤旧→挂新）
  #3 L1402 保本损 BE 移动 SL（撤旧→挂新；撤旧失败仅警告不阻断 = 双单风险）
  #5 L3125 部分减仓换挂 SL（先挂新→再撤旧，M1 消除空窗期）
  #6 L3172 部分减仓换挂 TP（同上）

收编方案（B2-8）：
A. _assert_create_allowed 扩展 replace_order_id 参数（换挂语义）：
     CONFIRMED + replace_order_id == entry.order_id → 允许（确认的旧单将被撤销替换，无双单）
     CONFIRMED + 不匹配/未提供 → 拒绝（保持既有语义，防双单）
     未决态（PENDING_CREATE/PENDING_VERIFY/NOT_CONFIRMED/MISMATCH/HARD_LOCK）→ 仍拒绝（攻击点3闭环）
B. 5 处调用点接入：gate → PENDING_CREATE 意图落盘 → create → _verify_and_update_registry 三态
     #1/#2/#3（先撤旧）：撤旧成功/确认不存在 → 旧 identity 置 ABSENT（程序确认无单）→ gate 放行
     #5/#6（先挂新再撤旧）：gate(replace_order_id=旧id) → PENDING_CREATE → create → verify
C. 保本损撤旧失败 → 阻断（return False，不 create）—— 消除"撤旧失败仍挂新"的双单风险

TDD：本文件先红（replace_order_id 参数不存在 + 5 处无 gate + 保本损不阻断）→ 实施 B2-8 后全绿。
"""
import ast
import inspect
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b2_close_gap"
RESULTS = []

# B2-8 收编的 5 处 create 调用点（实施后 Grep 实测 2026-08-20；后续插入代码行号会漂移 → 更新）
#   #1 用户改TP  L1289  |  #2 用户改SL  L1438  |  #3 保本损  L1652
#   #5 部分减仓SL L3495  |  #6 部分减仓TP L3569
# ⚠️ 行号偏移记录：R1/R2/R3（ChatGPT 终审 2026-08-20）+~170 行后重新 Grep 实测 2026-08-20
# ⚠️ R-A/B/C/D（事件3四件套，2026-08-21）再偏移 +8~+152，重新实测 2026-08-21（AST 函数归属核实）：
#    用户改TP→update_batch_tp 1318 | 用户改SL→update_batch_sl 1469 | 保本损→_update_sl_no_validation 1683
#    | 部分减仓SL→_start_monitoring 3670 | 部分减仓TP→_start_monitoring 3744
GAP_CREATE_LINES = {1318, 1469, 1683, 3670, 3744}  # ChatGPT 终审补强 v3 + E2(+2) + R-A/B/C/D 后实测（2026-08-21）


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_base_fake():
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex
    fake._api_cooldown_until = 0
    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    return fake


def _bind_helpers(fake, states):
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    if hasattr(CryptoTrader, '_assert_create_allowed'):
        fake._assert_create_allowed = lambda s, b, i, **k: CryptoTrader._assert_create_allowed(fake, s, b, i, **k)
    if hasattr(CryptoTrader, '_verify_order_created'):
        fake._verify_order_created = lambda oid, s, kind='conditional': CryptoTrader._verify_order_created(
            fake, oid, s, kind)
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': 'o_sl_old',
        'tp_order_id': 'o_tp_old',
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'last_filled_count': 1,
        'target_amounts': [0.01],
        'params_base': {'positionSide': 'LONG', 'leverage': 50},
        'is_hedge_mode': False,
        'pending_sl_orders': [],
        'protection_registry': {},
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


def _sl_identity(layer=0):
    return f"{BATCH}|SL|L{layer}|LONG"


def _tp_identity(layer=0):
    return f"{BATCH}|TP|L{layer}|LONG"


def _reg_entry(state, order_id='o_sl_old', **over):
    e = {'state': state, 'order_id': order_id, 'id_known': True,
         'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
         'updated_at': time.time()}
    e.update(over)
    return e


# =====================================================================
# G1-G8：_assert_create_allowed 换挂语义（replace_order_id 参数）
# =====================================================================
def t_replace_semantics():
    if not hasattr(CryptoTrader, '_assert_create_allowed'):
        report("G1/CONFIRMED+ID匹配→允许", False, "[TDD红] 闸门未实现")
        report("G2/CONFIRMED+ID不匹配→拒绝", False, "[TDD红] 同 G1")
        report("G3/CONFIRMED无replace→拒绝", False, "[TDD红] 同 G1")
        report("G4/PENDING_VERIFY+replace→拒绝", False, "[TDD红] 同 G1")
        report("G5/NOT_CONFIRMED+replace→拒绝", False, "[TDD红] 同 G1")
        report("G6/PENDING_CREATE+replace→拒绝", False, "[TDD红] 同 G1")
        report("G7/HARD_LOCK+replace→拒绝", False, "[TDD红] 同 G1")
        report("G8/FAILED+replace→允许", False, "[TDD红] 同 G1")
        return

    def gate(fake, identity, replace=None):
        if replace is None:
            return fake._assert_create_allowed(SYMBOL, BATCH, identity)
        return fake._assert_create_allowed(SYMBOL, BATCH, identity, replace_order_id=replace)

    # G1: CONFIRMED + replace_order_id 匹配 → 允许（换挂：确认的旧单将被撤销替换）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('CONFIRMED', order_id='o_sl_old')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G1/CONFIRMED+ID匹配→允许", allowed is True, f"(allowed={allowed!r}, reason={reason!r} → 换挂语义)")
    except TypeError as e:
        report("G1/CONFIRMED+ID匹配→允许", False, f"[TDD红] replace_order_id 参数不存在: {e}")

    # G2: CONFIRMED + replace_order_id 不匹配 → 拒绝（旧单与声明不符，防错收编）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('CONFIRMED', order_id='o_sl_old')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_WRONG')
        report("G2/CONFIRMED+ID不匹配→拒绝", allowed is False, f"(allowed={allowed!r} → ID 不匹配禁换挂)")
    except TypeError as e:
        report("G2/CONFIRMED+ID不匹配→拒绝", False, f"[TDD红] {e}")

    # G3: CONFIRMED + 无 replace_order_id → 拒绝（既有语义保持，防双单）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('CONFIRMED')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = gate(fake, _sl_identity())
    report("G3/CONFIRMED无replace→拒绝", allowed is False, f"(allowed={allowed!r} → 既有语义保持)")

    # G4: PENDING_VERIFY + replace → 拒绝（结果未知，禁换挂——攻击点3闭环）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('PENDING_VERIFY')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G4/PENDING_VERIFY+replace→拒绝", allowed is False, f"(allowed={allowed!r} → 未决态禁换挂)")
    except TypeError as e:
        report("G4/PENDING_VERIFY+replace→拒绝", False, f"[TDD红] {e}")

    # G5: NOT_CONFIRMED + replace → 拒绝（查询不到≠不存在，禁换挂）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('NOT_CONFIRMED')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G5/NOT_CONFIRMED+replace→拒绝", allowed is False, f"(allowed={allowed!r} → 禁换挂，C5 根因)")
    except TypeError as e:
        report("G5/NOT_CONFIRMED+replace→拒绝", False, f"[TDD红] {e}")

    # G6: PENDING_CREATE + replace → 拒绝（意图已落盘，create 可能已发出）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('PENDING_CREATE', id_known=False)
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G6/PENDING_CREATE+replace→拒绝", allowed is False, f"(allowed={allowed!r} → 意图已落盘禁换挂)")
    except TypeError as e:
        report("G6/PENDING_CREATE+replace→拒绝", False, f"[TDD红] {e}")

    # G7: HARD_LOCK + replace → 拒绝（硬锁等待人工）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('HARD_LOCK')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G7/HARD_LOCK+replace→拒绝", allowed is False, f"(allowed={allowed!r} → 硬锁禁换挂)")
    except TypeError as e:
        report("G7/HARD_LOCK+replace→拒绝", False, f"[TDD红] {e}")

    # G8: FAILED + replace → 允许（FAILED 本就允许重试）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_sl_identity()] = _reg_entry('FAILED')
    fake = _bind_helpers(_make_base_fake(), states)
    try:
        allowed, reason = gate(fake, _sl_identity(), 'o_sl_old')
        report("G8/FAILED+replace→允许", allowed is True, f"(allowed={allowed!r} → FAILED 允许重试)")
    except TypeError as e:
        report("G8/FAILED+replace→允许", False, f"[TDD红] {e}")


# =====================================================================
# G9：AST 断言——5 处缺口 create 前存在 _assert_create_allowed（闸门）
# =====================================================================
def _is_attr_chain(node, parts):
    cur = node
    for p in reversed(parts[1:]):
        if not isinstance(cur, ast.Attribute):
            return False
        if cur.attr != p:
            return False
        cur = cur.value
    return isinstance(cur, ast.Name) and cur.id == parts[0]


def _is_safe_api_create(node):
    if not isinstance(node, ast.Call):
        return False
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == '_safe_api_call'):
        return False
    if not node.args:
        return False
    return _is_attr_chain(node.args[0], ('self', 'exchange', 'create_order'))


def _create_line(node):
    return node.args[0].lineno


def _enclosing_function(tree, node):
    best, best_span = None, None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(fn, 'end_lineno', None) or node.lineno
        if fn.lineno <= node.lineno <= end:
            if best_span is None or end < best_span:
                best, best_span = fn, end
    return best


def t_gap_coverage():
    with open(inspect.getsourcefile(CryptoTrader), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    gated, missing = [], []
    for node in ast.walk(tree):
        if not _is_safe_api_create(node):
            continue
        ln = _create_line(node)
        if ln not in GAP_CREATE_LINES:
            continue
        fn = _enclosing_function(tree, node)
        has_gate = False
        if fn:
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == '_assert_create_allowed'
                        and c.lineno < ln):
                    has_gate = True
                    break
        if has_gate:
            gated.append(ln)
        else:
            missing.append(ln)
    report("G9/5处缺口create前有闸门", len(gated) >= len(GAP_CREATE_LINES),
           f"(gated={sorted(gated)}/{len(GAP_CREATE_LINES)} → 缺失: {sorted(missing)})")


# =====================================================================
# G10：保本损撤旧失败 → 阻断不 create（_update_sl_no_validation 行为级）
# =====================================================================
def t_be_cancel_fail_blocks():
    b_data = {
        'is_active': True, 'side': 'BUY',
        'current_sl_id': 'o_sl_old', 'tp_order_id': 'o_tp_old',
        'last_filled_count': 1, 'target_amounts': [0.01],
        'params_base': {'positionSide': 'LONG'},
        'is_hedge_mode': False, 'stop_steps': [55000.0],
        'take_profit_price': 60000.0, 'protection_registry': {},
    }
    states = {SYMBOL: {BATCH: b_data}}
    fake = _bind_helpers(_make_base_fake(), states)
    # 撤旧抛 NetworkError（网络未知 ≠ 旧单不存在）→ 必须阻断，零 create
    fake.exchange.cancel_order.side_effect = ccxt.NetworkError('boom')
    fake.exchange.fetch_order = mock.MagicMock(return_value={
        'id': 'o_sl_old', 'symbol': SYMBOL, 'stopPrice': 55000.0, 'amount': 0.01,
        'side': 'sell', 'type': 'STOP_MARKET', 'reduceOnly': True})
    try:
        ok, msg = CryptoTrader._update_sl_no_validation(fake, SYMBOL, BATCH, b_data, 56000.0)
        blocked = (ok is False and fake.exchange.create_order.call_count == 0)
        report("G10/保本损撤旧失败→阻断零create", blocked,
               f"(ok={ok!r}, create={fake.exchange.create_order.call_count}, msg={str(msg)[:60]!r} → 必须阻断)")
    except TypeError:
        report("G10/保本损撤旧失败→阻断零create", False, "[TDD红] helper 签名不匹配")


def main():
    t_replace_semantics()   # G1-G8
    t_gap_coverage()        # G9
    t_be_cancel_fail_blocks()  # G10
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\nB2-8 收编 5 处缺口：{passed}/{total} 通过\n{'#' * 60}")


if __name__ == '__main__':
    main()
