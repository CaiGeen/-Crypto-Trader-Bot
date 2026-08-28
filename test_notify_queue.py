# -*- coding: utf-8 -*-
"""
D-010 Batch 1（bot_runner 消费侧）离线验收测试（不连真实 Telegram、不连交易所）

背景（2026-08-28 VPN 换 IP 刷屏事故）：旧 .notify 单文件 + 消费端 Markdown 无降级 +
失败无限重试 + 失败路径触发汇总刷屏（E4）。Batch 1 将消费侧改造为事件队列模型。

设计依据：.workbuddy/memory/discussions/D-010_通知链路加固与2015分流_设计确认稿_v3.md
（v3.1 实施约束 C1-C4 + 故障注入场景 1/2/3/6）

覆盖场景（ChatGPT Batch 1 验收标准）：
  S1  场景1: 坏 Markdown（binance_error 下划线）→ 纯文本 fallback 成功 → DONE
  S2  场景2: TG 连续不可达 3 轮 → SILENCED + 文件保留 + state/audit 留痕
             + 第 4 轮（模拟重启后）0 次 TG 调用（C4 + 计数持久化）
  S3  场景3: 同内容双事件（不同 event_id）→ 计数互不干扰（v2 hash 键缺陷专项）
  S4  C3a: queue 有 state 无 → 新建 ACTIVE → 正常发送成功 → DONE
  S5  C3b: state 有 queue 无 → ORPHAN_STATE_IGNORED + audit 留痕 + 清条目 + 0 次 TG
  S6  场景6: 旧单文件 .notify → 一次性迁移到队列 → 消费送达
  S7  C1: 原子入队（tmp→flush→os.replace，AST 验证 + 无残留 tmp 文件）
  S8  E4: process_notifications 异常路径不再调用 send_summary_notification（AST 验证）
  S9  C2: DONE 语义 = 发送成功+删除完成；下一轮 0 次调用不重发
  S10 crash_alert 分支行为保持：TG 成功 + email + summary_cb 各 1 次
  S11 summary_restart 分支：summary 失败计 1 轮（有界重试）
  S12 state 损坏 → 重置为 {}（SILENCED 丢失但有界 3 轮，不崩溃）

用法: .venv\\Scripts\\python.exe test_notify_queue.py
"""
import asyncio
import ast
import json
import os
import sys
import tempfile
import shutil

from telegram.error import BadRequest

import bot_runner

RESULTS = []


def report(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"\n{'=' * 60}\n[{'PASS' if passed else 'FAIL'}] {name} {detail}\n{'=' * 60}")


# ---------------- 测试基建 ----------------

class FakeTgBot:
    """模拟 Telegram：Markdown 模式下文本含奇数个下划线 → BadRequest（真实行为）；
    always_fail 模式下任何发送都失败"""

    def __init__(self, always_fail=False):
        self.sent = []          # (text, parse_mode)
        self.calls = 0          # 总调用次数（含失败）
        self.always_fail = always_fail

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.calls += 1
        if self.always_fail:
            raise BadRequest("模拟：发送通道不可用")
        if parse_mode == 'Markdown' and text.count('_') % 2 == 1:
            raise BadRequest("Can't parse entities: can't find end of the entity")
        self.sent.append((text, parse_mode))


# 事故原样复现的消息（含奇数个下划线：binance_error 1 个 + 批次号 0 → 共 1 个）
IP_MSG = (
    "⚠️ IP 地址已变化！\n"
    "新 IP: 220.246.89.210\n"
    "来源: binance_error\n"
    "时间: 2026-08-28 09:56:03\n"
    "请将新 IP 添加到币安 API 白名单！"
)


class Env:
    """每个场景独立的临时目录：queue_dir / state_file / audit_log 三件套"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="d010_b1_")
        self.queue_dir = os.path.join(self.dir, ".notify_queue")
        self.state_file = os.path.join(self.dir, ".notify.state.json")
        self.audit_log = os.path.join(self.dir, ".notify_audit.log")
        os.makedirs(self.queue_dir, exist_ok=True)

    def enqueue(self, event_id, content):
        with open(os.path.join(self.queue_dir, f"{event_id}.notify"), "w", encoding="utf-8") as f:
            f.write(content)

    def queue_files(self):
        return sorted(f for f in os.listdir(self.queue_dir) if f.endswith(".notify"))

    def state(self):
        if not os.path.exists(self.state_file):
            return {}
        with open(self.state_file, encoding="utf-8") as f:
            return json.load(f)

    def audit_lines(self):
        if not os.path.exists(self.audit_log):
            return []
        with open(self.audit_log, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run_round(bot, env, summary_cb=None, email_cb=None):
    """执行一轮队列消费（生产循环 10s 周期的单轮体）"""
    return asyncio.run(bot_runner._process_notify_queue_once(
        bot, 12345,
        state_file=env.state_file, queue_dir=env.queue_dir, audit_log=env.audit_log,
        summary_cb=summary_cb, email_cb=email_cb,
    ))


def reset_summary_spam_guard():
    """防刷屏护栏（无关本测试语义，仅防意外重复）"""
    return None


# ---------------- 场景 ----------------

def scenario_1():
    """S1 场景1: 坏 Markdown（下划线）→ 纯文本 fallback 成功 → DONE"""
    env = Env()
    try:
        env.enqueue("20260828_095603_123456_4f82c1a7", f"ip_notify|{IP_MSG}")
        bot = FakeTgBot()
        stats = run_round(bot, env)

        ok = (stats.get('sent') == 1
              and len(bot.sent) == 1 and bot.sent[0][1] is None      # 纯文本送达
              and 'binance_error' in bot.sent[0][0]                   # 原内容完整
              and env.queue_files() == []                             # DONE：文件删除
              and env.state() == {})                                  # state 条目清除
        report("S1 场景1: 坏Markdown→纯文本fallback→DONE", ok,
               f"(sent={stats.get('sent')}, 发送={len(bot.sent)}, 模式={bot.sent[0][1] if bot.sent else None}, "
               f"队列={env.queue_files()}, state键={list(env.state().keys())})")
    finally:
        env.close()


def scenario_2():
    """S2 场景2: 3 轮失败 → SILENCED（文件保留+state/audit 留痕）→ 第 4 轮 0 次 TG 调用"""
    env = Env()
    try:
        eid = "20260828_095603_123456_4f82c1a7"
        env.enqueue(eid, f"ip_notify|{IP_MSG}")

        # 前 3 轮全部失败
        for i in range(3):
            stats = run_round(FakeTgBot(always_fail=True), env)
            assert stats.get('failed_rounds') == 1, f"第{i + 1}轮应计 1 次失败"

        st = env.state().get(eid, {})
        silenced_in_state = st.get('status') == 'SILENCED' and st.get('failed_attempts') == 3
        file_kept = env.queue_files() == [f"{eid}.notify"]
        audit_hit = any('SILENCED' in ln and eid in ln for ln in env.audit_lines())

        # 第 4 轮：全新 bot（模拟重启后进程），SILENCED 先于任何 TG 调用（C4）
        bot4 = FakeTgBot(always_fail=False)
        stats4 = run_round(bot4, env)
        zero_calls = bot4.calls == 0 and stats4.get('skipped_silenced') == 1

        ok = silenced_in_state and file_kept and audit_hit and zero_calls
        report("S2 场景2: 3轮→SILENCED+证据保留+audit留痕+第4轮0调用", ok,
               f"(status={st.get('status')}, attempts={st.get('failed_attempts')}, "
               f"队列文件={env.queue_files()}, audit命中={audit_hit}, 第4轮调用={bot4.calls})")
    finally:
        env.close()


def scenario_3():
    """S3 场景3: 同内容双事件（不同 event_id）→ 计数互不干扰（v2 hash 键缺陷专项）"""
    env = Env()
    try:
        eid_a = "20260828_100000_111111_aaaaaaaa"
        eid_b = "20260828_100500_222222_bbbbbbbb"
        env.enqueue(eid_a, f"ip_notify|{IP_MSG}")

        # 事件 A 先失败 2 轮
        for _ in range(2):
            run_round(FakeTgBot(always_fail=True), env)

        # 事件 B 到达（同内容、不同 event_id）
        env.enqueue(eid_b, f"ip_notify|{IP_MSG}")
        # 第 3 轮：A 失败第 3 轮 → SILENCED；B 失败第 1 轮
        run_round(FakeTgBot(always_fail=True), env)

        st = env.state()
        a = st.get(eid_a, {})
        b = st.get(eid_b, {})
        # v2 hash 键缺陷下 B 会共享 A 的计数而直接 SILENCED；v3 必须 B=1 ACTIVE
        ok = (a.get('failed_attempts') == 3 and a.get('status') == 'SILENCED'
              and b.get('failed_attempts') == 1 and b.get('status') == 'ACTIVE')
        report("S3 场景3: 同内容双事件计数互不干扰", ok,
               f"(A: attempts={a.get('failed_attempts')}, status={a.get('status')}; "
               f"B: attempts={b.get('failed_attempts')}, status={b.get('status')})")
    finally:
        env.close()


def scenario_4():
    """S4 C3a: queue 有 state 无 → 新建 ACTIVE → 正常发送成功 → DONE"""
    env = Env()
    try:
        # 手工制造"queue 有 state 无"：只写队列文件，不写该事件的 state
        env.enqueue("20260828_100000_111111_cccccccc", f"ip_notify|{IP_MSG}")
        # 同时预置一条 SILENCED 孤儿条目（设计规定：SILENCED 记录永久保留，不参与孤儿清理）
        with open(env.state_file, "w", encoding="utf-8") as f:
            json.dump({"silenced_event": {"content_sha256": "x" * 64, "failed_attempts": 3,
                                          "status": "SILENCED", "first_seen": "t", "last_attempt": "t"}}, f)

        bot = FakeTgBot()
        stats = run_round(bot, env)

        st = env.state()
        ok = (stats.get('sent') == 1
              and len(bot.sent) == 1
              and env.queue_files() == []
              and 'silenced_event' in st                                  # SILENCED 记录保留
              and not any(k.endswith('cccccccc') for k in st))            # 新事件 DONE 后清除
        report("S4 C3a: queue有state无→新建ACTIVE→发送成功DONE", ok,
               f"(sent={stats.get('sent')}, 队列={env.queue_files()}, state键={list(st.keys())})")
    finally:
        env.close()


def scenario_5():
    """S5 C3b: state 有 queue 无 → ORPHAN_STATE_IGNORED + audit 留痕 + 清条目 + 0 次 TG"""
    env = Env()
    try:
        orphan = "20260828_100000_111111_dddddddd"
        with open(env.state_file, "w", encoding="utf-8") as f:
            json.dump({orphan: {"content_sha256": "x" * 64, "failed_attempts": 1,
                                 "status": "ACTIVE", "first_seen": "t", "last_attempt": "t"}}, f)

        bot = FakeTgBot()
        stats = run_round(bot, env)

        audit_hit = any('ORPHAN_STATE_IGNORED' in ln and orphan in ln for ln in env.audit_lines())
        ok = (bot.calls == 0                                           # 不重发
              and env.state() == {}                                    # 条目清除
              and audit_hit)
        report("S5 C3b: orphan state→IGNORED+audit+清条目+0次TG", ok,
               f"(调用={bot.calls}, state键={list(env.state().keys())}, audit命中={audit_hit})")
    finally:
        env.close()


def scenario_6():
    """S6 场景6: 旧单文件 .notify → 一次性迁移 → 消费送达"""
    base = tempfile.mkdtemp(prefix="d010_b1_legacy_")
    try:
        legacy = os.path.join(base, ".notify")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write(f"ip_notify|{IP_MSG}")

        event_id = bot_runner._migrate_legacy_notify(base_dir=base)

        qdir = os.path.join(base, ".notify_queue")
        migrated = (event_id is not None
                    and not os.path.exists(legacy)                       # 旧文件已移除
                    and os.path.isdir(qdir)
                    and os.listdir(qdir) == [f"{event_id}.notify"])
        content_ok = False
        if migrated:
            with open(os.path.join(qdir, f"{event_id}.notify"), encoding="utf-8") as f:
                content_ok = f.read() == f"ip_notify|{IP_MSG}"

        # 迁移产物进入正常消费链路 → 纯文本送达
        sent = False
        if migrated:
            env = Env()
            try:
                for fn in os.listdir(qdir):
                    shutil.copy(os.path.join(qdir, fn), os.path.join(env.queue_dir, fn))
                bot = FakeTgBot()
                stats = run_round(bot, env)
                sent = stats.get('sent') == 1 and bot.sent and bot.sent[0][1] is None
            finally:
                env.close()

        ok = migrated and content_ok and sent
        report("S6 场景6: 旧.notify迁移→消费送达", ok,
               f"(event_id={event_id}, 旧文件已删={not os.path.exists(legacy)}, "
               f"内容一致={content_ok}, 迁移后送达={sent})")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def scenario_7():
    """S7 C1: 原子入队（AST 验证 + 无残留 tmp 文件）"""
    # AST 验证：_write_notify_event_file 必须用 tempfile + os.replace，禁止直接 open(final,'w')
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_runner.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_write_notify_event_file':
            fn = node
    ast_ok = fn is not None
    if ast_ok:
        fn_src = ast.get_source_segment(src, fn)
        uses_replace = 'os.replace' in fn_src
        uses_tmp = 'tempfile' in fn_src or 'mkstemp' in fn_src
        # 禁止对 final 文件直接 open(...,'w')
        no_direct = f"open(os.path.join(" not in fn_src.replace("tempfile", "")
        ast_ok = uses_replace and uses_tmp and no_direct

    # 行为验证：入队后目录中只有最终文件，无 tmp 残留
    env = Env()
    try:
        eid = bot_runner._write_notify_event_file(f"ip_notify|{IP_MSG}", queue_dir=env.queue_dir)
        leftovers = [f for f in os.listdir(env.queue_dir) if not f.endswith('.notify')]
        final_exists = eid is not None and os.listdir(env.queue_dir) == [f"{eid}.notify"]
        ok = ast_ok and eid is not None and leftovers == [] and final_exists
        report("S7 C1: 原子入队(tmp→flush→os.replace)", ok,
               f"(AST={ast_ok}, event_id={eid}, 残留tmp={leftovers}, 目录={os.listdir(env.queue_dir)})")
    finally:
        env.close()


def scenario_8():
    """S8 E4: process_notifications 异常路径不再调用 send_summary_notification（AST 验证）"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_runner.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'process_notifications':
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler):
                    for sub2 in ast.walk(sub):
                        if (isinstance(sub2, ast.Call) and isinstance(sub2.func, ast.Attribute)
                                and sub2.func.attr == 'send_summary_notification'):
                            offenders.append(sub.lineno)
    ok = not offenders
    report("S8 E4: 通知失败路径不再触发汇总刷屏", ok, f"(异常路径内违规调用行号: {offenders})")


def scenario_9():
    """S9 C2: DONE 语义 = 发送成功+删除完成；下一轮 0 次调用不重发"""
    env = Env()
    try:
        env.enqueue("20260828_100000_111111_eeeeeeee", f"ip_notify|{IP_MSG}")
        bot1 = FakeTgBot()
        run_round(bot1, env)

        # 第二轮：文件已删，state 无条目 → 零调用
        bot2 = FakeTgBot()
        stats2 = run_round(bot2, env)
        ok = (bot1.calls == 2                                      # 首轮 Markdown 失败 + 纯文本成功
              and bot2.calls == 0
              and stats2.get('processed') == 0
              and env.queue_files() == []
              and env.state() == {})
        report("S9 C2: DONE后不重发(有界重复投递)", ok,
               f"(首轮调用={bot1.calls}, 次轮调用={bot2.calls}, 队列={env.queue_files()})")
    finally:
        env.close()


def scenario_10():
    """S10 crash_alert 分支行为保持：TG 成功 + email + summary_cb 各 1 次"""
    env = Env()
    try:
        env.enqueue("20260828_100000_111111_ffffffff", "crash_alert|程序异常退出: RuntimeError")
        bot = FakeTgBot()

        email_calls = []

        async def summary_cb():
            summary_calls.append(1)

        summary_calls = []
        stats = run_round(bot, env, summary_cb=summary_cb, email_cb=lambda *a, **k: email_calls.append(a))

        ok = (stats.get('sent') == 1
              and len(bot.sent) == 1
              and len(email_calls) == 1                              # 邮件兜底 1 次
              and len(summary_calls) == 1                            # 崩溃后汇总 1 次
              and env.queue_files() == [])
        report("S10 crash_alert: TG+email+summary各1次", ok,
               f"(sent={stats.get('sent')}, email={len(email_calls)}, summary={len(summary_calls)})")
    finally:
        env.close()


def scenario_11():
    """S11 summary_restart 分支：summary 失败计 1 轮（有界重试，3 轮后 SILENCED）"""
    env = Env()
    try:
        eid = "20260828_100000_111111_10101010"
        env.enqueue(eid, "summary_restart|watchdog restarted")

        async def bad_summary():
            raise RuntimeError("模拟汇总发送失败")

        # 3 轮 summary 失败 → SILENCED
        for _ in range(3):
            run_round(FakeTgBot(), env, summary_cb=bad_summary)

        st = env.state().get(eid, {})
        ok = (st.get('status') == 'SILENCED' and st.get('failed_attempts') == 3
              and env.queue_files() == [f"{eid}.notify"]             # 证据保留
              and any('SILENCED' in ln for ln in env.audit_lines()))
        report("S11 summary_restart: 失败计轮次+3轮SILENCED", ok,
               f"(status={st.get('status')}, attempts={st.get('failed_attempts')}, 文件保留={len(env.queue_files())})")
    finally:
        env.close()


def scenario_12():
    """S12 state 损坏 → 重置为 {}（不崩溃，SILENCED 丢失但有界 3 轮）"""
    env = Env()
    try:
        with open(env.state_file, "w", encoding="utf-8") as f:
            f.write("{not valid json!!")
        st = bot_runner._load_notify_state(env.state_file)
        ok = st == {}
        report("S12 state损坏→重置空dict", ok, f"(load 结果: {st!r})")
    finally:
        env.close()


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    scenario_5()
    scenario_6()
    scenario_7()
    scenario_8()
    scenario_9()
    scenario_10()
    scenario_11()
    scenario_12()
    print("\n" + "#" * 60)
    failed = [n for n, p in RESULTS if not p]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 个场景失败: {failed}")
        sys.exit(1)
    print(f"✅ 全部 {len(RESULTS)} 个场景通过")
    print("D-010 Batch 1（bot_runner 消费侧）验收完成")
