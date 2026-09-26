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

  PASS           退出码 0
  BASELINE-FAIL  退出码 1，且已登记为"基线即如此"（有基线实测支撑）
  NOT-VERIFIED   退出码 42，Bot 运行中持互斥体所致，需停机窗口补验
  BASELINE-DRIFT 退出码在预期集合内，但**失败项组成**与登记基线不符 —— 必须处理
  FAIL           退出码不在预期集合内 —— 真回归

  汇总行同时给出 5 个计数。"PASS 48 / FAIL 0" 不等于"全绿"，
  BASELINE-FAIL 与 NOT-VERIFIED 必须单独列出来。

## 进程退出码（第十七轮 ChatGPT P1：不能只靠文字，退出码也必须能区分）

  0 = ALL-GREEN                    全部退出码 0，无任何待验证项
  1 = FAIL                         真回归 / 基线漂移 / 生产哨兵变化 / --strict 下未验证
  2 = COMPLETED-WITH-EXCEPTIONS    运行完成，仅含**已登记**的基线失败与待验证项，无回归
  3 = REFUSED                      生产 Bot 运行中**或探测不确定**且未给 --allow-live，
                                   本次**未执行任何测试**

  这样"只看退出码"的发布脚本把 0 当成功、把 1 当失败、把 2 当"需要人确认"、
  把 3 当"根本没跑"，不再出现"文字说未全绿、退出码却是 0"的歧义。

## 用法

  python run_test_gate.py                  # 完整门禁（Bot 运行中、或探测不确定 → 拒绝执行）
  python run_test_gate.py --allow-live     # 明确接受"测试与生产并存"风险后照常跑
  python run_test_gate.py --strict         # NOT-VERIFIED 也判为失败（停机窗口内用）
  python run_test_gate.py --scripts        # 只跑脚本式测试
  python run_test_gate.py --pytest         # 只跑 pytest 收集集

## 这个门禁**不是**隔离（第十七轮 P3，收窄上一轮的失实表述）

  生产哨兵是**事后检测**：跑完后比对两个状态文件的内容指纹与巡检日志里的测试标记行数。
  它**不是**隔离，已知边界：
    - 判 FAIL 时文件写入或外发**已经发生**了，它只是让你知道；
    - 未列入哨兵清单的文件不在检测范围；
    - 先改后改回原内容的短暂写入检测不到；
    - 测试直接外发（不落任何状态文件）完全检测不到。
  真正的隔离是"临时状态目录 + 独立外发通道"，见 UPGRADE_TRIGGER。
"""

import hashlib
import json
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
# 仅比对 `GREEN: n/m` 的**数量**也不够（第十七轮 ChatGPT P2 指出）：
#   原本通过的 3 项里坏 1 项、原本失败的另 1 项恰好修好 → 仍是 3/9，数量看不出来。
# 因此基线按**逐项失败身份**登记，任何一项的通过/失败状态改变都判 BASELINE-DRIFT。
BASELINE_GREEN = {
    'test_v64_p3_lifecycle.py': (3, 9),
}
GREEN_RE = re.compile(r'GREEN:\s*(\d+)\s*/\s*(\d+)')

# 逐项失败身份基线。
# 基线依据：2026-09-26 在 `5d3ab5d`（与 `f0a21d2`/`9d9526c` 逐项一致）上实测
#   `python test_v64_p3_lifecycle.py` → rc=1、`GREEN: 3/9`、下面 6 项打印
#   `❌ <name>: <reason>`，其余 3 项打印 `✅ <name>`。
# 改动这个集合必须同时更新基线依据，否则视为未经验证的放宽。
BASELINE_FAIL_SET = {
    'test_v64_p3_lifecycle.py': frozenset({
        'r1_clear_during_sleep_zero_side_effect',
        'r3_zombie_no_protection_repair',
        'r4_corrupted_is_not_empty',
        'r5_settlement_report_exactly_once',
        'r7_settlement_uses_net_qty_not_gross',
        'r6_normal_batch_unchanged',
    }),
}
# 逐行形态：`✅ <name>`（通过）/ `❌ <name>: <reason>`（失败）。
# 直接锚定 ✅/❌ 两个标记字符，`GREEN: n/m` 汇总行因此天然不会被误判成用例行。
# `_base_env()` 已设 PYTHONIOENCODING=utf-8，且本文件以 utf-8 解码 stdout，
# 故标记字符可原样匹配（2026-09-26 实测字节无损）。
CASE_LINE_RE = re.compile(r'^\s*([✅❌])\s+(\w+)(:\s.*)?$')

# ------------------------------------------------- 生产哨兵（**事后检测，不是隔离**）
# 第十六轮 P3 引入，第十七轮 P3 收窄表述：这里做的是"跑完之后告诉你有没有变"，
# 不是"测试根本碰不到生产"。判 FAIL 时写入或外发**已经发生**。
# 真正的隔离需要临时状态目录 + 独立外发通道（见文件头 UPGRADE_TRIGGER）。
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


# ------------------------------------------------ 生产 Bot 进程探测（三态，Fail-Closed）
# 第十七轮 ChatGPT P1（2026-09-26）：旧实现是 **Fail-Open** ——
#   `except Exception: return []` 把 powershell 超时 / 不存在 / WMI 故障统统变成
#   「没有 Bot」；「非零退出码 + 空 stdout」也落到 `[]`；查询还只认 `Name='python.exe'`。
#   于是准确口径只是「**成功探测到进程时**才拒绝」，而不是「探测不确定时也拒绝」。
# 现在三态，且**不确定按最坏情况处理**：
#   LIVE_RUNNING  探测成功且命中 ≥1 个进程
#   LIVE_NONE     探测成功**且明确确认**零命中（必须收到 NO_MATCH 哨兵，缺一不可）
#   LIVE_UNKNOWN  探测不可用 / 输出不可解析 / 自相矛盾 → 与 RUNNING 同样拒绝执行（rc=3）
#
# 口径要点：**「没有输出」永远不等于「没有 Bot」**。所以脚本对两种结论都显式表态：
# 命中打 `PID <pid> <name>`，确认零命中打 `NO_MATCH`；CIM 故障或一条记录都取不到 → 非零退出。
#
# KNOWN_LIMITATION: 仍靠命令行里的 `bot_runner.py` / `watchdog.py` 字样识别生产进程。
#   误报方向是安全的（多拒绝一次，人看一眼 pid+进程名即可判断），且 --allow-live 可越过；
#   真正的结构保证要靠 `acquire_instance_lock()` 用的具名互斥体（跨进程可见、不受命令行影响）。
# UPGRADE_TRIGGER: 任何一次「门禁拒绝执行但实际没有生产 Bot」或「生产在跑却探测成 NONE」
#                  的现场案例，即改为以具名互斥体为唯一判据。
LIVE_RUNNING, LIVE_NONE, LIVE_UNKNOWN = 'RUNNING', 'NONE', 'UNKNOWN'

_LIVE_PROBE_PS = """
$ErrorActionPreference='Stop';
try {
  $shells = @('powershell.exe','pwsh.exe','cmd.exe','conhost.exe');
  $all = @(Get-CimInstance Win32_Process);
  if ($all.Count -eq 0) { exit 1 };
  $hit = @($all | Where-Object {
      ($_.ProcessId -ne $PID) -and ($shells -notcontains $_.Name) -and
      ($_.CommandLine -like '*bot_runner.py*' -or $_.CommandLine -like '*watchdog.py*')
  });
  if ($hit.Count -eq 0) { Write-Output 'NO_MATCH' }
  else { $hit | ForEach-Object { Write-Output ('PID ' + $_.ProcessId + ' ' + $_.Name) } }
  exit 0
} catch { exit 1 }
"""

# 心跳是**独立于进程查询**的第二信号（由 watchdog 每 60s 写入）。
# 用途只有一处：进程查询说「零命中」而心跳在新鲜窗口内说 `bot_alive=true`
# → 两个信号矛盾 → 说明探测不可信 → 按 UNKNOWN 拒绝。
HEARTBEAT_FILE = os.path.join(ROOT, '.heartbeat.json')
HEARTBEAT_FRESH_S = 150   # 60s 写一次，留 2 个周期的容差


def detect_live_bot(timeout=25):
    """三态探测生产 Bot 进程。返回 `(state, procs, reason)`。

    procs: `[(pid, name), ...]`，仅 LIVE_RUNNING 时非空；reason 仅非 RUNNING 时有意义。
    """
    if os.name != 'nt':
        return (LIVE_UNKNOWN, [],
                f'非 Windows 平台（os.name={os.name}），CIM 进程探测不可用')
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', _LIVE_PROBE_PS],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return LIVE_UNKNOWN, [], f'powershell 探测超时（{timeout}s），进程清单不可得'
    except OSError as e:
        return LIVE_UNKNOWN, [], f'powershell 无法启动：{type(e).__name__}: {e}'

    if proc.returncode != 0:
        err = ' '.join((proc.stderr or '').split())[:120]
        return (LIVE_UNKNOWN, [],
                f'powershell 退出码 {proc.returncode}' + (f'，stderr: {err}' if err else ''))

    procs, saw_no_match = [], False
    for raw in (proc.stdout or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == 'NO_MATCH':
            saw_no_match = True
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == 'PID' and parts[1].isdigit():
            procs.append((int(parts[1]), parts[2] if len(parts) > 2 else '?'))
            continue
        return LIVE_UNKNOWN, [], f'探测输出无法解析：{line[:80]!r}'

    if procs and saw_no_match:
        return LIVE_UNKNOWN, [], '探测输出自相矛盾（同时给出 PID 与 NO_MATCH）'
    if procs:
        return LIVE_RUNNING, procs, ''
    if saw_no_match:
        return LIVE_NONE, [], ''
    return LIVE_UNKNOWN, [], '探测输出为空（**空输出不等于没有 Bot**）'


def _heartbeat_bot_alive():
    """心跳能否证明 Bot 在跑：`True` / `False` / `None`（无可用信息，含心跳陈旧）。"""
    try:
        with open(HEARTBEAT_FILE, encoding='utf-8') as f:
            hb = json.load(f)
        age = time.time() - float(hb['ts'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if age > HEARTBEAT_FRESH_S:
        return None        # 陈旧心跳既不能证明活着、也不能证明死了
    return bool(hb.get('bot_alive'))


def live_bot_guard_state():
    """合并进程探测与心跳佐证，返回 `(state, procs, reason)`。"""
    state, procs, reason = detect_live_bot()
    hb = _heartbeat_bot_alive()
    if state == LIVE_NONE and hb is True:
        return (LIVE_UNKNOWN, [],
                f'进程查询确认零命中，但 {os.path.basename(HEARTBEAT_FILE)} 在 '
                f'{HEARTBEAT_FRESH_S}s 内报 bot_alive=true —— 两个信号矛盾，探测不可信')
    if state == LIVE_UNKNOWN:
        reason += f'（心跳佐证：{"报活" if hb else "无/未报活"}）'
    return state, procs, reason


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


def _parse_failed_cases(stdout: str):
    """解析脚本式测试的**逐项结果**，返回失败用例名集合；一行都没匹配到返回 None。

    形态见 test_v64_p3_lifecycle.py:526/529/531：
        print(f'✅ {fn.__name__}')                      # 通过
        print(f'❌ {fn.__name__}: {e}')                 # 断言失败
        print(f'❌ {fn.__name__}: {type(e).__name__}: …')  # 抛异常

    返回 None 表示"解析不出逐项身份"，调用方必须当作漂移处理 ——
    宁可误报一次让人去看，也不能在看不见组成的情况下声称"组成未变"。
    """
    failed = set()
    seen = 0
    for line in (stdout or '').splitlines():
        m = CASE_LINE_RE.match(line)
        if not m:
            continue
        seen += 1
        if m.group(1) == '❌':
            failed.add(m.group(2))
    return failed if seen else None


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
        # `| {0}`：rc=0 也必须进基线校验。否则 p3 一旦全绿（rc=0 不在 {1} 里）
        # 会被直接判 PASS，§19.2 声称的"变好也必须暴露"在这条路上就不成立。
        # rc=2/-9 等仍走原有 FAIL 分支，分类不因本次放宽而改变。
        if name in BASELINE_GREEN and rc in (allowed | {0}):
            m = GREEN_RE.search(stdout)
            if m:
                got = (int(m.group(1)), int(m.group(2)))
                exp = BASELINE_GREEN[name]
                if got != exp:
                    drift = (f'基线 GREEN 期望 {exp[0]}/{exp[1]}，'
                             f'实际 {got[0]}/{got[1]}')
            else:
                drift = '基线 GREEN 行未找到（脚本输出格式变更，基线登记失效）'
            # 第十七轮 P2：数量相同 **不等于** 组成相同 —— 原本通过的坏 1 项、
            # 原本失败的修好 1 项，仍是 3/9，纯计数完全看不出来。逐项比对失败身份。
            if drift is None and name in BASELINE_FAIL_SET:
                got_fail = _parse_failed_cases(stdout)
                if got_fail is None:
                    drift = '逐项结果解析失败（stdout 无 `✅ <name>` / `❌ <name>: <reason>` 行）'
                else:
                    exp_fail = BASELINE_FAIL_SET[name]
                    newly = sorted(got_fail - exp_fail)
                    fixed = sorted(exp_fail - got_fail)
                    if newly or fixed:
                        drift = '失败项组成变了：'
                        if newly:
                            drift += f'新增失败 {newly}；'
                        if fixed:
                            drift += f'转为通过 {fixed}；'

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
    allow_live = '--allow-live' in argv

    # ---- 第十七轮 P3：生产 Bot 运行时默认**拒绝执行**（不是警告，是拒绝）----
    # 上一轮只打印一行 ⚠ 然后照跑，等于把"是否接受风险"的决定权隐式拿走。
    # 这里的事实是：生产哨兵**只能事后发现**，判 FAIL 时写入或外发已经发生，
    # 未列入清单的文件与"改了再改回"也检测不到。所以默认不跑，显式同意才跑。
    # KNOWN_LIMITATION: 这仍是"人工同意"而非隔离。
    # UPGRADE_TRIGGER: 要真正隔离，需给测试提供临时状态目录 + 独立外发通道，
    #                 使测试在默认情况下**没有能力**触碰生产文件与真实外发。
    state, procs, reason = live_bot_guard_state()
    procs_text = ', '.join(f'pid={p}({n})' for p, n in procs)
    if state != LIVE_NONE and not allow_live:
        print('=' * 70)
        if state == LIVE_RUNNING:
            print(f'⛔ 门禁拒绝执行：检测到生产 Bot 运行中（{procs_text}）。')
        else:
            # 第十七轮 P1：探测不出来 ≠ 没有 Bot。旧实现在这里 Fail-Open（照跑测试）。
            print(f'⛔ 门禁拒绝执行：**无法确定**生产 Bot 是否在运行 —— {reason}')
            print('   按最坏情况处理（Fail-Closed）：请先自行确认无 bot_runner/watchdog 在跑，')
            print('   再追加 --allow-live；修好探测（powershell / WMI）后重跑更佳。')
        print('   这不是隔离——生产哨兵只能在**全部测试跑完之后**比对指纹，')
        print('   判 FAIL 时文件写入或外发已经发生；未列入清单的文件、')
        print('   以及"改了又改回原样"的短暂写入，都在检测范围之外。')
        print('   继续请二选一：')
        print('     1) 先停机，再跑门禁（推荐，此时 test_orphan_guard 也能拿到 rc=0）；')
        print('     2) 追加 --allow-live，明确接受"测试与生产并存"的风险。')
        print('   本次未执行任何测试。')
        print('=' * 70)
        return 3

    before = _snapshot()
    if state == LIVE_RUNNING:
        print(f'⚠ --allow-live：已明确接受测试与生产并存（{procs_text}）。'
              f'下方生产哨兵是**事后检测**，不是隔离。')
    elif state == LIVE_UNKNOWN:
        print(f'⚠ --allow-live：进程探测**不确定**（{reason}），已按明确同意执行。')
        print('   ⚠ 本次**无法确认**生产 Bot 是否在运行，请先自行确认再依赖本轮结论。')
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

    # -------- 生产哨兵（第十六轮 P3，第十七轮收窄表述）：**事后检测**，非隔离 --------
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
        verdict, exit_code = 'FAIL', 1
    elif exceptions:
        verdict, exit_code = ('COMPLETED-WITH-EXCEPTIONS —— 运行完成，'
                              '但**不是全绿**（存在下述未通过项）'), 2
    else:
        verdict, exit_code = 'ALL-GREEN —— 全部退出码 0，无待验证项', 0
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
        print(f'✔ 生产哨兵（事后检测）未发现改动：{", ".join(before.keys())}')
    # 第十七轮 P1：文字说了"未全绿"，退出码却还是 0 —— 只看 rc 的发布脚本会
    # 把它当成功。现在 rc 与结论一一对应，且与"根本没跑"（3）区分开。
    print(f'进程退出码：{exit_code}  '
          f'(0=ALL-GREEN | 1=FAIL | 2=仅含已登记的基线失败/待验证 | '
          f'3=被拒绝未执行[生产在跑或探测不确定])')
    print('=' * 70)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
