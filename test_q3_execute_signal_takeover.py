#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Q3 建仓成功路径「业务 Commit → 接管失败」——反例 / 修后 / 健康阳性对照（v1.2 P1）。

## 缺陷（审计报告 Q3，主审读码核验 E4，时序锚点 6927→6948→6982 旧号）

`execute_signal` 正常成功建单的收尾顺序是：

1. `save_batch_state(...)`（**业务 Commit**：ENTRY 单已全部挂出、完整状态落盘）
2. `load_time_difference`（NTP 网络调用，可抛）
3. 启动监控线程 `_start_monitoring`（**接管**）
4. `return batch_id`；外层 `except → return None`

三个缺口：
  ① Commit 之后、监控启动之前的任何异常（典型：NTP 同步失败）→ 落到外层
     except → `return None`：**ENTRY 已挂、账本 active、监控未启动**——
     调用方按失败处理 → 盲重试风险（C5 同类），本进程内无人接管；
  ② Commit 的 `save_batch_state` 返回值**未检查**（`is not True` 硬门只在
     骨架阶段，提交点靠运气继续）；
  ③ 监控线程自身启动失败时无处置（无告警、返回 None 诱导重试）。

修后：监控接管**紧邻 Commit**；Commit 返回值显式接管（只补语境日志，
不重复发泛化告警——save_batch_state 是落盘告警唯一源，见 test_m2_m4 合同）；
提交点之后全部内联 try（保证金提示 / 时间同步失败只记录），监控启动失败
发 critical（无人接管是资金安全事件）且仍返回 batch_id；外层 except 只剩
Commit 之前的失败，消息携带「部分成功」准确状态（骨架已落、ENTRY 可能部分挂出）。

## 分组

- **反例组（修前红 → 修后绿）**：R1 时间同步失败 → 仍须返回 batch_id + 监控已启动；
  R2 提交点落盘失败 → 须有 [Q3] 语境日志（且 critical 恒空 = M2 告警合同）；
  R3 监控线程启动失败 → 返回 batch_id + critical（无人接管告警）。
- **健康阳性对照**：C1 正常成功 → 与修前完全一致（batch_id + 恰 1 个监控线程 +
  3 层 ENTRY + 无 [Q3] 日志 + 无 critical）。
- **结构锚点**：S1 `monitor_thread.start()` 在 `load_time_difference` 之前、
  `_commit_ok` 接管在场（防顺序回退/漏检）。

夹具：复用 `tests_archive/test_b2_crashsafe_entry.py` 的 `make_fake`/`FakeSignal`
（test_m2_m4_maintenance 同款加载方式；全离线、零生产文件）。
跑法：`.venv\\Scripts\\python.exe test_q3_execute_signal_takeover.py`（rc=0 即全过）
"""
import ast
import contextlib
import copy
import importlib.util
import io
import os
import sys
import threading
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trader_260725  # noqa: E402
from trader_260725 import CryptoTrader  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


def _load_fixture():
    """tests_archive 夹具加载（与 test_m2_m4_maintenance._fixture 同款）。"""
    path = os.path.join(ROOT, 'tests_archive', 'test_b2_crashsafe_entry.py')
    spec = importlib.util.spec_from_file_location('q3_b2_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIX = _load_fixture()


class _RecordingThread:
    """threading.Thread 替换：记录构造与 start，不真正起线程。"""
    created = []

    def __init__(self, *a, **k):
        self.kwargs = k
        self.started = False
        _RecordingThread.created.append(self)

    def start(self):
        self.started = True


class _BoomThread:
    """start() 必抛——模拟监控线程启动失败（资源耗尽等）。"""
    def __init__(self, *a, **k):
        self.kwargs = k

    def start(self):
        raise RuntimeError('can not start new thread')


def _run(fake, thread_cls=_RecordingThread):
    _RecordingThread.created.clear()
    buf = io.StringIO()
    with mock.patch.object(trader_260725.threading, 'Thread', thread_cls):
        with contextlib.redirect_stdout(buf):
            ret = CryptoTrader.execute_signal(fake, FIX.FakeSignal())
    return ret, buf.getvalue(), list(_RecordingThread.created)


def _raises(fn_exc):
    def _boom():
        raise fn_exc
    return _boom


def _monitor_threads(threads):
    """RecordingThread 存的是 Thread.__init__ 的原始 k：真实业务 kwargs 在 k['kwargs']。"""
    out = []
    for t in threads:
        kw = getattr(t, 'kwargs', {}) or {}
        inner = kw.get('kwargs') if isinstance(kw, dict) else None
        target = (inner if isinstance(inner, dict) else kw) or {}
        if 'batch_id' in target:
            out.append(t)
    return out


def _criticals(fake):
    return [t for lv, t in fake.sent if lv == 'critical']


def _batch(fake):
    return fake.states.get(FIX.SYMBOL, {}).get(FIX.BATCH, {})


# --------------------------------------------------------------------------
# 反例1：业务 Commit 已过、NTP 同步失败 → 不得 return None、不得丢接管
# --------------------------------------------------------------------------

def check_r1_timesync_fail_keeps_takeover():
    fake = FIX.make_fake()
    fake.exchange.load_time_difference = _raises(RuntimeError('NTP sync failed'))
    ret, out, threads = _run(fake)

    report(
        "R1-1 前提：Commit 已发生（3 层 ENTRY 挂出 + 完整状态已落盘）",
        fake._create_n == 3 and len(_batch(fake).get('entry_orders', [])) == 3,
        f"create={fake._create_n}；磁盘 entry_orders={len(_batch(fake).get('entry_orders', []))}")

    report(
        "R1-2 NTP 同步失败仍返回 batch_id（修前=None → 调用方按失败处理，盲重试双挂）",
        ret == FIX.BATCH,
        f"ret={ret!r}（期望 {FIX.BATCH!r}）")

    report(
        "R1-3 监控接管已启动（修前：线程在时间同步之后才创建 → 未启动 = 无人接管裸奔）",
        len(_monitor_threads(threads)) == 1 and threads[0].started,
        f"监控线程数={len(_monitor_threads(threads))} started="
        f"{[t.started for t in threads]}")


# --------------------------------------------------------------------------
# 反例2：业务 Commit 返回值必须显式接管（语境日志；critical 恒空 = M2 合同）
# --------------------------------------------------------------------------

def check_r2_commit_return_checked():
    fake = FIX.make_fake()
    orig_save = fake.save_batch_state
    calls = {'n': 0}

    def _save_2nd_fail(symbol, batch_id, data):
        calls['n'] += 1
        if calls['n'] >= 2:          # 第 1 次=骨架（硬门须过），第 2 次=业务 Commit
            return False
        return orig_save(symbol, batch_id, data)

    fake.save_batch_state = _save_2nd_fail
    ret, out, threads = _run(fake)

    report(
        "R2-1 提交点落盘失败 → [Q3] 语境日志（修前：返回值未检查，零痕迹）",
        '[Q3]' in out and '落盘未确认' in out,
        f"stdout 含 [Q3]={('[Q3]' in out)} 含『落盘未确认』={('落盘未确认' in out)}；"
        f"calls={calls['n']}")
    report(
        "R2-2 提交点失败仍接管并返回 batch_id（修前也成立，钉住不回退）",
        ret == FIX.BATCH and len(_monitor_threads(threads)) == 1,
        f"ret={ret!r}；监控线程={len(_monitor_threads(threads))}")
    report(
        "R2-3 M2 告警合同：execute_signal 不得自发泛化落盘 critical（恒空）",
        _criticals(fake) == [],
        f"critical={_criticals(fake)}")


# --------------------------------------------------------------------------
# 反例3：监控线程启动失败 → critical（无人接管）+ 不返回 None
# --------------------------------------------------------------------------

def check_r3_monitor_start_fail_alerts():
    fake = FIX.make_fake()
    # 评审反例：先发告警再封闸——通知阻塞时两道闸门均为空，另一信号可继续开新仓。
    # 快照钉住「发『监控线程启动失败』告警那一刻」的闸门状态。
    gate_snap = {'unresolved': None, 'degraded': None}
    _orig_send = fake.send_tg_notification

    def _send_snap(text, **kw):
        if '监控线程启动失败' in str(text):
            gate_snap['unresolved'] = set(fake._unresolved_intent_batches)
            gate_snap['degraded'] = set(fake._poll_degraded_batches)
        return _orig_send(text, **kw)

    fake.send_tg_notification = _send_snap
    ret, out, _ = _run(fake, thread_cls=_BoomThread)
    crit = _criticals(fake)

    report(
        "R3-1 监控启动失败仍返回 batch_id（修前=None → 盲重试双挂）",
        ret == FIX.BATCH,
        f"ret={ret!r}（期望 {FIX.BATCH!r}）")
    report(
        "R3-2 监控启动失败 → critical『监控线程启动失败』（修前：零告警无人接管）",
        any('监控线程启动失败' in t for t in crit),
        f"critical={len(crit)} 条；正文前100={crit[0][:100] if crit else None!r}")
    report(
        "R3-3 启动失败的批次进风险闸门（未决意图+降级；G1 复核）",
        FIX.BATCH in fake._unresolved_intent_batches
        and FIX.BATCH in fake._poll_degraded_batches,
        f"unresolved={sorted(fake._unresolved_intent_batches)}；degraded={sorted(fake._poll_degraded_batches)}")
    report(
        "R3-4 先封闸后通知：发 critical 那一刻两道闸门已置位"
        "（修前通知先行，通知阻塞=裸奔窗口）",
        gate_snap['unresolved'] is not None and FIX.BATCH in gate_snap['unresolved']
        and FIX.BATCH in gate_snap['degraded'],
        f"通知时 unresolved={sorted(gate_snap['unresolved'])}；"
        f"degraded={sorted(gate_snap['degraded'])}")


# --------------------------------------------------------------------------
# 反例4（R1 独立复审）：监控线程**初始化阶段**崩溃（Thread.start 成功但线程
# 目标 8731-8831 段未捕获异常）→ 旧测试桩永不触达真实 _start_monitoring，
# 缺口漏检 → 线程静默死亡、marker 残留、无 critical。修后对称于恢复路径：
# critical + marker 清理 + 仍返回 batch_id
# --------------------------------------------------------------------------

def check_r4_monitor_init_crash_alerted_cleaned():
    fake = FIX.make_fake()
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitor_generations = {}
    fake._active_monitors = set()
    original_load = fake.load_all_states

    def _load_states_in_monitor():
        # 主线程照常读账本；监控子线程首次读账本即抛
        if threading.current_thread() is threading.main_thread():
            return original_load()
        raise RuntimeError('monitor-init load fail')

    fake.load_all_states = _load_states_in_monitor
    fake._start_monitoring = lambda *a, **k: CryptoTrader._start_monitoring(fake, *a, **k)
    # 评审反例：初始化失败出口也是先通知后封闸 → 快照通知瞬间闸门状态
    gate_snap = {'unresolved': None, 'degraded': None}
    _orig_send = fake.send_tg_notification

    def _send_snap(text, **kw):
        if '初始化阶段异常退出' in str(text):
            gate_snap['unresolved'] = set(fake._unresolved_intent_batches)
            gate_snap['degraded'] = set(fake._poll_degraded_batches)
        return _orig_send(text, **kw)

    fake.send_tg_notification = _send_snap
    buf = io.StringIO()
    ret = None
    with contextlib.redirect_stdout(buf):
        ret = CryptoTrader.execute_signal(fake, FIX.FakeSignal())
        time.sleep(1.0)  # 给监控线程留出崩溃与收尾窗口
    crit = _criticals(fake)
    marker_cleaned = FIX.BATCH not in fake._active_monitors
    gen_cleaned = FIX.BATCH not in fake._active_monitor_generations

    report(
        "R4-1 初始化崩溃仍返回 batch_id（不得 return None 诱导盲重试）",
        ret == FIX.BATCH,
        f"ret={ret!r}")
    report(
        "R4-2 初始化崩溃 → critical『初始化阶段异常退出』（修前零告警）",
        any('初始化阶段异常退出' in t for t in crit),
        f"critical={len(crit)} 条；首条前100={crit[0][:100] if crit else None!r}")
    report(
        "R4-3 崩溃后监控登记（marker + 代次）已清理（修前残留）",
        marker_cleaned and gen_cleaned,
        f"marker={not marker_cleaned}；代次登记残留={not gen_cleaned}")
    report(
        "R4-4 初始化崩溃的批次进风险闸门（未决意图+降级；G1 复核）",
        FIX.BATCH in fake._unresolved_intent_batches
        and FIX.BATCH in fake._poll_degraded_batches,
        f"unresolved={sorted(fake._unresolved_intent_batches)}；degraded={sorted(fake._poll_degraded_batches)}")
    report(
        "R4-5 先封闸后通知：发 critical 那一刻两道闸门已置位"
        "（修前通知先行，通知阻塞=裸奔窗口）",
        gate_snap['unresolved'] is not None and FIX.BATCH in gate_snap['unresolved']
        and FIX.BATCH in gate_snap['degraded'],
        f"通知时 unresolved={sorted(gate_snap['unresolved'])}；"
        f"degraded={sorted(gate_snap['degraded'])}")


# --------------------------------------------------------------------------
# 反例5（外部评审）：旧线程初始化失败时代次已被新线程替换——清理与封闸
# 必须受当前代次所有权约束（8873-8875 旧实现只查登记删除，marker 删除与
# 封闸无条件）。修前：新代次 marker 被误删 + 新代次被置未决闸门 + 误发
# 「无人接管」critical；修后三项全部让位新代次。
# --------------------------------------------------------------------------

def check_r5_gen_ownership_on_init_crash():
    fake = FIX.make_fake()
    fake._active_monitors_lock = threading.Lock()
    fake._active_monitor_generations = {}
    fake._active_monitors = set()
    original_load = fake.load_all_states
    hijacked = {'done': False}

    def _load_states_takeover_then_boom():
        # 主线程（execute_signal 本体）照常；监控子线程首次读账本时，
        # 模拟新代次已在本线程初始化期间完成接管登记，然后本（旧）线程崩溃
        if threading.current_thread() is threading.main_thread():
            return original_load()
        if not hijacked['done']:
            hijacked['done'] = True
            with fake._active_monitors_lock:
                fake._active_monitor_generations[FIX.BATCH] = 'gen_replacement'
                fake._active_monitors.add(FIX.BATCH)
        raise RuntimeError('init fail after replacement')

    fake.load_all_states = _load_states_takeover_then_boom
    fake._start_monitoring = lambda *a, **k: CryptoTrader._start_monitoring(fake, *a, **k)
    with contextlib.redirect_stdout(io.StringIO()):
        CryptoTrader.execute_signal(fake, FIX.FakeSignal())
        time.sleep(1.0)

    marker_kept = FIX.BATCH in fake._active_monitors
    owner_kept = fake._active_monitor_generations.get(FIX.BATCH) == 'gen_replacement'
    gate_empty = (FIX.BATCH not in fake._unresolved_intent_batches
                  and FIX.BATCH not in fake._poll_degraded_batches)
    no_takeover_crit = not any('初始化阶段异常退出' in t for t in _criticals(fake))

    report(
        "R5-1 代次被替换：新代次的活跃 marker 不得被旧代次误删"
        "（修前无条件 discard → 新代次监控标记丢失）",
        marker_kept and owner_kept,
        f"marker 在场={marker_kept}；owner={fake._active_monitor_generations.get(FIX.BATCH)!r}")
    report(
        "R5-2 代次被替换：不得给新代次置未决/降级闸门"
        "（修前无条件置闸 → 健康新代次被误伤）",
        gate_empty,
        f"unresolved={sorted(fake._unresolved_intent_batches)}；"
        f"degraded={sorted(fake._poll_degraded_batches)}")
    report(
        "R5-3 代次被替换：不向新代次发『无人接管』critical（责任归新代次）",
        no_takeover_crit,
        f"critical={_criticals(fake)}")


# --------------------------------------------------------------------------
# 健康阳性对照：正常成功路径与修前完全一致
# --------------------------------------------------------------------------

def check_c1_happy_path_unchanged():
    fake = FIX.make_fake()
    ret, out, threads = _run(fake)
    report(
        "C1-1 正常成功：batch_id + 恰 1 个监控线程 + 3 层 ENTRY",
        ret == FIX.BATCH
        and len(_monitor_threads(threads)) == 1 and threads[0].started
        and fake._create_n == 3
        and len(_batch(fake).get('entry_orders', [])) == 3,
        f"ret={ret!r}；监控={len(_monitor_threads(threads))} started="
        f"{[t.started for t in threads]}；create={fake._create_n}")
    report(
        "C1-2 正常成功：无 [Q3] 警告、无 critical（健康路径零回归）",
        '[Q3]' not in out and _criticals(fake) == [],
        f"[Q3]={('[Q3]' in out)}；critical={len(_criticals(fake))}")


# --------------------------------------------------------------------------
# 结构锚点：接管紧邻 Commit 的顺序 + 返回值接管在场
# --------------------------------------------------------------------------

def check_s1_source_anchors():
    with open(os.path.join(ROOT, 'trader_260725.py'), encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    seg = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'execute_signal':
            seg = ast.get_source_segment(src, node)
            break
    assert seg, 'execute_signal 源码段未找到'
    i_start = seg.find('monitor_thread.start()')
    i_sync = seg.find('load_time_difference')
    ok = (i_start >= 0 and i_sync >= 0 and i_start < i_sync
          and '_commit_ok' in seg
          and "监控线程启动失败" in seg)
    report(
        "S1 结构：监控启动先于时间同步 + _commit_ok 接管 + 启动失败告警在场",
        ok,
        f"monitor_start@{i_start} time_sync@{i_sync}（须 ≥0 且 start<sync）；"
        f"_commit_ok={('_commit_ok' in seg)} 启动失败告警={('监控线程启动失败' in seg)}")


CHECKS = [
    check_r1_timesync_fail_keeps_takeover,
    check_r2_commit_return_checked,
    check_r3_monitor_start_fail_alerts,
    check_r4_monitor_init_crash_alerted_cleaned,
    check_r5_gen_ownership_on_init_crash,
    check_c1_happy_path_unchanged,
    check_s1_source_anchors,
]


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f"测试自身异常: {type(e).__name__}: {e}")
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"Q3 建仓接管时序测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if failed == 0 and ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
