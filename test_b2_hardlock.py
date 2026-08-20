#!/usr/bin/env python3
"""B2-4 TDD：HARD_LOCK 真熔断 + 解锁审计收口（规格 §5.4/§5.5 + 重启恢复表 §6.2）

范围（本批）：
- registry 条目级 fail_count（FAILED 路径 fail_count_incr 递增，≥5 → hard_locked=True 落盘 + 进入时 1 次 critical）
- _assert_create_allowed 增加 HARD_LOCK 拦截（置于 registry 状态检查之前；reason 以 'HARD_LOCK' 开头供调用点静默）
- 6 个 FAILED 分支接入 fail_count_incr（AST/文本断言）
- 启动校验 _validate_registry_locks_on_startup：
    HARD_LOCK + hard_locked=false 且无审计三字段 → 回滚 hard_locked=true + critical（§5.5 非法解锁）
    HARD_LOCK + hard_locked=false 且有审计三字段（unlock_reason/time/operator）→ 合法解锁，不干预
    FAILED + fail_count≥5 未置锁（旧数据）→ 补置 hard_locked + critical（重启恢复表）
    HARD_LOCK + hard_locked=true → 维持锁定（静默）

范围外（后续批）：软计数 sl_fail_count/sl_error_count 收编合并、TG 解锁命令（U2 不做）、全批锁（Q7 倾向单锁）
"""
import os
import re
import sys
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trader_260725 import CryptoTrader

SYMBOL = 'BTCUSDT'
BATCH = 'batch_b2_4'
IDENTITY = f'{BATCH}|SL|L0|LONG'
PASS, FAIL = 0, 0
RESULTS = []


def report(name, passed, detail=''):
    global PASS, FAIL
    if passed:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((passed, name, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")


# =====================================================================
# 测试基建：MagicMock 基座 + 显式绑定全部 registry helper
# =====================================================================

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


def make_fake(states):
    """MagicMock 基座 + 内存 states 闭环（load→modify→save 生效）。
    ⚠️ 必记（第 4 次同类坑）：MagicMock 未显式绑定的方法退化为自动 mock；
    getattr 默认值被吞 → 必须显式 _api_cooldown_until = 0。"""
    fake = mock.MagicMock()
    fake._api_cooldown_until = 0
    fake._states = states
    fake.load_all_states = lambda: states
    fake.save_batch_state = lambda s, b, d: states.setdefault(s, {}).update({b: d})
    fake.send_tg_notification = lambda text, **kw: fake.sent.append((kw.get('level', 'info'), str(text)))
    fake.sent = []
    fake._update_registry = lambda s, b, i, **k: CryptoTrader._update_registry(fake, s, b, i, **k)
    if hasattr(CryptoTrader, '_assert_create_allowed'):
        fake._assert_create_allowed = (
            lambda s, b, i, **k: CryptoTrader._assert_create_allowed(fake, s, b, i, **k))
    if hasattr(CryptoTrader, '_validate_registry_locks_on_startup'):
        fake._validate_registry_locks_on_startup = (
            lambda: CryptoTrader._validate_registry_locks_on_startup(fake))
    return fake


# =====================================================================
# T1/T2: registry fail_count 计数（_update_registry fail_count_incr）
# =====================================================================

def scenario_fail_count_incr():
    try:
        states = _state_batch()
        fake = make_fake(states)
        fn = getattr(CryptoTrader, '_update_registry')
        # T1: 单次 incr 1 → 1
        fc1 = fn(fake, SYMBOL, BATCH, IDENTITY, state='FAILED', fail_count_incr=1)
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        ok1 = (fc1 == 1 and entry.get('fail_count') == 1 and entry.get('state') == 'FAILED')
        report('T1/单次fail_count递增', ok1, f"(fc={fc1}, entry={entry.get('fail_count')})")
        # T2: 连续 incr 累计 → 3
        fc2 = fn(fake, SYMBOL, BATCH, IDENTITY, state='FAILED', fail_count_incr=1)
        fc3 = fn(fake, SYMBOL, BATCH, IDENTITY, state='FAILED', fail_count_incr=1)
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        ok2 = (fc2 == 2 and fc3 == 3 and entry.get('fail_count') == 3)
        report('T2/连续递增累计', ok2, f"(fc2={fc2}, fc3={fc3}, entry={entry.get('fail_count')})")
    except Exception as e:
        report('T1/T2/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T3: 状态机闭环——5 次 FAILED → hard_locked=True + critical 1 次 + 闸门拦截
# =====================================================================

def scenario_lock_after_5_fails():
    try:
        states = _state_batch()
        fake = make_fake(states)
        # 模拟补挂 SL 的 FAILED 分支语义（与真实分支同构）：
        # 连续 5 次 ExchangeError → FAILED + fail_count_incr=1；达 5 → 置锁 + critical
        for i in range(5):
            fc = fake._update_registry(SYMBOL, BATCH, IDENTITY, state='FAILED',
                                       fail_count_incr=1, id_known=False, order_kind='conditional')
            if fc >= 5:
                fake._update_registry(SYMBOL, BATCH, IDENTITY, hard_locked=True)
                fake.send_tg_notification(
                    f"🚨【资金安全】批次 {BATCH} 止损单连续失败 5 次，已 HARD_LOCK",
                    level='critical')
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        crits = [m for lvl, m in fake.sent if lvl == 'critical']
        ok_lock = (entry.get('fail_count') == 5 and entry.get('hard_locked') is True
                   and entry.get('state') == 'FAILED')
        report('T3a/5次失败置硬锁', ok_lock,
               f"(fail_count={entry.get('fail_count')}, hard_locked={entry.get('hard_locked')})")
        report('T3b/进入时critical一次', len(crits) == 1, f"(crits={len(crits)})")
        # 第 6 次 create 前仲裁 → 必须拦截
        allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, IDENTITY, desc='补挂止损单')
        report('T3c/硬锁后闸门拦截', (allowed is False and 'HARD_LOCK' in reason),
               f"(allowed={allowed}, reason={reason[:60]})")
    except Exception as e:
        report('T3/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T4/T5: _assert_create_allowed 对 HARD_LOCK 拦截（reason 前缀供静默）
# =====================================================================

def scenario_gate_hardlock():
    try:
        states = _state_batch()
        states[SYMBOL][BATCH]['protection_registry'][IDENTITY] = {
            'state': 'HARD_LOCK', 'hard_locked': True, 'fail_count': 5,
            'order_kind': 'conditional', 'updated_at': time.time(),
        }
        fake = make_fake(states)
        allowed, reason = fake._assert_create_allowed(SYMBOL, BATCH, IDENTITY, desc='补挂止损单')
        ok = (allowed is False and reason.startswith('HARD_LOCK'))
        report('T4/HARD_LOCK拦截+前缀', ok, f"(allowed={allowed}, reason={reason[:60]})")
        # T5: 硬锁 reason 不含 cooldown（不是全局冷却导致），且不抛异常可稳定调用
        allowed2, reason2 = fake._assert_create_allowed(SYMBOL, BATCH, IDENTITY, desc='降级恢复')
        ok2 = (allowed2 is False and 'cooldown' not in reason2)
        report('T5/硬锁≠cooldown且稳定', ok2, f"(reason2={reason2[:50]})")
    except Exception as e:
        report('T4/T5/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T6-T9: 启动校验 _validate_registry_locks_on_startup
# =====================================================================

def _reg_entry(**over):
    e = {'state': 'HARD_LOCK', 'hard_locked': True, 'fail_count': 5,
         'order_kind': 'conditional', 'updated_at': time.time()}
    e.update(over)
    return e


def scenario_startup_validation():
    # T6: HARD_LOCK + hard_locked=false + 无审计三字段 → 回滚 true + critical
    try:
        states = _state_batch()
        states[SYMBOL][BATCH]['protection_registry'][IDENTITY] = _reg_entry(hard_locked=False)
        fake = make_fake(states)
        fake._validate_registry_locks_on_startup()
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        crits = [m for lvl, m in fake.sent if lvl == 'critical']
        ok = (entry.get('hard_locked') is True and len(crits) == 1)
        report('T6/非法解锁回滚锁+critical', ok,
               f"(hard_locked={entry.get('hard_locked')}, crits={len(crits)})")
    except Exception as e:
        report('T6/异常', False, f"EXC {type(e).__name__}: {e}")

    # T7: HARD_LOCK + hard_locked=false + 有审计三字段 → 合法解锁，不干预
    try:
        states = _state_batch()
        states[SYMBOL][BATCH]['protection_registry'][IDENTITY] = _reg_entry(
            hard_locked=False,
            unlock_reason='交易所核实确无此单',
            unlock_time='2026-08-20T10:00:00+08:00',
            unlock_operator='manual-human')
        fake = make_fake(states)
        fake.sent = []
        fake._validate_registry_locks_on_startup()
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        ok = (entry.get('hard_locked') is False and fake.sent == [])
        report('T7/合法解锁不干预', ok,
               f"(hard_locked={entry.get('hard_locked')}, sent={len(fake.sent)})")
    except Exception as e:
        report('T7/异常', False, f"EXC {type(e).__name__}: {e}")

    # T8: FAILED + fail_count≥5 未置锁（旧数据）→ 补置 hard_locked + critical
    try:
        states = _state_batch()
        states[SYMBOL][BATCH]['protection_registry'][IDENTITY] = {
            'state': 'FAILED', 'fail_count': 5, 'order_kind': 'conditional',
            'updated_at': time.time(),
        }
        fake = make_fake(states)
        fake._validate_registry_locks_on_startup()
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        crits = [m for lvl, m in fake.sent if lvl == 'critical']
        ok = (entry.get('hard_locked') is True and entry.get('state') == 'HARD_LOCK'
              and len(crits) == 1)
        report('T8/旧数据补置硬锁+critical', ok,
               f"(state={entry.get('state')}, hard_locked={entry.get('hard_locked')}, crits={len(crits)})")
    except Exception as e:
        report('T8/异常', False, f"EXC {type(e).__name__}: {e}")

    # T9: HARD_LOCK + hard_locked=true → 维持（静默）
    try:
        states = _state_batch()
        states[SYMBOL][BATCH]['protection_registry'][IDENTITY] = _reg_entry(hard_locked=True)
        fake = make_fake(states)
        fake.sent = []
        fake._validate_registry_locks_on_startup()
        entry = states[SYMBOL][BATCH]['protection_registry'][IDENTITY]
        ok = (entry.get('hard_locked') is True and fake.sent == [])
        report('T9/已锁维持静默', ok,
               f"(hard_locked={entry.get('hard_locked')}, sent={len(fake.sent)})")
    except Exception as e:
        report('T9/异常', False, f"EXC {type(e).__name__}: {e}")


# =====================================================================
# T10: 源码断言——helper 签名 / HARD_LOCK 分支 / 6 FAILED 分支接入 / 静默分支
# =====================================================================

def scenario_source_asserts():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'trader_260725.py'), encoding='utf-8').read()
    # a) _update_registry 支持 fail_count_incr / hard_locked
    m = re.search(r'def _update_registry\(self, symbol, batch_id, identity, state=None, order_id=None,'
                  r'\s*id_known=None, order_kind=None, role=None, layer=None, side=None,\s*intent=None'
                  r'([^)]*)\)', src)
    ok_a = m is not None and 'fail_count_incr' in m.group(1) and 'hard_locked' in m.group(1)
    report('T10a/_update_registry签名含fail_count_incr+hard_locked', ok_a)
    # b) _assert_create_allowed 体内含 HARD_LOCK 检查
    m = re.search(r'def _assert_create_allowed\(self, symbol, batch_id, identity, desc=.*?\):(.*?)'
                  r'\n    def ', src, re.S)
    ok_b = m is not None and "state == 'HARD_LOCK'" in m.group(1)
    report('T10b/_assert_create_allowed含HARD_LOCK分支', ok_b,
           f"(found={'state == HARD_LOCK' if ok_b else 'NO'})")
    # c) _validate_registry_locks_on_startup 存在且含审计三字段
    m = re.search(r'def _validate_registry_locks_on_startup\(self\):(.*?)\n    def ', src, re.S)
    ok_c = m is not None and all(k in m.group(1) for k in
                                 ('unlock_reason', 'unlock_time', 'unlock_operator'))
    report('T10c/启动校验含审计三字段', ok_c)
    # d) FAILED 分支接入 fail_count_incr=1 —— 5 个独立 FAILED 分支
    #    （补挂SL/补挂TP/预生成SL×2/预生成TP）。降级恢复复用补挂 SL 的 FAILED
    #    （recovery_identity == sl_identity，同一 identity 不重复计数）
    n_incr = src.count('fail_count_incr=1')
    report('T10d/FAILED分支接入fail_count_incr≥5', n_incr >= 5, f"(count={n_incr})")
    # e) 6 个调用点硬锁静默分支（gate_reason.startswith("HARD_LOCK")）
    n_silent = src.count("gate_reason.startswith('HARD_LOCK')")
    report('T10e/调用点硬锁静默分支≥6', n_silent >= 6, f"(count={n_silent})")


# =====================================================================

def main():
    print("=" * 60)
    print("B2-4 HARD_LOCK + 解锁审计 TDD")
    print("=" * 60)
    scenario_fail_count_incr()
    scenario_lock_after_5_fails()
    scenario_gate_hardlock()
    scenario_startup_validation()
    scenario_source_asserts()
    print("=" * 60)
    print(f"✅ PASS {PASS}  ❌ FAIL {FAIL}")
    if FAIL:
        for p, n, d in RESULTS:
            if not p:
                print(f"  ❌ {n} {d}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
