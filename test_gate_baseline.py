#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁自身逻辑的回归测试（第十七轮 ChatGPT P1「退出码仍宽」/ P2「只检数量不检组成」）。

## 为什么要有这个文件

`run_test_gate.py` 是"判断别的测试可不可信"的那个东西，它自己却一直没有任何测试。
一旦 `BASELINE-DRIFT` 的判定写错，它照样会打印一套看起来很正常的结论，而复审方
只能读代码、无法独立复现。本文件把两件新判定钉成可直接执行的用例：

1. **逐项失败身份**（不是计数）：`GREEN: 3/9` 计数不变、但通过/失败的**具体是哪几项**
   换过 → 必须判 `BASELINE-DRIFT`。纯计数对这种情况完全失明。
2. **基线登记本身仍与现实一致**：真跑一次 `test_v64_p3_lifecycle.py`，逐项解析结果
   必须等于 `run_test_gate.BASELINE_FAIL_SET` 登记的那 6 项。
   这一条同时是 `BASELINE_FAIL_SET` 的**基线依据**的自动化复核。

## 跑法

    python test_gate_baseline.py        # rc=0 即全过

门禁 `run_test_gate.py` 的脚本段会自动把本文件当作一个被测脚本执行。
命名用 `check_*` 而非 `test_*`，是为了和其它根目录脚本一致：pytest 收集 0 项，
只在脚本段跑一次，不重复执行。
"""

import io
import os
import subprocess
import sys
import types
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import run_test_gate as g  # noqa: E402

SCRIPT = 'test_v64_p3_lifecycle.py'

# p3 实际打印的 9 个用例名（与 BASELINE_FAIL_SET 同源，用于构造合成输出）
CASE_NAMES = [
    'r1_clear_during_sleep_zero_side_effect',
    'r2_toctou_second_guard',
    'r3_zombie_no_protection_repair',
    'r4_corrupted_is_not_empty',
    'r5_settlement_report_exactly_once',
    'r5b_atomic_settlement_claim',
    'r5c_settlement_reported_ratchet',
    'r7_settlement_uses_net_qty_not_gross',
    'r6_normal_batch_unchanged',
]
EXP_FAIL = set(g.BASELINE_FAIL_SET[SCRIPT])
assert EXP_FAIL.issubset(set(CASE_NAMES)), 'BASELINE_FAIL_SET 含未登记用例名'


def _fake_stdout(failed):
    """按 p3 的真实行形态（test_v64_p3_lifecycle.py:526/529/531）造一份 stdout。"""
    lines = [('❌ %s: 理由' % n) if n in failed else ('✅ %s' % n)
             for n in CASE_NAMES]
    lines.append('GREEN: %d/%d' % (len(CASE_NAMES) - len(failed), len(CASE_NAMES)))
    return '\n'.join(lines) + '\n'


def _run_gate_scripts(rc, out, name=SCRIPT):
    """以假的 subprocess 跑一遍 run_scripts，取回 (overall_ok, counts, 打印文本)。"""
    real_discover, real_run = g.discover_scripts, g.subprocess.run
    g.discover_scripts = lambda: [name]
    g.subprocess.run = lambda *a, **k: types.SimpleNamespace(
        returncode=rc, stdout=out, stderr='')
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok, counts = g.run_scripts(strict=False)
    finally:
        g.discover_scripts, g.subprocess.run = real_discover, real_run
    return ok, counts, buf.getvalue()


def check_parse_edge_cases():
    """解析器：锚定 ✅/❌，空输出返回 None（宁可误报也不能假装'组成未变'）。"""
    assert g._parse_failed_cases('') is None, '空输出必须返回 None'
    assert g._parse_failed_cases('GREEN: 3/9') is None, '汇总行不能被当成用例行'
    assert not g.CASE_LINE_RE.match('GREEN: 3/9'), 'GREEN 行不得匹配用例正则'
    assert not g.CASE_LINE_RE.match('some unrelated line'), '杂散行不得匹配'
    got = g._parse_failed_cases(_fake_stdout(EXP_FAIL))
    assert got == EXP_FAIL, '合成输出解析结果与登记基线不符'


def check_composition_beats_count():
    """核心用例：计数完全不变（仍 3/9）时，组成变化必须被判漂移。"""
    # 1) 基线原样 → 只判 BASELINE-FAIL（已登记），不致命
    ok, counts, _ = _run_gate_scripts(1, _fake_stdout(EXP_FAIL))
    assert ok and counts['BASELINE-FAIL'] == 1 and not counts['BASELINE-DRIFT'], \
        '基线原样不应判漂移'

    # 2) 换掉其中一项（1 个失败转通过 + 1 个通过转失败）→ 仍是 6 失败 / GREEN 3/9
    swapped = (EXP_FAIL - {'r6_normal_batch_unchanged'}) | {'r2_toctou_second_guard'}
    assert len(swapped) == len(EXP_FAIL), '用例数必须不变，否则比的就不是组成'
    ok, counts, text = _run_gate_scripts(1, _fake_stdout(swapped))
    assert not ok and counts['BASELINE-DRIFT'] == 1, \
        '计数不变但组成变了，必须判漂移（旧的纯计数逻辑对它完全失明）'
    assert '失败项组成变了' in text and '新增失败' in text, '必须打印组成差异明细'


def check_count_still_caught():
    """原有的计数校验不能因本次改动而失效。"""
    ok, counts, text = _run_gate_scripts(1, _fake_stdout(EXP_FAIL - {'r6_normal_batch_unchanged'}))
    assert not ok and counts['BASELINE-DRIFT'] == 1, '通过数变多仍要判漂移'
    assert '实际 4/9' in text, '必须打印实测 GREEN 值'


def check_rc_zero_exposes_improvement():
    """p3 一旦全绿（rc=0），基线登记就过期了 —— 必须暴露，不能直接 PASS。

    第十七轮补的口子：旧判定是 `rc in allowed`（allowed={1}），rc=0 会跳过整个
    基线校验、直接落进 `elif rc == 0: PASS`，于是"变好"永远看不见。
    """
    ok, counts, text = _run_gate_scripts(0, _fake_stdout(set()))
    assert not ok and counts['BASELINE-DRIFT'] == 1, '全绿必须判基线漂移'
    assert '实际 9/9' in text, '必须打印实测 GREEN 值'


def check_unexpected_rc_is_fail():
    """基线脚本给出未登记的退出码 → FAIL，不被基线身份豁免。"""
    ok, counts, _ = _run_gate_scripts(2, _fake_stdout(EXP_FAIL))
    assert not ok and counts['FAIL'] == 1, '未登记退出码必须 FAIL'


def check_registered_baseline_matches_reality():
    """真跑一次 p3，逐项结果必须等于登记的 6 项失败 —— 这就是基线依据本身。

    这一条失败，说明 `BASELINE_FAIL_SET` / `BASELINE_GREEN` 已过期，门禁会以
    BASELINE-DRIFT 呈现；本测试的作用是让它**可以被单独、快速地复现**。
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONPATH'] = ROOT
    proc = subprocess.run([sys.executable, SCRIPT], cwd=ROOT, env=env,
                          capture_output=True, encoding='utf-8', errors='replace',
                          timeout=300)
    out = proc.stdout or ''
    m = g.GREEN_RE.search(out)
    assert m, 'p3 未打印 GREEN 行 —— 输出格式变更，基线登记失效'
    assert (int(m.group(1)), int(m.group(2))) == g.BASELINE_GREEN[SCRIPT], \
        'GREEN 实测 %s 与登记 %s 不符' % (m.group(0), g.BASELINE_GREEN[SCRIPT])
    got = g._parse_failed_cases(out)
    assert got is not None, 'p3 逐项结果解析不出（行形态变更）'
    newly, fixed = sorted(got - EXP_FAIL), sorted(EXP_FAIL - got)
    assert not newly and not fixed, \
        '失败项组成与登记不符：新增失败=%s 转为通过=%s' % (newly, fixed)
    assert proc.returncode == 1, '基线脚本 rc 应为 1，实测 %s' % proc.returncode


CHECKS = [
    check_parse_edge_cases,
    check_composition_beats_count,
    check_count_still_caught,
    check_rc_zero_exposes_improvement,
    check_unexpected_rc_is_fail,
    check_registered_baseline_matches_reality,
]


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print('✅ %s' % fn.__name__)
        except AssertionError as e:
            failed += 1
            print('❌ %s: %s' % (fn.__name__, e))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print('❌ %s: %s: %s' % (fn.__name__, type(e).__name__, e))
    print('\nGATE-BASELINE: %d/%d' % (len(CHECKS) - failed, len(CHECKS)))
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
