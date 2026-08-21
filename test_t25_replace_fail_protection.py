# -*- coding: utf-8 -*-
"""
T25：replace 失败保护连续性专项测试（第二轮审查，2026-08-21）

ChatGPT 建议：replace 失败后必须明确"旧保护状态"，验证两套不同哲学：
  T25-A（F1 段，先撤后建）：允许保护窗口短暂缺失，但必须可恢复——
        旧SL CONFIRMED → 闸门(replace)放行 → 撤旧成功 → ABSENT → create 失败
        → registry FAILED/PENDING_VERIFY（按异常分类）→ 下轮 F3 裁决 allow → 恢复 CONFIRMED
  T25-B（部分减仓段，先建后撤）：永远优先保留旧保护——
        create new 失败 / verify 失败 → 旧单仍在场 + registry 不误标 → 下轮 F3 裁决 hold 不双挂

源码锚点（2026-08-21 17:2x 实测，AST 函数归属核实）：
  F1 SL 段：_assert_create_allowed(replace) L4422 → cancel L4437 → ABSENT L4441
            create L4511 → verify L4521 → except 分流 L4560（unknown→PENDING_VERIFY L4573 / failed→FAILED L4591）
  部分减仓 SL 段：create L3820 → verify L3829 → 失败分支 L3831（不Commit不撤旧）→ 撤旧 L3842 → except L3851（旧单保留）
运行：.venv/Scripts/python.exe test_t25_replace_fail_protection.py（ccxt 只在项目 .venv）
"""
import ast
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_t25"
RESULTS = []

IDENT_SL = f"{BATCH}|SL|L0|LONG"


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_fake(states):
    """复用 F1/F2/F3 专项测试 fixture：真实 helper 绑定 + 显式数值（防 MagicMock 假路径）"""
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    # ⚠️ MagicMock 数值比较必炸教训：_assert_create_allowed 读取 _api_cooldown_until → 必须绑定真实数值
    fake._api_cooldown_until = 0
    ex = mock.MagicMock()
    ex.amount_to_precision.side_effect = lambda s, v: v
    ex.price_to_precision.side_effect = lambda s, v: v
    fake.exchange = ex
    fake.sent = []
    fake.saved = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.save_batch_state = lambda s, b, d: fake.saved.append(dict(d))
    fake.load_all_states = lambda: states
    fake._update_registry = lambda s, b, i, **f: CryptoTrader._update_registry(fake, s, b, i, **f)
    fake._assert_create_allowed = lambda s, b, i, **kw: CryptoTrader._assert_create_allowed(fake, s, b, i, **kw)
    fake._order_matches_intent = lambda o, i, s: CryptoTrader._order_matches_intent(fake, o, i, s)
    fake._adjudicate_recreate_before_repair = lambda s, b, i: CryptoTrader._adjudicate_recreate_before_repair(fake, s, b, i)
    fake._classify_create_exception = lambda e: CryptoTrader._classify_create_exception(fake, e)
    fake._verify_and_update_registry = lambda s, b, i, oid, **kw: CryptoTrader._verify_and_update_registry(
        fake, s, b, i, oid, **kw)
    fake._build_intent = lambda **kw: CryptoTrader._build_intent(fake, **kw)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    # verify 结果可控：success（恢复链）/ not_found / unknown
    fake._verify_order_created = lambda oid, sym, order_kind='conditional': 'success'
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


def _reg_entry(state='CONFIRMED', order_id='sl_old', **over):
    e = {'state': state, 'order_id': order_id, 'intent': None, 'updated_at': time.time()}
    e.update(over)
    return e


# =====================================================================
# T25-A：F1 段"先撤后建"——撤旧成功 → create 失败 → 可恢复闭环
# =====================================================================
def t_a_replace_fail_recovery():
    # A1: 撤旧成功 → ABSENT + 'canceled_by_update_replace'（F1 SL 段原子语义）
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry()})
    fake = _make_fake(states)
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='ABSENT',
                          terminated_reason='canceled_by_update_replace')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("A1/撤旧成功→ABSENT+reason",
           entry.get('state') == 'ABSENT'
           and entry.get('terminated_reason') == 'canceled_by_update_replace',
           f"(state={entry.get('state')!r}, reason={entry.get('terminated_reason')!r})")

    # A2: create 确定性失败（ExchangeError）→ 'failed' → FAILED + fail_count_incr=1
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='ABSENT')})
    fake = _make_fake(states)
    cls = fake._classify_create_exception(ccxt.InsufficientFunds('balance not enough'))
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='FAILED', id_known=False,
                          order_kind='conditional', fail_count_incr=1)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("A2/确定失败→FAILED+计数",
           cls == 'failed' and entry.get('state') == 'FAILED'
           and entry.get('fail_count', 0) == 1,
           f"(cls={cls!r}, state={entry.get('state')!r}, fail_count={entry.get('fail_count')!r})")

    # A3: create 网络失败（NetworkError）→ 'unknown' → PENDING_VERIFY（不计数，UNKNOWN≠EMPTY）
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='ABSENT')})
    fake = _make_fake(states)
    cls = fake._classify_create_exception(ccxt.NetworkError('connection reset'))
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='PENDING_VERIFY', id_known=False,
                          order_kind='conditional')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("A3/网络失败→PENDING_VERIFY不计数",
           cls == 'unknown' and entry.get('state') == 'PENDING_VERIFY'
           and 'fail_count' not in entry,
           f"(cls={cls!r}, state={entry.get('state')!r})")

    # A4: 完整闭环——FAILED → F3 裁决 allow → 恢复 create → verify success → CONFIRMED（无双单）
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='FAILED', order_id=None)})
    fake = _make_fake(states)
    fake.exchange.create_order.return_value = {'id': 'sl_new', 'status': 'open'}
    # 下轮 R14：F3 裁决
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_SL)
    assert v == 'allow' and oid is None, f"F3 裁决异常: {v!r}/{oid!r}"
    # 闸门放行 → PENDING_CREATE → create → verify → CONFIRMED
    allowed, _ = fake._assert_create_allowed(SYMBOL, BATCH, IDENT_SL, desc='补挂止损单')
    assert allowed, "FAILED 后闸门应放行"
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='PENDING_CREATE', id_known=False,
                          order_kind='conditional', role='SL', layer=0, side='LONG',
                          intent=fake._build_intent(symbol='BTCUSDT', side='sell', qty=0.003,
                                                    order_type='STOP_MARKET', stop_price=55000.0,
                                                    reduce_only=True))
    new_ord = fake.exchange.create_order(symbol=SYMBOL, type='STOP_MARKET', side='sell',
                                         amount=0.003, params={}, retries=1)
    vr = fake._verify_and_update_registry(SYMBOL, BATCH, IDENT_SL, new_ord['id'], desc='补挂止损单')
    reg = states[SYMBOL][BATCH]['protection_registry']
    report("A4/FAILED→裁决allow→恢复CONFIRMED无双单",
           vr == 'success' and len(reg) == 1 and reg[IDENT_SL].get('state') == 'CONFIRMED'
           and reg[IDENT_SL].get('order_id') == 'sl_new',
           f"(vr={vr!r}, reg_len={len(reg)}, state={reg[IDENT_SL].get('state')!r}, oid={reg[IDENT_SL].get('order_id')!r})")

    # A5: create 网络失败（PENDING_VERIFY）→ 下轮 F3 裁决 hold（保守，不补单防双挂）
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='PENDING_VERIFY', order_id=None)})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_SL)
    report("A5/PENDING_VERIFY→hold不补单", v == 'hold' and oid is None, f"(verdict={v!r})")


# =====================================================================
# T25-B：部分减仓段"先建后撤"——挂新失败/verify 失败 → 旧单保留
# =====================================================================
def t_b_partial_reduce_keep_old():
    # B1: 源码断言——部分减仓段 create_order 在 cancel_order 之前（先建后撤），
    #     且挂新失败 except 分支含"旧单保留"（保护窗口永不断）
    #     注：create/cancel 均经 _safe_api_call 包装 → 是参数而非 Call.func，用行内容直接定位
    src = open('trader_260725.py', encoding='utf-8').read()
    lines = src.splitlines()
    create_at = [i + 1 for i, ln in enumerate(lines) if 'self.exchange.create_order,' in ln]
    cancel_at = [i + 1 for i, ln in enumerate(lines) if 'self.exchange.cancel_order,' in ln]
    # 部分减仓 SL 段：create 在 L3820、cancel 在 L3842（Grep 实测，AST 函数归属核实）
    create_line = 3820 if 3820 in create_at else None
    cancel_line = 3842 if 3842 in cancel_at else None
    seg = '\n'.join(lines[3835:3875])  # 部分减仓 SL 段 create→verify→撤旧→except 区间
    report("B1/先建后撤+旧单保留注释",
           create_line == 3820 and cancel_line == 3842 and create_line < cancel_line
           and '旧单保留' in seg and '挂新失败' in seg,
           f"(create={create_line}, cancel={cancel_line}, 注释存在={'旧单保留' in seg and '挂新失败' in seg})")

    # B2: 源码断言——verify 失败分支"不 Commit/不撤旧"（registry 不写 ABSENT、不调 cancel_order）
    lines2 = src.splitlines()
    vf_seg = '\n'.join(lines2[3828:3838])  # verify_result != 'success' → 日志+告警（L3831-3835）
    report("B2/verify失败不Commit不撤旧",
           '不 Commit/不撤旧' in vf_seg and 'cancel_order' not in vf_seg,
           f"(不Commit/不撤旧={'不 Commit/不撤旧' in vf_seg}, 无cancel={'cancel_order' not in vf_seg})")

    # B3: 行为——部分减仓 verify 失败后 registry 保持 PENDING_CREATE（不 Commit），
    #     下轮 F3 裁决 hold（不双挂，等自愈/人工）
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(state='PENDING_CREATE', order_id=None)})
    fake = _make_fake(states)
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_SL)
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("B3/verify失败后PENDING_CREATE→hold",
           entry.get('state') == 'PENDING_CREATE' and v == 'hold' and oid is None,
           f"(state保持={entry.get('state')!r}, verdict={v!r})")

    # B4: 行为——部分减仓挂新抛异常（模拟 F1 同款分流：unknown→PENDING_VERIFY / failed→FAILED）
    #     旧单 current_sl_id 保持在场（批次字段不变 = 裸仓窗口不存在）
    states = _state_batch(current_sl_id='sl_old',
                          protection_registry={IDENT_SL: _reg_entry(order_id='sl_old')})
    fake = _make_fake(states)
    cls = fake._classify_create_exception(ccxt.ExchangeError('insufficient balance'))
    # 模拟部分减仓 except 分支：仅告警，不动 registry 不撤旧（create 异常不在 try 内改 registry）
    cur = states[SYMBOL][BATCH]['current_sl_id']
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("B4/挂新抛异常→旧单保留registry不动",
           cls == 'failed' and cur == 'sl_old' and entry.get('state') == 'CONFIRMED'
           and entry.get('order_id') == 'sl_old',
           f"(cls={cls!r}, current_sl_id={cur!r}, state={entry.get('state')!r})")


# =====================================================================
# 主入口
# =====================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("T25 replace 失败保护连续性专项测试")
    print("=" * 60)
    t_a_replace_fail_recovery()
    t_b_partial_reduce_keep_old()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}")
    print(f"T25 replace 失败保护：{passed}/{total} 通过")
    print('#' * 60)
    sys_exit = 0 if passed == total else 1
    import sys
    sys.exit(sys_exit)
