# -*- coding: utf-8 -*-
"""v6.4-P6 守恒分级观察器决定性测试（R34–R42，RED-first）。

设计依据：P6_守恒误报收窄_设计草案_送审ChatGPT.md v1.3（ChatGPT FULLY ALIGNED）。
契约要点：
- 观察器 `_maybe_report_conservation_conflict(symbol, side, actual_position)` 每轮
  无条件调用；同方向批次 <2 → 删整份事件记录 + return（绝不告警）；
- Σnet 只累加同方向 active 批次（(symbol, side) 计量边界）；
- 宽限资格 = 「有效在途平仓事务」五条件合取（回滚保留 close_reason 审计，陈旧
  reason 结构性排除；limit_pending_normal 另须 limit_close_order_id 非空）；
- 分级：无有效在途 → 立即 critical；有 → 首见 warning + 300s 后 critical；
  事件内单调：critical_count>0 不降级、不重新宽限；
- 事件模型：每 (symbol, side) 一份 {'first_seen','warning_sent','critical_count'}；
  守恒恢复或批次<2 → 整份删除（≤3 critical 为单事件上限）；
- 锁内认领、锁外通知；零新增 API。

harness：CryptoTrader.__new__ 绕 __init__（纯函数离线惯例），只注入观察器
依赖的事件存储/锁/状态/stub 通知——不触碰交易所、不触碰生产文件。
时钟注入：预种 ev['first_seen']，不 patch 全局 time。
"""
import copy
import threading
import time

import trader_260725
from trader_260725 import CryptoTrader

SYM = 'BTCUSDT'
GRACE = trader_260725.CONSERVATION_GRACE_S


# ── fixture ──────────────────────────────────────────────────────────────
def _b(batch_id='bA', side='BUY', qty=0.002, **extra):
    b = {
        'is_active': True, 'batch_id': batch_id, 'symbol': SYM, 'side': side,
        'target_amounts': [qty], 'filled_details': [76650.0],
        'last_filled_count': 1, 'total_entry_fee': 0.0,
        'close_phase': 0, 'pending_close': False,
        'close_reason': '', 'close_op_id': '',
    }
    b.update(extra)
    return b


def _inflight(b, reason='limit_pending_normal', op='OP1', oid='L1', phase=1):
    b['close_phase'] = phase
    b['pending_close'] = True
    b['close_op_id'] = op
    b['close_reason'] = reason
    if reason == 'limit_pending_normal':
        b['limit_close_order_id'] = oid
    return b


class _Trader:
    """仅注入观察器依赖的最小实例（__new__ 绕 __init__，零联网零文件）。"""


def make_trader(batches, actual):
    # __new__ 绕 __init__（纯函数离线惯例）：真实方法可解析，不联网不触文件
    t = CryptoTrader.__new__(CryptoTrader)
    t._conservation_events = {}
    t._conservation_event_lock = threading.Lock()
    t._states = {SYM: {b['batch_id']: b for b in batches}}
    t.load_all_states = lambda: copy.deepcopy(t._states)
    t._criticals = []
    t._warnings = []
    t.send_tg_notification = lambda msg, level='info': (
        t._criticals.append(msg) if level == 'critical' else t._warnings.append(msg))
    return t


def _obs(t, actual):
    """经真实观察器（BUY 方向）触发一轮观察。"""
    t._maybe_report_conservation_conflict(SYM, 'BUY', actual)


def _ev(t, side='BUY'):
    return t._conservation_events.get((SYM, side))


# ── R34：有效在途事务的瞬时冲突 → 仅 warning，零 critical ────────────────
def r34_inflight_transient_conflict_warning_only():
    t = make_trader([_b('bA'), _inflight(_b('bB'))], actual=0.003)
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev is not None, '冲突必须建立事件记录'
    assert ev['warning_sent'] is True and ev['critical_count'] == 0, ev
    assert len(t._warnings) == 1, f'必须恰好 1 条 warning: {len(t._warnings)}'
    assert len(t._criticals) == 0, f'瞬时在途冲突不得 critical: {len(t._criticals)}'
    # 持续冲突但在宽限期内（再次观察）→ 仍零 critical
    _obs(t, 0.003)
    assert len(t._criticals) == 0 and len(t._warnings) == 1, (
        len(t._criticals), len(t._warnings))


# ── R35：无有效在途事务的外部减仓 → 立即 critical（零延迟）────────────────
def r35_no_inflight_immediate_critical():
    t = make_trader([_b('bA'), _b('bB')], actual=0.003)
    _obs(t, 0.003)
    assert len(t._criticals) == 1, f'外部减仓必须立即 critical: {len(t._criticals)}'
    assert len(t._warnings) == 0, '无在途事务不得发 warning'
    ev = _ev(t)
    assert ev['critical_count'] == 1, ev


# ── R36：有效在途事务下冲突持续 ≥300s → 升级 critical ────────────────────
def r36_inflight_conflict_escalates_after_grace():
    t = make_trader([_b('bA'), _inflight(_b('bB'))], actual=0.003)
    _obs(t, 0.003)
    assert len(t._criticals) == 0
    ev = _ev(t)
    ev['first_seen'] -= (GRACE + 1)          # 时钟注入：宽限期已过
    _obs(t, 0.003)
    assert len(t._criticals) == 1, f'宽限期满必须升级 critical: {len(t._criticals)}'
    assert _ev(t)['critical_count'] == 1, _ev(t)


# ── R37：显式收敛 → 整份记录删除；新事件重新从 warning 开始 ───────────────
def r37_explicit_recovery_deletes_event():
    t = make_trader([_b('bA'), _b('bB')], actual=0.003)
    _obs(t, 0.003)
    assert len(t._criticals) == 1 and _ev(t) is not None
    t._states[SYM]['bB']['last_filled_count'] = 0   # 守恒恢复：Σnet 0.002 ≤ actual
    t._states[SYM]['bA']['last_filled_count'] = 0
    _obs(t, 0.003)
    assert _ev(t) is None, '显式收敛必须整份删除事件记录'
    # 新事件：重新建立冲突 → fresh 记录（critical_count 从 0 起）
    t._states[SYM]['bB']['last_filled_count'] = 1
    t._states[SYM]['bA']['last_filled_count'] = 1
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev is not None and ev['critical_count'] == 1, ev
    assert len(t._criticals) == 2, len(t._criticals)   # 两次独立事件各 1 次


# ── R38：有效在途事务 + 稀疏观测（间隔 >300s）→ 仍必须升级 critical ──────
def r38_sparse_observation_must_escalate():
    t = make_trader([_b('bA'), _inflight(_b('bB'))], actual=0.003)
    _obs(t, 0.003)                            # 首见 warning
    assert len(t._warnings) == 1 and len(t._criticals) == 0
    ev = _ev(t)
    ev['first_seen'] -= (GRACE * 3)           # 观测间隔远超宽限期
    _obs(t, 0.003)                            # 稀疏回归观测：仍冲突
    assert len(t._criticals) == 1, \
        f'稀疏观测不得把真实冲突当新事件无限延期: {len(t._criticals)}'
    assert _ev(t)['critical_count'] == 1


# ── R39：Hedge 双方向并存 → LONG/SHORT 计量与事件完全隔离 ────────────────
def r39_hedge_sides_fully_isolated():
    # 2 个 LONG 批次（Σnet 0.004）+ 1 个 SHORT 批次（Σnet 0.002）
    t = make_trader([_b('bL1'), _b('bL2'), _b('bS1', side='SELL')], actual=0.0035)
    _obs(t, 0.0035)
    # 正确实现：LONG Σnet=0.004，actual 0.0035 ≥ 0.004−tol → 无冲突、无事件；
    # 若 Σnet 误加 SHORT（0.006）→ 0.0035 < 0.0055 → 误报 critical（RED 判据）
    assert _ev(t) is None, f'LONG 不得计入 SHORT 净量: {_ev(t)}'
    assert len(t._criticals) == 0 and len(t._warnings) == 0
    # SHORT 侧只有 1 个批次 → 观察器必须只清理、不建事件
    t._maybe_report_conservation_conflict(SYM, 'SELL', 0.0035)
    assert _ev(t, 'SELL') is None, '单方向批次 <2 不得建立事件记录'
    assert len(t._criticals) == 0
    # 真实 LONG 冲突（actual 0.002）→ 事件键必须是 (SYM, 'BUY')
    _obs(t, 0.002)
    assert list(t._conservation_events.keys()) == [(SYM, 'BUY')], t._conservation_events


# ── R40：陈旧 reason 负测（回滚审计残留）→ 立即 critical，绝不宽限 ────────
def r40_stale_reason_denies_grace():
    # bB 带回滚残留：phase=0 + pending=False + 旧 reason='market_confirming'
    t = make_trader([_b('bA'), _b('bB', close_reason='market_confirming',
                                   close_op_id='OP0')], actual=0.003)
    _obs(t, 0.003)
    assert len(t._criticals) == 1, \
        f'陈旧 reason 不得构成宽限（必须立即 critical）: {len(t._criticals)}'
    assert len(t._warnings) == 0
    # 反向对照：同 reason 但五条件齐备 → 必须走 warning 宽限
    t2 = make_trader([_b('bA'), _inflight(_b('bB', close_reason='market_confirming',
                                            op='OP1'), reason='market_confirming')],
                     actual=0.003)
    _obs(t2, 0.003)
    assert len(t2._criticals) == 0 and len(t2._warnings) == 1, (
        len(t2._criticals), len(t2._warnings))


# ── R41：sibling 消失/守恒恢复 → 事件整份删除 → 新冲突重新开始 ────────────
def r41_sibling_disappearance_full_reset():
    t = make_trader([_b('bA'), _b('bB')], actual=0.003)
    _obs(t, 0.003)
    assert _ev(t)['critical_count'] == 1
    # sibling 消失（只剩 1 个同方向批次）→ 观察器入口短路清理
    del t._states[SYM]['bB']
    _obs(t, 0.003)
    assert _ev(t) is None, 'sibling 消失必须删除事件记录（v1.3 可达路径）'
    # 批次恢复 + 冲突再现 → 全新事件，critical_count 从 0 重计
    t._states[SYM]['bB'] = _b('bB')
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev is not None and ev['critical_count'] == 1, \
        f'新事件必须独立计数（≤3 为单事件上限）: {ev}'
    assert len(t._criticals) == 2, len(t._criticals)


# ── R42：事件内单调棘轮 → critical 后不得降级回 warning ───────────────────
def r42_monotonic_no_downgrade_after_critical():
    t = make_trader([_b('bA'), _inflight(_b('bB'))], actual=0.003)
    _obs(t, 0.003)                              # warning
    ev = _ev(t)
    ev['first_seen'] -= (GRACE + 1)
    _obs(t, 0.003)                              # 升级 critical（count=1）
    assert len(t._criticals) == 1
    # 同一事件内：有效在途事务仍在 → 不得重新获得宽限/降级
    _obs(t, 0.003)
    assert len(t._criticals) == 2, f'critical 后必须单调升级: {len(t._criticals)}'
    assert len(t._warnings) == 1, f'不得重发 warning: {len(t._warnings)}'
    # ≤3 封顶（单事件上限）
    _obs(t, 0.003)
    _obs(t, 0.003)
    assert len(t._criticals) == 3, f'critical 必须单事件 3 次封顶: {len(t._criticals)}'
    _obs(t, 0.003)
    assert len(t._criticals) == 3, '超过单事件上限后必须静默'


# ── R43：批次集变更 = 新事件（防「全监控退出」窗口的跨事件污染）──────────
def r43_batch_set_change_starts_new_event():
    # 旧事件（bA+bB 无在途冲突，已 critical 1 次）
    t = make_trader([_b('bA'), _b('bB')], actual=0.003)
    _obs(t, 0.003)
    assert _ev(t)['critical_count'] == 1
    # 旧批次对全部归档 → 监控线程退出 → 观察器停调（删除路径不可达的窗口）；
    # 之后新批次对上线并冲突（带有效在途事务）
    t._states[SYM] = {'bC': _inflight(_b('bC')), 'bD': _b('bD')}
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev['critical_count'] == 0 and ev['warning_sent'] is True, \
        f'批次集变更=新事件，不得继承旧事件 critical 状态: {ev}'
    assert len(t._criticals) == 1 and len(t._warnings) == 1, (
        len(t._criticals), len(t._warnings))


# ── R44：批次集部分重叠 → 保留计时与棘轮（ChatGPT 终审收口）──────────────
def r44_partial_overlap_keeps_event():
    # 段 1：{A,B} → {A,B,C}：新增批次，真实冲突从未消失 → 计时/棘轮不得重置
    t = make_trader([_b('bA'), _inflight(_b('bB'))], actual=0.003)
    _obs(t, 0.003)                            # warning + 事件建立
    ev = _ev(t)
    old_first_seen = ev['first_seen']
    t._states[SYM]['bC'] = _inflight(_b('bC', op='OP2', oid='L2'))
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev['first_seen'] == old_first_seen, \
        f'重叠批次集不得重置计时（重新获得宽限）: {ev}'
    assert ev['critical_count'] == 0, ev
    assert len(t._warnings) == 1, f'不得重发 warning: {len(t._warnings)}'
    assert len(t._criticals) == 0, f'宽限期内不得 critical: {len(t._criticals)}'
    # 段 2：{A,B,C} → {A,C}：B 归档，冲突仍在 → 事件延续（不重置）
    del t._states[SYM]['bB']
    _obs(t, 0.003)
    ev = _ev(t)
    assert ev['first_seen'] == old_first_seen and ev['critical_count'] == 0, ev
    assert len(t._warnings) == 1 and len(t._criticals) == 0
    # 段 3：时钟注入宽限期满 → 正常升级 critical（棘轮启动）
    ev['first_seen'] = old_first_seen - (GRACE + 1)
    _obs(t, 0.003)
    assert len(t._criticals) == 1, f'事件延续后仍必须按时升级: {len(t._criticals)}'
    # 段 4：critical 后批次集再变（{A,C} → {A,C,D}）→ 棘轮保持，不降级不重宽限
    t._states[SYM]['bD'] = _inflight(_b('bD', op='OP3', oid='L3'))
    _obs(t, 0.003)
    assert len(t._criticals) == 2, f'critical 后不得因批次集变更降级: {len(t._criticals)}'
    assert len(t._warnings) == 1, f'不得重发 warning: {len(t._warnings)}'
    assert _ev(t)['critical_count'] == 2, _ev(t)


# ── R45：生产接线结构锁（防「helper 建好没人用」/ 接线回退的空转通过）─────
def r45_production_wiring_locked():
    """R34-R44 直调观察器，不证明监控循环真的在调用——本测试锁死生产接线：
    ①观察器调用点恰好 1 处（除定义外），且为无条件每轮契约的新签名；
    ②旧 (symbol, batch_id) 签名调用零残留；
    ③调用位于方向仓位取得（L6611 同型）之后。"""
    import os
    src = open(os.path.abspath(trader_260725.__file__), encoding='utf-8').read()
    n_calls = src.count('self._maybe_report_conservation_conflict(')
    assert n_calls == 1, \
        f'生产接线必须恰好 1 处调用（实际 {n_calls}）：旧分支调用必须已移除'
    assert src.count('_maybe_report_conservation_conflict(symbol, '
                     'side, current_actual_position)') == 1, \
        '必须存在无条件每轮契约调用 (symbol, side, current_actual_position)'
    assert '_maybe_report_conservation_conflict(symbol, batch_id)' not in src, \
        '旧 (symbol, batch_id) 签名调用残留'
    _i_pos = src.find('current_actual_position = self._get_current_position_amt(')
    _i_call = src.find('self._maybe_report_conservation_conflict(')
    assert 0 < _i_pos < _i_call, '观察器调用必须位于方向仓位取得之后（复用不重查）'


TESTS = [r34_inflight_transient_conflict_warning_only,
         r35_no_inflight_immediate_critical,
         r36_inflight_conflict_escalates_after_grace,
         r37_explicit_recovery_deletes_event,
         r38_sparse_observation_must_escalate,
         r39_hedge_sides_fully_isolated,
         r40_stale_reason_denies_grace,
         r41_sibling_disappearance_full_reset,
         r42_monotonic_no_downgrade_after_critical,
         r43_batch_set_change_starts_new_event,
         r44_partial_overlap_keeps_event,
         r45_production_wiring_locked]


def main():
    passed = 0
    failed = []
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {e}')
            failed.append(fn.__name__)
        except Exception as e:
            print(f'💥 {fn.__name__}: {type(e).__name__}: {e}')
            failed.append(fn.__name__)
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
