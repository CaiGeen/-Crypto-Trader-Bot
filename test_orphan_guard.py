# -*- coding: utf-8 -*-
"""
P0-2 v2 单实例锁（命名互斥体版）+ watchdog 拒绝退出码处理 离线验收测试

v1 文件锁缺陷（12:04 实测暴露）：
  - taskkill /F 强杀不触发 atexit → 每次强停残留死锁文件
  - 死 PID 在重启窗口被系统复用 → OpenProcess 误判"存活" → 拒绝启动
  - watchdog 把锁拒绝(退出码1)当崩溃 → 3 秒无限重启循环

v2 设计：
  - Windows 命名互斥体为权威判据（内核对象，进程死亡自动释放，无 PID 复用问题）
  - 锁文件降级为纯诊断（写 PID 供排查，永不阻断启动）
  - 拒绝启动用专用退出码 42；watchdog 识别后停止而非崩溃重启

场景：
  1: 首次获取 → 成功，诊断锁文件写入自身 PID
  2: 互斥体已被持有（同进程第二次调用模拟第二实例）→ SystemExit 且退出码 == 42
  3: 诊断锁文件内容（含死 PID）不影响判定 → 仍因互斥体拒绝（锁文件永不阻断）
  4: watchdog AST——停止杀进程树(taskkill /T) + 识别退出码 42 不进崩溃重启

用法: .venv\\Scripts\\python.exe test_orphan_guard.py
"""
import ast
import os
import sys

import bot_runner

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


LOCK = bot_runner.LOCK_FILE


def write_lock(pid_text):
    with open(LOCK, 'w') as f:
        f.write(pid_text)


def read_lock():
    with open(LOCK, 'r') as f:
        return f.read().strip()


def refusal_code(fn):
    """调用 fn，返回 SystemExit 的退出码（未抛出则返回 None）"""
    try:
        fn()
        return None
    except SystemExit as e:
        return e.code


def scenario_1():
    if os.path.exists(LOCK):
        os.remove(LOCK)
    bot_runner.acquire_instance_lock()   # 本测试进程持有互斥体
    ok = os.path.exists(LOCK) and read_lock() == str(os.getpid())
    report("场景1: 首次获取成功+诊断锁写入 PID", ok,
           f"(锁内容: {read_lock() if os.path.exists(LOCK) else '缺失'})")


def scenario_2():
    code = refusal_code(bot_runner.acquire_instance_lock)  # 互斥体已被场景1持有
    ok = code == 42
    report("场景2: 第二实例 -> SystemExit(42)", ok, f"(退出码: {code})")


def scenario_3():
    write_lock('99999999')   # 死 PID 诊断残留
    code = refusal_code(bot_runner.acquire_instance_lock)
    ok = code == 42          # 拒绝必须来自互斥体而非锁文件
    report("场景3: 锁文件内容不影响判定(互斥体权威)", ok, f"(退出码: {code})")


def scenario_4():
    with open('watchdog.py', 'r', encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)

    kill_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_kill_main_process_tree':
            kill_fn = node
    tree_kill = False
    if kill_fn is not None:
        for node in ast.walk(kill_fn):
            if isinstance(node, ast.Constant) and str(node.value) == '/T':
                tree_kill = True

    handler_calls = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            t = node.type
            if isinstance(t, ast.Name) and t.id == 'KeyboardInterrupt':
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                            and sub.func.id == '_kill_main_process_tree'):
                        handler_calls = True

    # 退出码 42 识别（不进崩溃重启循环）
    refuses_42 = '42' in src and 'returncode' in src

    ok = kill_fn is not None and tree_kill and handler_calls and refuses_42
    report("场景4: watchdog 杀进程树+识别拒绝码42", ok,
           f"(清理函数: {kill_fn is not None}, 含/T: {tree_kill}, "
           f"处理器调用: {handler_calls}, 识别42: {refuses_42})")


def scenario_5():
    """5: 真实跨进程第二实例（子进程）→ 退出码 42
    此前的同进程测试恰好绕过了 ctypes GetLastError 的可靠性问题（15:27 实测漏判根因）"""
    import subprocess
    r = subprocess.run(
        [sys.executable, '-c', 'import bot_runner; bot_runner.acquire_instance_lock()'],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=30)
    ok = r.returncode == 42
    report("场景5: 跨进程第二实例 -> 退出码42", ok,
           f"(returncode: {r.returncode})")


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    scenario_5()
    if os.path.exists(LOCK):
        os.remove(LOCK)
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("P0-2v2 单实例锁验收完成")
