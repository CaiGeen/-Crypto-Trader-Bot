# -*- coding: utf-8 -*-
"""全量 sweep：逐个跑 `test_*.py`，打印 rc。"""
import glob
import os
import subprocess
import sys
import time

py = sys.executable
files = sorted(glob.glob("test_*.py"))
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"

rows = []
t0 = time.time()
for f in files:
    st = time.time()
    try:
        p = subprocess.run([py, f], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=env, timeout=900)
        rc, out = p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        rc, out = 99, "TIMEOUT"
    rows.append((f, rc, time.time() - st, out))
    print("%-46s rc=%-3s %5.1fs" % (f, rc, time.time() - st), flush=True)

print("")
bad = [(f, rc) for f, rc, _, _ in rows if rc != 0]
print("total=%d  rc0=%d  nonzero=%d  elapsed=%.0fs"
      % (len(rows), len(rows) - len(bad), len(bad), time.time() - t0))
for f, rc, _, out in rows:
    if rc != 0:
        print("NONZERO %s rc=%s" % (f, rc))
        tail = [l for l in out.splitlines() if "FAIL" in l or "Error" in l or "error" in l]
        for l in tail[-6:]:
            print("    " + l)
