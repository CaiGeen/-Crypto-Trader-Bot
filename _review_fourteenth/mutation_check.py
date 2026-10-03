# -*- coding: utf-8 -*-
"""因果性检查（变异测试）：把修复新增的 `_poll_position_known` 判据摘掉，
重跑 test_poll_degradation，应重新变红（B35b/B35c FAIL）→ 证明 RED/GREEN
的差异由该判据造成，而非其他改动。

**无论成败都还原**原文件，并在结束时逐字节断言还原成功。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

WT = r"G:\my-crypto-bot-wt"
P = os.path.join(WT, "trader_260725.py")
OUT = os.path.join(WT, "_review_fourteenth", "b35_run3_mutation.txt")

# 文件是 CRLF，锚点必须按实际行尾匹配（只删那一行）
NEEDLE = re.compile(r"[ \t]*and _poll_position_known\r?\n")

orig = io.open(P, encoding="utf-8", newline="").read()
sha_before = hashlib.sha256(orig.encode("utf-8")).hexdigest()
hits = [m for m in NEEDLE.finditer(orig)]
lines = ["变异前 sha256(content) = %s" % sha_before,
         "needle 命中次数 = %d" % len(hits)]
assert len(hits) == 1, "判据锚点不唯一/不存在，放弃变异"

mutated = orig[:hits[0].start()] + orig[hits[0].end():]
io.open(P, "w", encoding="utf-8", newline="").write(mutated)
lines.append("已摘除 `and _poll_position_known` → 重跑 test_poll_degradation.py")
try:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, "test_poll_degradation.py"], cwd=WT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=env, timeout=900)
    out = p.stdout.decode("utf-8", "replace")
    lines.append("变异后 rc = %d（期望非 0 = 重新变红）" % p.returncode)
    for i, l in enumerate(out.splitlines(), 1):
        if "B35" in l or l.startswith("GREEN:") or "[FAIL]" in l:
            lines.append("%6d| %s" % (i, l))
finally:
    io.open(P, "w", encoding="utf-8", newline="").write(orig)
    after = io.open(P, encoding="utf-8", newline="").read()
    sha_after = hashlib.sha256(after.encode("utf-8")).hexdigest()
    lines.append("还原后 sha256(content) = %s" % sha_after)
    lines.append("还原逐字节一致 = %s" % (after == orig))

io.open(OUT, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
print("\n".join(lines))
