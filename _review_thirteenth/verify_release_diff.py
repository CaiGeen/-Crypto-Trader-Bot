# -*- coding: utf-8 -*-
"""验证最终发布差异：
  ① 生产目录 `git apply --check`（只读，不落盘）
  ② 在**仓库外**的临时目录，用生产 4f0afb4 的文件内容起步，真实贴上该补丁，
     再逐个与候选工作树比对（raw + 换行归一）。
注意：临时目录必须在仓库之外 —— 否则 git 以仓库根为前缀把 patch 判成
"Skipped patch" 而 rc 仍为 0（上一轮踩过）。
"""
import io
import os
import shutil
import subprocess

WT = r"G:\my-crypto-bot-wt"
PROD = r"G:\my-crypto-bot"
PATCH = os.path.join(WT, "_review_thirteenth", "release-diff-4f0afb4-to-AB.patch")
TMP = r"C:\Users\Administrator\AppData\Local\Temp\opencode\reldiff_check"

FILES = [
    "R1R2_\u6536\u655b\u5ba1\u67e5_\u9a8c\u6536\u5951\u7ea6\u77e9\u9635.md",
    "run_test_gate.py", "test_b2_create_gate.py", "test_gate_baseline.py",
    "test_monitor_poll_recovery.py", "test_poll_degradation.py",
    "test_recover_semantics.py", "test_sg3_p1.py",
    "test_v64_p3_lifecycle.py", "test_v64_partial_close.py",
    "tests_archive/test_b2_crashsafe_entry.py", "trader_260725.py",
]

lines = []

# ① 生产侧只读 check
p = subprocess.run(["git", "apply", "--check", PATCH], cwd=PROD,
                   stderr=subprocess.PIPE, stdout=subprocess.PIPE)
lines.append("① 生产目录 git apply --check -> rc=%d %s" % (
    p.returncode, p.stderr.decode("utf-8", "replace").strip()[:200]))

# ② 仓库外真实贴
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
missing_before = [f for f in FILES
                  if not os.path.exists(os.path.join(TMP, f))]
lines.append("② 起步树：生产文件 %d/%d（缺=%s 即生产尚不存在的新增文件）"
             % (len(FILES) - len(missing_before), len(FILES),
                ", ".join(missing_before) or "无"))

p = subprocess.run(["git", "apply", "-v", "--whitespace=nowarn", PATCH],
                   cwd=TMP, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
err = p.stderr.decode("utf-8", "replace").strip()
import re
skipped = re.findall(r"Skipped patch '([^']+)'", err)
lines.append("   git apply -> rc=%d  skipped=%d %s" % (
    p.returncode, len(skipped), "!! " + "; ".join(skipped[:4]) if skipped else ""))
if p.returncode != 0:
    lines.append("   stderr: " + err[:500])

raw_ok = norm_ok = 0
for f in FILES:
    a, b = os.path.join(TMP, f), os.path.join(WT, f)
    if not (os.path.exists(a) and os.path.exists(b)):
        lines.append("   MISSING %s (tmp=%s wt=%s)" % (
            f, os.path.exists(a), os.path.exists(b)))
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
io.open(os.path.join(WT, "_review_thirteenth", "release_diff_check.txt"),
        "w", encoding="utf-8", newline="\n").write(out + "\n")
print(out)
