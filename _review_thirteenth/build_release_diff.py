# -*- coding: utf-8 -*-
"""生成"最终发布差异"：生产 4f0afb4 → A+B 候选，方向正确的补丁。

做法（全部在隔离工作树仓库内，生产目录零写入）：
  1. 用临时 GIT_INDEX_FILE 载入生产树 4f0afb4（该对象与候选同库，可直接读）；
  2. 只对这 12 个路径 `git add`（把工作树内容写进临时索引）；
  3. `git diff --cached 4f0afb4 -- <12 路径>` → 得到 生产→候选 的差异，
     新增文件自动带 `new file mode`，生产独有文件根本不进 pathspec。

与第十一轮那版 17 文件补丁的区别：**不以 `git diff 4f0afb4 72a6311` 生成**，
因此不会把生产独有的 `.gitignore` / `strategies_backtest/*` 当成反向补丁。
"""
import hashlib
import io
import os
import subprocess
import sys

WT = r"G:\my-crypto-bot-wt"
PROD = r"G:\my-crypto-bot"
OUT_DIR = os.path.join(WT, "_review_thirteenth")
PATCH = os.path.join(OUT_DIR, "release-diff-4f0afb4-to-AB.patch")
IDX = os.path.join(WT, "_tmp_release.idx")

FILES = [
    "R1R2_\u6536\u655b\u5ba1\u67e5_\u9a8c\u6536\u5951\u7ea6\u77e9\u9635.md",
    "run_test_gate.py", "test_b2_create_gate.py", "test_gate_baseline.py",
    "test_monitor_poll_recovery.py", "test_poll_degradation.py",
    "test_recover_semantics.py", "test_sg3_p1.py",
    "test_v64_p3_lifecycle.py", "test_v64_partial_close.py",
    "tests_archive/test_b2_crashsafe_entry.py", "trader_260725.py",
]

os.makedirs(OUT_DIR, exist_ok=True)
if os.path.exists(IDX):
    os.remove(IDX)

env = dict(os.environ)
env["GIT_INDEX_FILE"] = IDX
env["GIT_AUTHOR_NAME"] = "release-diff"
env["GIT_AUTHOR_EMAIL"] = "release-diff@local"
env["GIT_COMMITTER_NAME"] = "release-diff"
env["GIT_COMMITTER_EMAIL"] = "release-diff@local"


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), cwd=WT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


r = git("read-tree", "4f0afb4")
print("read-tree 4f0afb4 -> rc=%d %s" % (
    r.returncode, r.stderr.decode("utf-8", "replace").strip()[:200]))
if r.returncode:
    sys.exit(1)

r = git("add", "-f", "--", *FILES)
print("add (12 paths)    -> rc=%d %s" % (
    r.returncode, r.stderr.decode("utf-8", "replace").strip()[:300]))
if r.returncode:
    sys.exit(1)

r = git("diff", "--cached", "4f0afb4", "--", *FILES)
print("diff --cached      -> rc=%d" % r.returncode)
patch = r.stdout
io.open(PATCH, "wb").write(patch)

# 统计
names = []
for line in patch.decode("utf-8", "replace").splitlines():
    if line.startswith("diff --git "):
        names.append(line.split(" b/")[-1])
new_files = [l for l in patch.decode("utf-8", "replace").splitlines()
             if l.startswith("new file mode")]
stat = git("diff", "--cached", "--numstat", "4f0afb4", "--", *FILES)
rows = [l.split("\t") for l in stat.stdout.decode("utf-8", "replace").splitlines()
        if l.strip()]
prod_only = [n for n in names if n == ".gitignore"
             or n.startswith("strategies_backtest/")]

h = hashlib.sha256(patch).hexdigest()
print("")
print("patch file : %s" % PATCH)
print("size       : %d B" % len(patch))
print("sha256     : %s" % h)
print("files      : %d  (new file mode: %d)" % (len(names), len(new_files)))
print("+%d -%d" % (sum(int(r[0]) for r in rows), sum(int(r[1]) for r in rows)))
print("prod-only files hit : %d" % len(prod_only))
for n in names:
    print("   " + n)

os.remove(IDX)
