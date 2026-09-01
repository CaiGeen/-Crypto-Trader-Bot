# -*- coding: utf-8 -*-
"""check_doc_code_blocks.py — 送审文档代码块机器检查（ChatGPT 终审 §六 + §七 要求）

背景：
  §六：v3 送审稿的 AFTER 代码块里有重复粘贴（`order = self._safe_api_call(` 连续
  两次），贴出去直接语法错误——而稿头声称「可直接套用的完整代码」。这类机械错误
  不应再靠人眼，所以：**文档里每个 python 代码块都必须能过 ast.parse**。

  §七（v5 新增）：`ast.parse == 语法合法` **不等于** `diff 可直接应用`。
  `...`（Ellipsis）在 Python 里是合法语法，所以只做语法检查时占位块会 GREEN，
  但把 AFTER 整块贴进生产反而会破坏生产。因此 v5 增加**占位检测**：

      python      = 可执行完整替换块，**不得含任何 `...` 占位**
      python-frag = 人工 diff 片段 / 源码截断摘录，豁免全部检查

解析策略（依序尝试，记录命中策略）：
  1. 原文直接 parse（模块级代码）
  2. textwrap.dedent 后 parse（方法级缩进片段）
  3. 包一层 class 后 parse（方法定义片段）
  4. 包一层 async def 后 parse（await/async 片段）
四种全失败 → 该块语法错误，rc=1。

占位检测：解析成功后 `ast.walk` 找 `ast.Ellipsis` 节点 → 命中即失败。
注意：docstring 里出现的 `...` 是字符串内容，不是 AST 节点，不会被误报。

用法：python check_doc_code_blocks.py [文档路径]
      缺省 = 事故_市价平仓-4061_精确diff_送审ChatGPT.md
"""
import ast
import pathlib
import sys
import textwrap

sys.stdout.reconfigure(encoding='utf-8')

# v6.1：改为相对**本文件**解析，整个项目可搬移（原先写死 G:/my-crypto-bot/...）。
# 命令行参数仍优先，便于拿同一份 checker 去查别的文档。
DEFAULT_DOC = str(pathlib.Path(__file__).resolve().parent
                  / '事故_市价平仓-4061_精确diff_送审ChatGPT.md')


def find_ellipsis(code: str):
    """返回块中 Ellipsis 占位所在行号列表（相对块内行号，1-based）。"""
    for wrapped in [
        code,
        textwrap.dedent(code),
        'class _W:\n' + textwrap.indent(textwrap.dedent(code), '    '),
        'async def _w():\n' + textwrap.indent(textwrap.dedent(code), '    '),
    ]:
        try:
            tree = ast.parse(wrapped)
        except SyntaxError:
            continue
        hits = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Ellipsis)]
        return hits
    return []


def parse_block(code: str):
    """返回 (ok, strategy, err)。"""
    err = None
    for name, wrapped in [
        ('raw', code),
        ('dedent', textwrap.dedent(code)),
        ('class', 'class _W:\n' + textwrap.indent(textwrap.dedent(code), '    ')),
        ('asyncdef', 'async def _w():\n' + textwrap.indent(textwrap.dedent(code), '    ')),
    ]:
        try:
            ast.parse(wrapped)
            return True, name, None
        except SyntaxError as e:
            err = f"line {e.lineno}: {e.msg}: {e.text.strip() if e.text else ''}"
    return False, None, err


def main():
    doc_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOC
    doc = pathlib.Path(doc_path).read_text(encoding='utf-8')

    # 按行扫描围栏，保留行号（比正则更稳：围栏内若出现 ```python 字样也不误切）
    # python-frag = 源码原文截断摘录（带行号前缀 / try 截断），仅人工阅读，
    # 豁免 ast.parse——但围栏必须闭合，数量计入统计。
    lines = doc.splitlines()
    blocks = []  # (start_line, code, is_frag)
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == '```python' or s == '```python-frag':
            is_frag = s.endswith('-frag')
            j = i + 1
            buf = []
            while j < len(lines) and lines[j].strip() != '```':
                buf.append(lines[j])
                j += 1
            if j >= len(lines):
                print(f'🚨 未闭合的 python 围栏（起于 L{i + 1}）')
                return 1
            blocks.append((i + 1, '\n'.join(buf), is_frag))
            i = j + 1
        else:
            i += 1

    fails = 0
    n_checked = 0
    n_frag = 0
    print(f'共发现 {len(blocks)} 个 python 代码块')
    for start, code, is_frag in blocks:
        first = code.strip().splitlines()[0][:58] if code.strip() else '(空)'
        if is_frag:
            n_frag += 1
            print(f'  ⏭️ L{start:<5} [frag    ] {first}')
            continue
        n_checked += 1
        ok, strategy, err = parse_block(code)
        if not ok:
            fails += 1
            print(f'  ❌ L{start:<5} 语法错误（{err}）')
            print(f'       首行: {first}')
            continue
        # v5（§七）：语法合法 ≠ 可直接套用。`...` 占位会让整块贴进生产时
        # 把真实逻辑替换成 Ellipsis —— 必须一并拦住。
        ell = find_ellipsis(code)
        if ell:
            fails += 1
            print(f'  ❌ L{start:<5} 含 Ellipsis 占位（块内行 {ell[:5]}）'
                  f'——占位块不得标为「可直接套用」，请改为 python-frag 或补全代码')
            print(f'       首行: {first}')
            continue
        print(f'  ✅ L{start:<5} [{strategy:<8}] {first}')
    print('=' * 60)
    if fails:
        print(f'🚨 {fails}/{n_checked} 个可执行代码块未通过'
              f'（语法错误 或 含 `...` 占位）——送审稿不可交付'
              f'（另有 {n_frag} 个 frag 摘录块豁免）')
        return 1
    print(f'✅ {n_checked}/{n_checked} 个可执行代码块全部通过'
          f'（ast.parse + 无 `...` 占位）'
          f'（另有 {n_frag} 个 frag 摘录块豁免）——送审稿可交付')
    return 0


if __name__ == '__main__':
    sys.exit(main())
