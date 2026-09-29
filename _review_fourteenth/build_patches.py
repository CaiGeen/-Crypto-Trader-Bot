# -*- coding: utf-8 -*-
"""第十四轮：重建两包补丁 + 最终发布差异，并给出规模/sha。

包 A（P1-1，已提交侧）  : git diff 8c96867 72a6311
                         + git diff 72a6311 -- <两个 P1-1 夹具文件>
包 B（未提交侧）        : git diff 72a6311 -- .  再剔除上面那两个夹具
发布差异（生产→A+B）    : 临时 GIT_INDEX_FILE 载入 4f0afb4 → 只 add 12 路径
                         → git diff --cached 4f0afb4 -- <12 路径>
生产目录全程只读。
"""
import hashlib
import io
import os
import subprocess
import sys

WT = r"G:\my-crypto-bot-wt"
OUT = os.path.join(WT, "_review_fourteenth")
FIXTURES = ["test_recover_semantics.py",
            "tests_archive/test_b2_crashsafe_entry.py"]
RELEASE_FILES = [
    "R1R2_收敛审查_验收契约矩阵.md",
    "run_test_gate.py", "test_b2_create_gate.py", "test_gate_baseline.py",
    "test_monitor_poll_recovery.py", "test_poll_degradation.py",
    "test_recover_semantics.py", "test_sg3_p1.py",
    "test_v64_p3_lifecycle.py", "test_v64_partial_close.py",
    "tests_archive/test_b2_crashsafe_entry.py", "trader_260725.py",
]

os.makedirs(OUT, exist_ok=True)


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), cwd=WT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def numstat(data):
    """统计补丁体的 +/- 行数（排除 ---/+++ 文件头）。"""
    add = dele = 0
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            dele += 1
    return add, dele


def names(data):
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            out.append(line.split(" b/")[-1])
    return out


def report(tag, data, path):
    io.open(path, "wb").write(data)
    add, dele = numstat(data)
    ns = names(data)
    print("%-14s files=%-3d +%-5d -%-5d %8d B  sha=%s"
          % (tag, len(ns), add, dele, len(data), sha(data)))
    print("               %s" % ", ".join(ns))
    return sha(data)


# ── 包 A ──────────────────────────────────────────────────────────────────
a1 = git("diff", "8c96867", "72a6311").stdout
a2 = git("diff", "72a6311", "--", *FIXTURES).stdout
sha_a = report("pack-A", a1 + a2, os.path.join(OUT, "pack-A-p1-1.patch"))

# ── 包 B（剔除包 A 已含的两个夹具）───────────────────────────────────────
b = git("diff", "72a6311", "--", ".",
        ":(exclude)test_recover_semantics.py",
        ":(exclude)tests_archive/test_b2_crashsafe_entry.py").stdout
sha_b = report("pack-B", b, os.path.join(OUT, "pack-B-s6.patch"))

# ── 最终发布差异（生产 4f0afb4 → A+B 候选，12 路径，单向前进）─────────────
IDX = os.path.join(WT, "_tmp_release.idx")
if os.path.exists(IDX):
    os.remove(IDX)
env = dict(os.environ)
env["GIT_INDEX_FILE"] = IDX
r = subprocess.run(["git", "read-tree", "4f0afb4"], cwd=WT, env=env,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
r = subprocess.run(["git", "add", "-f", "--"] + RELEASE_FILES, cwd=WT, env=env,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
rel = subprocess.run(["git", "diff", "--cached", "4f0afb4", "--"] + RELEASE_FILES,
                     cwd=WT, env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
assert rel.returncode == 0, rel.stderr.decode("utf-8", "replace")
os.remove(IDX)
ns = names(rel.stdout)
prod_only = [n for n in ns if n == ".gitignore" or n.startswith("strategies_backtest/")]
sha_r = report("release-diff", rel.stdout,
               os.path.join(OUT, "release-diff-4f0afb4-to-AB.patch"))
print("               new file mode=%d  命中生产独有文件=%d"
      % (rel.stdout.count(b"new file mode"), len(prod_only)))

io.open(os.path.join(OUT, "shas.txt"), "w", encoding="utf-8", newline="\n").write(
    "pack-A-p1-1.patch             sha256=%s\n"
    "pack-B-s6.patch               sha256=%s\n"
    "release-diff-4f0afb4-to-AB.patch sha256=%s\n" % (sha_a, sha_b, sha_r))
print("\nOK")
