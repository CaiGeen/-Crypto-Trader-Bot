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

import os
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
        try:
            proc = subprocess.run(
                [sys.executable, name], cwd=ROOT, env=_base_env(),
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=timeout)
            rc = proc.returncode
            err = ''
        except subprocess.TimeoutExpired:
            rc, err = -9, '超时'

        allowed = EXPECTED.get(name, {0})
        if rc == 0:
            status, fatal = 'PASS', False
        elif name in BASELINE_FAIL and rc in allowed:
            status, fatal = 'BASELINE-FAIL', False
        elif name in STOP_WINDOW and rc in allowed:
            status, fatal = 'NOT-VERIFIED', strict
        elif rc in allowed:
            status, fatal = 'PASS', False
        else:
            status, fatal = 'FAIL', True
        rows.append((name, rc, status, time.time() - t0, err))
        mark = {'PASS': '  ', 'BASELINE-FAIL': '!', 'NOT-VERIFIED': '?',
                'FAIL': 'X'}[status]
        note = {'PASS': '', 'BASELINE-FAIL': '（基线即 3/9，逐项与 f0a21d2 一致）',
                'NOT-VERIFIED': '（Bot 持互斥体，需停机窗口补验）',
                'FAIL': '（退出码超预期 → 真回归）'}[status]
        extra = f' {err}' if err else ''
        print(f'{mark} {status:<14} rc={rc:<4} {time.time() - t0:6.1f}s '
              f'{name}{extra} {note}')

    n_pass = sum(1 for r in rows if r[2] == 'PASS')
    n_base = sum(1 for r in rows if r[2] == 'BASELINE-FAIL')
    n_nv = sum(1 for r in rows if r[2] == 'NOT-VERIFIED')
    n_fail = sum(1 for r in rows if r[2] == 'FAIL')
    n_fatal = sum(1 for r in rows if r[2] == 'FAIL' or
                  (r[2] == 'NOT-VERIFIED' and strict))
    print('-' * 70)
    print(f'脚本式测试 {len(rows)} 项：'
          f'PASS {n_pass} | BASELINE-FAIL {n_base} '
          f'| NOT-VERIFIED {n_nv} | FAIL {n_fail}'
          + ('（--strict：NOT-VERIFIED 一并判失败）' if strict else ''))
    print('⚠ 口径提醒：PASS+BASELINE-FAIL 不等于"全部退出码 0"；'
          'NOT-VERIFIED 未取得任何运行证据，不得计入通过。')
    if n_nv:
        for r in rows:
            if r[2] == 'NOT-VERIFIED':
                print(f'  → 待补验：{r[0]}（停机窗口内 `python {r[0]}`，'
                      f'期望 rc=0，5 个场景全过）')
    print()
    return n_fatal == 0, rows


def main():
    _configure_stdio()
    argv = set(sys.argv[1:])
    do_all = not (argv & {'--scripts', '--pytest'})
    strict = '--strict' in argv

    overall = True

    if do_all or '--pytest' in argv:
        ok, _ = run_pytest()
        overall = overall and ok
        print()

    if do_all or '--scripts' in argv:
        ok, _ = run_scripts(strict=strict)
        overall = overall and ok

    print('=' * 70)
    print(f'门禁结论：{"PASS" if overall else "FAIL"}'
          f'{"（strict）" if strict else ""}')
    print('=' * 70)
    return 0 if overall else 1


if __name__ == '__main__':
    raise SystemExit(main())
