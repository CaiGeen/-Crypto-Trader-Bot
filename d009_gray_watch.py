# -*- coding: utf-8 -*-
"""D-009 灰度观察只读检查器（GRAY OBSERVATION WATCHER）

用途
----
D-009 已进入 CLOSED / GRAY OBSERVATION。ChatGPT 裁定：不再改设计、不做破坏性演练，
只观察三个真实事件：
    ① 首次真实有持仓后状态保存
    ② 首次真实平仓（沿用 Batch B 观察项）
    ③ 下一次自然异常重启（不人为制造）

本脚本用于观察期**随时快速体检**，把上面三项的前置/后置状态量化成一眼可读的结论。

安全边界（重要）
--------------
* **全程只读**：只调用 `load_all_states` / `_load_tombstones` 两个读取方法，
  **绝不调用** `_persist_states` / `_persist_tombstones` / `save_batch_state` /
  `clear_batch_state`，不碰任何下单/撤单/平仓路径。
* **.bak 只 stat 不 open**：D-009 裁定 `.bak` 仅人工取证、永不自动装载，
  本脚本只读其大小/时间用于提示，**从不解析其内容**。
* 判定逻辑**直接复用生产代码**（`trader_260725.load_all_states`），
  而非另写一套——避免"检查器说没问题、生产却误判"的假安全感。

退出码
------
    0 = 全部正常
    1 = 存在需要人工关注的 WARN
    2 = 存在 ERROR（账本损坏 / 进程未加载预期版本 / 进程不在）

用法
----
    .venv/Scripts/python.exe d009_gray_watch.py
"""

import os
import sys
import json
import time
import struct
import tempfile
import threading
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXPECT_COMMIT = "580f077"          # 灰度期预期加载的 commit
BASE = os.path.dirname(os.path.abspath(__file__))
PS_START = time.time()             # 本检查器启动时刻（用于排除自身进程）

WARN, ERR = [], []


def warn(m):
    WARN.append(m)
    print(f"  ⚠️  {m}")


def err(m):
    ERR.append(m)
    print(f"  ❌ {m}")


def ok(m):
    print(f"  ✅ {m}")


def section(t):
    print(f"\n{'='*74}\n{t}\n{'='*74}")


# ────────────────────────────────────────────────────────────────
# 1. 代码版本：pyc 头部 vs 磁盘源码
# ────────────────────────────────────────────────────────────────
def check_version():
    section("[1] 代码版本（pyc 头部硬证据）")
    src = os.path.join(BASE, 'trader_260725.py')
    pyc = os.path.join(BASE, '__pycache__', 'trader_260725.cpython-311.pyc')
    if not os.path.exists(pyc):
        warn("trader_260725 pyc 不存在（该模块从未被 import → 生产进程可能没起来）")
        return
    st = os.stat(src)
    raw = open(pyc, 'rb').read(16)
    magic, flags = raw[:4], struct.unpack('<I', raw[4:8])[0]
    if magic != importlib.util.MAGIC_NUMBER:
        err(f"pyc magic 不匹配（{magic.hex()} vs {importlib.util.MAGIC_NUMBER.hex()}）")
        return
    if flags & 0b1:
        warn("pyc 为 hash-based，无法用 mtime/size 比对，版本证据降级")
        return
    m, s = struct.unpack('<II', raw[8:16])
    pm = time.strftime('%m-%d %H:%M:%S', time.localtime(m))
    fm = time.strftime('%m-%d %H:%M:%S', time.localtime(os.path.getmtime(pyc)))
    print(f"    源码: mtime={int(st.st_mtime)} size={st.st_size}")
    print(f"    pyc : 记录 mtime={m} ({pm}) size={s}   文件写入于 {fm}")
    if m == int(st.st_mtime) and s == st.st_size:
        ok(f"pyc 与源码逐位一致 → 运行实例加载的是当前源码（预期 {EXPECT_COMMIT}）")
    else:
        err("pyc 头部与源码不一致 → **运行实例加载的不是当前源码**，需重启")


# ────────────────────────────────────────────────────────────────
# 2. 状态文件三态判定（复用生产代码逻辑）
# ────────────────────────────────────────────────────────────────
def check_state():
    section("[2] 账本三态判定（复用生产 load_all_states）")
    try:
        import trader_260725 as T
    except Exception as e:
        err(f"无法 import trader_260725: {e}")
        return None

    state_file = os.path.join(BASE, 'trade_state.json')
    tomb_file = os.path.join(BASE, 'trade_tombstones.json')

    f = type('F', (), {})()
    f._state_lock = threading.Lock()
    f._state_corrupted = False
    f._state_corruption_detail = ''
    f._tombstones_degraded = False
    f.tombstone_file = tomb_file
    f._tombstone_alerted = set()
    f._converge_alert_counts = {}
    f.send_tg_notification = lambda *a, **k: None
    for n in ('load_all_states', '_load_tombstones'):
        setattr(f, n, lambda *a, _n=n, **k: getattr(T.CryptoTrader, _n)(f, *a, **k))

    T.STATE_FILE = state_file
    try:
        st = f.load_all_states()
        if f._state_corrupted:
            err(f"账本损坏 → Fail-Closed（READY=False，系统已停止交易）：{f._state_corruption_detail[:80]}")
            err("  处置：先备份 trade_state.json + .bak，再人工修复；切勿直接删文件")
        else:
            n_sym = len(st)
            n_bat = sum(len(v) for v in st.values() if isinstance(v, dict))
            n_act = sum(1 for v in st.values() if isinstance(v, dict)
                        for b in v.values() if isinstance(b, dict) and b.get('is_active'))
            ok(f"账本合法（未损坏）→ {n_sym} 个 symbol / {n_bat} 个批次 / 活跃 {n_act} 个")
            if n_act > 0:
                print(f"    ℹ️  有活跃持仓 → 观察项①「真实持仓下状态保存」可进行")
    finally:
        T.STATE_FILE = state_file

    tb = f._load_tombstones()
    if f._tombstones_degraded:
        warn("墓碑损坏 → DEGRADED：系统保持 READY，但**全新 batch 会被拒绝**（已存在 batch 正常）")
    else:
        print(f"  ✅ 墓碑正常（{len(tb)} 条）" if os.path.exists(tomb_file)
              else "  ✅ 墓碑文件不存在 → 视同空，不降级（缺文件 ≠ 损坏）")
    return f


# ────────────────────────────────────────────────────────────────
# 3. .bak 语义（只 stat，绝不 open）
# ────────────────────────────────────────────────────────────────
def check_bak():
    section("[3] .bak 取证信息（只 stat，绝不读取内容）")
    bak = os.path.join(BASE, 'trade_state.json.bak')
    if not os.path.exists(bak):
        print("  （无 .bak —— 首次保存前不会生成，正常）")
        return
    st = os.stat(bak)
    print(f"    大小 {st.st_size}B  mtime={time.strftime('%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    print("  ℹ️  .bak = last-state-before-this-write，**不是** last-known-good（可能含已清理的幽灵批次）")
    print("  ℹ️  D-009 裁定：仅人工取证，永不自动装载。本脚本不解析其内容")


# ────────────────────────────────────────────────────────────────
# 4. 进程身份
# ────────────────────────────────────────────────────────────────
def check_process():
    section("[4] 进程身份（PID / 启动时刻 / 存活时长）")
    # 本脚本自身（含 venv 启动器子进程）也是 python.exe，须用启动时刻排除，
    # 否则每次检查都会误报"多了 2 个进程"。T0 = 本进程启动时刻，留 3s 余量。
    # 注意：CIM 的 CreationDate 形如 "2026/8/29 15:23:55"（月/日不补零），
    # 与 strftime 的 "2026/08/29" 做字符串比较必然出错，必须解析成 datetime 再比。
    import datetime
    t0 = datetime.datetime.fromtimestamp(PS_START) - datetime.timedelta(seconds=3)

    def parse_cim(s):
        try:
            return datetime.datetime.strptime(s.split('.')[0].strip(), '%Y/%m/%d %H:%M:%S')
        except Exception:
            return None
    try:
        import subprocess
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object ProcessId,CreationDate | ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=40)
        rows = [l for l in r.stdout.strip().splitlines()[1:] if l.strip()]
        if not rows:
            err("未发现任何 python.exe → 生产进程未运行")
            return
        ids = []
        for l in rows:
            parts = l.strip('"').split('","')
            if len(parts) >= 2:
                ids.append((parts[0], parts[1]))
        # 排除本脚本及其 venv 启动器：CreationDate 早于本次检查启动时刻的才是生产进程
        prod, unknown = [], 0
        for p, c in ids:
            dt = parse_cim(c)
            if dt is None:
                unknown += 1
                prod.append((p, c))      # 解析失败则保留，宁可多报不可漏报
            elif dt < t0:
                prod.append((p, c))
        excl = len(ids) - len(prod)
        print(f"    生产 python.exe: {len(prod)}（本项目正常 = 4：watchdog+bot_runner 各 2）")
        if excl:
            print(f"    （已排除 {excl} 个本检查器自身的进程）")
        if unknown:
            warn(f"{unknown} 个进程时间解析失败，已保守保留在统计内")
        for pid, cd in sorted(prod, key=lambda x: x[1]):
            print(f"      PID {pid:<8} {cd}")
        if not prod:
            err("未发现生产 python.exe（早于本次检查启动时刻）")
        elif len(prod) != 4:
            warn(f"生产进程数 = {len(prod)}，正常应为 4 → 检查是否双实例或进程缺失")
        else:
            ok("进程数量正常（4 = watchdog×2 + bot_runner×2）")
    except Exception as e:
        warn(f"进程检查失败（不影响状态判定）: {e}")


# ────────────────────────────────────────────────────────────────
# 5. 灰度观察项
# ────────────────────────────────────────────────────────────────
def check_observations():
    section("[5] 灰度观察项（ChatGPT 指定三项）")
    print("  ① 首次真实有持仓后状态保存")
    print("     - trade_state.json 正常更新 / 无意外 _state_corrupted / fsync 不抛异常")
    print("  ② 首次真实平仓（沿用 Batch B）")
    print("     - open orders / algo orders / stop orders 三处收敛")
    print("     - close_phase / tombstone / registry 终态一致 / TG 通知正常")
    print("  ③ 下一次自然异常重启（**不人为制造**）")
    print("     - 检查 trade_state.json 是否仍然有效 → 为 D-009 fsync 提供真实断电样本")
    print()
    print("  触发异常时的回报方式：直接把本脚本输出 + TG 告警原文贴给 AI")


# ────────────────────────────────────────────────────────────────
def main():
    print("=" * 74)
    print("D-009 灰度观察检查器  (CLOSED / GRAY OBSERVATION)")
    print(f"检查时刻: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"预期 commit: {EXPECT_COMMIT}    模式: 只读（绝不写入任何状态文件）")
    print("=" * 74)

    check_version()
    check_state()
    check_bak()
    check_process()
    check_observations()

    print("\n" + "=" * 74)
    if ERR:
        print(f"❌ 结论：{len(ERR)} 项 ERROR（需立即处理），{len(WARN)} 项 WARN")
        for m in ERR:
            print(f"   - {m}")
        return 2
    if WARN:
        print(f"⚠️  结论：无 ERROR，{len(WARN)} 项 WARN（需关注但不必急停）")
        for m in WARN:
            print(f"   - {m}")
        return 1
    print("✅ 结论：全部正常，D-009 灰度观察进行中")
    return 0


if __name__ == '__main__':
    sys.exit(main())
