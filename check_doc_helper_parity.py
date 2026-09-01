# -*- coding: utf-8 -*-
"""
送审稿 helper 全集 vs helper 源码文件 —— 逐函数语义一致性核验（机器检查）

用途
----
送审稿里贴的 helper 全集代码块，必须与 `G:/tmp/new_helpers_vX.py` 里的实现**逐函数同构**。
手写文档最容易出的三类事故：
  ① 文档改了、helper 文件没改（或反之）→ 贴出去的代码与实际测试的不是同一份；
  ② 文档里同一函数出现多份（BEFORE / AFTER 残留）→ 应用时互相覆盖；
  ③ helper 文件悄悄多了一个函数而文档没有 → 审查者看到的不是完整实现。

判据（v6.1 重写，ChatGPT 交叉审核 R2-⑧⑨：旧版两个蓄意破坏均假绿）
----
两边同名函数分别 `ast.parse` → 剥离 lineno/col_offset → `ast.dump` 逐字符比对。
语义等价即通过；注释 / 缩进 / 换行差异不影响（这是刻意的：文档排版必然与源文件不同）。
**严格规则（对 DEFAULT_HELPERS 中每一个名字）**：
  - helper 文件中必须**恰好 1 份**定义；
  - 文档中必须**恰好 1 份**定义（2 份即使其中 1 份同构也判失败——旧版
    「至少一份同构即放行」是 Mutation B 假绿的根因）；
  - 两份 ast.dump 必须相等。
**集合规则**：
  - `set(文件内函数) == set(DEFAULT_HELPERS)`——文件多出任何函数即失败
    （旧版缺这条，Mutation A 假绿的根因）；
  - 文档中出现名字以 `_` 开头、却不在 DEFAULT_HELPERS 里的函数定义 → 失败
    （helper 命名约定都是前导下划线；普通示例函数不受影响）。

用法
----
    .venv/Scripts/python.exe check_doc_helper_parity.py [送审稿路径] [helper文件路径]
    .venv/Scripts/python.exe check_doc_helper_parity.py --self-test

`--self-test` 对自身做蓄意破坏实测（mutation test）：
  M1  helper 文件追加一个额外函数 → 必须 rc=1
  M2  文档追加一份同名但内容错误的 helper → 必须 rc=1
  M3  真实文档 + 真实 helper（基线）→ 必须 rc=0
三个期望全部满足才 exit 0。列为送审前置机器检查。

退出码：0 = 全部一致；1 = 存在差异（禁止交付）
"""
import ast
import os
import re
import sys
import tempfile
import textwrap

# v6.1：路径改为相对**本文件**解析，整个项目可搬移（原先写死 G:/my-crypto-bot/...
# 与 G:/tmp/...）。命令行参数仍优先，便于适配 v7+ 的新 helper 文件。
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DOC = os.path.join(_HERE, 'v6.2_正式diff_r6_送审ChatGPT.md')
DEFAULT_HELPER = os.path.join(_HERE, '送审附件_v6.2', 'new_helpers_v62.py')
# ⚠️ r6 起基线改为「完整正式 diff r6 + v6.2 helper（13 个）」。
#    旧基线（v6.1 送审稿 + v6.1 helper，11 个）与 DEFAULT_HELPERS 的 13 个
#    不匹配，会让 M3 基线恒 rc=1。checker 的基线必须指向**当前权威目标态**。

# 期望出现的 helper 全集（升级版本时按需增删）
DEFAULT_HELPERS = [
    '_begin_close_request_if_active',
    '_derive_close_txn_vars',
    '_rollback_close_request_if_current',
    '_set_close_reason_if_current',  # v6.1 新增（第 11 个）
    '_read_position_amt',
    '_fetch_close_order_state',
    '_confirm_close_filled',
    '_verify_entry_order_terminal',
    '_cancel_and_verify_entry_orders',
    '_survey_same_side_batches',
    '_close_amount_guard',
    # ── v6.2 新增（第 12 / 13 个）─────────────────────────────────
    # r5 稿仍写「11 helper」，已过期：parity 若按 11 个跑，会把这两个真实
    # helper 判成「文件多出的函数」而 rc=1，验证的根本不是目标态。
    '_pending_entry_ids_for_gate',           # v6.2 改动 8：registry 恢复视图
    '_commit_limit_close_order_if_current',  # v6.2 改动 9.2：LIMIT durable commit
]

# 本方案**显式改动签名的生产方法**（不是新增 helper，故不在上方全集里）。
#
# 为什么需要登记：parity 的【1】判据是「文档函数集合 == helper 全集」，双向严格。
# 送审稿里凡是完整 AFTER 定义都会进入文档集合；给生产方法加参数（如
# `_record_realized_pnl` 新增 `pnl_partial`）属于本方案的一部分，不该被当成
# 「文档多出的疑似 helper」。
#
# 默认 Fail-Closed：未登记的新定义照样拦。新增条目必须三处同步，缺一即报错：
#   ① 送审稿给出完整 AFTER 定义（可独立 ast.parse）
#   ② check_doc_helper_calls.py【6】把它列进「提议的生产签名改动清单」
#   ③ 此处登记
PROD_SIGNATURE_OVERRIDES = {
    '_record_realized_pnl',   # v6.1 §八-2：新增 pnl_partial（默认 False）
}


# helper 内部的**局部闭包**（不是 helper 全集成员，也不得被 builder 提成
# class method —— 见 build_v62_full.py 的 exact-set assert）。
#
# 为什么需要登记：这些 def 写在某个 helper 方法体内部，随所属 helper 的
# AFTER 一起出现在送审稿里；parity 的 1b 判据会把它当成「前导下划线且未申报
# 的疑似 helper」而 rc=1。它们不是全集成员，登记后豁免。
# 与 PROD_SIGNATURE_OVERRIDES 的区别：后者要求文档恰好 1 份定义，
# 而局部闭包随宿主 helper 出现，不单独要求份数。
INTERNAL_CLOSURES = {
    '_finite_pos_dv', '_finite_zero_dv',          # _derive_close_txn_vars 内
    '_topology_ok',                                # _survey_same_side_batches 内
    '_finite_nonneg', '_finite_pos', '_finite_zero',  # _close_amount_guard 内
    '_known_terminal_entry_ids',                   # r6：_cancel_and_verify_entry_orders 内
}


def extract_code_blocks(doc):
    """抽取 markdown 中所有 ```python 代码块（返回源码字符串列表）。

    跳过整块围栏行再取内容：'```python' 实为 9 字符，旧版 i+8 会在块首
    残留一个 col 0 的 'n'——块体若整体缩进（class 方法体被剥壳）即
    IndentationError 被静默跳过（v6.1 基线假阴性实案）。frag 摘录块
    直接排除（其语法完整性由 check_doc_code_blocks.py 负责）。"""
    blocks = []
    pos = 0
    while True:
        i = doc.find('```python', pos)
        if i < 0:
            break
        nl = doc.find('\n', i)
        if nl < 0:
            break
        j = doc.find('```', nl)
        if j < 0:
            break
        if not doc[i:].startswith('```python-frag'):
            blocks.append(doc[nl + 1:j])
        pos = j + 3
    return blocks


def norm_dump(node):
    """ast.dump 统一剥离位置信息，便于语义比对"""
    for n in ast.walk(node):
        for a in ('lineno', 'col_offset', 'end_lineno', 'end_col_offset'):
            if hasattr(n, a):
                try:
                    delattr(n, a)
                except AttributeError:
                    pass
    return ast.dump(node)


def collect_funcs(src):
    """收集函数定义 → {name: [dump, ...]}（同名多份全部保留，供「恰好 1 份」判据）。
    兼容两种排版：顶层 def（生产/测试片段）或 class _Holder 内的方法（helper 全集）"""
    out = {}
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        return None, out
    stack = list(tree.body)
    while stack:
        n = stack.pop(0)
        if isinstance(n, ast.FunctionDef):
            out.setdefault(n.name, []).append(norm_dump(n))
        elif isinstance(n, ast.ClassDef):
            stack.extend(n.body)
    return tree, out


def collect_doc_funcs(doc):
    """从文档全部 python 块收集函数定义 → {name: [dump, ...]}。
    frag 摘录块（AST 中存在 `...` 占位表达式）跳过。"""
    found = {}
    for blk in extract_code_blocks(doc):
        try:
            tree = ast.parse(textwrap.dedent(blk))
        except SyntaxError:
            continue  # 非完整块（frag / 摘录），由 check_doc_code_blocks.py 负责
        if any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
               and n.value.value is Ellipsis for n in ast.walk(tree)):
            continue
        stack = list(tree.body)
        while stack:
            n = stack.pop(0)
            if isinstance(n, ast.FunctionDef):
                found.setdefault(n.name, []).append(norm_dump(n))
            elif isinstance(n, ast.ClassDef):
                stack.extend(n.body)
    return found


def run_check(doc_path, helper_path, helpers=None, verbose=True):
    """核心检查。返回 rc（0/1）。供 main 与 --self-test 复用。"""
    helpers = helpers or DEFAULT_HELPERS
    doc = open(doc_path, encoding='utf-8').read()
    hsrc = open(helper_path, encoding='utf-8').read()

    _, hfuncs = collect_funcs(hsrc)
    if not hfuncs:
        print(f'❌ helper 文件解析失败或无函数定义：{helper_path}')
        return 1

    if verbose:
        print('=' * 72)
        print('送审稿 helper 全集 vs 实现文件 逐函数语义比对（v6.1 严格判据）')
        print('=' * 72)
        print(f'  送审稿：{doc_path}')
        print(f'  实现  ：{helper_path}')
        print(f'  函数  ：{sorted(hfuncs)}')

    doc_found = collect_doc_funcs(doc)

    rc = 0
    if verbose:
        print('\n【1】函数集合一致性（双向严格相等）')
    # 1a. 文件侧集合必须恰好等于 DEFAULT_HELPERS（Mutation A 修复）
    file_set, want_set = set(hfuncs), set(helpers)
    if file_set != want_set:
        extra_f = sorted(file_set - want_set)
        miss_f = sorted(want_set - file_set)
        if extra_f:
            print(f'  ❌ helper 文件多出未申报函数：{extra_f}（文档看不到完整实现）')
        if miss_f:
            print(f'  ❌ helper 文件缺失：{miss_f}')
        rc = 1
    else:
        if verbose:
            print(f'  ✅ helper 文件函数集合 == 申报全集（{len(helpers)} 个）')
    # 1b. 文档不得出现「前导下划线且未申报」的函数（helper 命名约定）。
    #     已登记的**生产签名改动**（PROD_SIGNATURE_OVERRIDES）豁免，但仍要求
    #     文档里恰好 1 份定义，见下方 1c —— 防止改签名改出两份互相矛盾的版本。
    doc_extra = sorted(k for k in doc_found
                       if k.startswith('_') and k not in want_set
                       and k not in PROD_SIGNATURE_OVERRIDES
                       and k not in INTERNAL_CLOSURES)
    if doc_extra:
        print(f'  ❌ 文档多出疑似 helper 的函数定义：{doc_extra}')
        print(f'     （若为有意改动的生产方法，须登记进 PROD_SIGNATURE_OVERRIDES，'
              f'并同步 check_doc_helper_calls.py【6】）')
        rc = 1
    elif verbose:
        print('  ✅ 文档无多余 helper 定义')

    # 1c. 已登记的生产签名改动：文档必须给出**恰好 1 份**定义
    if PROD_SIGNATURE_OVERRIDES:
        if verbose:
            print('\n【1c】已登记的生产签名改动 ⇒ 文档恰好 1 份定义')
        for name in sorted(PROD_SIGNATURE_OVERRIDES):
            n = len(doc_found.get(name, []))
            if n != 1:
                print(f'  ❌ {name}：文档有 {n} 份定义（要求恰好 1 份）'
                      f'—— 多份意味着落地时不知以哪份为准')
                rc = 1
            else:
                print(f'  ✅ {name:<34} 文档 1 份')

    if verbose:
        print('\n【2】逐函数：文件恰好 1 份、文档恰好 1 份、ast.dump 相等')
    for name in helpers:
        fvars = hfuncs.get(name, [])
        dvars = doc_found.get(name, [])
        if len(fvars) != 1:
            print(f'  ❌ {name}：实现文件内出现 {len(fvars)} 份（必须恰好 1 份）')
            rc = 1
            continue
        if len(dvars) == 0:
            print(f'  ❌ {name}：文档缺失')
            rc = 1
            continue
        if len(dvars) > 1:
            # Mutation B 修复：多份即失败，不再「至少一份同构即放行」
            same = sum(1 for d in dvars if d == fvars[0])
            print(f'  ❌ {name}：文档中出现 {len(dvars)} 份（其中 {same} 份与实现同构）'
                  f'——必须恰好 1 份，重复定义会在应用时互相覆盖')
            rc = 1
            continue
        if dvars[0] == fvars[0]:
            if verbose:
                print(f'  ✅ {name}')
        else:
            print(f'  ❌ {name}：文档与实现不同构')
            rc = 1

    if verbose:
        print('\n【3】重名重复定义检查（实现文件内，文本级兜底）')
    names = re.findall(r'^\s*def (\w+)', hsrc, re.M)
    dup = sorted(n for n in set(names) if names.count(n) > 1)
    if dup:
        print(f'  ❌ 重复定义：{dup}')
        rc = 1
    elif verbose:
        print(f'  ✅ {len(names)} 个函数定义，无重名')

    if verbose:
        print('\n' + '=' * 72)
        print('✅ helper 全集与送审稿完全一致' if rc == 0 else '❌ 存在不一致，禁止交付')
        print('=' * 72)
    return rc


def self_test():
    """mutation test：对 checker 自身做蓄意破坏，三个期望全部满足才 exit 0。"""
    print('=' * 72)
    print('checker 自身 mutation 自测（v6.1，ChatGPT R2-⑧⑨）')
    print('=' * 72)
    results = []
    tmp = tempfile.mkdtemp(prefix='parity_mt_')

    # M1：helper 文件追加一个额外函数 → 必须 rc=1
    m1 = os.path.join(tmp, 'helper_extra.py')
    with open(DEFAULT_HELPER, encoding='utf-8') as f:
        src = f.read()
    with open(m1, 'w', encoding='utf-8', newline='\n') as f:
        f.write(src + '\n\n    def hidden_extra_helper(self):\n        return 999\n')
    rc1 = run_check(DEFAULT_DOC, m1, verbose=False)
    ok1 = rc1 == 1
    results.append(('M1 helper 文件多出未申报函数 → 必须 rc=1', rc1, ok1))

    # M2：文档追加一份同名但内容错误的 helper → 必须 rc=1
    m2 = os.path.join(tmp, 'doc_dup.md')
    with open(DEFAULT_DOC, encoding='utf-8') as f:
        dsrc = f.read()
    dsrc += ('\n\n```python\n'
             'def _read_position_amt(self, symbol, side, is_hedge_mode):\n'
             '    return "WRONG"\n'
             '```\n')
    with open(m2, 'w', encoding='utf-8', newline='\n') as f:
        f.write(dsrc)
    rc2 = run_check(m2, DEFAULT_HELPER, verbose=False)
    ok2 = rc2 == 1
    results.append(('M2 文档出现同名错误副本 → 必须 rc=1', rc2, ok2))

    # M4：文档再插一份**已登记**生产方法的定义 → 必须 rc=1（【1c】恰好 1 份）。
    #     证明白名单不是全开：登记只豁免「是不是 helper」，不豁免「有几份」。
    m4 = os.path.join(tmp, 'doc_dup_override.md')
    with open(DEFAULT_DOC, encoding='utf-8') as f:
        d4 = f.read()
    d4 += ('\n\n```python\n'
           'def _record_realized_pnl(self, batch_id, symbol, side, amount, avg_price,\n'
           '                         exit_price, net_pnl, mode):\n'
           '    return None\n'
           '```\n')
    with open(m4, 'w', encoding='utf-8', newline='\n') as f:
        f.write(d4)
    rc4 = run_check(m4, DEFAULT_HELPER, verbose=False)
    ok4 = rc4 == 1
    results.append(('M4 已登记方法出现第 2 份定义 → 必须 rc=1', rc4, ok4))

    # M5：文档出现**未登记**的下划线函数定义 → 必须 rc=1（【1b】Fail-Closed）。
    #     证明 PROD_SIGNATURE_OVERRIDES 白名单没把 1b 变成摆设。
    m5 = os.path.join(tmp, 'doc_undeclared.md')
    with open(DEFAULT_DOC, encoding='utf-8') as f:
        d5 = f.read()
    d5 += ('\n\n```python\n'
           'def _undeclared_new_helper(self):\n'
           '    return 1\n'
           '```\n')
    with open(m5, 'w', encoding='utf-8', newline='\n') as f:
        f.write(d5)
    rc5 = run_check(m5, DEFAULT_HELPER, verbose=False)
    ok5 = rc5 == 1
    results.append(('M5 未登记的新 helper 定义 → 必须 rc=1', rc5, ok5))

    # M3：基线（真实文档 + 真实 helper）→ 必须 rc=0
    rc3 = run_check(DEFAULT_DOC, DEFAULT_HELPER, verbose=False)
    ok3 = rc3 == 0
    results.append(('M3 基线（真实文档+实现）→ 必须 rc=0', rc3, ok3))

    all_ok = True
    for name, rc, ok in results:
        print(f'  {"✅" if ok else "❌"} {name}（实得 rc={rc}）')
        all_ok = all_ok and ok
    print('=' * 72)
    print('✅ mutation 自测全部符合预期' if all_ok else '❌ mutation 自测未过：checker 存在假绿')
    print('=' * 72)
    return 0 if all_ok else 1


def main(argv):
    if '--self-test' in argv:
        return self_test()
    doc_path = argv[1] if len(argv) > 1 else DEFAULT_DOC
    helper_path = argv[2] if len(argv) > 2 else DEFAULT_HELPER
    return run_check(doc_path, helper_path)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
