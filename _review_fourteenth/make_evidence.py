# -*- coding: utf-8 -*-
"""把 B35 两态证据汇总成一个可送审文件（RED / GREEN 完整关键行 + 关键文件 sha256）。"""
import hashlib
import io
import os
import re

WT = r"G:\my-crypto-bot-wt"
OUT = os.path.join(WT, "_review_fourteenth")
RED = os.path.join(OUT, "b35_run1_red.txt")
GREEN = os.path.join(OUT, "b35_run2_green.txt")

KEY = re.compile(r"B35|全部批次监控恢复|持仓 UNKNOWN|^\[FAIL\]|^GREEN")


def key_lines(path):
    rows = []
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if KEY.search(line):
                rows.append("%6d| %s" % (i, line.rstrip("\n")))
    return rows


def sha(path):
    return hashlib.sha256(io.open(path, "rb").read()).hexdigest()


files = [
    "trader_260725.py", "test_poll_degradation.py",
    "_review_fourteenth/pack-A-p1-1.patch",
    "_review_fourteenth/pack-B-s6.patch",
    "_review_fourteenth/release-diff-4f0afb4-to-AB.patch",
    "_review_fourteenth/b35_run1_red.txt",
    "_review_fourteenth/b35_run2_green.txt",
]

buf = []
buf.append("=" * 78)
buf.append("B35 定点反例两态证据 —— 普通轮询恢复分支不得在「持仓 UNKNOWN」时放闸")
buf.append("生成时间：2026-09-29 +08:00   候选分支 fix/converge-side-attribution（未提交）")
buf.append("生产 HEAD 4f0afb4（全程只读）  merge-base 8c96867")
buf.append("=" * 78)
buf.append("")
buf.append("【1】缺陷（ChatGPT 第十三轮指出，已实证）")
buf.append("-" * 78)
buf.append("普通恢复分支解除 _poll_degraded_batches 的条件只要：")
buf.append("    not _poll_orders_unresolved and _poll_protection_confirmed")
buf.append("缺 current_actual_position is not None。于是：")
buf.append("  · 订单查询恢复、账本无已识别成交 → batch_filled_amount == 0")
buf.append("  · _poll_needs_protection = False → 保护判据被「空过」判成已确认")
buf.append("  · 持仓查询返回 None（UNKNOWN ≠ EMPTY）→ 旧实现照样")
buf.append("    「✅ [POLL] 全部批次监控恢复」并清空降级闸门 → 放行新 ENTRY")
buf.append("S6 首轮接管判据（trader :10752 要求 current_actual_position is not None）")
buf.append("只在 _monitor_lifecycle 是 dict 的接管线程生效，替不了常规监控线程。")
buf.append("")
buf.append("【2】修复（隔离工作树、未提交；本文件只记录，不构成合并/部署批准）")
buf.append("-" * 78)
buf.append("恢复判定块新增第三判据：")
buf.append("    _poll_position_known = (current_actual_position is not None)")
buf.append("主 if 追加 and _poll_position_known；UNKNOWN 时不重置 streak，")
buf.append("并新增 elif 打印「订单已可读但持仓 UNKNOWN（查询失败 ≠ 零仓）→ 保持暂停新增风险」。")
buf.append("与 S6 :10752 同款 Fail-Closed。")
buf.append("")
buf.append("【3】新用例 test_block35_position_unknown_keeps_degraded_gate（已注册进 main()）")
buf.append("-" * 78)
buf.append("B35a 连续失败 → 降级闸门置位")
buf.append("B35b 订单可读 + 持仓 UNKNOWN → 不得解除降级闸门")
buf.append("B35c 该状态下实际发起新 ENTRY → 必须零下单")
buf.append("B35d 阳性对照：持仓核实为零 → 同一条恢复路径确实放闸")
buf.append("B35e 阳性对照：放闸后同一替身确实落单（证 B35c 非假绿）")
buf.append("")
buf.append("防假绿要点：B35c 的**信号时刻**把持仓查询恢复成 0.0，使 execute_signal")
buf.append("自身那道「持仓 UNKNOWN → 拒绝」(:6261) 不替本用例挡单 —— 闸门是唯一拦截。")
buf.append("")
buf.append("【4】修复前（RED）—— _review_fourteenth\\b35_run1_red.txt")
buf.append("-" * 78)
buf.append("执行：G:\\my-crypto-bot\\.venv\\Scripts\\python.exe test_poll_degradation.py")
buf.append("结果：rc=1")
buf.append("关键行（行号| 内容）：")
buf.extend(key_lines(RED))
buf.append("")
buf.append("【5】修复后（GREEN）—— _review_fourteenth\\b35_run2_green.txt")
buf.append("-" * 78)
buf.append("同一命令，结果：rc=0")
buf.append("关键行（行号| 内容）：")
buf.extend(key_lines(GREEN))
buf.append("")
buf.append("【6】关键文件 SHA-256")
buf.append("-" * 78)
for f in files:
    p = os.path.join(WT, f.replace("/", os.sep))
    if os.path.exists(p):
        buf.append("%-58s %s" % (f, sha(p)))
buf.append("")
buf.append("【7】复现")
buf.append("-" * 78)
buf.append("cd G:\\my-crypto-bot-wt")
buf.append("  # 修复后（期望 rc=0，GREEN 96/96）")
buf.append("  G:\\my-crypto-bot\\.venv\\Scripts\\python.exe test_poll_degradation.py")
buf.append("  # 回到修复前（期望 rc=1，B35b/B35c FAIL）：把恢复判定块的")
buf.append("  # `and _poll_position_known` 删掉并去掉新增 elif，再跑一次，随后还原")
buf.append("  # 重建三份补丁 + 校验发布差异")
buf.append("  G:\\my-crypto-bot\\.venv\\Scripts\\python.exe _review_fourteenth\\build_patches.py")
buf.append("  G:\\my-crypto-bot\\.venv\\Scripts\\python.exe _review_fourteenth\\verify_release_diff.py")
buf.append("")
buf.append("【8】边界（本文件不主张）")
buf.append("-" * 78)
buf.append("· 完整发布门禁 run_test_gate.py 仍未执行（rc=3：运行中生产 Bot 拒绝）；")
buf.append("  未跑门禁 ≠ 发布通过。**不批准合并、部署或重启。**")
buf.append("· B35 是受控替身驱动的定点反例，证明的是「持仓 UNKNOWN 时不得放闸」，")
buf.append("  不证明首轮完成后所有交易所与保护事实都已正确核验。")
buf.append("· 生产目录全程只读（git show / cat-file / apply --check），状态仍为 4f0afb4。")
buf.append("=" * 78)

out = "\n".join(buf) + "\n"
io.open(os.path.join(OUT, "b35_evidence.txt"), "w", encoding="utf-8",
        newline="\n").write(out)
print(out)
