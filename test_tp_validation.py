# -*- coding: utf-8 -*-
"""
TP 参数校验 4 层修复 —— ChatGPT 终审补强 v3 专项测试（改动 D，2026-08-20）

背景：ChatGPT 终审结论"不回滚 96b94ed，整体设计正确"，要求 4 项补强：
  A. R2 成本边界修正（BUY: TP > 现价 且 TP >= 成本，允许保本退出）——已实施
  B. R3 熔断通知（熔断时 1 次 critical + 去重，成功挂出后清除）——已实施
  C. FAILED 告警恢复（成功路径 _gate_alert_clear 恢复 3 次 TG 额度）——已实施
  D. 本文件：3 个专项测试锁定 B/C 行为

验收标准映射：
  ② 成交后 TP 不合理 → critical + 稳定等待（不循环打 API）→ T2
  ③ 临时失败最多 N 次 → 熔断 → T1
  ④ 同一故障最多 3 条 TG，恢复后重置 → T3（+ T1 熔断告警恰 1 次）

关键语义（源码实证 trader_260725.py）：
  - _tp_update_blocked（L721）：熔断短路 → R2 可行性 → 标记短路/清除。标记不短路校验（自愈）。
  - _mark_tp_param_invalid（L683）：写 tp_param_invalid + critical（60min 去重，_tp_invalid_alerted）。
  - _tp_breaker_alerted（L130）：熔断告警去重，键=(batch_id, layer)；成功挂出时 pop（L4396）。
  - _gate_alert_notify / _gate_alert_clear（L626/L652）：同一 identity+类别最多 3 次 TG；clear 恢复额度。

测试基建坑（第 N 次同类）：
  MagicMock 未绑定 helper → 自动 mock 吞默认值/返回 truthy → 短路判定失真。
  必须绑定：_tp_update_blocked/_mark_tp_param_invalid/_clear_tp_param_invalid/_check_tp_viability/
           _gate_alert_notify/_gate_alert_clear/_update_registry
  必须显式：_api_cooldown_until=0、_tp_invalid_alerted={}、_tp_breaker_alerted={}、
           _gate_alert_counts={}、_gate_alert_lock=threading.Lock()

用法: .venv\Scripts\python.exe test_tp_validation.py
"""
import threading
import time
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
BATCH = "batch_tpv_001"
IDENTITY = f"{BATCH}|TP|L0|LONG"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


# =====================================================================
# 测试基建：MagicMock 基座 + 真实 helper 绑定 + 内存 states 闭环
# =====================================================================

def _state_batch(**over):
    b = {
        'is_active': True,
        'side': 'BUY',
        'current_sl_id': 'sl_1',
        'tp_order_id': None,
        'user_modified': False,
        'stop_steps': [55000.0],
        'take_profit_price': 60000.0,
        'pending_sl_orders': [],
        'protection_registry': {},
    }
    b.update(over)
    return {SYMBOL: {BATCH: b}}


def make_fake(states):
    """MagicMock 基座 + 内存 states 闭环 + 真实 helper 绑定（test_tp_validation 专用）。"""
    fake = mock.MagicMock()
    fake._api_cooldown_until = 0
    fake._states = states
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: states.setdefault(s, {}).update({b: d})
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.sent = []
    # TP 校验/标记/熔断/告警体系：全部真实绑定（MagicMock 会自动 mock，判定失真）
    fake._tp_invalid_alerted = {}
    fake._tp_breaker_alerted = {}
    fake._gate_alert_counts = {}
    fake._gate_alert_lock = threading.Lock()
    if hasattr(CryptoTrader, '_check_tp_viability'):
        fake._check_tp_viability = (
            lambda side, tp, cost, mark: CryptoTrader._check_tp_viability(fake, side, tp, cost, mark))
    if hasattr(CryptoTrader, '_mark_tp_param_invalid'):
        fake._mark_tp_param_invalid = (
            lambda s, b, r: CryptoTrader._mark_tp_param_invalid(fake, s, b, r))
    if hasattr(CryptoTrader, '_clear_tp_param_invalid'):
        fake._clear_tp_param_invalid = (
            lambda s, b: CryptoTrader._clear_tp_param_invalid(fake, s, b))
    if hasattr(CryptoTrader, '_tp_update_blocked'):
        fake._tp_update_blocked = (
            lambda s, b, side, layer, tp, cost, **k:
            CryptoTrader._tp_update_blocked(fake, s, b, side, layer, tp, cost, **k))
    if hasattr(CryptoTrader, '_gate_alert_notify'):
        fake._gate_alert_notify = (
            lambda i, r, m, **k: CryptoTrader._gate_alert_notify(fake, i, r, m, **k))
    if hasattr(CryptoTrader, '_gate_alert_clear'):
        fake._gate_alert_clear = (
            lambda i: CryptoTrader._gate_alert_clear(fake, i))
    if hasattr(CryptoTrader, '_update_registry'):
        fake._update_registry = (
            lambda s, b, i, **k: CryptoTrader._update_registry(fake, s, b, i, **k))
    # P0 Batch B（2026-08-29）：clear_batch_state proof 门依赖的 helper 必须绑定
    # （未绑定 → _verify_clear_proof 返回 MagicMock 恒非 None → proof 恒被拒）
    for _n in ('_verify_clear_proof', '_batch_has_active_exposure', '_converge_alert'):
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n, (lambda _n=_n: lambda *a, **k:
                               getattr(CryptoTrader, _n)(fake, *a, **k))())
    fake._converge_alert_counts = {}   # 真实 dict（MagicMock → 告警静默丢失）
    return fake


def _crits(fake):
    return [m for lvl, m in fake.sent if lvl == 'critical']


def _tp_invalid_alert_msgs(fake):
    return [m for m in _crits(fake) if '止盈价不合理' in m]


# =====================================================================
# T1: R3 补挂 TP 层熔断 —— 连续失败 ≥5 → 短路零 API + 熔断告警恰 1 次
# =====================================================================

def scenario_t1_breaker():
    """熔断短路：tp_fail_count L0=5 → _tp_update_blocked 返回 True；
    连续调用告警去重（critical 恰 1 次）；成功挂出 pop 后再次熔断可再提醒。"""
    try:
        states = _state_batch(tp_fail_count={'0': 5})
        fake = make_fake(states)
        # 第 1 次：熔断短路 + 告警 1 次
        blocked1 = fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 60000.0, 55000.0,
                                          mark_price=61000.0)
        crits1 = _crits(fake)
        ok_short = (blocked1 is True)
        ok_alert1 = (len(crits1) == 1 and '止盈补挂已熔断' in crits1[0])
        report('T1a/熔断短路跳过自动重试', ok_short,
               f"(blocked={blocked1})")
        report('T1b/熔断时critical恰1次', ok_alert1,
               f"(crits={len(crits1)}, msg={'止盈补挂已熔断' if crits1 else '无'})")
        # 第 2、3 次：去重生效 → critical 仍 1 次
        fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 60000.0, 55000.0, mark_price=61000.0)
        fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 60000.0, 55000.0, mark_price=61000.0)
        crits2 = _crits(fake)
        ok_dedup = (len(crits2) == 1)
        report('T1c/熔断告警去重(连续3次仍1次)', ok_dedup, f"(crits={len(crits2)})")
        # 熔断键已记录（成功挂出时将被 pop）
        ok_key = ((BATCH, 0) in fake._tp_breaker_alerted)
        report('T1d/熔断键已记录', ok_key, f"(key={(BATCH, 0)} in alerted)")
        # 成功挂出 → pop 解除 → 再次熔断可再提醒
        fake._tp_breaker_alerted.pop((BATCH, 0), None)
        fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 60000.0, 55000.0, mark_price=61000.0)
        crits3 = _crits(fake)
        ok_recover = (len(crits3) == 2)
        report('T1e/成功挂出后熔断可再提醒', ok_recover,
               f"(crits={len(crits3)} → 恢复后第2次熔断再告警)")
        # 熔断短路全过程中 create API 零调用（不进入补挂流程）
        ok_noapi = (fake.exchange.create_order.call_count == 0)
        report('T1f/熔断短路零create API', ok_noapi,
               f"(create={fake.exchange.create_order.call_count})")
    except Exception as e:
        report('T1/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T2: 用户改价自愈 —— 不合理→标记+critical+静默 → 改合理→清标记放行
# =====================================================================

def scenario_t2_user_fix_self_heal():
    """TP 不合理 → blocked + 标记 + critical 1 次；仍不合理 → 静默（不重复告警）；
    用户改合理（含改动 A 边界：TP == 成本 = 保本退出合法）→ 清标记放行。"""
    try:
        states = _state_batch()
        fake = make_fake(states)
        # 阶段 1：TP 不合理（BUY TP=69000 <= 现价 70000）→ blocked + 标记 + critical 1 次
        blocked1 = fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 69000.0, 71000.0,
                                          mark_price=70000.0)
        b1 = states[SYMBOL][BATCH]
        marked = bool(b1.get('tp_param_invalid'))
        alerts1 = _tp_invalid_alert_msgs(fake)
        report('T2a/TP不合理→短路+写标记', (blocked1 is True and marked),
               f"(blocked={blocked1}, marked={marked})")
        report('T2b/TP不合理→critical恰1次', len(alerts1) == 1,
               f"(alerts={len(alerts1)})")
        # 阶段 2：仍不合理 → 静默跳过（标记存在，不再告警、不打 API）
        blocked2 = fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 69000.0, 71000.0,
                                          mark_price=70000.0)
        alerts2 = _tp_invalid_alert_msgs(fake)
        ok_still_blocked = (blocked2 is True)
        ok_silent = (len(alerts2) == 1)  # 不新增告警
        report('T2c/仍不合理→持续短路', ok_still_blocked, f"(blocked={blocked2})")
        report('T2d/仍不合理→静默不重复告警', ok_silent, f"(alerts={len(alerts2)})")
        # 阶段 3：用户改合理（TP=72000 > 现价 70000 且 >= 成本 71000）→ 清标记放行
        blocked3 = fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 72000.0, 71000.0,
                                          mark_price=70000.0)
        b3 = states[SYMBOL][BATCH]
        cleared = ('tp_param_invalid' not in b3)
        report('T2e/改价合理→放行挂单', (blocked3 is False),
               f"(blocked={blocked3} → 补挂段 `if need_update_tp and not blocked` 放行)")
        report('T2f/改价合理→标记自动清除(自愈)', cleared, f"(tp_param_invalid={'仍存在' if not cleared else '已清除'})")
        # 阶段 4（改动 A 边界）：TP == 成本 = 合法保本退出（BUY: TP=71000=成本，现价 70000）
        states[SYMBOL][BATCH].pop('tp_param_invalid', None)
        blocked4 = fake._tp_update_blocked(SYMBOL, BATCH, 'BUY', 0, 71000.0, 71000.0,
                                          mark_price=70000.0)
        report('T2g/改动A边界:TP==成本=合法保本退出', (blocked4 is False),
               f"(blocked={blocked4} → 放行，不误拦)")
        # 阶段 5（改动 A 反向边界）：SELL 对称 —— TP < 现价 且 TP <= 成本
        states[SYMBOL][BATCH]['side'] = 'SELL'
        blocked5 = fake._tp_update_blocked(SYMBOL, BATCH, 'SELL', 0, 69000.0, 69000.0,
                                          mark_price=70000.0)
        report('T2h/改动A边界:SELL保本退出', (blocked5 is False),
               f"(blocked={blocked5} → SELL TP==成本且<现价 → 放行)")
        # 阶段 6（改动 A 反向无效）：SELL TP > 成本 → 无意义止盈 → 拦截
        blocked6 = fake._tp_update_blocked(SYMBOL, BATCH, 'SELL', 0, 70000.0, 68000.0,
                                          mark_price=70000.0)
        report('T2i/改动A边界:SELL TP>成本→拦截', (blocked6 is True),
               f"(blocked={blocked6} → TP=70000 >= 现价70000 且 > 成本68000 → 无效)")
    except Exception as e:
        report('T2/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T3: FAILED 告警恢复 —— 3 次 TG → 静默 → 成功挂出 clear → 重新 3 次额度
# =====================================================================

def scenario_t3_failed_alert_recovery():
    """_gate_alert_notify(identity, 'FAILED')：前 3 次发 TG、第 4 次静默；
    _gate_alert_clear（成功挂出）→ 计数清零 → 再次 FAILED 重新获得 3 次额度。"""
    try:
        states = _state_batch()
        fake = make_fake(states)
        # 阶段 1：连续 3 次 FAILED → 全部发 TG（计数 1→3）
        for i in range(3):
            fake._gate_alert_notify(IDENTITY, 'FAILED', f'fail-{i}', level='warning')
        sent1 = len(fake.sent)
        report('T3a/FAILED前3次各发TG', sent1 == 3,
               f"(sent={sent1}/3)")
        # 阶段 2：第 4 次 → 静默（print only）
        fake._gate_alert_notify(IDENTITY, 'FAILED', 'fail-3', level='warning')
        sent2 = len(fake.sent)
        ok_silent = (sent2 == 3)
        report('T3b/第4次静默', ok_silent, f"(sent={sent2}/3 → 第4次不发)")
        # 阶段 3：成功挂出 → _gate_alert_clear → 计数清零
        fake._gate_alert_clear(IDENTITY)
        left = len([k for k in fake._gate_alert_counts if k[0] == IDENTITY])
        report('T3c/成功挂出clear清计数', left == 0, f"(left={left})")
        # 阶段 4：再次 FAILED × 3 → 重新获得 3 次额度
        for i in range(3):
            fake._gate_alert_notify(IDENTITY, 'FAILED', f'fail2-{i}', level='warning')
        sent3 = len(fake.sent)
        ok_recover = (sent3 == 6)  # 3 + 3
        report('T3d/恢复后重新3次额度', ok_recover,
               f"(sent={sent3} → 恢复后 3+3=6)")
        # 阶段 5：恢复后再第 4 次 → 又静默（闭环稳定）
        fake._gate_alert_notify(IDENTITY, 'FAILED', 'fail2-3', level='warning')
        sent4 = len(fake.sent)
        report('T3e/恢复后再静默(闭环稳定)', sent4 == 6,
               f"(sent={sent4}/6 → 第7次不发)")
    except Exception as e:
        report('T3/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T4（补充）：改动 A 成本边界完整矩阵（_check_tp_viability 纯函数）
# =====================================================================

def scenario_t4_viability_matrix():
    """改动 A 边界矩阵（BUY/SELL × TP/现价/成本 组合），锁定 v2 修正后的判定：
    BUY 有效 ⟺ TP > 现价 且 TP >= 成本；SELL 有效 ⟺ TP < 现价 且 TP <= 成本。"""
    try:
        states = _state_batch()
        fake = make_fake(states)
        fn = fake._check_tp_viability
        cases = [
            # (side, tp, cost, mark, expect_valid, 说明)
            ('BUY', 72000, 71000, 70000, True,  '正常止盈'),
            ('BUY', 71000, 71000, 70000, True,  '保本退出(TP==成本)'),
            ('BUY', 70000, 71000, 70000, False, 'TP<=现价→-2021'),
            ('BUY', 69000, 71000, 70000, False, 'TP<成本→无意义止盈'),
            ('BUY', 71000, 71000, 71000, False, 'TP<=现价(现价==TP)→-2021'),
            ('SELL', 69000, 70000, 71000, True,  '正常止盈'),
            ('SELL', 70000, 70000, 71000, True,  '保本退出(TP==成本)'),
            ('SELL', 70000, 69000, 71000, False, 'TP>成本→无意义止盈'),
            ('SELL', 71000, 70000, 71000, False, 'TP>=现价→-2021'),
        ]
        all_ok = True
        details = []
        for side, tp, cost, mark, expect, desc in cases:
            valid, reason = fn(side, tp, cost, mark)
            ok = (valid == expect)
            all_ok = all_ok and ok
            details.append(f"{'✓' if ok else '✗'}{desc}")
        report('T4/改动A边界矩阵9例全对', all_ok, ' | '.join(details))
    except Exception as e:
        report('T4/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T5（ChatGPT 终审补强 E2）：clear_batch_state 终态清理熔断告警去重键
# =====================================================================

def scenario_t5_breaker_cleanup():
    """批次终态清理（clear_batch_state）应释放该 batch 的 _tp_breaker_alerted 键：
    本 batch 多 layer 键全清、其他 batch 键保留、批次状态同步删除。"""
    try:
        states = _state_batch()
        fake = make_fake(states)
        # 预置熔断键：本 batch 2 个 layer + 其他 batch 1 个
        fake._tp_breaker_alerted = {
            (BATCH, 0): 100.0,
            (BATCH, 1): 200.0,
            ('batch_other_xyz', 0): 300.0,
        }
        # 直接调真实 clear_batch_state（fake 已绑内存 states；_state_lock 为 MagicMock 支持 with）
        # P0 Batch B：proof 门适配——T5 关注熔断键清理语义，交易所侧收敛由
        # test_b_batch.py 覆盖，此处提交最小合法 proof（无敞口批次 → PRE_ENTRY）
        _proof = {
            'batch_id': BATCH, 'symbol': SYMBOL, 'checked_at': time.time(),
            'scope': 'PRE_ENTRY', 'position_zero': True,
            'state_ids_resolved': [], 'exchange_scan': 'zero',
            'l1_canceled': [], 'l2_canceled': [], 'l3_orphans': [],
        }
        CryptoTrader.clear_batch_state(fake, SYMBOL, BATCH, proof=_proof)
        left = fake._tp_breaker_alerted
        ok_self = all(k[0] != BATCH for k in left)
        ok_other = ('batch_other_xyz', 0) in left
        ok_state = (BATCH not in states.get(SYMBOL, {}))
        report('T5a/本batch熔断键全清', ok_self, f"(left={sorted(left)})")
        report('T5b/其他batch键保留', ok_other, f"(left={sorted(left)})")
        report('T5c/批次状态同步清理', ok_state, f"(state batch 已删)")
    except Exception as e:
        report('T5/异常', False, f"EXC {type(e).__name__}: {e}")


if __name__ == '__main__':
    print("#" * 60)
    print("TP 参数校验 4 层修复 —— ChatGPT 终审补强 v3 专项测试（改动 D + 终审 E1/E2）")
    print("状态: 绿阶段（T1 熔断 / T2 自愈 / T3 告警恢复 / T4 边界矩阵 / T5 终态清理）")
    print("#" * 60)
    scenario_t1_breaker()
    scenario_t2_user_fix_self_heal()
    scenario_t3_failed_alert_recovery()
    scenario_t4_viability_matrix()
    scenario_t5_breaker_cleanup()

    passed = [n for n, p in RESULTS if p]
    failed = [n for n, p in RESULTS if not p]
    print("\n" + "#" * 60)
    print(f"✅ PASS {len(passed)}  ❌ FAIL {len(failed)}")
    if failed:
        print("❌ FAIL 明细:")
        for n in failed:
            print(f"  - {n}")
    print("#" * 60)
    if failed:
        raise SystemExit(1)
