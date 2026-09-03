"""
_merge_batch_state 回滚语义测试（ChatGPT 2026-08-29 §十五 要求新增）。

核心命题（一次证明到位）：
    allow_flag_rollback=True **不是关闭整个安全棘轮**，
    而只是打开「本次 close 操作设置的临时状态」的回滚能力；
    代表已发生事实的字段（settled_by_limit_close）绝不降级。

方法（沿用项目已验证的纪律）：
    ast 从目标文件**原样提取** _merge_batch_state，配合从生产文件提取的
    模块级常量表，在隔离 namespace 执行 —— 被测对象是源码本身，不是手抄副本。
    常量一律取自生产 trader_260725.py（本次 diff 不改常量）。

用法：
    python test_merge_rollback_semantics.py                       # 校验生产文件
    python test_merge_rollback_semantics.py G:/tmp/merge_after.py # 校验提议实现

预期：
    生产原样（改动未落地） → rc=1  ← 预期的 RED
    merge_after.py          → rc=0
    merge_after_broken.py   → rc=1  ← 负向对照，证明断言真的会失败

离线：零网络 / 零 API / 零写盘。
⚠️ 本文件为离线验证工具，非生产代码；工作树出现对它的修改不构成生产变更。
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
PROD = HERE / 'trader_260725.py'
FUNC_NAME = '_merge_batch_state'
REQUIRED_CONSTS = ('_MERGE_RATCHET_BOOL_FIELDS', '_MERGE_ID_MIRROR_FIELDS')


def extract_constants(path):
    """抽取模块级字面量赋值（模块级常量表）。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        ns[t.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        pass
    return ns


def extract_func(path, name):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise SystemExit(f"❌ 目标文件中未找到函数 {name}: {path}")
    return ast.get_source_segment(src, node)


class _Self:
    pass


def build(func_path):
    ns = extract_constants(PROD)
    missing = [c for c in REQUIRED_CONSTS if c not in ns]
    if missing:
        raise SystemExit(f"❌ 生产文件缺少必需常量: {missing}")
    exec(extract_func(func_path, FUNC_NAME), ns)
    fn = ns[FUNC_NAME]

    def call(disk, snap, allow=False):
        return fn(_Self(), dict(disk), dict(snap), allow_flag_rollback=allow)

    return call, ns


def main():
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else PROD
    call, ns = build(target)

    print(f"目标实现: {target}")
    print(f"常量来源: {PROD}")
    print(f"_MERGE_RATCHET_BOOL_FIELDS = {ns['_MERGE_RATCHET_BOOL_FIELDS']}")
    print()

    # 用 AST 查签名，而不是靠 TypeError 崩出来 —— 未落地时给出可读的 RED 结论
    src = target.read_text(encoding='utf-8')
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == FUNC_NAME)
    has_param = any(a.arg == 'allow_flag_rollback' for a in node.args.kwonlyargs)
    if not has_param:
        print("=" * 74)
        print("❌ RED：目标实现的签名中没有 allow_flag_rollback —— 改动尚未落地。")
        print("   这是预期的失败（改动落地后必须转 rc=0），不是缺陷。")
        return 1

    results = []

    # ── S1 棘轮不破坏（allow=False，原行为零变更） ──
    disk1 = {'close_phase': 1, 'pending_close': True,
             'is_programmatic_cancel': True, 'settled_by_limit_close': True}
    snap1 = {'close_phase': 0, 'pending_close': False,
             'is_programmatic_cancel': False, 'settled_by_limit_close': False}
    m = call(disk1, snap1, allow=False)
    ok1 = (m.get('close_phase') == 1 and m.get('pending_close') is True
           and m.get('is_programmatic_cancel') is True
           and m.get('settled_by_limit_close') is True)
    print(f"[S1] allow=False 棘轮不破坏              : {ok1}")
    print(f"     → close_phase={m.get('close_phase')} pending={m.get('pending_close')} "
          f"prog={m.get('is_programmatic_cancel')} "
          f"settled={m.get('settled_by_limit_close')}  (期望 1/True/True/True)")
    results.append(ok1)

    # ── S2 回滚生效 + 只回滚临时字段（ChatGPT §十五 关键场景）──
    m = call(disk1, snap1, allow=True)
    ok2 = (m.get('close_phase') == 0 and m.get('pending_close') is False
           and m.get('is_programmatic_cancel') is False
           and m.get('settled_by_limit_close') is True)   # ← 关键：结算事实不降级
    print(f"[S2] allow=True 回滚生效且不动结算事实    : {ok2}")
    print(f"     → close_phase={m.get('close_phase')} pending={m.get('pending_close')} "
          f"prog={m.get('is_programmatic_cancel')} "
          f"settled={m.get('settled_by_limit_close')}  (期望 0/False/False/**True**)")
    results.append(ok2)

    # ── S3 白名单外字段独立验证（只有 settled 想降级）──
    disk3 = {'close_phase': 0, 'pending_close': False,
             'is_programmatic_cancel': False, 'settled_by_limit_close': True}
    snap3 = {'close_phase': 0, 'pending_close': False,
             'is_programmatic_cancel': False, 'settled_by_limit_close': False}
    m = call(disk3, snap3, allow=True)
    ok3 = m.get('settled_by_limit_close') is True
    print(f"[S3] 仅 settled 想降级 → 仍被棘轮挡住     : {ok3}")
    print(f"     → settled={m.get('settled_by_limit_close')}  (期望 True)")
    results.append(ok3)

    # ── S4 int max 在 allow=False 下仍成立 ──
    m = call({'close_phase': 3}, {'close_phase': 1}, allow=False)
    ok4 = m.get('close_phase') == 3
    print(f"[S4] allow=False close_phase int max 3/1→3: {ok4}")
    print(f"     → close_phase={m.get('close_phase')}  (期望 3)")
    results.append(ok4)

    # ── S5 受控通道不应阻塞正向推进 ──
    m = call({'close_phase': 0}, {'close_phase': 2}, allow=True)
    ok5 = m.get('close_phase') == 2
    print(f"[S5] allow=True 仍允许正向推进 0→2        : {ok5}")
    print(f"     → close_phase={m.get('close_phase')}  (期望 2)")
    results.append(ok5)

    # ── S6 user_modified OR 语义不受影响 ──
    m = call({'user_modified': True}, {'user_modified': False}, allow=True)
    ok6 = m.get('user_modified') is True
    print(f"[S6] user_modified OR 语义不受影响        : {ok6}")
    print(f"     → user_modified={m.get('user_modified')}  (期望 True)")
    results.append(ok6)

    print()
    print("=" * 74)
    if all(results):
        print(f"✅ 回滚语义全部通过（{len(results)}/{len(results)}）")
        print("   ⇒ 受控通道只回滚临时状态，不关闭整个安全棘轮")
        return 0
    bad = [i + 1 for i, v in enumerate(results) if not v]
    print(f"❌ 失败场景: S{bad}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
