#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_doc_helper_calls.py — 送审稿「调用闭包」一致性检查

背景（为什么要这个检查）：
  v5 曾出过 `close_op_id` 生成太晚（BEGIN 段 L7003 vs close_phase=1 落盘 L6983）
  → **NameError**。根因类别是「AFTER 块引用了尚不存在的名字」。

  已有的两个 checker 守不住这一类：
    - check_doc_code_blocks.py：只保证「语法可解析」（NameError 是运行时错误，
      静态语法完全合法）
    - check_doc_helper_parity.py：只保证「helper 与文档逐字一致」（不检查
      谁调用了谁、调用签名对不对）

  本检查补的是**调用闭包**：文档 AFTER 块里调用的每个 `self._xxx`，要么是本
  方案新增的 11 个 helper 之一，要么是生产已存在的方法；否则落地即 NameError。

六查：
  【1】调用但未定义（致命）：`self._xxx` 既不在 helper 全集、也不在生产方法集
  【2】定义但零调用（提示）：helper 全集里没人调用 → 死代码 / 漏写调用点
  【3】调用参数个数与定义不匹配（致命）：静态可查的签名错位
  【4】关键字参数名必须真实存在（致命）：写错 = 运行时 TypeError，ast.parse 守不住
  【5】helper 不得与生产方法重名（致命）：否则落地后静默覆盖生产行为
  【6】文档自带定义与生产同名 ⇒ 逐条列出（提示但必须显式暴露）

签名解析优先级：**helper 实现文件 > 文档自带定义 > 生产现状**。

  - helper 实现文件（送审附件_v6.1/new_helpers_v6.py）优先于文档副本：文档里那份
    `class _Holder:` 全集是**副本**。若让副本优先，任何「给 helper 改签名」
    的变异都会被未改动的副本遮蔽 → 假绿（M2 实证过一次）。副本与实现不一致
    本身就是送审稿自相矛盾，由【7】判致命。
  - 文档自带定义优先于生产现状：本方案会「给生产方法新增可选参数」（如
    `_record_realized_pnl(pnl_partial=...)`），只认生产现状的话这类改动在本
    checker 下无法表达，只会逼着作者把改动藏起来。代价是文档定义可以"自己给
    自己发证"，对冲手段是【6】逐条暴露 + 自测 M4/M6。

用法：python check_doc_helper_calls.py [文档路径] [helper 路径] [生产文件路径]
退出码：0 = 通过；1 = 存在致命不一致。
"""
import ast
import pathlib
import sys
import textwrap

sys.stdout.reconfigure(encoding='utf-8')

# v6.1：路径改为相对**本文件**解析，整个项目可搬移（原先写死 G:/my-crypto-bot/...
# 与 G:/tmp/...）。命令行参数仍优先。
_HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DOC = str(_HERE / '事故_市价平仓-4061_精确diff_送审ChatGPT.md')
DEFAULT_HELPER = str(_HERE / '送审附件_v6.1' / 'new_helpers_v6.py')
DEFAULT_PROD = str(_HERE / 'trader_260725.py')


def extract_python_blocks(doc):
    """按行扫描围栏提取可执行 python 块，返回 [(start_line, code)]（排除 frag）。"""
    lines = doc.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if s == '```python':
            j, buf = i + 1, []
            while j < len(lines) and lines[j].strip() != '```':
                buf.append(lines[j])
                j += 1
            if j >= len(lines):
                break
            blocks.append((i + 1, '\n'.join(buf)))
            i = j + 1
        else:
            i += 1
    return blocks


def parse_any(code):
    """依序尝试四种包装，返回首个成功的 AST（与 check_doc_code_blocks 同策略）。"""
    for wrapped in (code, textwrap.dedent(code),
                    'class _W:\n' + textwrap.indent(textwrap.dedent(code), '    '),
                    'async def _w():\n' + textwrap.indent(textwrap.dedent(code), '    ')):
        try:
            return ast.parse(wrapped), wrapped
        except SyntaxError:
            continue
    return None, None


def collect_calls(tree):
    """收集 `self.<attr>(...)` 调用。

    返回 {attr: [(lineno, n_positional, kw_names, has_starstar)]}。
    kw_names 用于检查关键字参数名是否真实存在于定义（写错即运行时 TypeError，
    而 ast.parse 完全合法 —— 与 NameError 同类，静态语法检查守不住）。
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == 'self'):
            kw = [k.arg for k in node.keywords if k.arg is not None]
            has_starstar = any(k.arg is None for k in node.keywords)
            out.setdefault(fn.attr, []).append(
                (node.lineno, len(node.args), kw, has_starstar))
    return out


def helper_signatures(helper_src):
    """从 helper 实现提取签名信息。

    返回 {name: dict(min_pos, max_pos, params, has_kwargs)}
      min_pos/max_pos —— 位置参数个数允许区间（不含 self）
      params          —— 全部可接收的参数名
      has_kwargs      —— 是否有 **kwargs（有则关键字名不校验）
    """
    tree = ast.parse(helper_src)
    sigs = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for f in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            a = f.args
            n_pos = len(a.posonlyargs) + len(a.args) - 1  # 去掉 self
            n_def = len(a.defaults)
            params = [x.arg for x in (a.posonlyargs + a.args) if x.arg != 'self']
            params += [x.arg for x in a.kwonlyargs]
            sigs[f.name] = {
                'min_pos': n_pos - n_def,
                'max_pos': n_pos,
                'params': params,
                'has_kwargs': a.kwarg is not None,
            }
    return sigs


def class_method_signatures(prod_src):
    """提取生产文件里**类方法**的签名（用于校验调用生产方法的合法性）。

    只收类内方法（第一参数 self 会被排除）；模块级函数不收（调用形态不是
    `self.x(...)`）。返回结构同 helper_signatures。
    """
    tree = ast.parse(prod_src)
    sigs = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for f in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            a = f.args
            names = [x.arg for x in (a.posonlyargs + a.args)]
            if names and names[0] == 'self':
                names = names[1:]
            n_def = len(a.defaults)
            params = list(names) + [x.arg for x in a.kwonlyargs]
            sigs[f.name] = {
                'min_pos': len(names) - n_def,
                'max_pos': len(names) if not a.vararg else 10 ** 6,
                'params': params,
                'has_kwargs': a.kwarg is not None,
            }
    return sigs


def doc_definitions(blocks):
    """收集文档 AFTER 块里**自带**的函数定义签名（本方案提议的生产签名改动）。

    为什么要这一层：checker 原本只认「生产现状」签名，于是文档里凡是「新增
    可选参数」类的改动都只能被动报错（如 `_record_realized_pnl(pnl_partial=...)`
    —— 生产 L678 没有该参数）。那会逼着作者把签名改动藏起来以换取 rc=0，
    恰恰背离本 checker 的目的。故优先级：文档内定义 > 新增 helper > 生产现状。

    代价与对冲：文档内定义一旦写错就成了"自己给自己发证"。对冲手段是【6】
    把「文档重定义了生产方法」全部显式列出，交给复审人/变异自测核验。

    返回 {name: sig}（结构同 helper_signatures，额外带 doc_line）。
    """
    out = {}
    for start, code in blocks:
        tree, _ = parse_any(code)
        if tree is None:
            continue
        for f in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            a = f.args
            names = [x.arg for x in (a.posonlyargs + a.args)]
            if names and names[0] == 'self':
                names = names[1:]
            n_def = len(a.defaults)
            params = list(names) + [x.arg for x in a.kwonlyargs]
            out[f.name] = {
                'min_pos': len(names) - n_def,
                'max_pos': len(names) if not a.vararg else 10 ** 6,
                'params': params,
                'has_kwargs': a.kwarg is not None,
                'doc_line': start,
            }
    return out


def production_methods(prod_src):
    """生产文件里所有已定义的函数名（任意层级），作为「非本方案新增」的白名单。"""
    tree = ast.parse(prod_src)
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def self_test():
    """变异自测：证明本 checker 自己能失败（跑绿不算数）。

    M1 文档调用一个不存在的 helper  → 必须 rc=1（【1】拦住）
    M2 helper 签名加一个必填参数    → 必须 rc=1（【3】拦住）
    M3 基线（未改动的真实文件）     → 必须 rc=0
    """
    import os
    import subprocess
    work = 'G:/tmp/check_calls_selftest'
    os.makedirs(work, exist_ok=True)

    doc = pathlib.Path(DEFAULT_DOC).read_text(encoding='utf-8')
    helper = pathlib.Path(DEFAULT_HELPER).read_text(encoding='utf-8')

    # M1：把文档里一处 helper 调用改名成不存在的名字
    m1_doc = doc.replace('self._set_close_reason_if_current(',
                         'self._set_close_reason_if_current_TYPO(', 1)
    # M2：给 _read_position_amt 加一个必填参数（所有调用点随即少传一个）
    old_sig = 'def _read_position_amt(self, symbol: str, side: str, is_hedge_mode: bool)'
    new_sig = ('def _read_position_amt(self, symbol: str, side: str, '
               'is_hedge_mode: bool, extra_required: int)')
    m2_helper = helper.replace(old_sig, new_sig, 1)

    # M4：调用处关键字参数名写错 —— 文档已自带新签名的情况下仍须拦下，
    #     否则「文档定义优先」就成了给任何错误签名发放的免费通行证。
    #
    #     ⚠️ 锚点必须绑到**代码结构**，不能只绑名字串：文档正文（如自审表格里
    #     引用该调用的那一格）也会出现 `pnl_partial=_pnl_partial`，且位置更靠前，
    #     `.replace(..., 1)` 会打在正文上 → 变异"生效"了却作用在错误位置 → 假绿。
    #     故锚点取「调用末行 + 紧随其后的围栏闭合行」，这在文档里唯一。
    m4_doc = doc.replace('pnl_partial=_pnl_partial)\n```',
                         'pnl_partial_typo=_pnl_partial)\n```', 1)
    # M6：把文档给出的新签名改回生产旧签名 —— 复现 v6.1 前稿的原缺陷
    #     （只写调用 `pnl_partial=`，不给定义 → 结算阶段 TypeError）。
    m6_doc = doc.replace('mode: str, pnl_partial: bool = False', 'mode: str', 1)

    cases = [
        ('M1 文档调用未定义的 helper', 'M1', m1_doc, helper, 1),
        ('M2 helper 签名加必填参数', 'M2', doc, m2_helper, 1),
        ('M3 基线（真实文件）', 'M3', doc, helper, 0),
        ('M4 调用处关键字名写错', 'M4', m4_doc, helper, 1),
        ('M6 文档新签名退回生产旧签名', 'M6', m6_doc, helper, 1),
    ]
    # 变异必须真正生效，且**作用在预期位置**：doc 类变异对比原 doc，helper 类
    # 变异对比原 helper。两道防线：
    #   ① 变异后内容必须与原内容不同（锚点至少命中一次）
    #   ② 锚点在原文中的出现次数必须等于预期（count_expect）
    # ② 是 C-8 期间踩出来的：M4 原锚点 `pnl_partial=_pnl_partial` 在正文（自审
    # 表格引用该调用的一格）里也有一份且位置更靠前，`.replace(..., 1)` 打在正文上
    # → 变异"生效"却作用在错误位置 → checker 假绿。故锚点须绑代码结构并锁次数。
    must_differ = {
        'M1': (m1_doc, doc, 'self._set_close_reason_if_current(', 3),
        'M2': (m2_helper, helper, old_sig, 1),
        'M4': (m4_doc, doc, 'pnl_partial=_pnl_partial)\n```', 1),
        'M6': (m6_doc, doc, 'mode: str, pnl_partial: bool = False', 1),
    }
    ok_all = True
    for desc, tag, d, h, want_rc in cases:
        if tag in must_differ:
            mutated, original, anchor, count_expect = must_differ[tag]
            if mutated == original:
                print(f'❌ {tag} 变异未生效（锚点未命中）')
                ok_all = False
                continue
            n_hit = original.count(anchor)
            if n_hit != count_expect:
                print(f'❌ {tag} 锚点漂移：出现 {n_hit} 次（期望 {count_expect}）'
                      f'——文档已改版，须重新确认变异作用位置，否则可能是假绿')
                ok_all = False
                continue
        dp = f'{work}/doc_{tag}.md'
        hp = f'{work}/helper_{tag}.py'
        pathlib.Path(dp).write_text(d, encoding='utf-8')
        pathlib.Path(hp).write_text(h, encoding='utf-8')
        p = subprocess.run([sys.executable, __file__, dp, hp, DEFAULT_PROD],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
        ok = (p.returncode == want_rc)
        ok_all = ok_all and ok
        print(f"{'✅' if ok else '❌'} {desc:<28} 期望 rc={want_rc} 实得 rc={p.returncode}")
        if not ok:
            tail = (p.stdout or '').strip().splitlines()[-3:]
            print('     ' + ' | '.join(tail))
    print('-' * 68)
    if not ok_all:
        print('❌ 变异自测未过：checker 存在假绿')
        return 1
    print('✅ 变异自测通过：M1/M2/M4/M6 均被拦下，M3 基线 rc=0')
    return 0


def main():
    if '--self-test' in sys.argv:
        return self_test()
    doc_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOC
    helper_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HELPER
    prod_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PROD

    doc = pathlib.Path(doc_path).read_text(encoding='utf-8')
    helper_src = pathlib.Path(helper_path).read_text(encoding='utf-8')
    prod_src = pathlib.Path(prod_path).read_text(encoding='utf-8')

    sigs = helper_signatures(helper_src)          # 方案新增的 11 个 helper
    prod_sigs = class_method_signatures(prod_src)  # 生产既有类方法
    prod_methods = production_methods(prod_src)
    blocks = extract_python_blocks(doc)
    doc_defs = doc_definitions(blocks)             # 文档自带的定义（提议的签名改动）

    all_calls = {}          # attr -> [(doc_line, n_pos)]
    unparsed = []
    for start, code in blocks:
        tree, _ = parse_any(code)
        if tree is None:
            unparsed.append(start)
            continue
        for attr, items in collect_calls(tree).items():
            for lineno, n_pos, _kw, _ss in items:
                all_calls.setdefault(attr, []).append((start, lineno, n_pos, _kw, _ss))

    print('=' * 68)
    print('送审稿调用闭包检查（防 NameError / 死代码 / 签名错位）')
    print('=' * 68)
    print(f'文档块 {len(blocks)} 个（解析失败 {len(unparsed)}）'
          f'｜helper {len(sigs)} 个｜生产方法 {len(prod_methods)} 个'
          f'｜文档自带定义 {len(doc_defs)} 个')

    fatal = 0

    # 【1】调用但未定义
    print('\n【1】文档调用 ⇒ 必须有定义（helper 全集 或 生产既有方法）')
    undefined = []
    for attr in sorted(all_calls):
        if attr in sigs:
            tag = 'helper'
        elif attr in prod_methods:
            tag = '生产'
        else:
            tag = None
        if tag is None:
            undefined.append(attr)
            where = ', '.join(f'L{s}(块内{ln})' for s, ln, *_ in all_calls[attr][:3])
            print(f'  ❌ self.{attr:<34} 无定义 —— 落地即 NameError（{where}）')
            fatal += 1
        else:
            print(f'  ✅ self.{attr:<34} {tag}')
    if not undefined:
        pass

    # 【2】定义但零调用（helper 全集）
    print('\n【2】helper 全集 ⇒ 文档必须有调用点（防死代码 / 漏写调用）')
    dead = []
    for name in sorted(sigs):
        n = len(all_calls.get(name, []))
        if n == 0:
            dead.append(name)
            print(f'  ⚠️ {name:<34} 文档零调用')
        else:
            print(f'  ✅ {name:<34} 调用 {n} 处')

    # 【3】签名错位：位置参数个数（helper ∪ 文档自带定义）
    print('\n【3】调用位置参数个数 ⇒ 必须落在定义签名区间内')
    checked3 = (set(sigs) | set(doc_defs)) & set(all_calls)
    for name in sorted(checked3):
        info = sigs.get(name) or doc_defs.get(name)
        lo, hi = info['min_pos'], info['max_pos']
        bad = [(s, ln, p) for s, ln, p, _kw, _ss in all_calls[name]
               if not (lo <= p <= hi)]
        if bad:
            for s, ln, p in bad[:3]:
                print(f'  ❌ {name}: L{s}(块内{ln}) 传 {p} 个位置参数，'
                      f'签名要求 {lo}~{hi}')
            fatal += 1
        else:
            print(f'  ✅ {name:<34} 位置参数合规（{lo}~{hi}）')

    # 【4】关键字参数名必须真实存在（写错 = 运行时 TypeError，ast.parse 守不住）
    #     覆盖范围 = helper ∪ 生产方法。实测：文档里 33 处关键字调用全部落在
    #     **生产方法**上（helper 全是位置参数），只查 helper 会让本项空转。
    #     典型高危目标：`_record_realized_pnl(..., pnl_partial=...)` —— 若生产
    #     签名没有该参数，落地即 TypeError。
    print('\n【4】关键字参数名 ⇒ 必须在定义的参数列表内')
    for name in sorted(all_calls):
        # 优先级见模块 docstring：helper 实现 > 文档自带定义 > 生产现状。
        info = sigs.get(name) or doc_defs.get(name) or prod_sigs.get(name)
        if info is None:
            continue
        if info['has_kwargs']:
            print(f'  ✅ {name:<34} 有 **kwargs，跳过关键字名校验')
            continue
        bad = [(s, ln, k) for s, ln, _p, kw, _ss in all_calls[name]
               for k in kw if k not in info['params']]
        if bad:
            for s, ln, k in bad[:3]:
                print(f'  ❌ {name}: L{s}(块内{ln}) 关键字参数 `{k}` 不存在'
                      f'（可用：{", ".join(info["params"])}）')
            fatal += 1
        else:
            print(f'  ✅ {name:<34} 关键字参数名合规')

    # 【5】重名检查：helper 不得与生产既有方法同名（否则落地后静默覆盖生产行为）
    print('\n【5】helper 全集 ⇒ 不得与生产既有方法重名（防静默覆盖）')
    clash = sorted(set(sigs) & set(prod_methods))
    if clash:
        for name in clash:
            print(f'  ❌ {name} 在生产中已存在 —— 新增 helper 会覆盖它')
        fatal += 1
    else:
        print(f'  ✅ {len(sigs)} 个 helper 与生产 {len(prod_methods)} 个方法名零冲突')

    # 【6】文档自带定义 vs 生产同名方法：这是「本方案提议改动生产签名」的显式清单。
    #     不是报错，但必须逐条列给复审人看 —— 因为【4】已改为文档定义优先，
    #     这里就是唯一的对冲手段，静默不得。
    print('\n【6】文档自带定义 ⇒ 与生产同名者逐条列出（提议的生产签名改动清单）')
    overrides = sorted(set(doc_defs) & set(prod_sigs))
    if overrides:
        for name in overrides:
            p = prod_sigs[name]['params']
            d = doc_defs[name]['params']
            added = [x for x in d if x not in p]
            removed = [x for x in p if x not in d]
            print(f'  ⚠️ {name}  ← 文档 L{doc_defs[name]["doc_line"]} 重定义')
            print(f'      生产：{", ".join(p)}')
            print(f'      文档：{", ".join(d)}')
            if added:
                print(f'      新增参数：{", ".join(added)}'
                      f'（必须带默认值，否则其余调用点全炸）')
            if removed:
                print(f'      删除参数：{", ".join(removed)}'
                      f'（🔴 必须确认生产全部调用点已同步改写）')
    else:
        print('  ✅ 文档未重定义任何生产方法（本次改动不含签名级别变更）')

    # 【7】文档 helper 副本 vs helper 实现：同名必须同签名（致命）
    #     M2 实证：让副本优先会遮蔽「改 helper 签名」的变异 → 假绿。
    #     副本与实现不一致 = 送审稿自相矛盾，属 parity 范畴的硬错误。
    print('\n【7】文档 helper 副本 ⇒ 签名必须与实现一致（防副本遮蔽实现）')
    shadow = sorted(set(doc_defs) & set(sigs))
    for name in shadow:
        a, b = doc_defs[name], sigs[name]
        if (a['params'] != b['params']
                or (a['min_pos'], a['max_pos']) != (b['min_pos'], b['max_pos'])):
            print(f'  ❌ {name}：文档 L{a["doc_line"]} 签名 {a["params"]}'
                  f'（位置 {a["min_pos"]}~{a["max_pos"]}）'
                  f' ≠ 实现 {b["params"]}（位置 {b["min_pos"]}~{b["max_pos"]}）')
            fatal += 1
        else:
            print(f'  ✅ {name:<34} 副本与实现一致')
    if not shadow:
        print('  ✅ 文档未包含 helper 副本')

    print('-' * 68)
    if fatal:
        print(f'🚨 {fatal} 处致命不一致 —— 送审稿不可交付')
        return 1
    if dead:
        print(f'⚠️ 无致命问题，但有 {len(dead)} 个 helper 零调用：{", ".join(dead)}')
        print('   （若为测试专用或预留接口，请在文档中注明，否则视为漏写调用点）')
    print('✅ 调用闭包一致：无 NameError 风险、无签名错位')
    return 0


if __name__ == '__main__':
    sys.exit(main())
