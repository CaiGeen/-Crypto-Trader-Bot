# -*- coding: utf-8 -*-
"""v6.4-P1 决定性测试（6 个，RED-first）——partial resize 实盘事故闭环
（2026-09-02 14:27 resize_cancel_unverified_SL 假阴性 + 自愈缺口 + TOCTOU）。

设计冻结清单（ChatGPT 四轮收敛，2026-09-02 15:0x）：
- Fix1 durable 取价：SL=stop_steps[last_filled_count-1]（缺层回退末位，全缺 loud）；
  TP=take_profit_price（缺 loud）。不再依赖即将被撤销的旧交易所订单取价。
- Fix1 撤单有界确认：cancel 后 4 次 × 0.5s 复核终态，全败仍 Fail-Closed。
- Fix2 partial_resize_stage 0/1/2：每腿 verify 后同锁 durable commit（新 id + stage）；
  resume 按 stage 跳过已完成腿；最终 CAS 清 stage。
- Fix2 守恒门双置位：resume 入口快速门 + 每腿「撤旧终态确认后、create 前」权威门
  （TOCTOU：撤单窗口内旧保护单可能触发成交）；
  _close_amount_guard 返回量必须 ≈ net_qty（1e-9）才放行，OrderNotFound 不单独视为未成交证明。
- Fix2 运行期自愈：monitor 冻结分支对 partial_resize_pending 60s 节流续跑；
  暂态失败静默重试，连续失败一次 critical，成功 TG 通报。
- Fix3 /partial 无参：纯本地列出净持仓>0 且未冻结批次（零 API，排除 ghost）。
"""
import ast
import re
import textwrap

import test_v64_partial_close as H

TRADER_PATH = H.TRADER_PATH
BOTRUNNER_PATH = H.BOTRUNNER_PATH
SRC = H.SRC
SYM = H.SYM

BR_SRC = open(BOTRUNNER_PATH, encoding='utf-8').read()
BR_TREE = ast.parse(BR_SRC)


def _extract_br(name):
    for node in ast.walk(BR_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(textwrap.dedent(ast.get_source_segment(BR_SRC, node)), globals())
            return globals()[name]
    return None


F = H.F

# ── 桩扩展（在 StubExchange 状态机上叠加故障注入） ─────────────────────────


class FlakyVerifyExchange(H.StubExchange):
    """cancel 成功后前 N 次 fetch 仍报 open（撤单确认传播延迟假阴性模拟）。"""

    def __init__(self, stale_fetches=1):
        super().__init__()
        self.stale_fetches = stale_fetches
        self._stale = {}

    def cancel_order(self, order_id, symbol, params=None, **k):
        r = super().cancel_order(order_id, symbol, params, **k)
        self._stale[str(order_id)] = self.stale_fetches
        return r

    def fetch_order(self, order_id, symbol, params=None, **k):
        oid = str(order_id)
        if self._stale.get(oid, 0) > 0:
            self._stale[oid] -= 1
            return dict(self.orders.get(oid) or {}, status='open')
        return super().fetch_order(order_id, symbol, params, **k)


class TOCTOUExchange(H.StubExchange):
    """cancel 成功瞬间注入仓位变化（撤单窗口内旧保护单触发成交模拟）。"""

    def __init__(self, on_cancel=None):
        super().__init__()
        self.on_cancel = on_cancel

    def cancel_order(self, order_id, symbol, params=None, **k):
        r = super().cancel_order(order_id, symbol, params, **k)
        if self.on_cancel:
            self.on_cancel(str(order_id))
        return r


class TPCreateFailExchange(H.StubExchange):
    """TP create 注入失败（SL 腿完成后崩溃窗口模拟）。"""

    def __init__(self, fail_times=99):
        super().__init__()
        self.fail_times = fail_times

    def create_order(self, symbol, otype, side, amount, price=None, params=None, **k):
        if otype == 'TAKE_PROFIT_MARKET' and self.fail_times > 0:
            self.fail_times -= 1
            raise Exception('binanceusdm {"code":-2019,"msg":"injected TP create failure"}')
        return super().create_order(symbol, otype, side, amount, price, params, **k)


def _pending_batch(**extra):
    """partial_resize_pending 冻结态批次（gross 1.0 已减 0.5 → net 0.5）。"""
    return H._batch(realized_reduce_amount=0.5, realized_reduce_cost=50.0,
                    close_phase=1, pending_close=True, is_programmatic_cancel=True,
                    close_reason='partial_resize_pending', close_op_id='OPX',
                    stop_steps=[75001.0], take_profit_price=80000.0, **extra)


# ── R1：撤单确认传播延迟假阴性 → 有界重试后必须继续而非中止 ────────────────
def t01_r1_cancel_verify_bounded_retry():
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)
    t.exchange = FlakyVerifyExchange(stale_fetches=1)
    H._seed_protection_orders(t)
    ok, msg = F['_resume_partial_resize'](t, SYM, 'batch_A', 'OPX')
    assert ok is True, f'单次 stale open 不得中止 resize: {msg}'
    assert ('STOP_MARKET', 'sell', 0.5) in t.exchange.create_calls, t.exchange.create_calls
    assert ('TAKE_PROFIT_MARKET', 'sell', 0.5) in t.exchange.create_calls
    b = t._states[SYM]['batch_A']
    assert b['close_phase'] == 0 and b['close_reason'] == '', b['close_reason']

    # 对照（Fail-Closed 保留）：4 次重试全部 stale → 仍必须判死冻结
    t2 = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)
    t2.exchange = FlakyVerifyExchange(stale_fetches=99)
    H._seed_protection_orders(t2)
    ok2, msg2 = F['_resume_partial_resize'](t2, SYM, 'batch_A', 'OPX')
    assert ok2 is False and 'resize_cancel_unverified_SL' in msg2, (ok2, msg2)
    assert not any(c[0] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET')
                   for c in t2.exchange.create_calls), '未证明终态绝不 create'


# ── R2：stage=1（SL 腿已 durable 完成）→ resume 绝不重碰 SL，只续 TP ──────
def t02_r2_stage_skip_completed_leg():
    b = _pending_batch(partial_resize_stage=1, current_sl_id='S2')
    # 状态建模：SL 腿已完成并 commit（registry SL CONFIRMED=新 id S2），TP 腿未动
    b['protection_registry']['batch_A|SL|L0|LONG']['order_id'] = 'S2'
    t = H.make_trader({SYM: {'batch_A': b}}, actual_pos=0.5)
    H._seed_protection_orders(t)
    t.exchange.orders['S1'] = {'id': 'S1', 'status': 'canceled', 'filled': 0.0,
                               'amount': 1.0, 'stopPrice': 75001.0, 'type': 'STOP_MARKET'}
    t.exchange.orders['S2'] = {'id': 'S2', 'status': 'open', 'filled': 0.0,
                               'amount': 0.5, 'stopPrice': 75001.0, 'type': 'STOP_MARKET'}
    ok, msg = F['_resume_partial_resize'](t, SYM, 'batch_A', 'OPX')
    assert ok is True, msg
    assert 'S2' not in t.exchange.cancel_calls, \
        f'stage=1 已完成的 SL 腿绝不可重撤/重挂: {t.exchange.cancel_calls}'
    assert not any(c[0] == 'STOP_MARKET' for c in t.exchange.create_calls), t.exchange.create_calls
    assert ('TAKE_PROFIT_MARKET', 'sell', 0.5) in t.exchange.create_calls
    b2 = t._states[SYM]['batch_A']
    assert b2['current_sl_id'] == 'S2', b2['current_sl_id']
    assert b2['tp_order_id'] != 'T1'
    assert b2['close_phase'] == 0 and 'partial_resize_stage' not in b2


# ── R3+R3b：守恒门双置位——入口快速门 + 撤单窗口 TOCTOU 权威门 ─────────────
def t03_r3_toctou_conservation_gate():
    # 场景 a（入口快速门）：resume 时 actual 已 < net（空窗期旧 SL 已触发）→ 拒绝 + critical
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.25)
    H._seed_protection_orders(t)
    ok, msg = F['_resume_partial_resize'](t, SYM, 'batch_A', 'OPX')
    assert ok is False and 'resume_guard_rejected' in msg, (ok, msg)
    assert not t.exchange.cancel_calls and not t.exchange.create_calls, '入口门拦截后零副作用'
    assert len(t._criticals) >= 1, '入口门拦截必须 critical'

    # 场景 b（TOCTOU 权威门）：入口 actual=Σnet ✅，cancel 旧 SL 瞬间触发成交 →
    # create 前二次守恒门必须 FAIL → create 次数=0 + critical
    def _sl_triggered(order_id):
        if order_id == 'S1':
            t2._stub_actual = 0.25  # 旧 SL 0.5 在撤单窗口内触发 → 实际仓位 0.5→0.25

    t2 = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)
    t2.exchange = TOCTOUExchange(on_cancel=_sl_triggered)
    H._seed_protection_orders(t2)
    ok2, msg2 = F['_resume_partial_resize'](t2, SYM, 'batch_A', 'OPX')
    assert ok2 is False and 'resize_guard_rejected_SL' in msg2, (ok2, msg2)
    assert not any(c[0] in ('STOP_MARKET', 'TAKE_PROFIT_MARKET')
                   for c in t2.exchange.create_calls), \
        f'TOCTOU 窗口后绝不按过期净量 create: {t2.exchange.create_calls}'
    assert len(t2._criticals) >= 1, 'TOCTOU 拦截必须 critical'


# ── R4：durable 取价——旧保护单 OrderNotFound 也不影响重挂参数 ─────────────
def t04_r4_durable_price_no_old_order_dependency():
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)
    # S1/T1 不在交易所（已撤/查不到）→ cancel/fetch 全部 -2011
    ok, msg = F['_resume_partial_resize'](t, SYM, 'batch_A', 'OPX')
    assert ok is True, f'durable 取价必须摆脱旧单依赖: {msg}'
    created = {c[0]: c[2] for c in t.exchange.create_calls}
    assert created.get('STOP_MARKET') == 0.5 and created.get('TAKE_PROFIT_MARKET') == 0.5, created


# ── R5：每腿同锁 durable commit（id + stage）——TP 失败后 SL 进度已持久化；stage=2 只 final CAS ──
def t05_r5_per_leg_durable_commit():
    # 场景 a：SL 腿完成 → TP create 失败 → 磁盘必须已持久化 SL 进度（stage=1 + 新 id）
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)
    t.exchange = TPCreateFailExchange()
    H._seed_protection_orders(t)
    ok, msg = F['_resume_partial_resize'](t, SYM, 'batch_A', 'OPX')
    assert ok is False and 'resize_create_failed_TP' in msg, (ok, msg)
    b = t._states[SYM]['batch_A']
    assert b.get('partial_resize_stage') == 1, \
        f'SL 腿 verify 成功后 stage 必须已持久化: {b.get("partial_resize_stage")}'
    assert b['current_sl_id'] != 'S1', 'SL 腿新 id 必须已 durable commit'
    assert b['close_reason'] == 'partial_resize_pending' and b['close_phase'] == 1

    # 场景 b：stage=2（两腿均已完成 commit，final CAS 前崩溃）→ resume 只做 final CAS，零交易所副作用
    b2 = _pending_batch(partial_resize_stage=2, current_sl_id='S2', tp_order_id='T2')
    b2['protection_registry']['batch_A|SL|L0|LONG']['order_id'] = 'S2'
    b2['protection_registry']['batch_A|TP|L0|LONG']['order_id'] = 'T2'
    t2 = H.make_trader({SYM: {'batch_A': b2}}, actual_pos=0.5)
    ok2, msg2 = F['_resume_partial_resize'](t2, SYM, 'batch_A', 'OPX')
    assert ok2 is True, msg2
    assert not t2.exchange.cancel_calls and not t2.exchange.create_calls, \
        f'stage=2 只做 final CAS，零交易所副作用: {t2.exchange.cancel_calls} {t2.exchange.create_calls}'
    b3 = t2._states[SYM]['batch_A']
    assert b3['close_phase'] == 0 and b3['close_reason'] == ''
    assert 'partial_resize_stage' not in b3 and b3['current_sl_id'] == 'S2' and b3['tp_order_id'] == 'T2'


# ── R6：/partial 批次列表（纯本地、排除 ghost/冻结）+ monitor 运行期自愈接线 ─
def t06_r6_partial_listing_and_runtime_resume_wiring():
    fmt = _extract_br('_format_partial_candidates')
    assert fmt is not None, 'bot_runner 缺 _format_partial_candidates'

    def net_fn(b):
        gross = sum(float(x) for x in (b.get('target_amounts') or [])[:int(b.get('last_filled_count', 0) or 0)])
        return gross - float(b.get('realized_reduce_amount') or 0.0), 0.0

    ghost = H._batch(batch_id='batch_20260902_131748_0b15d7', entry_orders=[],
                     target_amounts=[], last_filled_count=0, current_sl_id=None,
                     tp_order_id=None)  # 净 0 ghost
    frozen = _pending_batch(batch_id='batch_20260902_142338_5693ba')  # 冻结中
    normal = H._batch(batch_id='batch_20260902_131855_c04f5c')  # net 1.0
    text = fmt({SYM: {'batch_20260902_131748_0b15d7': ghost,
                      'batch_20260902_142338_5693ba': frozen,
                      'batch_20260902_131855_c04f5c': normal}}, net_fn)
    assert 'batch_20260902_131855_c04f5c' in text, text
    assert 'batch_20260902_131748_0b15d7' not in text, f'ghost 必须排除: {text}'
    assert 'batch_20260902_142338_5693ba' not in text, f'冻结批次必须排除: {text}'
    assert '13:18:55' in text, f'开仓时间必须解析展示: {text}'
    empty = fmt({SYM: {'batch_20260902_131748_0b15d7': ghost}}, net_fn)
    assert '无可减仓批次' in empty, empty

    # monitor 冻结分支必须接线运行期自愈（结构断言：freeze 段 → continue 之间含调度调用）
    i = SRC.find('本轮跳过保护单维护')
    assert i > 0
    j = SRC.find('continue', i)
    seg = SRC[i:j]
    assert '_maybe_runtime_resume_partial' in seg, \
        'monitor 冻结分支未接线 partial 运行期自愈调度'
    assert SRC.count('_partial_resume_state') >= 2, '运行期自愈状态簿记缺失（init + 使用）'


# ── R7：守恒门拒绝 = terminal safety conflict → halted 停止本进程自动续跑 ────
def t07_r7_guard_terminal_halts_runtime_resume():
    calls = []
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.25)

    def fake_resume(symbol, batch_id, op):
        calls.append((symbol, batch_id, op))
        return False, 'resume_guard_rejected（归因冲突：actual < Σnet）'

    t._resume_partial_resize = fake_resume  # 影子桩：真实 callee 在入口 guard FAIL 时已 critical 一次
    sched = t._maybe_runtime_resume_partial
    sched(SYM, 'batch_A', 'OPX')            # 首见：只登记（R8 语义）
    assert calls == []
    t._partial_resume_state['batch_A']['ts'] -= 61
    sched(SYM, 'batch_A', 'OPX')            # 60s 后第一次尝试 → guard FAIL → 必须置 halted
    assert len(calls) == 1, calls
    assert t._partial_resume_state['batch_A'].get('halted') is True
    for _ in range(3):
        t._partial_resume_state['batch_A']['ts'] -= 61
        sched(SYM, 'batch_A', 'OPX')        # 之后无论过多久，绝不再调 resume/API
    assert len(calls) == 1, f'守恒 terminal 后必须停止自动续跑: {calls}'
    assert not any('连续 3 轮' in c for c in t._criticals), \
        'terminal 停机不得追加「将继续自动重试」类风暴 critical'

    # resize_guard_rejected_ 前缀（每腿 TOCTOU 权威门）同样必须 halt
    calls2 = []
    t2 = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)

    def fake_resume2(symbol, batch_id, op):
        calls2.append(op)
        return False, 'resize_guard_rejected_SL（撤单窗口内仓位已变化）'

    t2._resume_partial_resize = fake_resume2
    t2._maybe_runtime_resume_partial(SYM, 'batch_A', 'OPX')
    t2._partial_resume_state['batch_A']['ts'] -= 61
    t2._maybe_runtime_resume_partial(SYM, 'batch_A', 'OPX')
    assert len(calls2) == 1
    assert t2._partial_resume_state['batch_A'].get('halted') is True
    t2._partial_resume_state['batch_A']['ts'] -= 61
    t2._maybe_runtime_resume_partial(SYM, 'batch_A', 'OPX')
    assert len(calls2) == 1, '每腿守恒门拒绝后同样必须停机'


# ── R8：首次看到 pending 只登记不执行（防 /partial 事务 inflight 撞车假失败）；新 op 重新登记 ──
def t08_r8_first_sight_registers_only():
    calls = []
    t = H.make_trader({SYM: {'batch_A': _pending_batch()}}, actual_pos=0.5)

    def fake_resume(symbol, batch_id, op):
        calls.append(op)
        return True, 'partial_active'

    t._resume_partial_resize = fake_resume
    sched = t._maybe_runtime_resume_partial
    sched(SYM, 'batch_A', 'OPX')            # 首见：只登记 ts，零调用
    assert calls == [], '首见不得立即抢执行（会给 /partial 事务线程撞 inflight）'
    t._partial_resume_state['batch_A']['ts'] -= 59
    sched(SYM, 'batch_A', 'OPX')
    assert calls == [], '59s 不得执行'
    t._partial_resume_state['batch_A']['ts'] -= 2   # → 61s
    sched(SYM, 'batch_A', 'OPX')
    assert calls == ['OPX'], '60s+ 恰好执行一次'
    assert 'batch_A' not in t._partial_resume_state, '成功后状态必须清除'
    # 同批次新事务（新 close_op_id）必须重新登记——陈旧 ts 不得立即放行
    sched(SYM, 'batch_A', 'OPY')
    assert calls == ['OPX'], '新 op 首见必须重新登记（R8 时序重新起算）'
    st = t._partial_resume_state['batch_A']
    assert st.get('op') == 'OPY' and 'ts' in st


# ── R9（v6.4-P2 Fix A）：外部全平结算分支不得引用未绑定 b_data（实盘监控线程崩溃）──
def t09_p2_external_close_settlement_no_crash():
    """2026-09-02 16:30 实盘：app 手动全平 → 持仓归零结算分支引用 b_data
    （变量早已改名 latest_b_data，L5496 加载；函数 finally 区才赋值 b_data
    → Python 判定局部变量 → UnboundLocalError → 监控线程死亡，批次失去 SL/TP 维护）。
    结构断言：结算分支必须使用 latest_b_data；机制存证 finally 区 b_data 赋值仍在
    （它正是「局部变量未绑定」崩溃机制的来源，ChatGPT 核实）。"""
    seg = ''
    for node in ast.walk(H.TREE):
        if isinstance(node, ast.FunctionDef) and node.name == '_start_monitoring':
            seg = ast.get_source_segment(H.SRC, node) or ''
            break
    assert seg, '未找到 _start_monitoring'
    # 注意：'b_data.get(...)' 是 'latest_b_data.get(...)' 的后缀子串 → 必须用词边界正则
    pat = r"(?<![A-Za-z0-9_])b_data\."
    assert not re.search(pat + r"get\('realized_reduce_cost'", seg), \
        '持仓归零结算分支仍引用未绑定的 b_data（实盘崩溃点 L5525）'
    assert 'self._batch_net_position(b_data)' not in seg, \
        '持仓归零结算分支仍引用未绑定的 b_data（实盘崩溃点 L5524）'
    assert 'self._batch_net_position(latest_b_data)' in seg, '结算必须使用已加载的 latest_b_data'
    assert 'b_data = all_states.get(symbol, {}).get(batch_id, {})' in seg, \
        'finally 区 b_data 赋值（崩溃机制存证）应仍存在'


# ── R10（v6.4-P2 Fix C）：freeze console 提示节流（状态变化即打 / 不变 5min heartbeat）──
def t10_p2_freeze_print_throttle():
    """2026-09-02 14:27-16:25 实盘：🧊[P0 冻结] 行每监控周期无条件 print 刷屏 70+ 行。
    「3 次后静默」约定只覆盖 TG 通道；console 从未限流。断言：冻结分支 print 已节流
    （_freeze_print_state 簿记：状态变化立即打，持续不变 300s 一条 heartbeat）。"""
    i = SRC.find('本轮跳过保护单维护')
    assert i > 0
    j = SRC.find('continue', i)
    seg = SRC[max(0, i - 700):j]
    assert '_freeze_print_state' in seg, '冻结 print 未接节流簿记'
    assert '300' in seg, '节流窗口应为 300s heartbeat'
    # ChatGPT P2 边界：签名必须含 close_op_id——同批次新事务（新 op）立即打印，
    # 且退出冻结后旧缓存不会吞掉新事务首报
    assert 'close_op_id' in seg, '冻结 print 签名缺 close_op_id（同状态重入会被旧缓存吞掉）'
    assert SRC.count('_freeze_print_state') >= 2, '节流簿记缺失（init + 使用）'


TESTS = [t01_r1_cancel_verify_bounded_retry,
         t02_r2_stage_skip_completed_leg,
         t03_r3_toctou_conservation_gate,
         t04_r4_durable_price_no_old_order_dependency,
         t05_r5_per_leg_durable_commit,
         t06_r6_partial_listing_and_runtime_resume_wiring,
         t07_r7_guard_terminal_halts_runtime_resume,
         t08_r8_first_sight_registers_only,
         t09_p2_external_close_settlement_no_crash,
         t10_p2_freeze_print_throttle]


def main():
    if H.MISSING:
        print(f'RED 0/{len(TESTS)}（trader 未实现: {H.MISSING}）')
        return 1
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
