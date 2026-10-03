# -*- coding: utf-8 -*-
"""第十四轮：验证新的最终发布差异（生产 4f0afb4 → A+B，已含 B35 修复）。

  ① 生产目录 `git apply --check`（只读，不落盘）
  ② **仓库外**临时目录：以生产 4f0afb4 的文件内容起步，真实贴上该补丁
  ③ 贴完 12 文件与候选工作树逐个比对（raw + 换行归一）

⚠️ 临时目录必须在仓库之外 —— 否则 git 以仓库根为前缀把 patch 判成
"Skipped patch" 而 rc 仍为 0（第十二轮踩到的假绿）。
"""
import io
import os
import re
import shutil
import subprocess

WT = r"G:\my-crypto-bot-wt"
PROD = r"G:\my-crypto-bot"
PATCH = os.path.join(WT, "_review_fourteenth", "release-diff-4f0afb4-to-AB.patch")
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode\reldiff_check14"

FILES = [
    "R1R2_收敛审查_验收契约矩阵.md",
    "run_test_gate.py", "test_b2_create_gate.py", "test_gate_baseline.py",
    "test_monitor_poll_recovery.py", "test_poll_degradation.py",
    "test_recover_semantics.py", "test_sg3_p1.py",
    "test_v64_p3_lifecycle.py", "test_v64_partial_close.py",
    "tests_archive/test_b2_crashsafe_entry.py", "trader_260725.py",
]

lines = []

p = subprocess.run(["git", "apply", "--check", PATCH], cwd=PROD,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
lines.append("① 生产目录 git apply --check -> rc=%d %s" % (
    p.returncode, p.stderr.decode("utf-8", "replace").strip()[:200]))

if os.path.isdir(TMP):
    shutil.rmtree(TMP)
os.makedirs(TMP)
for f in FILES:
    r = subprocess.run(["git", "-C", PROD, "show", "4f0afb4:" + f],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode == 0:
        dst = os.path.join(TMP, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        io.open(dst, "wb").write(r.stdout)
missing = [f for f in FILES if not os.path.exists(os.path.join(TMP, f))]
lines.append("② 起步树：生产文件 %d/%d（缺=生产尚不存在的新增文件：%s）"
             % (len(FILES) - len(missing), len(FILES), ", ".join(missing) or "无"))

p = subprocess.run(["git", "apply", "-v", "--whitespace=nowarn", PATCH],
                   cwd=TMP, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
err = p.stderr.decode("utf-8", "replace")
skipped = re.findall(r"Skipped patch '([^']+)'", err)
lines.append("   git apply -> rc=%d  skipped=%d %s" % (
    p.returncode, len(skipped),
    "!! " + "; ".join(skipped[:4]) if skipped else ""))
if p.returncode != 0:
    lines.append("   stderr: " + err[:500])

raw_ok = norm_ok = 0
for f in FILES:
    a, b = os.path.join(TMP, f), os.path.join(WT, f)
    if not (os.path.exists(a) and os.path.exists(b)):
        lines.append("   MISSING %s" % f)
        continue
    ba, bb = open(a, "rb").read(), open(b, "rb").read()
    raw = ba == bb
    nrm = ba.replace(b"\r\n", b"\n") == bb.replace(b"\r\n", b"\n")
    raw_ok += raw
    norm_ok += nrm
    if not nrm:
        lines.append("   DIFF      %s" % f)
    elif not raw:
        lines.append("   EOL-ONLY  %s (norm_eq=True)" % f)
lines.append("   files=%d  raw_identical=%d  norm_identical=%d"
             % (len(FILES), raw_ok, norm_ok))

shutil.rmtree(TMP, ignore_errors=True)
out = "\n".join(lines)
io.open(os.path.join(WT, "_review_fourteenth", "release_diff_check.txt"),
        "w", encoding="utf-8", newline="\n").write(out + "\n")
print(out)
