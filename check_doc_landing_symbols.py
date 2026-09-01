#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_doc_landing_symbols.py — 送审稿「落地级自由变量」检查（第五道门）

背景（为什么要这道门）：
  C-7（v6.1 自审抓到的落地级 P0）——结算处写了
      self._record_realized_pnl(..., "市价平仓", pnl_partial=_pnl_partial)
  但**全文 `def _record_realized_pnl` 命中 0 次**，生产 L678-680 只有 8 个必填
  位置参数。照前稿落地 → 平仓已成交之后的结算阶段 TypeError → 批次钉在
  close_phase=1 → 监控线程冻结，**与本次事故的最终症状完全一致**。

  C-7 由 calls checker【4】偶然抓到，但**同类问题还有别的形态它守不住**：
  【4】只管「`self.xxx(...)` 的关键字参数名」，管不到**模块级名字**。
  最典型的就是 import：helper 文件头有 `import uuid`（BEGIN 用 `uuid4().hex`），
  而送审稿明写「文件头 import 略」——若生产 trader_260725.py 没有 `import uuid`，
  落地即 NameError。（实测生产 L12 有，但**必须逐个查完才算数**。）

  前三道门都守不住这一类：
    - check_doc_code_blocks：只保证语法可解析（NameError 是运行时错误）
    - check_doc_helper_parity：只保证 helper 与文档逐字一致
    - check_doc_helper_calls：只管 `self.xxx` 的调用闭包，不管 bare name

判据：
  把 helper 实现文件与送审稿每个可执行 AFTER 块里的**自由变量**全部扫出来，
  逐个对照生产 trader_260725.py 的模块级符号表（import / def / class / 顶层赋值）。
  任何自由变量在生产模块级找不到 ⇒ 落地 NameError ⇒ 致命。

  「自由变量」= Load 上下文的 ast.Name，且不满足以下任一豁免：
    - 是 self / cls
    - 是本函数内的局部绑定（参数/赋值/for/with-as/except-as/walrus/嵌套 def/global/nonlocal）
    - 是 builtins
    - 是所在模块/代码块的顶层符号
    - 是模块级 dunder 白名单成员（MODULE_DUNDERS，**显式列举，不用模式匹配**）

复用说明（重要）：
  块提取与解析包装**直接 import** check_doc_helper_calls 的
  `extract_python_blocks` / `parse_any`，不复制一份。
  理由：AFTER 块是方法级缩进片段，直接 ast.parse 必然 SyntaxError；而包装策略
  必须与 check_doc_code_blocks 保持一致。复制两份的话，改一处忘一处就会重演
  首版的 21/35 块解析失败 —— 而「扫不到」等于「没扫」，是比报错更坏的假绿。
  这正是本稿 §八-B 批判的「副本漂移」，自己不能犯。

用法：
    python check_doc_landing_symbols.py [文档] [helper] [生产]
    python check_doc_landing_symbols.py --self-test   # 变异自测，证明不是假绿

退出码：0 = 通过；1 = 存在落地 NameError 风险（或自测不符预期）。
"""
import ast
import builtins
import os
import pathlib
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')

# v6.1 路径契约：相对**本文件**解析，整个项目可搬移，不依赖 CWD。
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DEFAULT_DOC = str(_HERE / '事故_市价平仓-4061_精确diff_送审ChatGPT.md')
DEFAULT_HELPER = str(_HERE / '送审附件_v6.1' / 'new_helpers_v6.py')
DEFAULT_PROD = str(_HERE / 'trader_260725.py')

# 复用同目录 checker 的实现，避免副本漂移（详见模块 docstring）
from check_doc_helper_calls import extract_python_blocks, parse_any  # noqa: E402

BUILTIN_NAMES = set(dir(builtins))

# 模块级 dunder 白名单（**显式列举，不用模式匹配**）。
#
# 为什么不用 `name.startswith('__') and name.endswith('__')`：
#   那样会把 `__no_such_dunder__` 一并豁免，等于给自己开后门 —— 真正的落地
#   NameError 反而被静默放过。这是本 checker 最需要防的假绿，由变异体 V3 守着。
# `__class__` 是方法内的隐式闭包，同样天然可用。
MODULE_DUNDERS = {
    '__name__', '__doc__', '__package__', '__loader__', '__spec__',
    '__file__', '__builtins__', '__debug__', '__class__',
}


def local_bindings(fn):
    """收集一个函数体内所有局部绑定的名字（含嵌套作用域）。"""
    bound = set()
    a = fn.args
    for group in (a.posonlyargs, a.args, a.kwonlyargs):
        for arg in group:
            bound.add(arg.arg)
    if a.vararg:
        bound.add(a.vararg.arg)
    if a.kwarg:
        bound.add(a.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            # 🔒 修复（三路自审 A 路）：嵌套 def 的形参也是绑定（如
            # _topology_ok(amounts, details, n)），否则外层函数的 Load 扫描
            # 会把嵌套形参误报成「落地级自由变量」（假警报 rc=1）。
            _na = node.args
            for _grp in (_na.posonlyargs, _na.args, _na.kwonlyargs):
                for _arg in _grp:
                    bound.add(_arg.arg)
            if _na.vararg:
                bound.add(_na.vararg.arg)
            if _na.kwarg:
                bound.add(_na.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split('.')[0])
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
    return bound


def free_names(tree, module_level):
    """返回 [(函数名, 自由变量名)]，排除局部绑定 / self / builtins / 模块级 / dunder。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound = local_bindings(node)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id in bound or sub.id in ('self', 'cls'):
                        continue
                    if sub.id in BUILTIN_NAMES or sub.id in module_level:
                        continue
                    if sub.id in MODULE_DUNDERS:
                        continue
                    out.append((node.name, sub.id))
    return out


def module_symbols(tree):
    """提取模块的顶层符号：import 名 / def / class / 顶层赋值目标。"""
    syms = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                syms.add((al.asname or al.name).split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    syms.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            syms.add(node.target.id)
        elif isinstance(node, (ast.If, ast.Try)):
            # 顶层条件块里的 import/def（如 try: import xxx）
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for al in sub.names:
                        syms.add((al.asname or al.name).split('.')[0])
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    syms.add(sub.name)
    return syms


def block_level_symbols(tree):
    """提取一个代码块自己定义的顶层符号（块内 def / import / 赋值）。"""
    syms = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                syms.add((al.asname or al.name).split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    syms.add(t.id)
    return syms


def count_frag_blocks(doc):
    """统计 python-frag 摘录块数量（仅用于输出展示）。"""
    return sum(1 for ln in doc.splitlines() if ln.strip() == '```python-frag')


def main():
    # 环境变量优先级高于默认值，供 --self-test 注入变异体（不改本文件文本）。
    argv = [a for a in sys.argv[1:] if a != '--self-test']
    doc_path = (os.environ.get('SCAN_DOC_OVERRIDE')
                or (argv[0] if len(argv) > 0 else DEFAULT_DOC))
    helper_path = (os.environ.get('SCAN_HELPER_OVERRIDE')
                   or (argv[1] if len(argv) > 1 else DEFAULT_HELPER))
    prod_path = argv[2] if len(argv) > 2 else DEFAULT_PROD

    for p in (doc_path, helper_path, prod_path):
        if not os.path.exists(p):
            print('❌ 找不到文件：%s' % p)
            return 1

    prod_src = open(prod_path, encoding='utf-8').read()
    prod_tree = ast.parse(prod_src)
    prod_syms = module_symbols(prod_tree)

    doc = open(doc_path, encoding='utf-8').read()
    helper_src = open(helper_path, encoding='utf-8').read()
    helper_tree = ast.parse(helper_src)
    hmod = module_symbols(helper_tree)

    print('=' * 72)
    print('落地级自由变量检查：helper 实现 + 送审稿 AFTER 块 vs 生产模块级符号')
    print('=' * 72)
    print('生产模块级符号 %d 个' % len(prod_syms))

    bad = []

    # ── 【A】helper 实现文件 ──
    print('\n【A】helper 实现文件（%s）' % os.path.basename(helper_path))
    print('-' * 72)
    free_h = {}
    for fn, name in free_names(helper_tree, hmod):
        free_h.setdefault(name, set()).add(fn)
    if not free_h:
        print('  ✅ 零自由变量（全部名字可在生产模块级解析）')
    for name in sorted(free_h):
        fns = sorted(free_h[name])
        ok = name in prod_syms
        print('  %-30s %s   ← %s' % (
            name, '✅' if ok else '❌ 生产模块级无此符号',
            ', '.join(fns[:3]) + (' …' if len(fns) > 3 else '')))
        if not ok:
            bad.append(('helper', name))

    # ── 【B】送审稿可执行 AFTER 块 ──
    blocks = extract_python_blocks(doc)
    n_frag = count_frag_blocks(doc)
    print('\n【B】送审稿可执行 AFTER 块（%d 个；另有 %d 个 python-frag 摘录豁免）'
          % (len(blocks), n_frag))
    print('-' * 72)
    free_b = {}
    parse_fail = []
    for lineno, code in blocks:
        tree, _ = parse_any(code)
        if tree is None:
            parse_fail.append(lineno)
            continue
        scope = block_level_symbols(tree) | hmod | prod_syms
        for fn, name in free_names(tree, scope):
            free_b.setdefault(name, set()).add('L%d/%s' % (lineno, fn))

    if parse_fail:
        print('  ❌ 无法解析的代码块：L%s'
              % ', L'.join(str(x) for x in parse_fail))
        print('     （扫不到就等于没扫 —— 必须归零）')
        bad.append(('doc', 'parse_fail'))
    else:
        print('  ✅ 全部 %d 个块解析成功（无漏扫）' % len(blocks))

    if not free_b:
        print('  ✅ 零自由变量')
    for name in sorted(free_b):
        where = sorted(free_b[name])
        ok = (name in prod_syms or name in hmod)
        print('  %-30s %s   ← %s' % (
            name, '✅' if ok else '❌ 生产与 helper 均无',
            ', '.join(where[:2]) + (' …' if len(where) > 2 else '')))
        if not ok:
            bad.append(('doc', name))

    print('\n' + '=' * 72)
    if bad:
        uniq = sorted(set(n for _, n in bad))
        print('❌ 存在落地级 NameError 风险：%s' % ', '.join(uniq))
        return 1
    print('✅ 全部自由变量均可在生产模块级作用域解析（无落地 NameError 风险）')
    return 0


# ──────────────────────────────────────────────────────────────────────
# 变异自测：跑绿不算数。证明本 checker 抓得到真缺陷、且不误报。
# ──────────────────────────────────────────────────────────────────────
SELFTEST_MUTANTS = [
    # (key, 说明, 追加到 helper 末尾的代码, 期望 rc, 必须出现在输出里的符号)
    ('V0', '对照组：无害注释（不引入任何新符号）',
     '\n\n# mutation-control: no-op\n', 0, None),
    ('V1', '引用不存在的模块级符号',
     '\n\ndef _mutation_probe():\n    return _no_such_symbol_xyz\n',
     1, '_no_such_symbol_xyz'),
    ('V3', '引用假 dunder（守 MODULE_DUNDERS 白名单后门）',
     '\n\ndef _mutation_probe():\n    return __no_such_dunder__\n',
     1, '__no_such_dunder__'),
    ('V4', '同名局部变量（应视为已绑定，不误报）',
     '\n\ndef _mutation_probe():\n'
     '    _no_such_symbol_xyz = 1\n'
     '    return _no_such_symbol_xyz\n',
     0, None),
]


def self_test():
    print('=' * 72)
    print('变异自测：证明「零自由变量」不是假绿')
    print('=' * 72)
    helper_src = open(DEFAULT_HELPER, encoding='utf-8').read()
    ok_all = True
    with tempfile.TemporaryDirectory() as work:
        for key, desc, append, want_rc, must_name in SELFTEST_MUTANTS:
            path = os.path.join(work, 'h_%s.py' % key)
            open(path, 'w', encoding='utf-8').write(helper_src + append)
            env = dict(os.environ, SCAN_HELPER_OVERRIDE=path.replace('\\', '/'))
            p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace', env=env)
            out = (p.stdout or '') + (p.stderr or '')
            ok = (p.returncode == want_rc) and (not must_name or must_name in out)
            print('  %s %-4s rc=%d（期望 %d）  %s'
                  % ('✅' if ok else '❌', key, p.returncode, want_rc, desc))
            if not ok:
                ok_all = False

        # 文档侧变异体（合成最小文档，避开锚点漂移）
        doc_path = os.path.join(work, 'doc_v2.md')
        open(doc_path, 'w', encoding='utf-8').write(
            '# probe\n\n```python\n'
            'def _after_probe(self):\n'
            '    return _undefined_call_xyz()\n'
            '```\n')
        env = dict(os.environ, SCAN_DOC_OVERRIDE=doc_path.replace('\\', '/'))
        p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', env=env)
        out = (p.stdout or '') + (p.stderr or '')
        ok = (p.returncode == 1) and ('_undefined_call_xyz' in out)
        print('  %s %-4s rc=%d（期望 1）  文档 AFTER 块里的未定义调用'
              % ('✅' if ok else '❌', 'V2', p.returncode))
        if not ok:
            ok_all = False

    print('-' * 72)
    if not ok_all:
        print('❌ 变异自测失败：本 checker 存在盲区或误报')
        return 1
    print('✅ 变异自测通过：V0 基线 rc=0；V1/V2/V3 被抓（rc=1）；V4 不误报')
    return 0


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
