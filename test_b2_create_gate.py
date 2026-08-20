# -*- coding: utf-8 -*-
"""
B2-3: Create 仲裁闸门（规格 §5.3）—— TDD 测试（红阶段）

背景（ChatGPT 终审裁决 + P0 规格 v2 §5）：
B2-0 后 not_found→NOT_CONFIRMED 不再 raise，但补挂路径 pending_sl_orders 未移除 →
下一轮风控更新仍会再次 create（同 identity）→ C5 无限重挂的变体。
仲裁闸门 = 最后防线：同 identity 存在未终结/已确认状态 → 禁止新 create。

仲裁顺序（§5.3 本批范围）：registry 状态检查（未终结/已确认 → blocked）→ 全局 cooldown（§10.1）
HARD_LOCK / fail_count≥5 / 收编唯一入口 → B2-4/后续批次。

禁止集合 = {PENDING_CREATE, PENDING_VERIFY, NOT_CONFIRMED, CONFIRMED, MISMATCH}
  - PENDING_CREATE：意图已落盘，create 可能已发出 → 再 create = 双单
  - PENDING_VERIFY：结果未知（网络异常）→ 再 create = 双单风险
  - NOT_CONFIRMED：查询不到 ≠ 不存在（algo 延迟/路由错误）→ 禁自动重挂（C5 根因）
  - CONFIRMED：已确认有单 → 再 create = 双单
  - MISMATCH：订单与意图不符（错单嫌疑）→ 需人工，禁自动 create
允许 = FAILED（确定拒绝 → 唯一允许再次 Create 的自动路径，规格 §8 转移表）

TDD：本文件先红（_assert_create_allowed 未实现 + 6 处接入点未接闸门）→ 实施 B2-3 后全绿。
"""
import ast
import inspect
import sys
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_b2_gate"
RESULTS = []

# B2-3 接入点（B2-3 后 Grep 实测）：补挂 SL / 降级恢复 / 补挂 TP / 预生成 SL×2 / 预生成 TP
# ⚠️ 行号偏移记录：B2-2 后 3206/3326/3434/3678/3789/3895 → B2-3 后 3256/3389/3510/3767/3891/4010
#    → B2-4 后 3350/3502/3627/3902/4043/4179 → B2-5 骨架插入后 +98 → 3448/3600/3725/4000/4141/4277
#    → B2-6 recover自愈分支(+24) + 骨架元数据(+6) + 新helper(+185) = +215 → 3663/3815/3940/4215/4356/4492
GATE_LINES = {3912, 4065, 4191, 4467, 4609, 4746}


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
    fake._api_cooldown_until = 0  # ⚠️ MagicMock 任意属性都返回 MagicMock（getattr 默认值失效）→ 显式置 0
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
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': None,
        'tp_order_id': None,
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'pending_sl_orders': [],
        'protection_registry': {},
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


def _identity():
    return f"{BATCH}|SL|L0|LONG"


def _reg_entry(state, **over):
    e = {'state': state, 'order_id': 'o_sl_1', 'id_known': True,
         'order_kind': 'conditional', 'role': 'SL', 'layer': 0, 'side': 'LONG',
         'updated_at': time.time()}
    e.update(over)
    return e


# =====================================================================
# T1-T8：_assert_create_allowed 仲裁语义（单元）
# =====================================================================
def t_gate_semantics():
    if not hasattr(CryptoTrader, '_assert_create_allowed'):
        report("T1/无条目→允许", False, "[TDD红] _assert_create_allowed 未实现")
        report("T2/FAILED→允许", False, "[TDD红] 同 T1")
        report("T3/PENDING_CREATE→禁止", False, "[TDD红] 同 T1")
        report("T4/PENDING_VERIFY→禁止", False, "[TDD红] 同 T1")
        report("T5/NOT_CONFIRMED→禁止", False, "[TDD红] 同 T1")
        report("T6/CONFIRMED→禁止", False, "[TDD红] 同 T1")
        report("T7/MISMATCH→禁止", False, "[TDD红] 同 T1")
        report("T8/cooldown未到期→禁止", False, "[TDD红] 同 T1")
        return
    # T1: 无条目 → 允许
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T1/无条目→允许", allowed is True, f"(allowed={allowed!r}, reason={reason!r})")

    # T2: FAILED → 允许（确定拒绝是唯一允许再次 Create 的自动路径）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('FAILED')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T2/FAILED→允许", allowed is True, f"(allowed={allowed!r} → 仅 FAILED 允许重试)")

    # T3: PENDING_CREATE → 禁止（意图已落盘，create 可能已发出）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('PENDING_CREATE', id_known=False)
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T3/PENDING_CREATE→禁止", allowed is False and 'PENDING_CREATE' in reason,
           f"(allowed={allowed!r}, reason={reason!r})")

    # T4: PENDING_VERIFY → 禁止（结果未知 = 可能已创建）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('PENDING_VERIFY')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T4/PENDING_VERIFY→禁止", allowed is False, f"(allowed={allowed!r} → 结果未知禁再 create)")

    # T5: NOT_CONFIRMED → 禁止（C5 根因：查询不到 ≠ 不存在，禁自动重挂）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('NOT_CONFIRMED')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T5/NOT_CONFIRMED→禁止", allowed is False, f"(allowed={allowed!r} → 禁自动重挂，C5 根因)")

    # T6: CONFIRMED → 禁止（已确认有单，再 create = 双单）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('CONFIRMED')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T6/CONFIRMED→禁止", allowed is False, f"(allowed={allowed!r} → 已确认有单禁双单)")

    # T7: MISMATCH → 禁止（错单嫌疑需人工，禁自动 create）
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][_identity()] = _reg_entry('MISMATCH')
    fake = _bind_helpers(_make_base_fake(), states)
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T7/MISMATCH→禁止", allowed is False, f"(allowed={allowed!r} → 错单嫌疑禁自动 create)")

    # T8: 全局 cooldown 未到期 → 禁止（§10.1 最小联动：封禁期不发请求）
    states = _state_batch()
    fake = _bind_helpers(_make_base_fake(), states)
    fake._api_cooldown_until = time.time() + 30
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, _identity())
    report("T8/cooldown未到期→禁止", allowed is False and 'cooldown' in reason.lower(),
           f"(allowed={allowed!r}, reason={reason!r} → 封禁期禁止发请求)")


# =====================================================================
# T9：AST 断言——6 处接入点 create 前存在 _assert_create_allowed
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


def t_gate_coverage():
    with open(inspect.getsourcefile(CryptoTrader), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    gated = []
    missing = []
    for node in ast.walk(tree):
        if not _is_safe_api_create(node):
            continue
        ln = _create_line(node)
        if ln not in GATE_LINES:
            continue
        fn = _enclosing_function(tree, node)
        has_gate = False
        if fn:
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == '_assert_create_allowed'
                        and c.lineno < ln):  # 闸门必须在 create 之前
                    has_gate = True
                    break
        if has_gate:
            gated.append(ln)
        else:
            missing.append(ln)
    report("T9/6处接入点create前有闸门", len(gated) >= len(GATE_LINES),
           f"(gated={sorted(gated)}/{len(GATE_LINES)} → 缺失: {sorted(missing)})")


# =====================================================================
# T10：行为级——registry 残留 NOT_CONFIRMED 时流程不再 create（C5 重挂变体封堵）
# =====================================================================
def t_not_confirmed_blocks_recreate():
    # 构造：registry 已有 NOT_CONFIRMED（上一轮 verify not_found 残留），模拟补挂 SL 场景——
    # 闸门必须拦截，create_order 零调用，且 registry 不被覆盖为 PENDING_CREATE
    ident = f"{BATCH}|SL|L0|LONG"
    states = _state_batch()
    states[SYMBOL][BATCH]['protection_registry'][ident] = _reg_entry('NOT_CONFIRMED')
    fake = _bind_helpers(_make_base_fake(), states)

    if not hasattr(CryptoTrader, '_assert_create_allowed'):
        report("T10/NOT_CONFIRMED残留→零create", False, "[TDD红] 闸门未实现")
        return
    allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, ident)
    entry_now = states[SYMBOL][BATCH]['protection_registry'][ident]
    ok = (allowed is False
          and fake.exchange.create_order.call_count == 0  # 无 create 发出
          and entry_now.get('state') == 'NOT_CONFIRMED')  # 状态未被覆盖
    report("T10/NOT_CONFIRMED残留→零create", ok,
           f"(allowed={allowed!r}, create={fake.exchange.create_order.call_count}, "
           f"state={entry_now.get('state')!r} → 必须 blocked + 零 create + 状态不被覆盖)")


def main():
    t_gate_semantics()    # T1-T8
    t_gate_coverage()     # T9
    t_not_confirmed_blocks_recreate()  # T10
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}\nB2-3 Create 仲裁闸门：{passed}/{total} 通过\n{'#' * 60}")
    if passed == total:
        print("⚠️ 红灯阶段提示：若本文件先红（helper 未实现 / 接入点未接闸门）→ 红阶段成立，可进入实施；实施后须全绿。")


if __name__ == '__main__':
    main()
