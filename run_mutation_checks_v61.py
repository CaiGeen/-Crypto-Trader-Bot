#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v6.1 变异检查器 —— 证明测试断言「跑绿不算数」。

背景：2026-08-29 市价平仓 -4061 事故的修复方案（v6.1）送外部复审时，复审方明确
提出判据：「新增负向样本先对当前 v6 RED，再对 v6.1 GREEN，比把场景数堆到 100+
更有价值」。本脚本把这条判据固化成可复跑的一等工件：逐个往 helper 实现里注入
「撤销某个 v6.1 防护」的变异，跑完整测试套件，断言**指定用例必须失败**。

若某个变异体活下来（测试仍全绿），说明对应防护没有被任何断言守护 —— 这正是
C 路自审抓出的 `_set_close_reason_if_current.persist_failed` 那种盲区。

用法：
    python run_mutation_checks_v61.py            # 跑全部变异体
    python run_mutation_checks_v61.py --list     # 只列出变异体清单

退出码：0 = 全部变异体被杀死（含基线对照符合预期）；1 = 存在存活变异体或对照异常。

约束：本脚本只读项目文件，所有变异体与测试副本写入 G:/tmp/mut61_auto/。
"""
import os
import re
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
TEST_SRC = os.path.join(HERE, 'test_close_confirmation_v6.py')
# v6.1：helper 正本从 G:/tmp 移入项目内 `送审附件_v6.1/`，不再依赖临时目录。
# WORKDIR 仍留在 G:/tmp —— 它是每轮重新生成的**产物**，不该污染项目。
HELPER = os.path.join(HERE, '送审附件_v6.1', 'new_helpers_v6.py')
WORKDIR = 'G:/tmp/mut61_auto'

# 变异体注入方式：环境变量，不再做文本替换。
#
# 原先是把测试文件复制一份、再把 `V6_PATH = 'G:/tmp/...'` 那一行替换成变异体路径。
# 那套做法有两个毛病：①依赖一个会随排版漂移的文本锚点（改一次路径写法就得同步改
# 这里，否则静默 ANCHOR_NOT_FOUND）；②路径写死后整个项目无法搬移。
# 改为环境变量后，测试侧只需 `os.environ.get('V6_HELPER_OVERRIDE')`，此处负责注入。
# 下方 ANCHOR 校验用于确保「测试仍然支持这个注入点」，防止注入被悄悄移除。
TEST_HOOK = "os.environ.get('V6_HELPER_OVERRIDE')"

# ──────────────────────────────────────────────────────────────────────
# 变异体定义：(key, 说明, 原文片段, 替换片段, 必须失败的用例标签集合)
#
# 「原文片段」必须与 new_helpers_v6.py 逐字一致 —— 若 helper 演进导致片段失配，
# 本脚本会直接报 ANCHOR_NOT_FOUND 而不是静默放过（fail-loud 优先于假绿）。
# ──────────────────────────────────────────────────────────────────────
MUTANTS = [
    dict(
        key='M0-baseline',
        desc='对照组：注入一条无害注释（不撤销任何防护）',
        old='class _Holder:\n',
        new='class _Holder:\n    # mutation-control: no-op\n',
        expect=set(),          # 期望零失败、rc=0 —— 证明「跑绿」本身有意义
        expect_rc=0,
    ),
    dict(
        key='M1-begin-persist',
        desc='撤销 BEGIN 的 _persist_states() 返回值检查（回到 v6.0 行为）',
        old="""            if not self._persist_states(all_states):
                return False, '', ('claim_persist_failed（状态写盘失败，'
                                   '视为未取得所有权，绝不发单）'), None
""",
        new='',
        expect={'B11', 'B11b', 'B11c'},
        expect_rc=1,
    ),
    dict(
        key='M2-rollback-persist',
        desc='撤销 rollback 的 _persist_states() 返回值检查（谎称 rolled_back）',
        old="""            if not self._persist_states(all_states):
                return False, ('rollback_persist_failed（回滚写盘失败，'
                               '磁盘仍为 close_phase=1）')
""",
        new='',
        expect={'B12', 'B12b'},
        expect_rc=1,
    ),
    dict(
        key='M3-reason-persist',
        desc='撤销 _set_close_reason_if_current 的写盘检查（C 路原存活变异体）',
        old="""            if not self._persist_states(all_states):
                return False, 'persist_failed（reason 写盘失败）'
""",
        new='',
        expect={'B13', 'B13b'},
        expect_rc=1,
    ),
    dict(
        key='M4-target-short',
        desc='撤销 target_amounts_short 校验（切片静默少平漏洞回归）',
        old="""        if len(target_amounts) < last_filled_count:
            return False, None, (f'target_amounts_short（台账计划层 '
                                 f'{len(target_amounts)} < 已成交层 '
                                 f'{last_filled_count}）')
""",
        new='',
        expect={'D3c', 'D3cb'},
        expect_rc=1,
    ),
    dict(
        key='M5-side-check',
        desc='撤销 side 严格校验（非法 side 可被默认成 BUY）',
        old="""        if side not in ('BUY', 'SELL'):
            return False, None, f'side_invalid（{side!r}，必须是 BUY/SELL）'
""",
        new='',
        expect={'D3d', 'D3db'},
        expect_rc=1,
    ),
    dict(
        key='M6-entry-orders',
        desc='撤销 entry_orders 缺失/不足两项校验（UNKNOWN→EMPTY 假确认回归）',
        old="""            if not isinstance(_eo, list):
                return False, None, ('entry_orders_missing（存在 '
                                     f'{len(target_amounts) - last_filled_count} '
                                     '个未成交计划层，但 entry_orders 缺失/非列表）')
            if len(_eo) < len(target_amounts) and not (0 < len(_eo) == last_filled_count):
                return False, None, (f'entry_orders_short（entry_orders 长度 '
                                     f'{len(_eo)} 与已成交层数 {last_filled_count} /'
                                     f' 计划层数 {len(target_amounts)} 不一致，'
                                     '未成交层无法逐 ID 归因）')
""",
        new='            pass\n',
        expect={'D6c', 'D6cb', 'D6d', 'D6db'},
        expect_rc=1,
    ),
    dict(
        key='M7-f1-wide',
        desc='entry_orders 校验回退成 v6.1 初版宽判据（误伤 🗑️ 按钮批次，即 F-1）',
        old="            if len(_eo) < len(target_amounts) and not (0 < len(_eo) == last_filled_count):",
        new="            if len(_eo) < len(target_amounts):",
        expect={'D6e', 'D6g', 'D6gb'},
        expect_rc=1,
    ),
    dict(
        key='M8-verify-always-gone',
        desc='_verify_entry_order_terminal 恒返回 gone（逐 ID 终态验证形同虚设）',
        old="""        for i in range(max(1, attempts)):
            try:
                order = self._safe_api_call(""",
        new="""        return 'gone', None
        for i in range(max(1, attempts)):
            try:
                order = self._safe_api_call(""",
        # 语义上必须失败的：逐 ID 判据本身（E3/E4）+ 市价路径不再冻结保护
        # （S2 系列）+ 限价路径不再回滚（L2 系列）。
        expect={'E3', 'E4', 'S2', 'S2b', 'S2c', 'S2e', 'L2', 'L2c'},
        expect_rc=1,
    ),
    dict(
        key='M9-snapshot-check',
        desc='绕过 gate 第 1 层快照可判定性检查（`or []` 假确认回归路径）',
        old="        if remaining is None or not isinstance(remaining, list):",
        new="        if False:",
        # 该变异外在表现与「干净 Fail-Closed」形似（TypeError 被 catch 吞掉后
        # 同样是 (False, '<EXC…>')），只有 L3c（断言「非异常兜底」）能分辨。
        expect={'E1', 'L3', 'L3c'},
        expect_rc=1,
    ),
]


def failed_labels(output):
    """从测试输出解析失败的用例标签（行形如 `  ❌ <label> ...`）。"""
    labels = set()
    for line in output.splitlines():
        s = line.strip()
        if s.startswith('❌'):
            rest = s.lstrip('❌').strip()
            labels.add(rest.split()[0] if rest else '')
    return labels


def run_one(m):
    """执行单个变异体，返回 (ok, 详情文本)。"""
    src = open(HELPER, encoding='utf-8').read()
    if src.count(m['old']) != 1:
        return False, "ANCHOR_NOT_FOUND（原文片段出现 %d 次，期望 1 次）" % src.count(m['old'])

    os.makedirs(WORKDIR, exist_ok=True)
    mut_path = os.path.join(WORKDIR, 'mut_%s.py' % m['key'].replace('-', '_'))
    test_path = os.path.join(WORKDIR, 'test_%s.py' % m['key'].replace('-', '_'))
    open(mut_path, 'w', encoding='utf-8').write(src.replace(m['old'], m['new']))

    tsrc = open(TEST_SRC, encoding='utf-8').read()
    if TEST_HOOK not in tsrc:
        return False, ('TEST_HOOK_NOT_FOUND（测试不再读取 V6_HELPER_OVERRIDE；'
                       '注入点被移除会让变异体形同未注入 → 假绿）')
    open(test_path, 'w', encoding='utf-8').write(tsrc)

    # 两个注入点：PROJECT_DIR 让副本仍能找到原项目的附件与送审稿，
    # V6_HELPER_OVERRIDE 把「被测 helper」换成这个变异体改坏后的版本。
    env = dict(os.environ,
               HELPER_PROJECT_DIR=HERE,
               V6_HELPER_OVERRIDE=mut_path.replace('\\', '/'))
    p = subprocess.run([PY, test_path], cwd=HERE, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=env)
    out = (p.stdout or '') + (p.stderr or '')
    labels = failed_labels(out)
    missing = m['expect'] - labels
    rc_ok = (p.returncode == m['expect_rc'])

    # 🔒 防假杀死：rc≠0 却零失败用例 = 测试被变异体搞崩（NameError/语法错），
    # 这不是「断言杀死变异体」，是噪声。必须 fail-loud 而不是当成通过。
    if m['expect'] and not labels:
        return False, ('CRASH_NOT_KILL（rc=%d 但无失败用例，测试被变异体搞崩）'
                       '：%s' % (p.returncode, out.strip().splitlines()[-1:]))

    if not rc_ok or missing:
        detail = ('rc=%d（期望 %d）；失败用例=%s；未如期失败的=%s'
                  % (p.returncode, m['expect_rc'],
                     sorted(labels) or '无', sorted(missing) or '无'))
        return False, detail
    # 探测模式（expect 为空）时列出全部失败用例，便于为新变异体确定期望集合
    shown = sorted(labels & m['expect']) if m['expect'] else sorted(labels)
    return True, 'rc=%d；失败 %d 项%s：%s' % (
        p.returncode, len(shown),
        '（如期）' if m['expect'] else '（探测）',
        ', '.join(shown) or '零失败')


def main():
    if '--list' in sys.argv:
        for m in MUTANTS:
            print('%-20s %s' % (m['key'], m['desc']))
        return 0

    if not os.path.exists(PY):
        print('❌ 找不到虚拟环境解释器：%s' % PY)
        return 1
    # 注意：不要 rmtree 整个工作目录 —— 产物每轮都会被覆盖重写，全量删除
    # 既没必要，又会触发沙箱「单轮批量删除超阈值需确认」的守卫而中断运行。
    os.makedirs(WORKDIR, exist_ok=True)

    print('=' * 68)
    print('v6.1 变异检查：逐个撤销防护，断言对应用例必须失败（跑绿不算数）')
    print('=' * 68)
    killed, survived = 0, []
    for m in MUTANTS:
        ok, detail = run_one(m)
        tag = '✅ 已杀死' if ok else '❌ 存活'
        print('%s  %-20s %s' % (tag, m['key'], detail))
        if ok:
            killed += 1
        else:
            survived.append(m['key'])

    print('-' * 68)
    print('变异体 %d 个，杀死 %d 个，存活 %d 个' % (len(MUTANTS), killed, len(survived)))
    if survived:
        print('❌ 存活变异体 = 存在未被任何断言守护的防护：%s' % ', '.join(survived))
        print('   → 必须补测试后再送审')
        return 1
    print('✅ 全部变异体被杀死（含 M0 基线对照 rc=0）——测试断言具备真实回归能力')
    return 0


if __name__ == '__main__':
    sys.exit(main())
