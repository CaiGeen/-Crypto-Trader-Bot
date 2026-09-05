#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【离线·零 API】止盈价校验 R1/R2 边界语义实测

目的：
  核实「TP 低于最低 entry 触发价」这一参数的真实后果链到底走到哪一步。
  本项目铁律：结论必须源码实证，不得凭记忆或推断下判断。

方法（避免 import 生产模块触发副作用）：
  用 ast 从 trader_260725.py 源码文本中**原样提取** `_check_tp_viability`
  与 `_validate_take_profit` 两个函数定义，编译后在隔离 namespace 中执行。
  → 被测对象就是磁盘上的源码本身，不是复述、不是重写。

  两者均为纯函数（不访问 self、不发 API、不读写状态），self 传 None 即可。

安全：零网络、零 API、零状态文件写入。

【v1.1 修正（2026-08-29，ChatGPT 终审指出）】
  原判定行 `d_symmetric = ok7 and (not ok8) is False or True` 尾部 `or True` 导致恒真，
  且未参与最终判定 → SELL 对称性只是打印、不是验收条件。
  已改为 `ok7 and (not ok8)` 并纳入 ALL；另补 `e_cost_dim = not ok9`。
  非恒真性已用负向对照证明（模拟 R2 失效时转 False）。

【说明】本文件是离线只读验证工具，不是生产代码。
  工作树中出现对它的修改，不构成生产变更。
"""
import ast
import sys

SRC = r'G:\my-crypto-bot\trader_260725.py'
TARGETS = ('_check_tp_viability', '_validate_take_profit')


def extract_funcs(path, names):
    """从源码中按名字原样提取函数定义（保留源码文本，不做任何改写）"""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            seg = ast.get_source_segment(open(path, encoding='utf-8').read(), node)
            found[node.name] = (seg, node.lineno)
    return found


class _Sig:  # 极简 signal 替身：只需 .side / .take_profit / .entries
    def __init__(self, side, tp, entries):
        self.side = side
        self.take_profit = tp
        self.entries = entries


funcs = extract_funcs(SRC, TARGETS)
missing = [n for n in TARGETS if n not in funcs]
if missing:
    print(f"❌ 源码中未找到: {missing}")
    sys.exit(1)

ns = {}
for name, (seg, lineno) in funcs.items():
    print(f"[提取] {name}  (源码 L{lineno})")
    exec(compile(seg, f'<{name}@L{lineno}>', 'exec'), ns)

check_tp_viability = ns['_check_tp_viability']      # = self._check_tp_viability
validate_take_profit = ns['_validate_take_profit']  # = self._validate_take_profit

print()
print("=" * 78)
print("场景 A：批次3 真实参数（2026-08-29 17:23 实盘）")
print("   BUY / TP=80000 / entry 触发价 80521、80701 / 建仓时现价 77591.8")
print("=" * 78)

sig = _Sig('BUY', 80000.0, [(80521.0, 0.001), (80701.0, 0.001)])

# --- R1：开仓前，用建仓时现价 ---
ok1, msg1 = validate_take_profit(None, sig, 77591.8)
print(f"[R1 开仓前] 现价 77591.8 → {'✅ 放行' if ok1 else '❌ 拦截'}  ({msg1})")

# --- R2：成交瞬间，现价已涨到最低触发价 80521 ---
ok2, msg2 = check_tp_viability(None, 'BUY', 80000.0, 0.0, 80521.0)
print(f"[R2 成交瞬间] 现价 80521.0 cost=0 → {'✅ 放行' if ok2 else '❌ 拦截'}")
print(f"    reason: {msg2}")

# --- R2：第二层成交时 ---
ok3, msg3 = check_tp_viability(None, 'BUY', 80000.0, 0.0, 80701.0)
print(f"[R2 二层成交] 现价 80701.0 cost=0 → {'✅ 放行' if ok3 else '❌ 拦截'}")

print()
print("=" * 78)
print("场景 B：当前活跃批次2 真实参数（对照，应当合法）")
print("   BUY / TP=80000 / 成本 77692.6 / 现价约 77690")
print("=" * 78)
ok4, msg4 = check_tp_viability(None, 'BUY', 80000.0, 77692.6, 77690.0)
print(f"[R2] → {'✅ 放行' if ok4 else '❌ 拦截'}" + (f"  reason: {msg4}" if msg4 else ""))

print()
print("=" * 78)
print("场景 C：合法阶梯入场（TP 低于高层 entry 但不低于最低层）")
print("   现价 77000 / L1=78000 L2=80000 L3=82000 / TP=79000")
print("=" * 78)
sig_c = _Sig('BUY', 79000.0, [(78000.0, 0.001), (80000.0, 0.001), (82000.0, 0.001)])
ok5, _ = validate_take_profit(None, sig_c, 77000.0)
print(f"[R1 开仓前] → {'✅ 放行' if ok5 else '❌ 拦截'}")
ok6, msg6 = check_tp_viability(None, 'BUY', 79000.0, 0.0, 78000.0)
print(f"[R2 L1成交@78000] → {'✅ 放行' if ok6 else '❌ 拦截'}" + (f"  reason: {msg6}" if msg6 else ""))

print()
print("=" * 78)
print("场景 D：SELL 对称性")
print("=" * 78)
sig_d = _Sig('SELL', 80000.0, [(79500.0, 0.001), (79300.0, 0.001)])
ok7, _ = validate_take_profit(None, sig_d, 80500.0)
print(f"[R1 开仓前] 现价 80500 TP=80000 → {'✅ 放行' if ok7 else '❌ 拦截'}")
ok8, msg8 = check_tp_viability(None, 'SELL', 80000.0, 0.0, 79500.0)
print(f"[R2 成交@79500] → {'✅ 放行' if ok8 else '❌ 拦截'}" + (f"  reason: {msg8}" if msg8 else ""))

print()
print("=" * 78)
print("场景 E：成交后自愈边界（关键：价格回落后 TP 能否自动挂出？）")
print("   批次3 若在 80521 成交 → 持仓成本 80521；假设价格回落至 79500")
print("   R2 是双维度：现价维度(TP>79500 ✅) 但成本维度(TP<80521 ❌)")
print("=" * 78)
ok9, msg9 = check_tp_viability(None, 'BUY', 80000.0, 80521.0, 79500.0)
print(f"[R2 成交后] TP=80000 cost=80521 mark=79500 → {'✅ 放行' if ok9 else '❌ 拦截'}")
print(f"    reason: {msg9}")
print("  → 双维度独立：现价维度已满足，但成本维度（止盈即亏损）仍拦截，")
print("    因此不是「等价格回落自动挂出」，而是需人工改 TP 才能自愈。")

print()
print("=" * 78)
print("判定")
print("=" * 78)
r1_passes_bad = ok1            # R1 是否放行了批次3
r2_blocks_bad = not ok2        # R2 是否拦住了批次3
c_legal_ok = ok5 and ok6       # 合法阶梯策略是否被误伤（不得误伤）
d_symmetric = ok7 and (not ok8)   # SELL 侧是否与 BUY 对称（R1 放行 + R2 拦截）
e_cost_dim = not ok9           # 成交后价格回落，成本维度是否仍拦截（自愈非自动）

print(f"  R1 放行批次3（缺口存在）          : {r1_passes_bad}")
print(f"  R2 拦住批次3（后果被兜住）        : {r2_blocks_bad}")
print(f"  合法阶梯场景 C 未被误伤           : {c_legal_ok}")
print(f"  SELL 侧对称性（场景 D）           : {d_symmetric}")
print(f"  成交后成本维度仍拦（场景 E）      : {e_cost_dim}")

ALL = (r1_passes_bad and r2_blocks_bad and c_legal_ok
       and d_symmetric and e_cost_dim)

if ALL:
    print()
    print("  ✅ 结论：缺口（R1 放行）成立，但 R2 在成交瞬间拦截 →")
    print("     零 create API → 不发生 -2021 → 不进入重试 → 不 HARD_LOCK。")
    print("     真实后果 = 上行保护缺失 + 1 次 critical + 等待人工改 TP（改后自愈）。")
    sys.exit(0)
else:
    print()
    print("  ❌ 与预期不符，需人工复核上方场景输出")
    sys.exit(1)
