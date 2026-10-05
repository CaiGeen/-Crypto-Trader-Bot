#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R6（独立复审）：test_orphan_guard 备份失败时必须中止全部场景。

旧实现：备份读取失败仅打印，scenario_1 仍 os.remove 锁原件，finally 因
_LOCK_BACKUP_TAKEN=False 不恢复 → 原文件丢失。
修后：main() 里备份失败即 sys.exit(2)，scenario_* 均不执行。

跑法：.venv\\Scripts\\python.exe test_r6_orphan_backup_abort.py（rc=0 全过）
"""
import io
import os
import sys
import tempfile
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\n        {detail}")


def check_backup_failure_aborts():
    import test_orphan_guard as og

    # 构造失败备份：备份文件不可读（PermissionError 模拟）
    tmp = tempfile.mkdtemp(prefix="q8r6_")
    lock_p = os.path.join(tmp, "lock")
    with open(lock_p, "w", encoding="utf-8") as f:
        f.write("12345")
    og._LOCK_BACKUP = None
    og._LOCK_BACKUP_TAKEN = False
    called = []
    orig_open = open

    def _open_fail(path, *a, **k):
        if os.path.abspath(path) == os.path.abspath(lock_p):
            raise PermissionError("simulated")
        return orig_open(path, *a, **k)

    import builtins
    og_open = builtins.open
    builtins.open = _open_fail
    orig_lock = og.LOCK
    og.LOCK = lock_p
    # scenario_* 不应被执行——用包装器探针
    orig_s1 = og.scenario_1
    og.scenario_1 = lambda: called.append('s1')
    exit_codes = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                og.main()
            except SystemExit as e:
                exit_codes.append(e.code)
    finally:
        builtins.open = og_open
        og.LOCK = orig_lock
        og.scenario_1 = orig_s1
    report(
        "R6-1 备份失败 → main 以退出码 2 中止",
        exit_codes == [2],
        f"exit_codes={exit_codes}")
    report(
        "R6-2 备份失败 → 任何场景都不执行",
        called == [],
        f"called={called}")
    report(
        "R6-3 备份失败 → 锁原件未被删除",
        os.path.exists(lock_p),
        f"exists={os.path.exists(lock_p)}")


def main():
    check_backup_failure_aborts()
    ok = sum(1 for _, p in RESULTS if p)
    print("\n" + "=" * 68)
    print(f"R6 orphan 备份失败中止测试: {ok}/{len(RESULTS)} 通过")
    print("=" * 68)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
