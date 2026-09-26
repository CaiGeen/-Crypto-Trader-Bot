#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量测试门禁运行器（ChatGPT 第十五轮复审提出：门禁结论必须可独立复现）。

## 为什么要有这个文件

第十四轮我用临时 PowerShell 循环跑根目录 49 个脚本，然后在报告里写
"脚本式全量 49/49 OK"，同时又承认 `test_v64_p3_lifecycle.py` 是 3/9。
这两句话互相矛盾：该脚本 `main()` 是 `return 0 if passed == len(tests) else 1`，
6 项失败必然退出码 1，根本不可能属于"49/49 退出码 0"。
循环里的计数变量只统计了"脚本是否抛异常"，没区分 `exit 1`，于是我把
"49 个都跑起来了"误报成了"49 个都过了"。

临时脚本还留在 shell 历史里、没进仓库 → 复审方无法独立核验。
本文件把两件事一次性钉死：
  1. 每个文件**退出码是多少**，逐行打印，不做任何隐式归并；
  2. 非 0 退出码必须在 EXPECTED 里登记并写明基线依据，否则判 FAIL。

## 输出口径（禁止再出现"全绿"这种含糊说法）

  PASS         退出码 0
  BASELINE-FAIL 退出码 1，且已登记为"基线即如此"（有基线实测支撑）
  NOT-VERIFIED 退出码 42，Bot 运行中持互斥体所致，需停机窗口补验
  FAIL         退出码不在预期集合内 —— 真回归

  汇总行同时给出 4 个计数。"PASS 48 / FAIL 0" 不等于"全绿"，
  NOT-VERIFIED 必须单独列出来。

## 用法

  python run_test_gate.py              # 完整门禁（含 pytest 收集集 + 49 个脚本）
  python run_test_gate.py --strict     # NOT-VERIFIED 也判为失败（停机窗口内用）
  python run_test_gate.py --scripts    # 只跑脚本式测试
  python run_test_gate.py --pytest     # 只跑 pytest 收集集
"""

import hashlib
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

# 根目录 pytest 忽略列表：策略回测/单测驱动脚本/归档/送审附件不是被测对象。
# ⚠ `--ignore` 不是通配符而是**路径前缀**（实测 `--ignore=送审附件_*` 无效，
# 仍报 4 个 collection error 并中断整轮），所以送审附件目录必须运行时枚举展开。
PYTEST_STATIC_IGNORES = [
    'strategies_backtest',
    'trader_test.py',
    'tests_archive',
]


def build_pytest_args():
    ignores = list(PYTEST_STATIC_IGNORES)
    try:
        ignores += sorted(
            d for d in os.listdir(ROOT)
            if d.startswith('送审附件_') and os.path.isdir(os.path.join(ROOT, d)))
    except OSError:
        pass
    args = ['-m', 'pytest']
    for d in ignores:
        args += ['--ignore=' + d]
    args.append('-q')
    return args

# ---------------------------------------------------------------- 预期退出码
# 默认 {0}。非 0 必须显式登记 + 写明依据，否则判 FAIL。
EXPECTED = {
    # 基线即 3/9：f0a21d2 与 9d9526c 逐项 marker 一致（r5c 两边均通过），
    # 6 项既有失败（线程时序 / AttributeError: 'function' object has no
    # attribute 'get'），属独立批次排查对象，不属通知体系范围。
    'test_v64_p3_lifecycle.py': {1},
    # 该文件开局即 acquire_instance_lock()（bot_runner.py:566），
    # Bot 运行中必然 rc=42（互斥体被持有，Fail-Closed 设计使然）；
    # 0 只在停机窗口可能。42 **不算已验证**。
    'test_orphan_guard.py': {0, 42},
}

BASELINE_FAIL = {'test_v64_p3_lifecycle.py'}
STOP_WINDOW = {'test_orphan_guard.py'}

# 仅比对退出码是**不够的**：p3 恒返回 1，失败项从 6 个涨到 8 个照样被接受。
# 这里额外登记它应打印的 `GREEN: n/m` 行，通过数一变就判 BASELINE-DRIFT（致命）——
# 变好说明基线该摘除，变坏说明真回归，两个方向都必须暴露。
BASELINE_GREEN = {
    'test_v64_p3_lifecycle.py': (3, 9),
}
GREEN_RE = re.compile(r'GREEN:\s*(\d+)\s*/\s*(\d+)')

# ------------------------------------------------- 生产哨兵（测试隔离，第十六轮 P3）
# ChatGPT 指出门禁无条件直跑根目录 test_*.py，与生产 Bot 无隔离。
# 进程检测只是**代理**，真正要守的不变量是"测试没有改动生产状态"，
# 所以这里直接对文件内容做指纹前后比对——变了就判 FAIL，与测试结果无关。
#
# 选哨兵的口径是「真实存在 + 正常情况下整轮门禁期间不被写」，实测（2026-09-26）：
#   trade_state.json         仅批次状态变化时写 → 稳定
#   .daily_report.state.json 仅 08:05~08:30 重试窗写 → 稳定（该窗口内跑门禁会误报，
#                            属"生产正常写入"，见 state_bad 的中性措辞）
#   .notify.state.json       生产每 ~10s 重写 → 不可用作哨兵
#   .bot_health/control.json 生产每 10s 原子替换   → 同上
#   .patrol_alert.state.json 每轮巡检写           → 同上
#   .notify_queue/           **目录根本不存在**（D-010 已迁移），首版哨兵盯了个空路径
#   .heartbeat.json          每 60s              → 同上
PRODUCTION_SENTINELS = ['trade_state.json', '.daily_report.state.json']
PATROL_LOG = os.path.join('logs', 'patrol.log')
# 测试输出顶着真实告警字样的行（这些行曾被写进生产 patrol.log）
PATROL_TEST_MARKERS = (
    '邮件告警已发送',
    '邮件已跳过（disabled）',
    '邮件已跳过（no_active_positions）',
)


def _live_bot_pids():
    """检测是否有 bot_runner/watchdog 在运行（仅用于提示，不作为安全边界）。"""
    if os.name != 'nt':
        return []
    try:
        import subprocess
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*bot_runner.py*' -or "
             "$_.CommandLine -like '*watchdog.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=25)
        return [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _snapshot():
    """生产哨兵快照：状态文件内容指纹 + patrol.log 中测试标记行数。"""
    snap = {}
    for rel in PRODUCTION_SENTINELS:
        path = os.path.join(ROOT, rel)
        try:
            with open(path, 'rb') as f:
                data = f.read()
            snap[rel] = (len(data), hashlib.sha256(data).hexdigest())
        except OSError:
            snap[rel] = None
    try:
        with open(os.path.join(ROOT, PATROL_LOG), encoding='utf-8',
                  errors='replace') as f:
            text = f.read()
        snap[PATROL_LOG] = sum(text.count(m) for m in PATROL_TEST_MARKERS)
    except OSError:
        snap[PATROL_LOG] = 0
    return snap


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def _base_env():
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def run_pytest(timeout=1800):
    """pytest 收集集（可被 pytest 正常收集的那部分）。"""
    print('=' * 70)
    print('[1/2] pytest 收集集')
    print('=' * 70)
    t0 = time.time()
    args = build_pytest_args()
    print('  cmd: ' + ' '.join(args[1:]))
    try:
        proc = subprocess.run(
            [sys.executable] + args, cwd=ROOT, env=_base_env(),
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        print('  FAIL  pytest 超时')
        return False, 'timeout'
    tail = [ln for ln in (proc.stdout or '').splitlines() if ln.strip()][-6:]
    for ln in tail:
        print('  ' + ln)
    rc = proc.returncode
    ok = (rc == 0)
    print(f'  → pytest exit={rc} 耗时 {time.time() - t0:.1f}s\n')
    return ok, rc


def discover_scripts():
    return sorted(f for f in os.listdir(ROOT)
                  if f.startswith('test_') and f.endswith('.py'))


def run_scripts(strict=False, timeout=600):
    """逐个以 python 直跑根目录 test_*.py —— 这些文件在 pytest 下收集 0 项
    （脚本式，scenario_* 不叫 test_*），只有这样才真正被执行到。"""
    print('=' * 70)
    print('[2/2] 脚本式测试（pytest 收集不到的那部分）')
    print('=' * 70)
    rows = []
    for name in discover_scripts():
        t0 = time.time()
        stdout = ''
        try:
            proc = subprocess.run(
                [sys.executable, name], cwd=ROOT, env=_base_env(),
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=timeout)
            rc, stdout, err = proc.returncode, (proc.stdout or ''), ''
        except subprocess.TimeoutExpired:
            rc, err = -9, '超时'

        allowed = EXPECTED.get(name, {0})
        drift = None
        if name in BASELINE_GREEN and rc in allowed:
            m = GREEN_RE.search(stdout)
            if m:
                got = (int(m.group(1)), int(m.group(2)))
                exp = BASELINE_GREEN[name]
                if got != exp:
                    drift = (f'基线 GREEN 期望 {exp[0]}/{exp[1]}，'
                             f'实际 {got[0]}/{got[1]}')
            else:
                drift = '基线 GREEN 行未找到（脚本输出格式变更，基线登记失效）'

        if drift is not None:
            status, fatal = 'BASELINE-DRIFT', True
        elif rc == 0:
            status, fatal = 'PASS', False
        elif name in BASELINE_FAIL and rc in allowed:
            status, fatal = 'BASELINE-FAIL', False
        elif name in STOP_WINDOW and rc in allowed:
            status, fatal = 'NOT-VERIFIED', strict
        elif rc in allowed:
            status, fatal = 'PASS', False
        else:
            status, fatal = 'FAIL', True
        rows.append((name, rc, status, time.time() - t0, err or drift or ''))
        mark = {'PASS': '  ', 'BASELINE-FAIL': '!', 'NOT-VERIFIED': '?',
                'BASELINE-DRIFT': 'D', 'FAIL': 'X'}[status]
        note = {'PASS': '', 'BASELINE-FAIL': '（已登记基线，非本轮引入）',
                'NOT-VERIFIED': '（Bot 持互斥体，需停机窗口补验）',
                'BASELINE-DRIFT': '（失败项数量/组成变了 → 基线失效，必须处理）',
                'FAIL': '（退出码超预期 → 真回归）'}[status]
        extra = f' {err}' if err else ''
        extra = extra or (f' [{drift}]' if drift else '')
        print(f'{mark} {status:<14} rc={rc:<4} {time.time() - t0:6.1f}s '
              f'{name}{extra} {note}')

    counts = {s: sum(1 for r in rows if r[2] == s) for s in
              ('PASS', 'BASELINE-FAIL', 'NOT-VERIFIED', 'BASELINE-DRIFT', 'FAIL')}
    n_fatal = (counts['FAIL'] + counts['BASELINE-DRIFT']
               + (counts['NOT-VERIFIED'] if strict else 0))
    print('-' * 70)
    print(f'脚本式测试 {len(rows)} 项：'
          f'PASS {counts["PASS"]} | BASELINE-FAIL {counts["BASELINE-FAIL"]} '
          f'| NOT-VERIFIED {counts["NOT-VERIFIED"]} '
          f'| BASELINE-DRIFT {counts["BASELINE-DRIFT"]} '
          f'| FAIL {counts["FAIL"]}'
          + ('（--strict：NOT-VERIFIED 一并判失败）' if strict else ''))
    green = (not counts['FAIL'] and not counts['BASELINE-DRIFT']
             and not counts['NOT-VERIFIED'] and not counts['BASELINE-FAIL'])
    if green:
        print('✔ 脚本段全绿（50/50 退出码 0，无任何待验证项）')
    else:
        print('✘ 脚本段**未全绿**：以下各项不计入通过 —— '
              + '、'.join(
                  f'{k}={counts[k]}' for k in
                  ('BASELINE-FAIL', 'NOT-VERIFIED', 'BASELINE-DRIFT', 'FAIL')
                  if counts[k]))
    if counts['NOT-VERIFIED']:
        for r in rows:
            if r[2] == 'NOT-VERIFIED':
                print(f'  → 待补验：{r[0]}（停机窗口内 `python {r[0]}`，'
                      f'期望 rc=0，5 个场景全过）')
    print()
    return n_fatal == 0, counts


def main():
    _configure_stdio()
    argv = set(sys.argv[1:])
    do_all = not (argv & {'--scripts', '--pytest'})
    strict = '--strict' in argv

    live = _live_bot_pids()
    before = _snapshot()
    if live:
        print(f'⚠ 检测到生产 Bot 运行中（pid={live}）。进程检测只是提示，'
              f'真正生效的是下面的**生产哨兵前后指纹比对** —— 改动即 FAIL，'
              f'与测试是否通过无关。')
    print()

    overall = True
    counts = {}

    if do_all or '--pytest' in argv:
        ok, _ = run_pytest()
        overall = overall and ok
        print()

    if do_all or '--scripts' in argv:
        ok, counts = run_scripts(strict=strict)
        overall = overall and ok

    # -------- 生产哨兵（第十六轮 P3）：测试隔离的结构性保证 --------
    after = _snapshot()
    state_bad = [f'{k}: {before[k]} -> {after.get(k)}'
                 for k in PRODUCTION_SENTINELS
                 if after.get(k) != before.get(k)]
    log_bad = ([f'{PATROL_LOG} 测试标记行 {before[PATROL_LOG]} -> '
                f'{after.get(PATROL_LOG)}']
               if after.get(PATROL_LOG) != before.get(PATROL_LOG) else [])
    sentinel_bad = state_bad + log_bad

    print('=' * 70)
    exceptions = counts and (counts.get('BASELINE-FAIL')
                             or counts.get('NOT-VERIFIED')
                             or counts.get('BASELINE-DRIFT'))
    if not overall or sentinel_bad:
        verdict = 'FAIL'
    elif exceptions:
        verdict = ('COMPLETED-WITH-EXCEPTIONS —— 运行完成，'
                   '但**不是全绿**（存在下述未通过项）')
    else:
        verdict = 'ALL-GREEN —— 全部退出码 0，无待验证项'
    print(f'门禁结论：{verdict}{"（strict）" if strict else ""}')
    if state_bad:
        print('✘ 运行期间生产状态文件发生变化（与测试通过与否无关；'
              '需人工甄别是**测试污染**、生产正常写入还是磁盘问题）：')
        for s in state_bad:
            print('   - ' + s)
    if log_bad:
        print('✘ 运行期间出现与真实告警同文的日志行，必须人工甄别是'
              '测试污染还是真事件：')
        for s in log_bad:
            print('   - ' + s)
    if not sentinel_bad:
        print(f'✔ 生产哨兵未被改动：{", ".join(before.keys())}')
    print('=' * 70)
    if sentinel_bad:
        return 1
    return 0 if overall else 1


if __name__ == '__main__':
    raise SystemExit(main())
