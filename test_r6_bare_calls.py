# -*- coding: utf-8 -*-
"""
C1/R6 裸调用收编离线验收测试（AST 结构级断言，不连交易所/Telegram）

契约（ChatGPT 锁定的三件事）：
  1: bot_runner 中所有 fetch_ticker 必须以"函数引用"形式传入 trader._safe_api_call
     （AST 语义：禁止任何 fetch_ticker 的直接调用节点）
  2: trader -1021 分支的 load_time_difference 必须位于 _api_semaphore 保护范围内
  3: -1021 分支内禁止嵌套调用 _safe_api_call（防递归：sync 失败→-1021→sync→...）

用法: .venv\\Scripts\\python.exe test_r6_bare_calls.py
"""
import ast
import sys

BOT = "bot_runner.py"
TRD = "trader_260725.py"
RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def parse(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read(), ast.parse(f.read())


def build_parents(tree):
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def ancestors(node, parents):
    p = parents.get(node)
    while p is not None:
        yield p
        p = parents.get(p)


def scenario_1():
    """1: bot_runner 全部 fetch_ticker 必须经 _safe_api_call（无直接调用节点）"""
    with open(BOT, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    bare = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'fetch_ticker']
    report("场景1: bot_runner 无 fetch_ticker 裸调用", not bare,
           f"(裸调用行号: {bare})")


def find_sync_call():
    """定位 trader 中 load_time_difference 的直接调用节点（非引用传参）"""
    with open(TRD, 'r', encoding='utf-8') as f:
        src, tree = f.read(), None
    with open(TRD, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    parents = build_parents(tree)
    direct = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == 'load_time_difference']
    return src, tree, parents, direct


def scenario_2():
    """2: -1021 分支 load_time_difference 必须在 _api_semaphore With 块内"""
    src, tree, parents, direct = find_sync_call()
    ok = len(direct) == 1
    detail = f"(直接调用数: {len(direct)}，期望恰好 1)"
    if ok:
        node = direct[0]
        in_sem = False
        for anc in ancestors(node, parents):
            if isinstance(anc, ast.With):
                for item in anc.items:
                    ce = item.context_expr
                    if isinstance(ce, ast.Attribute) and ce.attr == '_api_semaphore':
                        in_sem = True
        ok = in_sem
        detail += f"，位于 semaphore 保护: {in_sem}"
    report("场景2: load_time_difference 在 _api_semaphore 保护内", ok, detail)


def scenario_3():
    """3: -1021 分支内禁止嵌套 _safe_api_call（防 sync 失败递归）"""
    src, tree, parents, direct = find_sync_call()
    ok = len(direct) == 1
    detail = ""
    if ok:
        node = direct[0]
        # 向上找最近的 If 祖先（-1021 分支）
        branch = None
        for anc in ancestors(node, parents):
            if isinstance(anc, ast.If):
                branch = anc
                break
        nested = []
        if branch is not None:
            for n in ast.walk(branch):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == '_safe_api_call'):
                    nested.append(n.lineno)
        ok = branch is not None and not nested
        detail = f"(-1021 If 块内 _safe_api_call 调用: {nested}，期望空)"
    report("场景3: -1021 分支禁嵌套 _safe_api_call", ok, detail)


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("C1/R6 裸调用收编结构验收完成")
