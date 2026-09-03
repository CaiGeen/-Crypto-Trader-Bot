"""
AST 守卫：allow_flag_rollback=True 的调用点必须恰好 2 处（ChatGPT 2026-08-29 §四）。

背景：_merge_batch_state 的 A 类棘轮是安全设计（安全状态不能被旧快照降级）。
2026-08-29 -4061 事故暴露：平仓失败回滚需要合法的逆向迁移通道，故引入
allow_flag_rollback。但该参数**绝不能成为通用降级开关**——一旦泛滥，
安全棘轮形同虚设。

守卫目标（v1.1 按 ChatGPT §十二 升级）：
  原目标 = “检查两个合法调用点是否存在”
  新目标 = **全库禁止出现任何显式 / 动态的 allow_flag_rollback 绕过形式**

检查项（编号与运行时输出 ①②③ / 判定表 ①-⑥ 一一对应）：
  ① 具名关键字调用恰好 2 处
  ② 2 处分别位于 close_position_market / close_position_limit 内
  ③ 值必须是字面量 True（禁止传变量规避）
  ④ 被调用方必须是 save_batch_state
  ⑤ 禁止 `**{'allow_flag_rollback': True}` 动态展开形式（kw.arg is None）
  ⑥ 禁止字符串常量 'allow_flag_rollback' 散落在别处（如拼装 params 字典）

用法：
    python test_ast_rollback_guard.py                 # 校验生产文件 trader_260725.py
    python test_ast_rollback_guard.py <file.py>       # 负向对照：喂入改造过的副本

当前状态（改动尚未落地）：
    对生产文件跑 → rc=1，且失败原因必须打印为「改动尚未落地（0 处调用）」。
    改动落地后必须转 rc=0。

⚠️ 负向对照是强制的（项目纪律：断言必须证明它会在回归时失败）：
    G:/tmp/make_guard_fixtures.py 生成 8 个样本，期望：
      A 原样 rc=1（未落地）| B 合规 rc=0 | C 越界（3 处）rc=1 | D 函数名错 rc=1
      E `**_kw` 变量展开 rc=1 | E2 `**{...}` 内联展开 rc=1 | F 常量散落 rc=1
      G 非字面量 rc=1

离线：零网络 / 零 API / 零写盘。
⚠️ 本文件为离线验证工具，非生产代码；工作树出现对它的修改不构成生产变更。
"""
import ast
import pathlib
import sys

DEFAULT_TARGET = pathlib.Path(__file__).parent / 'trader_260725.py'
EXPECTED_FUNCS = {'close_position_market', 'close_position_limit'}
EXPECTED_CALLEE = 'save_batch_state'
EXPECTED_COUNT = 2
PARAM_NAME = 'allow_flag_rollback'


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return '<unknown>'


def _dict_has_param(node) -> bool:
    """`**{'allow_flag_rollback': True}` → keyword(arg=None, value=Dict)。"""
    if not isinstance(node, ast.Dict):
        return False
    for k in node.keys:
        if isinstance(k, ast.Constant) and k.value == PARAM_NAME:
            return True
    return False


def collect(tree):
    """返回 (named_hits, dynamic_hits, stray_constants)。
    named_hits   = [(函数名, 行号, callee, 是否字面量True)]
    dynamic_hits = [(函数名, 行号)]   ← **展开形式
    stray        = [行号]             ← 字符串常量散落

    ⚠️ 归属规则（交叉审查 C 的 d2 高危项，v1.2 修正）：
    每个 Call 只归属**最近的** FunctionDef 祖先，不是所有祖先。
    旧版用 `for node in walk(tree): if FunctionDef: for sub in walk(node)`，
    嵌套函数里的同一个调用会被外层函数**重复计数** —— 于是
    「嵌套一个同名 close_position_limit，内部放 1 个真实调用点」
    就能凑出 2 处，让「数量恰为 2」检查形同虚设。
    """
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}

    def nearest_func(node):
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(cur)
        return None

    named, dynamic = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = nearest_func(node)
        fname = fn.name if fn is not None else '<module>'
        callee = _callee_name(node)
        for kw in node.keywords:
            if kw.arg == PARAM_NAME:
                literal_true = (isinstance(kw.value, ast.Constant)
                                and kw.value.value is True)
                named.append((fname, node.lineno, callee, literal_true))
            elif kw.arg is None and _dict_has_param(kw.value):
                dynamic.append((fname, node.lineno))
    stray = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == PARAM_NAME]
    return (sorted(named, key=lambda x: x[1]),
            sorted(dynamic, key=lambda x: x[1]),
            sorted(stray))


def main():
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET
    tree = ast.parse(target.read_text(encoding='utf-8'))
    named, dynamic, stray = collect(tree)

    print(f"目标文件: {target}")
    print(f"① 具名关键字调用：{len(named)} 处（期望 {EXPECTED_COUNT}）")
    for f, ln, callee, lit in named:
        flag = '' if lit else '  ← ⚠️ 非字面量 True（规避检查）'
        cal = '' if callee == EXPECTED_CALLEE else f'  ← ⚠️ callee={callee}'
        print(f"  L{ln}  in {f}() -> {callee}(){flag}{cal}")
    print(f"② 动态 **展开形式：{len(dynamic)} 处（期望 0）")
    for f, ln in dynamic:
        print(f"  L{ln}  in {f}()  ← 🚨 可绕过 kw.arg 检查")
    print(f"③ 字符串常量 'allow_flag_rollback' 散落：{len(stray)} 处（期望 0）")
    for ln in stray:
        print(f"  L{ln}  ← 🚨 常量出现在非关键字位置")

    funcs = {f for f, _, _, _ in named}
    ok_count = len(named) == EXPECTED_COUNT
    ok_funcs = funcs == EXPECTED_FUNCS
    ok_literal = all(lit for _, _, _, lit in named)
    ok_callee = all(c == EXPECTED_CALLEE for _, _, c, _ in named)
    ok_dynamic = len(dynamic) == 0
    ok_stray = len(stray) == 0

    print()
    print(f"  数量恰为 {EXPECTED_COUNT}                          : {ok_count}")
    print(f"  函数名恰为 {sorted(EXPECTED_FUNCS)} : {ok_funcs}")
    print(f"  全部为字面量 True                    : {ok_literal}")
    print(f"  callee 全为 {EXPECTED_CALLEE}               : {ok_callee}")
    print(f"  无 **展开绕过形式                    : {ok_dynamic}")
    print(f"  无字符串常量散落                     : {ok_stray}")

    if all((ok_count, ok_funcs, ok_literal, ok_callee, ok_dynamic, ok_stray)):
        print("\n✅ AST 守卫通过：受控逆向迁移未扩散，且无动态绕过形式")
        return 0

    # 失败原因必须与实际情况一致（ChatGPT 2026-08-29 必须修 2 的同一条纪律）：
    # 「尚未落地（0 处）」和「越界 / 形式不合规」是性质完全不同的两件事，
    # 不能混用一句「使用范围越界或形式不合规」——那会让 RED 的含义失真。
    reasons = []
    if len(named) == 0:
        reasons.append('改动尚未落地：全库 0 处调用（期望 2 处）。'
                       '这是落地前的预期 RED，不是缺陷，落地后应转 rc=0')
    elif not ok_count:
        reasons.append(f'调用点数量 {len(named)} ≠ 期望 {EXPECTED_COUNT}（使用范围越界）')
    if named and not ok_funcs:
        reasons.append(f'调用点所在函数 {sorted(funcs)} ≠ 白名单 '
                       f'{sorted(EXPECTED_FUNCS)}（位置越界）')
    if not ok_literal:
        reasons.append('存在非字面量 True（变量传参，规避字面量检查）')
    if not ok_callee:
        reasons.append(f'存在 callee ≠ {EXPECTED_CALLEE} 的调用点')
    if not ok_dynamic:
        reasons.append('存在 `**{...}` 动态展开形式，可绕过 kw.arg 检查')
    if not ok_stray:
        reasons.append(f"字符串常量 '{PARAM_NAME}' 出现在非关键字位置")

    print("\n🚨 AST 守卫失败，禁止上线。失败原因：")
    for r in reasons:
        print(f"   - {r}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
