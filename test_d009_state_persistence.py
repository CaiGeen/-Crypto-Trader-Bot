# -*- coding: utf-8 -*-
"""
D-009 State Persistence / Crash Recovery 专项测试（2026-08-29）

依据：D-009_State持久化与崩溃恢复_阶段二实施规格v2_送审ChatGPT.md
      ChatGPT 阶段一正式裁定（Q1 READY=False 不退出 / Q2 最小 Position Census /
      Q3 墓碑局部 Fail-Closed / Persistence 批准 fsync）

裁定锁定语义（测试即规格）：
  P0-A  trade_state.json 损坏 → Fail-Closed：READY=False，绝不把"不知道"当成"没有"
        ⚠️ 必须区分三种"空"：文件不存在 / 合法 {} / 账本损坏（安全含义完全不同）
  P0-B  _persist_states：flush → fsync(file) → os.replace → fsync(dir, 尽力降级)
  P0-C  损坏时启动 Exchange Position Census：只读普查，绝不自动修复
  Q3    墓碑损坏 → DEGRADED：不阻断启动；全新批次拒绝写入，已存在批次放行
  边界  .bak 永不进入任何自动读取路径

状态（2026-08-29 实施后）：GREEN 16/16 PASS，rc=0。
  RED 基线（c35f014 实施前）为 2/16 PASS、rc=1，14 项 FAIL 均为真缺陷实证：
  R-D1  load_all_states 损坏 → 静默返回 {}（无 _state_corrupted 标志）
  R-D2  recover_active_batches 在损坏账本下返回 True → bot_runner 置位 READY（孤儿仓风险）
  R-D3  fetch_positions 在损坏场景下根本不被调用（位于循环体内，空账本零迭代）
  R-D4  _persist_states 无 fsync
  R-D5  墓碑损坏静默放行，无 DEGRADED 标志

运行：.venv/Scripts/python.exe test_d009_state_persistence.py（ccxt 只在项目 .venv）
⚠️ 安全：全部文件 I/O 重定向到临时目录，绝不触碰实盘 trade_state.json / .bak / 墓碑。
"""
import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

SYMBOL = "BTC/USDT:USDT"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, bool(passed)))
    print(f"\n{'=' * 64}\n[{'PASS' if passed else 'FAIL'}] {name}\n{'=' * 64}"
          + (f"\n  → {detail}" if detail else ""))
    return bool(passed)


# =====================================================================
# 夹具：临时目录 + 最小 fake trader
# =====================================================================
class Env:
    """临时状态目录，退出即销毁。绝不触碰实盘文件。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="d009_")
        self.state = os.path.join(self.dir, "trade_state.json")
        self.bak = self.state + ".bak"
        self.tomb = os.path.join(self.dir, "trade_tombstones.json")

    def write_state(self, text):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(text)

    def write_bak(self, text):
        with open(self.bak, "w", encoding="utf-8") as f:
            f.write(text)

    def write_tomb(self, text):
        with open(self.tomb, "w", encoding="utf-8") as f:
            f.write(text)


class FakeExchange:
    """最小交易所桩：健康检查 + 持仓普查所需。"""

    def __init__(self, position_amt=0.0, fail_positions=False):
        self.position_amt = position_amt
        self.fail_positions = fail_positions
        self.fetch_positions_calls = 0

    def fetch_time(self):
        return 1700000000000

    def fetch_positions(self, symbols=None, params=None):
        self.fetch_positions_calls += 1
        if self.fail_positions:
            raise RuntimeError("API unreachable")
        return [{'symbol': SYMBOL, 'side': 'long',
                 'contracts': self.position_amt, 'positionAmt': self.position_amt,
                 'info': {'symbol': 'BTCUSDT'}}]


def make_fake(env, ex, position_amt=0.0):
    """MagicMock 底座 + 真实方法绑定（沿用 Batch B 既定范式）。"""
    fake = mock.MagicMock()
    fake._state_lock = threading.Lock()
    fake._api_cooldown_until = 0
    fake._ready = False                       # SG1: 默认 Fail-Closed（与生产 L178 同）
    fake._not_ready_reason = "启动恢复中"
    fake._state_corrupted = False             # D-009 P0-A（GREEN 后由生产代码置位）
    fake._state_corruption_detail = ""
    fake._tombstones_degraded = False         # D-009 Q3
    fake._tombstone_alerted = set()
    fake.tombstone_file = env.tomb
    fake.exchange = ex
    fake._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    fake.sent = []
    fake.send_tg_notification = lambda text, **kw: fake.sent.append(
        (kw.get('level', 'info'), str(text)))
    fake.opened_paths = []

    # 真实方法绑定（生产逻辑本体参与测试）
    _bind = ('load_all_states', '_persist_states', '_load_tombstones',
             '_persist_tombstones', '_prune_tombstones', 'save_batch_state',
             'clear_batch_state', 'recover_active_batches', 'execute_signal',
             '_merge_batch_state', '_collect_batch_order_ids',
             # R-D3/R-D4/R-D5：D-009 新增件（RED 阶段缺失 → MagicMock → 即 RED 信号）
             '_run_position_census', '_fsync_dir')
    for _n in _bind:
        if hasattr(CryptoTrader, _n):
            setattr(fake, _n, (lambda _n=_n: lambda *a, **k: getattr(CryptoTrader, _n)(
                fake, *a, **k))())
    return fake


def _run(fn):
    """捕获被测代码 stdout，保持报告输出整洁。"""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            return fn()
    except Exception as e:
        return ('__EXC__', f"{type(e).__name__}: {e}", buf.getvalue())


# =====================================================================
# R-D1：损坏检测三态分离
# =====================================================================
def _case_corrupt(env, content, label):
    """通用损坏用例：账本损坏 → 必须置 _state_corrupted 且 recover 返回 False。"""
    env.write_state(content)
    ex = FakeExchange(position_amt=0.0)
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        states = _run(lambda: fake.load_all_states())
        corrupted = getattr(fake, '_state_corrupted', None)
        rec = _run(lambda: fake.recover_active_batches())
        # 模拟 bot_runner:2397 唯一置位源（精确复现生产链路）
        if rec is True:
            fake._ready = True
    ok = (corrupted is True) and (rec is False) and (fake._ready is False)
    return report(f"R-D1 {label}：损坏 → Fail-Closed", ok,
                  f"_state_corrupted={corrupted!r} recover={rec!r} _ready={fake._ready!r}")


def t1_zero_byte(env):
    return _case_corrupt(env, "", "0 字节")


def t2_truncated(env):
    return _case_corrupt(env, '{"BTC/USDT:USDT": {"b1": {"is_act', "截断 JSON")


def t3_non_json(env):
    return _case_corrupt(env, "not a json at all ###", "非 JSON")


def t4_non_dict_root(env):
    results = []
    for content, label in ('[1, 2, 3]', '数组根'), ('"just a string"', '字符串根'):
        env.write_state(content)
        ex = FakeExchange()
        fake = make_fake(env, ex)
        with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
            _run(lambda: fake.load_all_states())
            corrupted = getattr(fake, '_state_corrupted', None)
            rec = _run(lambda: fake.recover_active_batches())
            if rec is True:
                fake._ready = True
        results.append(report(f"R-D1 {label}：根节点非 dict → Fail-Closed",
                              corrupted is True and rec is False and fake._ready is False,
                              f"_state_corrupted={corrupted!r} recover={rec!r}"))
    return all(results)


# =====================================================================
# R-D1 负向：合法空不得误伤（回归保护，RED 阶段应保持 PASS）
# =====================================================================
def t7_missing_file(env):
    """文件不存在（首次启动）→ 正常，_state_corrupted=False，recover 返回 True。"""
    if os.path.exists(env.state):
        os.remove(env.state)
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        _run(lambda: fake.load_all_states())
        corrupted = getattr(fake, '_state_corrupted', None)
        rec = _run(lambda: fake.recover_active_batches())
    return report("R-D1 负向：文件不存在 → 正常启动（不得误伤）",
                  corrupted is False and rec is True,
                  f"_state_corrupted={corrupted!r} recover={rec!r}")


def t8_valid_empty(env):
    """合法 {} → 正常，_state_corrupted=False，READY 必须可置位。"""
    env.write_state("{}")
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        _run(lambda: fake.load_all_states())
        corrupted = getattr(fake, '_state_corrupted', None)
        rec = _run(lambda: fake.recover_active_batches())
        if rec is True:
            fake._ready = True
    return report("R-D1 负向：合法 {} → 正常启动且 READY（不得误伤）",
                  corrupted is False and rec is True and fake._ready is True,
                  f"_state_corrupted={corrupted!r} recover={rec!r} _ready={fake._ready!r}")


# =====================================================================
# .bak：永不自动读取
# =====================================================================
def t5_bak_intact(env):
    """主文件损坏 + .bak 完好 → 仍 Fail-Closed，且 .bak 绝不被读取。"""
    env.write_state("")
    env.write_bak(json.dumps({"BTC/USDT:USDT": {
        "batch_ghost": {"is_active": True, "tp_order_id": "tp_ghost"}}}))
    ex = FakeExchange()
    fake = make_fake(env, ex)
    opened = []
    _real_open = open

    def _spy_open(path, *a, **k):
        try:
            opened.append(str(path))
        except Exception:
            pass
        return _real_open(path, *a, **k)

    with mock.patch.object(trader_260725, 'STATE_FILE', env.state), \
            mock.patch('builtins.open', side_effect=_spy_open):
        rec = _run(lambda: fake.recover_active_batches())
    bak_reads = [p for p in opened if p.endswith('.bak')]
    corrupted = getattr(fake, '_state_corrupted', None)
    return report("R-D1 主损坏 + .bak 完好：仍 Fail-Closed 且 .bak 零读取",
                  rec is False and corrupted is True and not bak_reads,
                  f"recover={rec!r} _state_corrupted={corrupted!r} .bak读取={len(bak_reads)}次")


def t6_bak_also_corrupt(env):
    """主文件损坏 + .bak 也损坏 → Fail-Closed + CRITICAL 告警。"""
    env.write_state("")
    env.write_bak("also broken ###")
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        rec = _run(lambda: fake.recover_active_batches())
    crit = [t for lv, t in fake.sent if lv == 'critical']
    return report("R-D1 主损坏 + .bak 也损坏：Fail-Closed + CRITICAL 告警",
                  rec is False and len(crit) >= 1,
                  f"recover={rec!r} critical告警={len(crit)}条")


# =====================================================================
# R-D2 / R-D3：READY 恒 False + Position Census 触发
# =====================================================================
def t9_ready_false(env):
    """损坏 → READY 恒 False 且 _not_ready_reason 非空。"""
    env.write_state("{broken")
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        rec = _run(lambda: fake.recover_active_batches())
        if rec is True:
            fake._ready = True
    return report("R-D2 损坏 → READY 恒 False + 原因非空",
                  fake._ready is False and bool(fake._not_ready_reason),
                  f"_ready={fake._ready!r} reason={fake._not_ready_reason!r}")


def t10_no_risk_increase(env):
    """损坏 → 新建仓位被 SG1 拦截（不得因账本空而放行）。

    精确断言：SG1 生效时 execute_signal 返回 None（L2685-2687 拦截路径）；
    缺陷路径下 _ready 被错误置位 → 信号放行或深入执行 → 断言失败。
    """
    env.write_state("{broken")
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        rec = _run(lambda: fake.recover_active_batches())
        if rec is True:
            fake._ready = True            # 缺陷路径：账本空 → recover True → 置位
        # signal 必须是对象（生产为 TradingSignal），不可传 dict（否则测的是 AttributeError）
        sig = SimpleNamespace(
            symbol=SYMBOL, batch_id='batch_new_risk', side='BUY',
            entry_price=60000.0, stop_steps=[55000.0], take_profit_price=70000.0,
            is_hedge_mode=False, leverage=10, amount_usdt=100.0, signal_id='d009_t10')
        out = _run(lambda: fake.execute_signal(sig))
    refused = (out is None)               # SG1 拦截的唯一返回值
    return report("R-D2 损坏 → 禁止新建仓位（SG1 联动）",
                  fake._ready is False and refused,
                  f"_ready={fake._ready!r} execute_signal→{str(out)[:70]!r} "
                  f"(修复后应为 _ready=False 且返回 None)")


def t11_census_triggered(env):
    """核心回归锚点：损坏 → fetch_positions 必须被调用。
    （修复前 fetch_positions 位于循环体内，空账本导致根本不执行）"""
    env.write_state("{broken")
    ex = FakeExchange(position_amt=0.0)
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        _run(lambda: fake.recover_active_batches())
    return report("R-D3 损坏 → 触发 Position Census（fetch_positions 被调用）",
                  ex.fetch_positions_calls >= 1,
                  f"fetch_positions 调用={ex.fetch_positions_calls} 次（修复前=0）")


def t12_census_critical(env):
    """交易所存在仓位 → CRITICAL 告警且含仓位数量。"""
    env.write_state("{broken")
    ex = FakeExchange(position_amt=0.35)
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        _run(lambda: fake.recover_active_batches())
    crit = [t for lv, t in fake.sent if lv == 'critical']
    has_amt = any('0.35' in t for t in crit)
    return report("R-D3 交易所存在仓位 → CRITICAL 且含仓位数量",
                  len(crit) >= 1 and has_amt,
                  f"critical={len(crit)}条 含0.35={has_amt}")


# =====================================================================
# R-D4：fsync 持久化
# =====================================================================
def t15_fsync_called(env):
    """_persist_states 必须调用 os.fsync（降概率器，非安全边界）。"""
    ex = FakeExchange()
    fake = make_fake(env, ex)
    calls = []
    _real_fsync = os.fsync

    def _spy_fsync(fd):
        calls.append(fd)
        return None

    with mock.patch.object(trader_260725, 'STATE_FILE', env.state), \
            mock.patch.object(os, 'fsync', side_effect=_spy_fsync):
        _run(lambda: fake._persist_states({"BTC/USDT:USDT": {"b1": {"is_active": True}}}))
    return report("R-D4 _persist_states 调用 os.fsync（内容落盘）",
                  len(calls) >= 1, f"os.fsync 调用={len(calls)} 次（修复前=0）")


def t15b_replace_after_fsync(env):
    """顺序正确：fsync 必须在 os.replace 之前。"""
    ex = FakeExchange()
    fake = make_fake(env, ex)
    seq = []
    _real_replace = os.replace
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state), \
            mock.patch.object(os, 'fsync', side_effect=lambda fd: seq.append('fsync')), \
            mock.patch.object(os, 'replace',
                              side_effect=lambda a, b: (seq.append('replace'),
                                                        _real_replace(a, b))[1]):
        _run(lambda: fake._persist_states({"S": {"b1": {"is_active": True}}}))
    ok = 'fsync' in seq and 'replace' in seq and seq.index('fsync') < seq.index('replace')
    return report("R-D4 顺序：fsync(file) 先于 os.replace", ok, f"调用序列={seq}")


# =====================================================================
# R-D5：墓碑 DEGRADED（Q3 局部 Fail-Closed）
# =====================================================================
def t14_tombstone_degraded(env):
    """墓碑损坏 → DEGRADED：不阻断启动（READY 正常），但全新批次被拒、已有批次放行。"""
    env.write_state(json.dumps({"BTC/USDT:USDT": {
        "batch_exist": {"is_active": True, "batch_id": "batch_exist",
                        "symbol": SYMBOL, "entry_orders": []}}}))
    env.write_tomb("tombstone broken ###")
    ex = FakeExchange()
    fake = make_fake(env, ex)
    with mock.patch.object(trader_260725, 'STATE_FILE', env.state):
        _run(lambda: fake.load_all_states())
        _run(lambda: fake._load_tombstones())
        degraded = getattr(fake, '_tombstones_degraded', None)
        # 已有批次更新 → 必须放行（存在性由 trade_state 证明，与墓碑无关）
        before = len(fake.sent)
        _run(lambda: fake.save_batch_state(
            SYMBOL, "batch_exist", {"is_active": True, "symbol": SYMBOL}))
        existing_ok = len(fake.sent) == before        # 未产生新告警 = 放行
        states_after = _run(lambda: fake.load_all_states())
        existing_written = isinstance(states_after, dict) and \
            'batch_exist' in states_after.get(SYMBOL, {})
        # 全新批次 → 必须拒绝（无法排除是已清理批次复活）
        _run(lambda: fake.save_batch_state(
            SYMBOL, "batch_brand_new", {"is_active": True, "symbol": SYMBOL}))
        states_final = _run(lambda: fake.load_all_states())
        new_rejected = isinstance(states_final, dict) and \
            'batch_brand_new' not in states_final.get(SYMBOL, {})
    ok = (degraded is True) and existing_ok and existing_written and new_rejected
    return report("R-D5 墓碑损坏 → DEGRADED（已有批次放行 / 全新批次拒绝）", ok,
                  f"_tombstones_degraded={degraded!r} 已有放行={existing_ok and existing_written} "
                  f"全新拒绝={new_rejected}")


# =====================================================================
def main():
    print("=" * 64)
    print("D-009 State Persistence / Crash Recovery 专项测试")
    print(f"基线: {os.popen('git log -1 --format=%h').read().strip() or 'unknown'}  "
          f"(D-009 实施后：预期 16/16 PASS)")
    print("=" * 64)
    env = Env()
    cases = [
        ("T1  0 字节 trade_state", lambda: t1_zero_byte(env)),
        ("T2  截断 JSON", lambda: t2_truncated(env)),
        ("T3  非 JSON", lambda: t3_non_json(env)),
        ("T4  根节点非 dict", lambda: t4_non_dict_root(env)),
        ("T5  主损坏 + .bak 完好", lambda: t5_bak_intact(env)),
        ("T6  主损坏 + .bak 也损坏", lambda: t6_bak_also_corrupt(env)),
        ("T7  文件不存在（负向）", lambda: t7_missing_file(env)),
        ("T8  合法 {}（负向）", lambda: t8_valid_empty(env)),
        ("T9  损坏 → READY 恒 False", lambda: t9_ready_false(env)),
        ("T10 损坏 → 禁止风险增加", lambda: t10_no_risk_increase(env)),
        ("T11 损坏 → Position Census", lambda: t11_census_triggered(env)),
        ("T12 有仓位 → CRITICAL", lambda: t12_census_critical(env)),
        ("T13 .bak 零读取（并入 T5）", lambda: True),
        ("T14 墓碑 DEGRADED", lambda: t14_tombstone_degraded(env)),
        ("T15 fsync 调用", lambda: t15_fsync_called(env)),
        ("T15b fsync 先于 replace", lambda: t15b_replace_after_fsync(env)),
    ]
    for name, fn in cases:
        if name.startswith("T13"):
            print(f"\n[SKIP] {name} —— 已并入 T5（.bak 零读取断言）")
            continue
        try:
            fn()
        except Exception as e:
            report(name, False, f"用例异常: {type(e).__name__}: {e}")

    passed = sum(1 for _, p in RESULTS if p)
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"D-009 汇总: {passed}/{total} PASS, {total - passed}/{total} FAIL")
    print("=" * 64)
    for name, p in RESULTS:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
