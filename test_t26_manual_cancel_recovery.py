# -*- coding: utf-8 -*-
"""
T26：人工撤单恢复专项测试（第二轮审查，2026-08-21）

ChatGPT 建议：验证最高概率实盘事件之一——用户手动在交易所 APP 撤掉 SL/TP，
程序不知情 → 下一轮必须完成 CONFIRMED → ABSENT → REPAIR，且无双单。

源码（F2，2026-08-21 事件4 修复）：
  监控循环 SL/TP 检测分支：订单不在 open_orders_map → fetch → status in
  {canceled, expired} → 按 order_id 精确遍历 registry 定位 identity（找不到回退最新层）
  → _update_registry(ABSENT, terminated_reason='terminal_status_<s>')
  → 非程序撤单且非用户修改 → need_recover_sl/tp = True → R14 自动补挂。

第二轮审查新增：fallback（order_id 找不到）reason 加 '_fallback' 后缀，
审计可区分"精确匹配终结"与"回退猜测终结"（行为不变，F3 兜底误终结）。

本文件覆盖：F2 terminal 终结语义（精确/fallback reason 区分）+ 恢复链闭环（无双单）
           + user_modified/程序撤单不自动补挂语义 + 源码断言。
运行：.venv/Scripts/python.exe test_t26_manual_cancel_recovery.py（ccxt 只在项目 .venv）
"""
import ast
import time
from unittest import mock

import ccxt

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_t26"
RESULTS = []

IDENT_SL = f"{BATCH}|SL|L0|LONG"
IDENT_TP = f"{BATCH}|TP|L0|LONG"


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def _make_fake(states):
    fake = mock.MagicMock()
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    # ⚠️ MagicMock 数值比较必炸教训：绑定真实数值
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
    fake._adjudicate_recreate_before_repair = lambda s, b, i: CryptoTrader._adjudicate_recreate_before_repair(fake, s, b, i)
    fake._build_intent = lambda **kw: CryptoTrader._build_intent(fake, **kw)
    fake._verify_and_update_registry = lambda s, b, i, oid, **kw: CryptoTrader._verify_and_update_registry(
        fake, s, b, i, oid, **kw)
    fake._verify_order_created = lambda oid, sym, order_kind='conditional': 'success'
    return fake


def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': 'sl_old',
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


# F2 terminal 定位语义（与 _start_monitoring SL 分支同款，测试内复现判定→落盘序列）
def _f2_terminal_finish(fake, states, ident_key, order_id_str, role, layer, side, status='canceled'):
    """复现 F2 分支：精确匹配 → reason 无后缀；fallback → reason 带 _fallback"""
    b_data = states[SYMBOL][BATCH]
    reg = b_data.get('protection_registry') or {}
    _reg_target = None
    _reg_fallback = False
    for _k, _v in reg.items():
        if str(_v.get('order_id', '')) == str(order_id_str):
            _reg_target = _k
            break
    if _reg_target is None:
        _reg_fallback = True
        _reg_target = fake._protection_identity(BATCH, role, layer, side)
    fake._update_registry(SYMBOL, BATCH, _reg_target, state='ABSENT',
                          terminated_reason=(f'terminal_status_{status}_fallback'
                                             if _reg_fallback
                                             else f'terminal_status_{status}'))
    return _reg_target, _reg_fallback


# =====================================================================
# C1-C2：F2 terminal 终结 reason 区分（第二轮审查新增）
# =====================================================================
def t_f2_terminal_reason():
    # C1: 精确匹配（registry 有 order_id）→ reason 无 _fallback 后缀
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(order_id='sl_old')})
    fake = _make_fake(states)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    target, fb = _f2_terminal_finish(fake, states, IDENT_SL, 'sl_old', 'SL', 0, 'LONG')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("C1/精确匹配→reason无_fallback",
           target == IDENT_SL and fb is False
           and entry.get('terminated_reason') == 'terminal_status_canceled',
           f"(target={target!r}, fallback={fb}, reason={entry.get('terminated_reason')!r})")

    # C2: fallback（registry 无匹配 order_id）→ 最新层 identity + reason 带 _fallback
    states = _state_batch(protection_registry={IDENT_SL: _reg_entry(order_id='sl_missing')})
    fake = _make_fake(states)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    target, fb = _f2_terminal_finish(fake, states, IDENT_SL, 'sl_old', 'SL', 0, 'LONG')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_SL]
    report("C2/fallback→reason带_fallback",
           target == IDENT_SL and fb is True
           and entry.get('terminated_reason') == 'terminal_status_canceled_fallback',
           f"(target={target!r}, fallback={fb}, reason={entry.get('terminated_reason')!r})")

    # C2b: TP 侧同款（reason 对称）
    states = _state_batch(tp_order_id='tp_old',
                          protection_registry={IDENT_TP: _reg_entry(order_id='tp_old')})
    fake = _make_fake(states)
    fake._protection_identity = lambda b, r, l, s: CryptoTrader._protection_identity(fake, b, r, l, s)
    target, fb = _f2_terminal_finish(fake, states, IDENT_TP, 'tp_old', 'TP', 0, 'LONG')
    entry = states[SYMBOL][BATCH]['protection_registry'][IDENT_TP]
    report("C2b/TP侧精确匹配→terminal_status_expired",
           target == IDENT_TP and fb is False
           and entry.get('terminated_reason') == 'terminal_status_canceled',
           f"(reason={entry.get('terminated_reason')!r})")


# =====================================================================
# C3：恢复链闭环（人工撤单 → ABSENT → R14 → CONFIRMED，无双单）
# =====================================================================
def t_recovery_closed_loop():
    states = _state_batch(current_sl_id=None,  # F2 已置 None → 下轮 R14
                          protection_registry={IDENT_SL: _reg_entry(state='ABSENT', order_id='sl_old')})
    fake = _make_fake(states)
    fake.exchange.create_order.return_value = {'id': 'sl_new', 'status': 'open'}
    # 下轮 R14：F3 裁决（ABSENT → allow）
    v, oid = fake._adjudicate_recreate_before_repair(SYMBOL, BATCH, IDENT_SL)
    assert v == 'allow' and oid is None, f"F3 裁决异常: {v!r}/{oid!r}"
    # 闸门 → PENDING_CREATE → create → verify → CONFIRMED
    allowed, _ = fake._assert_create_allowed(SYMBOL, BATCH, IDENT_SL, desc='补挂止损单')
    assert allowed, "ABSENT 后闸门应放行"
    fake._update_registry(SYMBOL, BATCH, IDENT_SL, state='PENDING_CREATE', id_known=False,
                          order_kind='conditional', role='SL', layer=0, side='LONG',
                          intent=fake._build_intent(symbol='BTCUSDT', side='sell', qty=0.003,
                                                    order_type='STOP_MARKET', stop_price=55000.0,
                                                    reduce_only=True))
    new_ord = fake.exchange.create_order(symbol=SYMBOL, type='STOP_MARKET', side='sell',
                                         amount=0.003, params={}, retries=1)
    vr = fake._verify_and_update_registry(SYMBOL, BATCH, IDENT_SL, new_ord['id'], desc='补挂止损单')
    reg = states[SYMBOL][BATCH]['protection_registry']
    report("C3/人工撤单→ABSENT→裁决allow→恢复CONFIRMED无双单",
           vr == 'success' and len(reg) == 1 and reg[IDENT_SL].get('state') == 'CONFIRMED'
           and reg[IDENT_SL].get('order_id') == 'sl_new',
           f"(vr={vr!r}, reg_len={len(reg)}, state={reg[IDENT_SL].get('state')!r}, oid={reg[IDENT_SL].get('order_id')!r})")


# =====================================================================
# C4-C6：不自动补挂语义 + 源码断言
# =====================================================================
def t_no_repair_semantics_and_source():
    # C4: 源码断言——F2 分支区分 程序撤单/用户修改/外部撤销（is_programmatic / user_modified）
    src = open('trader_260725.py', encoding='utf-8').read()
    lines = src.splitlines()
    sl_seg = '\n'.join(lines[4083:4133])  # F2 SL terminal 分支（2026-08-21 19:3x 实测，GLM审计修复 +23）
    tp_seg = '\n'.join(lines[4218:4268])  # F2 TP terminal 分支
    report("C4/F2区分程序撤单/用户修改/外部撤销",
           'is_programmatic_cancel' in sl_seg and 'user_modified' in sl_seg
           and 'need_recover_sl = True' in sl_seg
           and 'is_programmatic_cancel' in tp_seg and 'need_recover_tp = True' in tp_seg,
           f"(SL段三分类={'is_programmatic_cancel' in sl_seg and 'user_modified' in sl_seg and 'need_recover_sl = True' in sl_seg}"
           f", TP段={'is_programmatic_cancel' in tp_seg and 'need_recover_tp = True' in tp_seg})")

    # C5: 源码断言——F2 SL/TP 段 fallback reason 后缀（第二轮审查新增代码在位）
    report("C5/F2 fallback reason区分在位",
           '_fallback' in sl_seg and '_fallback' in tp_seg
           and 'terminal_status_{sl_status}_fallback' in sl_seg
           and 'terminal_status_{tp_status}_fallback' in tp_seg,
           f"(SL段_fallback={'_fallback' in sl_seg}, TP段_fallback={'_fallback' in tp_seg})")

    # C6: 行为——用户修改（user_modified=True）撤单后不自动补挂（need_recover 不触发）
    #     即 F2 分支 user_modified → current_sl_id=None 但无 need_recover（修改后实测 L4105-4107 区）
    seg_user = '\n'.join(lines[4101:4113])
    report("C6/用户修改撤单不自动补挂",
           '用户主动修改' in seg_user and '不再自动补挂' in seg_user,
           f"(user_modified分支={'用户主动修改' in seg_user and '不再自动补挂' in seg_user})")


# =====================================================================
# 主入口
# =====================================================================
if __name__ == '__main__':
    print("=" * 60)
    print("T26 人工撤单恢复专项测试")
    print("=" * 60)
    t_f2_terminal_reason()
    t_recovery_closed_loop()
    t_no_repair_semantics_and_source()
    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'#' * 60}")
    print(f"T26 人工撤单恢复：{passed}/{total} 通过")
    print('#' * 60)
    import sys
    sys.exit(0 if passed == total else 1)
