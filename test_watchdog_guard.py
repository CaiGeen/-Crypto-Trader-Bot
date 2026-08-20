#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_watchdog_guard.py — Watchdog 安全补丁 v1 专项测试（D-004）
==============================================================
背景（2026-08-20 23:28 双 watchdog 演练事故）：
  第二 watchdog 启动 -> bot_runner 撞单实例互斥体 -> 拒绝路径 print("❌ ...")
  在 GBK 管道下抛 UnicodeEncodeError -> 退出码 42 变 1 -> watchdog 误判崩溃
  -> 无限重启循环（约 8s/轮）-> crash_alert 通知风暴（TG + 邮件刷屏）。

修复三件套：
  R1 make_stdout_crash_safe()   入口级编码保护（errors='replace'）
  R2 record_process_exit()      启动熔断（60s 窗口 x 5 次 -> 停止重启 + critical）
  R3 crash_alert_allowed()      crash_alert 同因去重（10min 窗口，稳定运行解除）

场景：
  W1: R1 编码保护——真 GBK TextIOWrapper 再现 D-004 崩溃并验证修复（含 None/StringIO 兼容）
  W2: R3 去重语义——首条放行/同因静默/异因放行/窗口过期/恢复解除
  W3: R2 熔断计数——4 次不熔断/第 5 次熔断/稳定运行清零/清零同时解除 R3
  W4: 源码接入校验——两文件入口调用先于首个 emoji 字面量、主循环熔断/去重接线

运行：.venv/Scripts/python.exe test_watchdog_guard.py
"""

import ast
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watchdog as wd

RESULTS = []


def report(name, ok, detail=''):
    RESULTS.append((name, bool(ok)))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  {detail}" if detail else ""))


# ============================================================
# W1: R1 编码保护（真 GBK 流再现 D-004 崩溃 + 修复验证）
# ============================================================
def scenario_w1_encoding():
    print("\n--- W1: R1 编码保护（GBK 管道再现 D-004） ---")

    # W1a: 未保护的 GBK 流写 emoji 必崩（D-004 根因再现）
    # 模拟子进程 PIPE：Python 对管道流用本地 ANSI 代码页（本机 cp936）
    raw = io.BytesIO()
    gbk_stream = io.TextIOWrapper(raw, encoding='gbk')
    crashed = False
    try:
        gbk_stream.write('❌ 检测到另一个 Bot 实例，正在运行中（单实例锁已存在），拒绝启动。\n')
    except UnicodeEncodeError:
        crashed = True
    report('W1a/未保护GBK流写emoji必崩(D-004再现)', crashed,
           '(UnicodeEncodeError 已按预期抛出)')

    # W1b: reconfigure(errors='replace') 后同一流写入不崩
    raw2 = io.BytesIO()
    safe_stream = io.TextIOWrapper(raw2, encoding='gbk')
    ok_fix = False
    try:
        if hasattr(safe_stream, 'reconfigure'):
            safe_stream.reconfigure(errors='replace')
        safe_stream.write('❌ 检测到另一个 Bot 实例，拒绝启动。\n')
        safe_stream.flush()
        ok_fix = True
    except UnicodeEncodeError:
        ok_fix = False
    report('W1b/reconfigure后同一GBK流写入不崩', ok_fix)

    # W1c: make_stdout_crash_safe() 对 GBK sys.stdout 整体生效（直接调被测函数）
    ok_w1c = False
    raw3 = io.BytesIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.TextIOWrapper(raw3, encoding='gbk')
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='gbk')
        wd.make_stdout_crash_safe()
        sys.stdout.write('🛡️🚨❌✅🔥 混合 emoji 输出\n')
        sys.stdout.flush()
        ok_w1c = True
    except UnicodeEncodeError:
        ok_w1c = False
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    report('W1c/make_stdout_crash_safe使GBK流安全', ok_w1c)

    # W1d: 无 reconfigure 属性的流（StringIO 等）不抛异常（hasattr 保护）
    ok_w1d = False
    old_out = sys.stdout
    try:
        sys.stdout = io.StringIO()  # 无 reconfigure 属性
        wd.make_stdout_crash_safe()
        ok_w1d = True
    except Exception:
        ok_w1d = False
    finally:
        sys.stdout = old_out
    report('W1d/无reconfigure属性流兼容不崩', ok_w1d)

    # W1e: stdout/stderr = None（pythonw 无控制台场景）不崩
    ok_w1e = False
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = None, None
        wd.make_stdout_crash_safe()
        ok_w1e = True
    except Exception:
        ok_w1e = False
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    report('W1e/stdout为None兼容不崩', ok_w1e)


# ============================================================
# W2: R3 crash_alert 同因去重语义
# ============================================================
def scenario_w2_dedup():
    print("\n--- W2: R3 crash_alert 同因去重 ---")
    wd._crash_alert_last_sent.clear()

    reason = '⚠️ 程序异常退出 (退出码: 1)'  # D-004 风暴中的真实 reason 字符串
    report('W2a/首次告警放行', wd.crash_alert_allowed(reason) is True)
    report('W2b/同因窗口内静默', wd.crash_alert_allowed(reason) is False)
    report('W2b2/同因再次静默', wd.crash_alert_allowed(reason) is False)
    report('W2c/异因(退出码不同)放行',
           wd.crash_alert_allowed('⚠️ 程序异常退出 (退出码: 2)') is True)

    # 窗口过期后重新放行
    wd._crash_alert_last_sent[reason] = (
        time.monotonic() - (wd.CRASH_ALERT_DEDUP_WINDOW + 1))
    report('W2d/窗口过期后重新放行', wd.crash_alert_allowed(reason) is True)

    # 恢复解除（稳定运行后 reset）
    wd.crash_alert_reset()
    report('W2e/reset后同因重新放行', wd.crash_alert_allowed(reason) is True)


# ============================================================
# W3: R2 启动熔断计数语义
# ============================================================
def scenario_w3_breaker():
    print("\n--- W3: R2 启动熔断（60s 窗口 x 5 次） ---")
    wd._init_fail_count = 0

    # D-004 风暴推演：若无 R2，每轮 ~8s 无限重启；有 R2，风暴止于第 5 次退出
    trips = [wd.record_process_exit(5.0) for _ in range(4)]  # 每轮 uptime≈5s（初始化窗口内）
    report('W3a/连续4次窗口内退出不熔断',
           (not any(trips)) and wd._init_fail_count == 4,
           f'(count={wd._init_fail_count}/5)')

    report('W3b/第5次窗口内退出触发熔断',
           wd.record_process_exit(5.0) is True,
           f'(count={wd._init_fail_count}/5)')

    # 稳定运行（如 2 小时后崩溃）-> 计数清零，不误杀长运行后的正常崩溃重启
    wd._init_fail_count = 0
    for _ in range(4):
        wd.record_process_exit(5.0)
    report('W3c/稳定运行清零计数',
           wd.record_process_exit(7200.0) is False and wd._init_fail_count == 0)

    # 清零后再 1 次失败不熔断（连续性语义正确）
    report('W3d/清零后再1次失败不熔断',
           wd.record_process_exit(5.0) is False and wd._init_fail_count == 1)

    # 稳定运行同时解除 R3 去重（恢复语义：下次崩溃重新提醒）
    wd._init_fail_count = 0
    for _ in range(4):
        wd.record_process_exit(5.0)
    wd._crash_alert_last_sent['x'] = time.monotonic()
    wd.record_process_exit(7200.0)
    report('W3e/稳定运行解除R3去重', len(wd._crash_alert_last_sent) == 0)

    wd._init_fail_count = 0
    wd._crash_alert_last_sent.clear()


# ============================================================
# W4: 源码接入校验（AST/文本——两文件入口调用先于首个 emoji、主循环接线）
# ============================================================
_EMOJI_CHARS = ['❌', '✅', '🚨', '🔥', '📊', '👀', '📈', '📉', '💰', '🧹',
                '🚀', '💥', '🛡', '⏰', '⏱', '📝', '📧', '📨', '🔇', '🚫',
                '🟢', '🟡', '🔴', '⚠', '😴', '🔄', '👋', '🧯', '🔒', '🆗']


def _first_emoji_pos(src):
    positions = [src.find(ch) for ch in _EMOJI_CHARS if src.find(ch) >= 0]
    return min(positions) if positions else len(src)


def scenario_w4_integration():
    print("\n--- W4: 源码接入校验 ---")
    base = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(base, 'bot_runner.py'), encoding='utf-8') as f:
        src_br = f.read()
    with open(os.path.join(base, 'watchdog.py'), encoding='utf-8') as f:
        src_wd = f.read()

    # bot_runner：定义 + 模块级调用 + 调用先于首个 emoji 字面量（保证覆盖所有输出路径）
    def_pos = src_br.find('def make_stdout_crash_safe')
    call_pos = src_br.find('make_stdout_crash_safe()', def_pos + 10)
    first_emoji = _first_emoji_pos(src_br)
    report('W4a/bot_runner定义并模块级调用编码保护',
           def_pos > 0 and call_pos > def_pos,
           f'(def@{def_pos}, call@{call_pos})')
    report('W4b/bot_runner调用先于首个emoji字面量',
           0 < call_pos < first_emoji,
           f'(call@{call_pos} < emoji@{first_emoji})')

    # watchdog：定义 + main() 首行调用
    main_pos = src_wd.find('def main():')
    main_body = src_wd[main_pos:main_pos + 400]
    report('W4c/watchdog定义并在main()入口调用编码保护',
           'def make_stdout_crash_safe' in src_wd
           and 'make_stdout_crash_safe()' in main_body)

    # watchdog：R2 熔断接入主循环（uptime 判定 + 熔断退出）
    report('W4d/watchdog熔断接入主循环',
           'record_process_exit(time.monotonic() - proc_start)' in src_wd
           and 'proc_start = time.monotonic()' in src_wd)

    # watchdog：R3 去重接入 is_crash 分支
    report('W4e/watchdog告警去重接入is_crash分支',
           'if crash_alert_allowed(restart_reason):' in src_wd)

    # watchdog AST 仍满足 test_orphan_guard 场景4 前置（42 识别不变）
    tree = ast.parse(src_wd)
    ok_kill = any(isinstance(n, ast.FunctionDef) and n.name == '_kill_main_process_tree'
                  for n in ast.walk(tree))
    report('W4f/orphan_guard前置仍满足(kill函数+42识别)',
           ok_kill and '42' in src_wd and 'returncode' in src_wd)


if __name__ == '__main__':
    print("#" * 60)
    print("Watchdog 安全补丁 v1 专项测试（D-004：重复启动风暴）")
    print("R1 编码保护 / R2 启动熔断 / R3 告警去重")
    print("#" * 60)
    scenario_w1_encoding()
    scenario_w2_dedup()
    scenario_w3_breaker()
    scenario_w4_integration()

    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    print(f"结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        print("失败场景:")
        for n in failed:
            print(f"  ❌ {n}")
        sys.exit(1)
    print("全部通过 ✅")
