# -*- coding: utf-8 -*-
"""重验：merge-base 树 + 包A + 包B 依序贴上，能否重建当前工作树（12 文件）。

坑：临时树若放在仓库工作树**内部**，`git apply` 会以仓库根为前缀，把每条 patch
判成 "Skipped patch" 且 **rc 仍为 0**（跳过不是错误）—— 必须从仓库根用
`--directory=` 指定贴入目录，否则"验证通过"是假的。
"""
import hashlib
import io
import os
import re
import shutil
import subprocess
import zipfile

WT = r"G:\my-crypto-bot-wt"
SUB = "_tmp_ab"                      # 相对仓库根
TMP = os.path.join(WT, SUB)
A = os.path.join(WT, "_review_twelfth", "pack-A-p1-1.patch")
B = os.path.join(WT, "_review_twelfth", "pack-B-s6.patch")

FILES = [
    "R1R2_\u6536\u655b\u5ba1\u67e5_\u9a8c\u6536\u5951\u7ea6\u77e9\u9635.md",
    "run_test_gate.py", "test_b2_create_gate.py", "test_gate_baseline.py",
    "test_monitor_poll_recovery.py", "test_poll_degradation.py",
    "test_recover_semantics.py", "test_sg3_p1.py",
    "test_v64_p3_lifecycle.py", "test_v64_partial_close.py",
    "tests_archive/test_b2_crashsafe_entry.py", "trader_260725.py",
]


def norm(p):
    return io.open(p, "rb").read().replace(b"\r\n", b"\n")


lines = []
if os.path.isdir(TMP):
    shutil.rmtree(TMP)
zp = os.path.join(WT, "_tmp_ab.zip")
subprocess.run(["git", "archive", "8c96867", "--format=zip", "-o", zp],
               cwd=WT, check=True)
with zipfile.ZipFile(zp) as z:
    z.extractall(TMP)
os.remove(zp)

for tag, patch in (("A", A), ("B", B)):
    p = subprocess.run(
        ["git", "apply", "-v", "--whitespace=nowarn",
         "--directory=" + SUB, patch],
        cwd=WT, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    err = p.stderr.decode("utf-8", "replace").strip()
    skipped = re.findall(r"Skipped patch '([^']+)'", err)
    lines.append("apply %s -> rc=%d  skipped=%d %s" % (
        tag, p.returncode, len(skipped),
        ("<< " + "; ".join(skipped[:4]) + " >>") if skipped else ""))
    if p.returncode != 0:
        lines.append("    stderr: " + err[:400])

raw_ok = norm_ok = 0
for rel in FILES:
    src, dst = os.path.join(TMP, rel), os.path.join(WT, rel)
    if not os.path.exists(src) or not os.path.exists(dst):
        lines.append("MISSING %s (tmp=%s wt=%s)" % (
            rel, os.path.exists(src), os.path.exists(dst)))
        continue
    raw = norm(src) == norm(dst)
    raw_eq = open(src, "rb").read() == open(dst, "rb").read()
    norm_ok += raw
    raw_ok += raw_eq
    if not raw:
        lines.append("DIFF     %s" % rel)
    elif not raw_eq:
        lines.append("EOL-ONLY %s (norm_eq=True)" % rel)

lines.append("files=%d  raw_identical=%d  norm_identical=%d"
             % (len(FILES), raw_ok, norm_ok))

shutil.rmtree(TMP)
out = "\n".join(lines)
io.open(os.path.join(WT, "_review_twelfth", "ab_rebuild_check.txt"),
        "w", encoding="utf-8", newline="\n").write(out + "\n")
print(out)
