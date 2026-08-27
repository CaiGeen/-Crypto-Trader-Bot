# -*- coding: utf-8 -*-
"""
D-005（2026-08-27 信号入口幂等去重）专项测试

背景：parser 每次解析重新生成 batch_id → 同一信号重发/快捷指令双击 = 新批次 =
重复开仓。修复：run_trader_execution 单一咽喉处指纹拦截 + signal_dedup.json
持久化 + /force 人工放行。

定稿设计（ChatGPT 交叉审 + 2 处反驳）：
  - 指纹 = 信号全字段剔除 batch_id（新字段自动入哈希）
  - 状态只有 EXECUTING/SUCCESS，无 FAILED——execute_signal except 兜底 return None
    不清理已挂单，"干净失败"与"部分成交后异常"不可分，FAILED 允许重发会翻倍仓位
  - 10 分钟时间窗自解 + /force 一次性放行（5 分钟 TTL）= Fail-Closed but not Fail-Stuck

本文件覆盖（纯函数层，注入时间与临时文件，不碰 TG/交易所）：
  T1  指纹确定性 + batch_id 排除（重发同信号 → 同指纹）
  T2  指纹区分度（同标的不同入场价 → 不同指纹 → 不拦截）
  T3  首次放行（first-seen，记录 EXECUTING）
  T4  窗口内二次提交拦截（双击模拟）
  T5  窗口过期自解锁（含崩溃残留 EXECUTING 场景）
  T6  SUCCESS 回写 + 窗口内仍拦截
  T7  /force 一次性放行（放行一次后标记清除，再拦）
  T8  /force TTL 过期不再放行
  T9  execute_signal 返回 None 不置 FAILED（保持 EXECUTING，防部分成交重发翻倍）
  T10 72h 保留期清理
  T11 去重表损坏降级为空表（best-effort，SG1/SG2 仍兜底）
  T12 /force 前缀歧义拒绝 + 过短短码拒绝
  T13 stop_loss_steps 参与指纹（不同阶梯止损 = 不同信号）
  T14 源码锚点：闸门位于 execute_signal 之前、/force 注册、唯一入口不旁路

运行：.venv/Scripts/python.exe test_dedup_entry.py（ccxt 只在项目 .venv）
"""
import os
import sys
import json
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot_runner
from parser import TradeSignal

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


def make_signal(entry_price=64650.0, sl=64600.0, batch_id=None):
    return TradeSignal(
        symbol="BTC/USDT:USDT",
        side="BUY",
        leverage=100,
        entries=[(entry_price, 0.001), (64800.0, 0.001)],
        stop_loss_steps=[sl, 64601.0],
        take_profit=66000.0,
        initial_stop_loss=64000.0,
        batch_id=batch_id,
    )


def main():
    tmpdir = tempfile.mkdtemp(prefix="d005_")
    dedup_path = os.path.join(tmpdir, "signal_dedup.json")
    T0 = 1_800_000_000.0  # 固定时间基点，全程注入

    # ---------- T1 指纹确定性 + batch_id 排除 ----------
    fp_a = bot_runner._signal_fingerprint(make_signal(batch_id="batch_A"))
    fp_b = bot_runner._signal_fingerprint(make_signal(batch_id="batch_B"))
    report("T1 指纹确定且排除 batch_id", fp_a == fp_b and len(fp_a) == 64)

    # ---------- T2 指纹区分度 ----------
    fp_diff = bot_runner._signal_fingerprint(make_signal(entry_price=64651.0))
    report("T2 不同入场价 → 不同指纹", fp_a != fp_diff)

    # ---------- T3 首次放行 ----------
    ok, info = bot_runner._check_and_record_dedup(fp_a, now=T0, path=dedup_path)
    rec = json.load(open(dedup_path, encoding="utf-8"))[fp_a]
    report("T3 首次放行并记录 EXECUTING",
           ok is True and info == 'first-seen' and rec['status'] == 'EXECUTING')

    # ---------- T4 窗口内二次提交拦截（双击模拟） ----------
    ok2, info2 = bot_runner._check_and_record_dedup(fp_a, now=T0 + 5, path=dedup_path)
    report("T4 窗口内拦截（双击）",
           ok2 is False and '剩' in info2 and 'EXECUTING' in info2)

    # ---------- T5 窗口过期自解锁（含崩溃残留 EXECUTING） ----------
    ok3, info3 = bot_runner._check_and_record_dedup(
        fp_a, now=T0 + bot_runner.SIGNAL_DEDUP_WINDOW_SEC + 1, path=dedup_path)
    report("T5 EXECUTING 残留窗口过期自解锁（崩溃恢复语义）",
           ok3 is True and info3 == 'window-expired')

    # ---------- T6 SUCCESS 回写 + 窗口内拦截 ----------
    bot_runner._mark_dedup_result(fp_a, "batch_success_001", now=T0 + 700, path=dedup_path)
    rec = json.load(open(dedup_path, encoding="utf-8"))[fp_a]
    ok4, info4 = bot_runner._check_and_record_dedup(fp_a, now=T0 + 705, path=dedup_path)
    report("T6 SUCCESS 回写 + 窗口内仍拦截",
           rec['status'] == 'SUCCESS' and rec['batch_id'] == 'batch_success_001'
           and ok4 is False and 'batch_success_001' in info4)

    # ---------- T7 /force 一次性放行 ----------
    okf, msgf = bot_runner._approve_dedup_force(fp_a[:8], now=T0 + 710, path=dedup_path)
    ok5, info5 = bot_runner._check_and_record_dedup(fp_a, now=T0 + 711, path=dedup_path)
    ok6, info6 = bot_runner._check_and_record_dedup(fp_a, now=T0 + 712, path=dedup_path)
    report("T7 /force 一次性放行（放行后标记清除）",
           okf is True and msgf == fp_a[:8] and ok5 is True and info5 == 'force-approved'
           and ok6 is False)

    # ---------- T8 /force TTL 过期 ----------
    okf2, _ = bot_runner._approve_dedup_force(fp_a[:8], now=T0 + 1000, path=dedup_path)
    # approved_ts=T0+1000, TTL 300s；approved 前记录 last_seen=T0+712
    # 检查时刻 T0+1401：approved 已过期（1401-1000=401 > 300），窗口年龄 689 > 600 → 放行但走 window-expired 而非 force
    ok7, info7 = bot_runner._check_and_record_dedup(fp_a, now=T0 + 1401, path=dedup_path)
    report("T8 /force TTL 过期不再以 force 放行",
           okf2 is True and ok7 is True and info7 != 'force-approved')

    # ---------- T9 None 返回不置 FAILED（防部分成交重发翻倍） ----------
    fp_c = bot_runner._signal_fingerprint(make_signal(entry_price=65000.0))
    bot_runner._check_and_record_dedup(fp_c, now=T0, path=dedup_path)
    bot_runner._mark_dedup_result(fp_c, None, now=T0 + 30, path=dedup_path)
    rec = json.load(open(dedup_path, encoding="utf-8"))[fp_c]
    ok8, _ = bot_runner._check_and_record_dedup(fp_c, now=T0 + 35, path=dedup_path)
    report("T9 None 返回保持 EXECUTING 且窗口内拦截",
           rec['status'] == 'EXECUTING' and ok8 is False)

    # ---------- T10 72h 保留期清理 ----------
    fp_old = bot_runner._signal_fingerprint(make_signal(entry_price=70000.0))
    data = {fp_old: {'status': 'SUCCESS', 'first_seen': T0, 'last_seen': T0,
                     'batch_id': 'b_old', 'approved': False, 'approved_ts': None}}
    with open(dedup_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    loaded = bot_runner._load_dedup(dedup_path, now=T0 + bot_runner.SIGNAL_DEDUP_RETENTION_SEC + 1)
    report("T10 超 72h 记录被清理", fp_old not in loaded)

    # ---------- T11 去重表损坏降级 ----------
    with open(dedup_path, 'w', encoding='utf-8') as f:
        f.write("{corrupted json !!!")
    loaded = bot_runner._load_dedup(dedup_path, now=T0)
    ok9, _ = bot_runner._check_and_record_dedup(fp_a, now=T0, path=dedup_path)
    report("T11 损坏文件降级为空表且放行（best-effort 防线）",
           loaded == {} and ok9 is True)

    # ---------- T12 /force 前缀校验 ----------
    ok_short, msg_short = bot_runner._approve_dedup_force("ab", now=T0, path=dedup_path)
    ok_none, msg_none = bot_runner._approve_dedup_force("deadbee", now=T0, path=dedup_path)
    # 造两条同前缀记录验证歧义拒绝
    data = json.load(open(dedup_path, encoding='utf-8'))
    twin = "abcdef01" + "0" * 56
    twin2 = "abcdef02" + "0" * 56
    data[twin] = {'status': 'SUCCESS', 'first_seen': T0, 'last_seen': T0,
                  'batch_id': None, 'approved': False, 'approved_ts': None}
    data[twin2] = dict(data[twin])
    bot_runner._save_dedup(data, dedup_path)
    ok_amb, msg_amb = bot_runner._approve_dedup_force("abcdef0", now=T0, path=dedup_path)
    report("T12 /force 过短拒绝 + 无记录拒绝 + 前缀歧义拒绝",
           ok_short is False and ok_none is False and ok_amb is False
           and '条记录' in msg_amb)

    # ---------- T13 stop_loss_steps 参与指纹 ----------
    fp_s1 = bot_runner._signal_fingerprint(make_signal(sl=64600.0))
    fp_s2 = bot_runner._signal_fingerprint(make_signal(sl=64605.0))
    report("T13 不同阶梯止损 → 不同指纹", fp_s1 != fp_s2)

    # ---------- T14 源码锚点 ----------
    src = open("bot_runner.py", encoding="utf-8").read()
    anchor_gate = src.find("_check_and_record_dedup(fingerprint)")
    anchor_exec = src.find("trader.execute_signal, signal")
    anchor_force = "CommandHandler(\"force\", force_command)" in src
    anchor_mark = "_mark_dedup_result(fingerprint, batch_id)" in src
    # 闸门必须出现在 execute_signal 调用之前（含 force 闸门那一次在内的第一次出现）
    first_gate = src.find("_check_and_record_dedup(fingerprint)")
    report("T14 闸门先于 execute_signal + /force 注册 + 结果回写",
           anchor_gate != -1 and first_gate < anchor_exec and anchor_force and anchor_mark)

    # ---------- 汇总 ----------
    failed = [n for n, p in RESULTS if not p]
    print(f"\n{'=' * 60}\nD-005 专项测试: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败项:", ", ".join(failed))
    print('=' * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
