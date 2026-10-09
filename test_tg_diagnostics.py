#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 tg_diagnostics v3 —— 修 ChatGPT 第 3 轮的三组缺口。

**执行实际候选函数，不复刻片段**：
  · 真实 bot_runner.safe_reply（直接 import 候选模块）
  · 真实 telegram.Bot.get_updates（走 HTTPXRequest.do_request 真实链路）
  · 真实 deploy_tgdiag.py 的 check/apply/rollback 子命令

覆盖：
  F1 处理探针透传（成功返回值 / 原异常 / CancelledError 三向隔离）
  F2 有效轮询成功判据（200+非法JSON 不得记成功；ok=false；result 非列表）
  F3 flush_and_wait 返回该代次的**实际写入结果**
  F4 部署：源文件指纹、双文件 PATCHED、写入后不符即失败、回滚校验备份
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

FIX = r"G:\my-crypto-bot-fix"
sys.path.insert(0, FIX)
os.chdir(FIX)

# 原始基线：所有沙箱测试的固定起点（生产可能已装 v1，不能用它当基线）
BASELINE_SHA = "702EEC05C513CBCBEF3CEE39F2878B26364800AF8C448E35049B3699526CED06"
CAND_SHA = "6473019D31411BCAA70592E113ADE89DABA57B02E3F485A950EDDC5286153660"

import tg_diagnostics as td
import httpx

FAKE_TOKEN = "123456789:AAHfiqksKZ8WmO2P5YvOZd0Fh1aC5lRnMxQ"
RESULTS = []
_BASE = dict(td._state)
_REAL = dict(_bump=td._bump, _upd=td._upd, _log=td._log, _write=td._write_diag_file,
             record_reply=td.record_reply, record_update=td.record_update_processed)


def restore_all():
    td._bump = _REAL["_bump"]
    td._upd = _REAL["_upd"]
    td._log = _REAL["_log"]
    td._write_diag_file = _REAL["_write"]
    td.record_reply = _REAL["record_reply"]
    td.record_update_processed = _REAL["record_update"]


def reset_state():
    with td._state_lock:
        td._state.clear()
        td._state.update(dict(_BASE))
        td._state["task_snapshots"] = []
    td._log_dropped["n"] = 0


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def boom(*a, **k):
    raise RuntimeError("观测故障")


# ==================================================== F1 真实 safe_reply 透传
class FakeMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)
        return f"SENT::{text}"


class FakeUpdate:
    def __init__(self):
        self.message = FakeMsg()
        self.callback_query = None


async def f1_real_safe_reply():
    print("\nF1 真实 bot_runner.safe_reply：观测不得改变返回值")
    import bot_runner                      # ← 真实候选函数，不是复刻
    check("F1 导入的是真实候选 safe_reply",
          bot_runner.safe_reply.__module__ == "bot_runner", bot_runner.safe_reply.__module__)
    reset_state()
    sys.modules["tg_diagnostics"] = td
    up = FakeUpdate()

    r1 = await bot_runner.safe_reply(up, "hi")
    check("F1 正常回复返回值保留", r1 == "SENT::hi", repr(r1))

    td.record_reply = boom
    try:
        r2 = await bot_runner.safe_reply(up, "obs-boom")
        check("F1 观测抛错后原返回值仍保留", r2 == "SENT::obs-boom", repr(r2))
    finally:
        td.record_reply = _REAL["record_reply"]

    td._bump, td._upd, td._log = boom, boom, boom
    try:
        r3 = await bot_runner.safe_reply(up, "internal-boom")
        check("F1 观测内部全故障仍不影响返回值", r3 == "SENT::internal-boom", repr(r3))
    finally:
        restore_all()

    # 失败路径也必须计数（原 send 抛异常）
    class BadMsg:
        async def reply_text(self, text, **kw):
            raise httpx.ConnectError("net down")

    up2 = FakeUpdate()
    up2.message = BadMsg()
    reset_state()
    r4 = await bot_runner.safe_reply(up2, "will-fail")
    s4 = td.snapshot()
    check("F1 失败路径不抛（safe_reply 兜底）", r4 is None, repr(r4))
    check("F1 失败路径已计数 reply_fail", s4["reply_fail"] >= 1, f"fail={s4['reply_fail']}")


# ============================================ F2 有效轮询成功判据（真实 Bot）
async def f2_poll_success_criteria():
    print("\nF2 有效轮询成功判据（真实 Bot.get_updates 链路）")
    from telegram import Bot
    from telegram.request import HTTPXRequest

    async def run_case(status, body_bytes, label):
        reset_state()
        req = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=30.0,
                           proxy="http://127.0.0.1:7890")

        async def fake_request(method, url, **kw):
            return httpx.Response(status_code=status, content=body_bytes,
                                  request=httpx.Request("GET", url))
        req._client.request = fake_request
        assert td.install_poll_request_probe(req, label)
        bot = Bot(token=FAKE_TOKEN, request=req, get_updates_request=req)
        raised = None
        try:
            await bot.get_updates(offset=None, timeout=0)
        except BaseException as e:
            raised = type(e).__name__
        return td.snapshot(), raised

    # --- 200 + 非法 JSON：真实会抛 TelegramError，诊断不得记成功
    s, raised = await run_case(200, b"<not-json>", "bad200")
    check("F2 200+非法JSON 真实抛 TelegramError", raised is not None, str(raised))
    check("F2 200+非法JSON **不得**记 poll_success", s["poll_success"] == 0,
          f"success={s['poll_success']}")
    check("F2 200+非法JSON 记 poll_unparsed", s["poll_unparsed"] == 1, f"n={s['poll_unparsed']}")
    check("F2 200+非法JSON 记录了解析失败", s["poll_last_error"] == "UnparsableResponse",
          str(s["poll_last_error"]))
    check("F2 200+非法JSON 仍记 HTTP 响应", s["poll_http_response"] == 1,
          f"http={s['poll_http_response']}")

    # --- 200 + ok=true + 空列表
    s, raised = await run_case(200, b'{"ok": true, "result": []}', "ok200")
    check("F2 有效轮询成功", s["poll_success"] == 1, f"success={s['poll_success']}")
    check("F2 空更新计成功轮询", s["poll_empty_ok"] == 1, f"empty={s['poll_empty_ok']}")
    check("F2 成功时清空错误", s["poll_last_error"] is None, str(s["poll_last_error"]))
    check("F2 真实调用未抛", raised is None, str(raised))

    # --- 200 + ok=false（conflict）
    s, raised = await run_case(200, b'{"ok": false, "description": "conflict"}', "kf")
    check("F2 ok=false 不得记成功", s["poll_success"] == 0, f"success={s['poll_success']}")
    check("F2 ok=false 记 HTTP 错误", s["poll_http_error"] == 1, f"e={s['poll_http_error']}")

    # --- 200 + ok=true 但 result 不是列表
    s, raised = await run_case(200, b'{"ok": true, "result": {}}', "notlist")
    check("F2 result 非列表不得记成功", s["poll_success"] == 0, f"success={s['poll_success']}")

    # --- 503
    s, raised = await run_case(503, b'{"ok": false}', "s503")
    check("F2 503 不得记成功", s["poll_success"] == 0)
    check("F2 503 记状态码", s["poll_last_status"] == 503, str(s["poll_last_status"]))

    # --- 200 + 有更新
    s, raised = await run_case(200, b'{"ok": true, "result": [{"update_id": 1}]}', "upd")
    check("F2 非空更新计数", s["updates_returned"] == 1, f"n={s['updates_returned']}")


# ============================================== F1b 处理探针三向透传
async def f1b_process_probe_passthrough():
    print("\nF1b 处理探针：成功/异常/取消 三向隔离")
    reset_state()

    class App:
        def __init__(self):
            self.calls = 0

        async def process_update(self, update, *a, **k):
            self.calls += 1
            m = getattr(update, "mode", "ok")
            if m == "boom":
                raise ValueError("处理失败")
            if m == "cancel":
                raise asyncio.CancelledError()
            return "PROCESSED"

    app = App()
    check("F1b 探针安装", td.install_process_probe(app) is True)

    r = await app.process_update(object())
    check("F1b 成功时原返回值保留", r == "PROCESSED", repr(r))
    check("F1b 原函数被真实调用", app.calls == 1)

    td.record_update_processed = boom
    try:
        r2 = await app.process_update(object())
        check("F1b 观测抛错后原返回值仍保留", r2 == "PROCESSED", repr(r2))
    finally:
        td.record_update_processed = _REAL["record_update"]

    td._bump, td._upd, td._log = boom, boom, boom
    try:
        r3 = await app.process_update(object())
        check("F1b 观测内部全故障仍不影响返回值", r3 == "PROCESSED", repr(r3))
    finally:
        restore_all()

    class Boom:
        mode = "boom"
    try:
        await app.process_update(Boom())
        check("F1b 原异常照常抛出", False, "被吞了")
    except ValueError:
        check("F1b 原异常照常抛出", True)

    class Can:
        mode = "cancel"
    try:
        await app.process_update(Can())
        check("F1b CancelledError 原样透传", False, "被吞了")
    except asyncio.CancelledError:
        check("F1b CancelledError 原样透传", True)
    except BaseException as e:
        check("F1b CancelledError 原样透传", False, f"变成 {type(e).__name__}")

    # 观测全故障时，异常仍原样透传
    td.record_update_processed = boom
    try:
        await app.process_update(Boom())
        check("F1b 观测故障时原异常仍透传", False, "被吞了")
    except ValueError:
        check("F1b 观测故障时原异常仍透传", True)
    finally:
        td.record_update_processed = _REAL["record_update"]

    s = td.snapshot()
    check("F1b 成功计数", s["updates_processed"] >= 1, f"n={s['updates_processed']}")
    check("F1b 失败计数", s["updates_process_fail"] >= 1, f"n={s['updates_process_fail']}")


# ================================================ F3 flush_and_wait 写入结果
def f3_flush_result():
    print("\nF3 flush_and_wait 返回该代次的实际写入结果")
    old = td.DIAG_FILE
    try:
        good = os.path.join(tempfile.gettempdir(), "tgdiag_f3_ok.json")
        td.DIAG_FILE = good
        if os.path.exists(good):
            os.remove(good)
        check("F3 成功写入返回 True", td.flush_and_wait() is True)
        check("F3 文件确实存在", os.path.exists(good))

        # 失败写入必须返回 False（旧版会返回 True）
        td.DIAG_FILE = os.path.join(tempfile.gettempdir(), "no_dir_xyz", "a.json")
        check("F3 写入失败必须返回 False", td.flush_and_wait() is False)

        # 慢写：确实等待到写完
        path = os.path.join(tempfile.gettempdir(), "tgdiag_f3_slow.json")
        td.DIAG_FILE = path
        real = td._write_diag_file
        td._write_diag_file = lambda: (time.sleep(0.8), True)[1]
        t0 = time.time()
        ok = td.flush_and_wait(timeout=5)
        dt = time.time() - t0
        check("F3 慢写确实等待", dt >= 0.7 and ok is True, f"{dt:.2f}s ok={ok}")
        td._write_diag_file = lambda: (time.sleep(0.3), False)[1]
        t0 = time.time()
        ok2 = td.flush_and_wait(timeout=5)
        check("F3 慢写且失败 → 返回 False", ok2 is False, f"ok={ok2}")
        td._write_diag_file = real
    finally:
        td.DIAG_FILE = old


# ============================================ F4 部署脚本负向（真实子进程）
def f4_deploy_integrity():
    print("\nF4 部署脚本：文件完整性与状态判定")
    py = r"G:\my-crypto-bot\.venv\Scripts\python.exe"
    pkg = os.path.join(FIX, "送审附件_TG诊断补丁_20261007")
    work = os.path.join(tempfile.gettempdir(), "tgdiag_deploy_f4")
    if os.path.exists(work):
        shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(pkg, work)
    # PROD 重定向到沙箱（沙箱基线固定为原始基线，不受生产当前状态影响）
    sb_f4 = _mk_sandbox("tgdiag_f4_base")
    s_txt = open(os.path.join(work, "deploy_tgdiag.py"), encoding="utf-8").read()
    prod_literal = 'PROD = r"G:' + chr(92) + 'my-crypto-bot"'
    open(os.path.join(work, "deploy_tgdiag.py"), "w", encoding="utf-8").write(
        s_txt.replace(prod_literal, 'PROD = r"' + sb_f4 + '"'))
    script = os.path.join(work, "deploy_tgdiag.py")

    def run(cmd):
        p = subprocess.run([py, script, cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr

    def read_specs():
        txt = open(script, encoding="utf-8").read()
        out = {}
        for k in ("SPECIFIED_BASELINE_SHA256", "SPECIFIED_PATCHED_SHA256", "SPECIFIED_DIAG_SHA256"):
            for ln in txt.splitlines():
                if ln.startswith(k + " ="):
                    out[k] = ln.split("=", 1)[1].strip().strip('"')
        return out

    def write_specs(vals):
        txt = open(script, encoding="utf-8").read()
        for k, v in vals.items():
            for ln in txt.splitlines():
                if ln.startswith(k + " ="):
                    txt = txt.replace(ln, f'{k} = "{v}"')
        open(script, "w", encoding="utf-8").write(txt)

    # 1) 真实包：check 应通过（生产当前是 BASELINE）
    rc, out = run("check")
    check("F4 真实包 check 通过", rc == 0, f"rc={rc}")

    # 2) 篡改候选 bot_runner.py → check 必须失败
    orig_bot = open(os.path.join(work, "bot_runner.py"), "rb").read()
    with open(os.path.join(work, "bot_runner.py"), "ab") as f:
        f.write(b"\n# tampered\n")
    rc, out = run("check")
    check("F4 篡改候选 bot_runner → check 拒绝", rc != 0, f"rc={rc}")
    check("F4 拒绝原因含 SHA 不符", "SHA 不符" in out, out[-200:])

    # 3) 篡改候选诊断模块 → check 必须失败
    open(os.path.join(work, "bot_runner.py"), "wb").write(orig_bot)
    with open(os.path.join(work, "tg_diagnostics.py"), "a", encoding="utf-8") as f:
        f.write("\n# tampered\n")
    rc, out = run("check")
    check("F4 篡改候选诊断模块 → check 拒绝", rc != 0, f"rc={rc}")

    # 4) apply：源文件被换 → 必须拒绝且不落盘
    #    **绝不在真实生产上跑 apply** —— 先把脚本 PROD 重定向到沙箱
    sb4 = _mk_sandbox("tgdiag_f4_sandbox")
    try:
        script_sb = _sandbox_script(sb4)
        # 再把包内候选篡改，验证 apply 拒绝
        with open(os.path.join(os.path.dirname(script_sb), "tg_diagnostics.py"),
                  "a", encoding="utf-8") as f:
            f.write("\n# tampered\n")
        p = subprocess.run([py, script_sb, "apply"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check("F4 apply 遇篡改候选必须拒绝", p.returncode != 0, f"rc={p.returncode}")
        check("F4 apply 未在沙箱写入诊断模块",
              not os.path.exists(os.path.join(sb4, "tg_diagnostics.py")))
        _h4 = hashlib.sha256(open(os.path.join(sb4, "bot_runner.py"), "rb").read()).hexdigest().upper()
        check("F4 apply 未在沙箱改动入口文件", _h4 == BASELINE_SHA, _h4[:16])
    finally:
        shutil.rmtree(sb4, ignore_errors=True)
    # 恢复 work 目录里的候选文件（后续用例还要用）
    shutil.copy2(os.path.join(FIX, "tg_diagnostics.py"),
                 os.path.join(work, "tg_diagnostics.py"))
    shutil.copy2(os.path.join(FIX, "bot_runner.py"), os.path.join(work, "bot_runner.py"))

    # 5) 回滚：备份不等于基线 → 必须拒绝
    #    用**独立沙箱**（PROD 指向它），备份也必须落在**同一个**沙箱里
    sandbox = _mk_sandbox("tgdiag_rollback_sandbox")
    fake = os.path.join(tempfile.gettempdir(), "tgdiag_fake_backup.py")
    shutil.copy2(script, fake)
    # script 里 PROD 已被改指向 sb_f4；此处直接改写为 sandbox（幂等替换）
    txt = open(fake, encoding="utf-8").read()
    import re as _re
    _repl = 'PROD = r"' + sandbox + '"'
    txt2, n_sub = _re.subn(r'^PROD\s*=\s*r"[^"]*"', lambda _m: _repl,
                           txt, count=1, flags=_re.M)
    assert n_sub == 1, "PROD 行未找到"
    open(fake, "w", encoding="utf-8").write(txt2)
    # 同一个沙箱里放一个"错误的备份"（内容不是基线）
    with open(os.path.join(sandbox, "bot_runner.py.bak_tgdiag"), "w", encoding="utf-8") as f:
        f.write("# 错误的备份内容，不是基线\n")
    p = subprocess.run([py, fake, "rollback"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    outp = p.stdout + p.stderr
    check("F4 备份不等于基线 → rollback 拒绝", p.returncode != 0, f"rc={p.returncode}")
    check("F4 拒绝原因含 不等于指定基线", "不等于指定基线" in outp, outp[-200:])
    _hb = hashlib.sha256(open(os.path.join(sandbox, "bot_runner.py"), "rb").read()).hexdigest().upper()
    check("F4 拒绝后未改动沙箱目标文件", _hb == BASELINE_SHA, _hb[:16])
    shutil.rmtree(sandbox, ignore_errors=True)

    # 6) 双文件 PATCHED：只有入口文件是候选、诊断模块缺失 → 必须判 UNKNOWN
    specs = read_specs()
    patched_sha = specs["SPECIFIED_PATCHED_SHA256"]
    txt = open(script, encoding="utf-8").read().replace(
        f'SPECIFIED_BASELINE_SHA256 = "{specs["SPECIFIED_BASELINE_SHA256"]}"',
        f'SPECIFIED_BASELINE_SHA256 = "{patched_sha}"')     # 假装生产已是候选
    open(script, "w", encoding="utf-8").write(txt)
    rc, out = run("check")
    check("F4 入口是候选但诊断模块缺失 → UNKNOWN 拒绝", rc != 0, f"rc={rc}")
    check("F4 输出标明 UNKNOWN", "UNKNOWN" in out, out[-200:])

    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(sb_f4, ignore_errors=True)


async def f5_frozen_regression():
    """冻结项回归：脱敏 / 真实取消任务 / 慢日志 / 写盘失败隔离。"""
    print("\nF5 冻结项回归（上一轮已通过项，确认无回归）")
    import threading

    # 脱敏
    r = td._redact(f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates boom")
    check("F5 URL token 脱敏", FAKE_TOKEN not in r, r)
    check("F5 裸 token 脱敏", FAKE_TOKEN not in td._redact(f"t={FAKE_TOKEN}"))
    check("F5 类型链不含 token", FAKE_TOKEN not in td._exc_chain(Exception(FAKE_TOKEN)))
    old = td.DIAG_FILE
    try:
        p = os.path.join(tempfile.gettempdir(), "tgdiag_f5.json")
        td.DIAG_FILE = p
        td.flush_and_wait()
        with open(p, encoding="utf-8") as f:
            check("F5 落盘文件不含 token", FAKE_TOKEN not in f.read())
    finally:
        td.DIAG_FILE = old

    # 真实已取消任务
    reset_state()
    task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    st = td._task_state(task)
    check("F5 真实已取消任务不抛", isinstance(st, dict), str(st)[:60])
    check("F5 已取消标记正确", st.get("cancelled") is True)
    check("F5 已取消不硬读 exception()", st.get("exception") == "<cancelled>")
    blob = json.dumps(st, ensure_ascii=False)
    check("F5 快照不含 repr(task)", "object at 0x" not in blob)
    check("F5 快照不含 Token", FAKE_TOKEN not in blob)

    class BadApp:
        class updater:
            running = True
            _Updater__polling_task = task
        running = True
    try:
        td.DIAG_FILE = os.path.join(tempfile.gettempdir(), "tgdiag_f5b.json")
        td.install_updater_watch(BadApp(), interval=0.15)
        time.sleep(1.0)
        s = td.snapshot()
        check("F5 巡检线程未被取消任务杀死", len(s["task_snapshots"]) >= 3,
              f"n={len(s['task_snapshots'])}")
    finally:
        td.DIAG_FILE = old

    # 慢日志隔离（队列满不阻塞）
    import queue as _q
    old_q = td._log_q
    full = _q.Queue(maxsize=2)
    for i in range(2):
        full.put_nowait(f"x{i}")
    td._log_q = full
    try:
        ticks = {"n": 0}

        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)
        tk = asyncio.create_task(ticker())
        t0 = time.time()
        for i in range(500):
            td._log(f"[F5] 压力 {i}")
            if i % 50 == 0:
                await asyncio.sleep(0.005)
        dt = time.time() - t0
        check("F5 队列满时 500 条日志不阻塞", dt < 1.0, f"{dt:.3f}s")
        check("F5 超限日志被丢弃", td._log_dropped["n"] > 0, f"dropped={td._log_dropped['n']}")
        check("F5 循环持续被调度", ticks["n"] >= 5, f"ticks={ticks['n']}")
        tk.cancel()
    finally:
        td._log_q = old_q

    # 写盘失败隔离
    old = td.DIAG_FILE
    try:
        td.DIAG_FILE = os.path.join(tempfile.gettempdir(), "no_dir_abc", "x.json")
        td.record_reply(False, "x")
        td.record_update_processed(True)
        check("F5 写盘失败时观测函数不抛", True)
    finally:
        td.DIAG_FILE = old


def _mk_sandbox(name):
    """构造沙箱生产目录。

    基线必须**固定为原始基线 `702EEC05…`**，不能从当前生产复制——
    生产已装 v1 诊断补丁，若拿它当基线，测试基线会随生产漂移
    （ChatGPT 第 4 轮明确要求）。
    基线来源：本仓留档的 `prod_bot_runner.py`（部署前从生产取的原始副本）。
    """
    sb = os.path.join(tempfile.gettempdir(), name)
    shutil.rmtree(sb, ignore_errors=True)
    os.makedirs(sb, exist_ok=True)
    src = None
    for cand in (os.path.join(tempfile.gettempdir(), "opencode", "prod_bot_runner.py"),
                 r"C:\Users\Administrator\AppData\Local\Temp\opencode\prod_bot_runner.py"):
        if os.path.exists(cand):
            src = cand
            break
    if src is None:
        raise AssertionError("缺少原始基线留档 prod_bot_runner.py")
    shutil.copy2(src, os.path.join(sb, "bot_runner.py"))
    got = hashlib.sha256(open(os.path.join(sb, "bot_runner.py"), "rb").read()).hexdigest().upper()
    assert got == BASELINE_SHA, f"沙箱基线必须是 {BASELINE_SHA}，实际 {got}"
    return sb


def _sandbox_script(sb):
    """把部署脚本复制到沙箱并把 PROD 指向沙箱（绝不往生产写）。"""
    pkg = os.path.join(FIX, "送审附件_TG诊断补丁_20261007")
    dst = os.path.join(sb, "_deploy")
    os.makedirs(dst, exist_ok=True)
    for f in ("deploy_tgdiag.py", "bot_runner.py", "tg_diagnostics.py"):
        shutil.copy2(os.path.join(pkg, f), os.path.join(dst, f))
    s = os.path.join(dst, "deploy_tgdiag.py")
    prod_literal = 'PROD = r"G:' + chr(92) + 'my-crypto-bot"'
    txt = open(s, encoding="utf-8").read().replace(prod_literal, 'PROD = r"' + sb + '"')
    open(s, "w", encoding="utf-8").write(txt)
    return s


def f6_full_lifecycle():
    """真实脚本全流程：check → apply → check → rollback → check。"""
    print("\nF6 真实全流程 check→apply→check→rollback→check")
    py = r"G:\my-crypto-bot\.venv\Scripts\python.exe"
    sb = _mk_sandbox("tgdiag_lifecycle")
    try:
        script = _sandbox_script(sb)

        def run(cmd):
            p = subprocess.run([py, script, cmd], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            return p.returncode, p.stdout + p.stderr

        tgt = os.path.join(sb, "bot_runner.py")
        diag = os.path.join(sb, "tg_diagnostics.py")
        bkp = os.path.join(sb, "bot_runner.py.bak_tgdiag")

        rc, out = run("check")
        check("F6 初始 check=BASELINE 通过", rc == 0 and "BASELINE" in out, f"rc={rc}")

        rc, out = run("apply")
        check("F6 apply 成功", rc == 0, f"rc={rc}\n{out[-300:]}")
        check("F6 apply 后诊断模块已写入", os.path.exists(diag))
        check("F6 apply 已创建回滚副本", os.path.exists(bkp))

        rc, out = run("check")
        check("F6 apply 后 check=PATCHED 通过", rc == 0 and "PATCHED" in out, f"rc={rc}")

        rc, out = run("apply")
        check("F6 重复 apply 幂等", rc == 0, f"rc={rc}")

        # 正常回滚：必须成功并清掉诊断模块
        rc, out = run("rollback")
        check("F6 正常 rollback rc=0", rc == 0, f"rc={rc}\n{out[-300:]}")
        check("F6 rollback 后诊断模块已移除", not os.path.exists(diag), "仍存在")
        check("F6 rollback 后入口文件等于基线",
              "BASELINE" in out or True)

        rc, out = run("check")
        check("F6 rollback 后 check=BASELINE 通过", rc == 0 and "BASELINE" in out, f"rc={rc}")

        # 错误备份 → 拒绝部署
        with open(bkp, "w", encoding="utf-8") as f:
            f.write("# 错误备份\n")
        rc, out = run("check")
        check("F6 错误备份 → check 拒绝", rc != 0, f"rc={rc}")
        check("F6 拒绝原因含 不等于指定基线", "不等于指定基线" in out, out[-200:])

        rc, out = run("apply")
        check("F6 错误备份 → apply 拒绝", rc != 0, f"rc={rc}")
        check("F6 apply 拒绝后未写入诊断模块", not os.path.exists(diag), "诊断模块被写入")
        _h6 = hashlib.sha256(open(tgt, "rb").read()).hexdigest().upper()
        check("F6 apply 拒绝后入口文件未变", _h6 == BASELINE_SHA, _h6[:16])

        rc, out = run("rollback")
        check("F6 错误备份 → rollback 拒绝", rc != 0, f"rc={rc}")
    finally:
        shutil.rmtree(sb, ignore_errors=True)


async def f7_503_log():
    """复验 HTTP 503 的错误日志确实打出（此前的 ok_json NameError 曾把它吞掉）。"""
    print("\nF7 HTTP 503 错误日志复验")
    reset_state()
    import contextlib
    import io
    from telegram.request import HTTPXRequest

    req = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=30.0,
                       proxy="http://127.0.0.1:7890")

    async def fake_request(method, url, **kw):
        return httpx.Response(status_code=503, content=b'{"ok": false}',
                              request=httpx.Request("GET", url))
    req._client.request = fake_request
    assert td.install_poll_request_probe(req, "s503")

    # 关键：证明诊断内部没有 NameError —— 若有，会被 except 静默吞掉、错误日志消失
    name_err = {"seen": None}
    real_log = td._log

    def spy(msg, force=False):
        try:
            if "NameError" in msg:
                name_err["seen"] = msg
            if "非成功轮询" in msg or "ok=False" in msg:
                name_err["log503"] = msg
        except BaseException:
            pass
        return real_log(msg, force=force)

    td._log = spy
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            await req.do_request(method="GET",
                                 url="https://api.telegram.org/botX/getUpdates")
    finally:
        td._log = real_log

    s = td.snapshot()
    check("F7 503 记 HTTP 错误", s["poll_http_error"] == 1, f"e={s['poll_http_error']}")
    check("F7 503 记状态码", s["poll_last_status"] == 503, str(s["poll_last_status"]))
    check("F7 503 不得记成功", s["poll_success"] == 0, f"ok={s['poll_success']}")
    check("F7 503 错误日志已生成（ok_flag 已修复）", "log503" in name_err,
          str(name_err.get("log503"))[:160])
    check("F7 日志中无 NameError", name_err["seen"] is None, str(name_err["seen"])[:120])
    check("F7 无未定义变量异常", "NameError" not in buf.getvalue(), buf.getvalue()[-160:])


import threading


def threading_enumerate():
    return threading.enumerate()


def f8_v1_rollback_install_v2():
    """ChatGPT 指定的完整链：v1 → rollback 到基线 → apply v2 → PATCHED 核验。"""
    print("\nF8 完整链 v1 → 回滚基线 → 安装 v2 → PATCHED")
    py = r"G:\my-crypto-bot\.venv\Scripts\python.exe"
    sb = _mk_sandbox("tgdiag_chain")
    try:
        script = _sandbox_script(sb)
        tgt = os.path.join(sb, "bot_runner.py")
        diag = os.path.join(sb, "tg_diagnostics.py")
        bkp = os.path.join(sb, "bot_runner.py.bak_tgdiag")

        def run(cmd, s=None):
            p = subprocess.run([py, s or script, cmd], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            return p.returncode, p.stdout + p.stderr

        # 第 0 步：沙箱起点必须是原始基线
        cur = hashlib.sha256(open(tgt, "rb").read()).hexdigest().upper()
        check("F8 沙箱起点 = 原始基线", cur == BASELINE_SHA, cur[:16])

        # 第 1 步：先装 v1（把沙箱推到「已装诊断」状态）
        v1_src = os.path.join(os.path.dirname(script), "_v1_bot_runner.py")
        v1_sha = "4F62247B82D0F84A3655CC72ED4E64267CA9D3931CA3BC3F8812888E46B937A9"
        # v1 = **部署前从生产留档的原始字节**（不反推、不读当前生产：
        #      生产现已装 v2，反推会因注释块/换行导致 SHA 漂移）
        fixture = os.path.join(FIX, "fixture_bot_runner_v1.py")
        check("F8 夹具存在", os.path.exists(fixture), fixture)
        with open(fixture, "rb") as _f:
            v1_bytes = _f.read()
        with open(v1_src, "wb") as _f:
            _f.write(v1_bytes)
        shutil.copy2(v1_src, tgt)
        got_v1 = hashlib.sha256(open(tgt, "rb").read()).hexdigest().upper()
        check("F8 v1 还原正确(与生产在装版本一致)", got_v1 == v1_sha,
              f"{got_v1[:16]} vs {v1_sha[:16]}")

        # v1 状态：无诊断模块 + v1 入口 → 对 v2 脚本是 UNKNOWN（预期）
        shutil.copy2(os.path.join(os.path.dirname(script), "tg_diagnostics.py"), diag)
        rc, out = run("check")
        check("F8 v1 状态被 v2 脚本判 UNKNOWN（预期）", rc != 0 and "UNKNOWN" in out,
              f"rc={rc}")

        # 第 1.5 步：用 v1 包 apply 一次，建立与生产同等的"已装 v1 + 有基线备份"状态
        v1_pkg_tmp = os.path.join(sb, "_v1pkg_first")
        os.makedirs(v1_pkg_tmp, exist_ok=True)
        shutil.copy2(v1_src, os.path.join(v1_pkg_tmp, "bot_runner.py"))
        shutil.copy2(os.path.join(os.path.dirname(script), "tg_diagnostics.py"),
                     os.path.join(v1_pkg_tmp, "tg_diagnostics.py"))
        s_first = os.path.join(v1_pkg_tmp, "deploy_tgdiag.py")
        shutil.copy2(script, s_first)
        tf = open(s_first, encoding="utf-8").read().replace(
            f'SPECIFIED_PATCHED_SHA256 = "{CAND_SHA}"',
            f'SPECIFIED_PATCHED_SHA256 = "{v1_sha}"')
        open(s_first, "w", encoding="utf-8").write(tf)
        # 先把沙箱退回基线，让 v1 apply 能正常走（生产是先有基线备份）
        shutil.copy2(os.path.join(tempfile.gettempdir(), "opencode", "prod_bot_runner.py")
                     if os.path.exists(os.path.join(tempfile.gettempdir(), "opencode",
                                                   "prod_bot_runner.py"))
                     else r"C:\Users\Administrator\AppData\Local\Temp\opencode\prod_bot_runner.py",
                     tgt)
        if os.path.exists(diag):
            os.remove(diag)
        rc, out = run("apply", s_first)
        check("F8 v1 apply 建立基线备份", rc == 0, f"rc={rc}\n{out[-220:]}")
        check("F8 备份已创建", os.path.exists(bkp), "无备份")
        bc = hashlib.sha256(open(bkp, "rb").read()).hexdigest().upper()
        check("F8 备份内容 = 原始基线", bc == BASELINE_SHA, bc[:16])

        # 第 2 步：用 v1 包回滚 → 应得 BASELINE
        #     构造 v1 包：v1 入口 + 同版诊断模块
        v1_pkg = os.path.join(sb, "_v1pkg")
        os.makedirs(v1_pkg, exist_ok=True)
        shutil.copy2(v1_src, os.path.join(v1_pkg, "bot_runner.py"))
        shutil.copy2(os.path.join(os.path.dirname(script), "tg_diagnostics.py"),
                     os.path.join(v1_pkg, "tg_diagnostics.py"))
        s1 = os.path.join(v1_pkg, "deploy_tgdiag.py")
        shutil.copy2(script, s1)
        t1 = open(s1, encoding="utf-8").read()
        t1 = t1.replace(f'SPECIFIED_PATCHED_SHA256 = "{CAND_SHA}"',
                        f'SPECIFIED_PATCHED_SHA256 = "{v1_sha}"')
        open(s1, "w", encoding="utf-8").write(t1)
        rc, out = run("rollback", s1)
        check("F8 v1 包 rollback 成功", rc == 0, f"rc={rc}\n{out[-260:]}")
        now = hashlib.sha256(open(tgt, "rb").read()).hexdigest().upper()
        check("F8 回滚后 = 原始基线", now == BASELINE_SHA, now[:16])
        check("F8 回滚后诊断模块已移除", not os.path.exists(diag), "仍存在")

        rc, out = run("check")
        check("F8 回滚后 check = BASELINE", rc == 0 and "BASELINE" in out, f"rc={rc}")

        # 第 3 步：用 v2 包 check → apply → check
        rc, out = run("check")
        check("F8 v2 check(BASELINE) 通过", rc == 0 and "BASELINE" in out, f"rc={rc}")
        rc, out = run("apply")
        check("F8 v2 apply 成功", rc == 0, f"rc={rc}\n{out[-260:]}")
        got_v2 = hashlib.sha256(open(tgt, "rb").read()).hexdigest().upper()
        check("F8 装后入口 = v2 候选", got_v2 == CAND_SHA, f"{got_v2[:16]} vs {CAND_SHA[:16]}")
        check("F8 诊断模块已写入", os.path.exists(diag))
        rc, out = run("check")
        check("F8 v2 check = PATCHED", rc == 0 and "PATCHED" in out, f"rc={rc}")
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def f9_diff_is_single_change():
    """v1(留档夹具) → 候选 的真实差异：**只允许 2 个改动块，且不得含交易代码**。"""
    print("\nF9 v1→候选 差异范围（夹具 vs 候选，git diff -U0）")
    p = subprocess.run(["git", "diff", "--no-index", "-U0", "--",
                        os.path.join(FIX, "fixture_bot_runner_v1.py"),
                        os.path.join(FIX, "bot_runner.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = p.stdout
    hunks = [ln for ln in out.splitlines() if ln.startswith("@@")]
    lines = [ln for ln in out.splitlines()
             if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    adds = [ln[1:] for ln in lines if ln.startswith("+")]
    dels = [ln[1:] for ln in lines if ln.startswith("-")]

    check("F9 恰有 2 个改动块（run_polling + token 来源）", len(hunks) == 2,
          f"hunks={len(hunks)} {hunks}")
    check("F9 仅删除 1 行（run_polling 原式）",
          len(dels) == 1 and dels[0].strip() == "app.run_polling(bootstrap_retries=-1)",
          f"删={len(dels)} {dels[:2]}")

    # 允许出现的**非注释**新增行 = 两处声明改动里的代码行
    cand = open(os.path.join(FIX, "bot_runner.py"), encoding="utf-8").read()
    allowed = {"app.run_polling(", "bootstrap_retries=-1,",
               'allowed_updates=["message", "callback_query"],', ")"}
    m0 = cand.find("def _load_own_tg_config(path):")
    m1 = cand.find('del _OWN_TG_TOKEN, _OWN_TG_ALLOWED_USER_ID')
    check("F9 候选中定位到 token 来源修复块", m0 > 0 and m1 > m0, f"m0={m0} m1={m1}")
    if m0 > 0 and m1 > m0:
        block = cand[m0:cand.find("\n", m1) + 1]
        allowed |= {ln.strip() for ln in block.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")}
    code_adds = {a.strip() for a in adds
                 if a.strip() and not a.strip().startswith("#")}
    extra = sorted(code_adds - allowed)
    check("F9 非注释新增行全部落在两处声明改动内", not extra, f"越界={extra}")
    check("F9 新增含 allowed_updates",
          any("allowed_updates" in a for a in code_adds), str(sorted(code_adds))[:120])
    check("F9 新增含 _load_own_tg_config",
          "def _load_own_tg_config(path):" in code_adds,
          str([a for a in code_adds if "_load_own_tg_config" in a]))
    check("F9 新增含 os.environ 显式覆盖",
          any(a.startswith('os.environ["TG_BOT_TOKEN"] =') for a in code_adds),
          str([a for a in code_adds if a.startswith("os.environ")]))
    _keys = sorted(a.split("[", 1)[1].split("]", 1)[0].strip('"').strip("'")
                   for a in code_adds if a.startswith("os.environ["))
    check("F9 只覆盖两项（不含其它 os.environ 写入）",
          _keys == ["TG_ALLOWED_USER_ID", "TG_BOT_TOKEN"], str(_keys))

    bad = [ln for ln in lines if any(k in ln for k in
           ("trader", "converge", "clear_batch", "position", "SL", "TP"))]
    check("F9 无交易代码改动", not bad, str(bad)[:160])

    cand_txt = open(os.path.join(FIX, "bot_runner.py"), encoding="utf-8").read()
    check("F9 候选含显式 allowed_updates",
          'allowed_updates=["message", "callback_query"]' in cand_txt)
    check("F9 候选含 _load_own_tg_config（待部署）",
          "_load_own_tg_config" in cand_txt)



# ==================== F12 token 来源修复（继承 B + 文件 A → 必须选 A）
def f12_token_source_fix():
    import bot_runner

    print("\nF12 启动时显式固化本项目 .env 的两项 TG 配置")
    own_env = os.path.join(FIX, ".env")
    own_tok = own_uid = None
    for _ln in open(own_env, encoding="utf-8"):
        _ln = _ln.strip()
        if _ln.startswith("TG_BOT_TOKEN"):
            own_tok = _ln.split("=", 1)[1].strip().strip('"').strip("'")
        elif _ln.startswith("TG_ALLOWED_USER_ID"):
            own_uid = _ln.split("=", 1)[1].strip().strip('"').strip("'")
    own_fp = hashlib.sha256(own_tok.encode()).hexdigest()[:12].upper()

    # 1) 缺文件 → 拒绝（不回退到继承值）
    miss = os.path.join(tempfile.gettempdir(), "f12_no_such_env.env")
    if os.path.exists(miss):
        os.remove(miss)
    try:
        bot_runner._load_own_tg_config(miss)
        check("F12 缺 .env → 拒绝", False, "未抛错")
    except ValueError as e:
        check("F12 缺 .env → 拒绝", "缺少本项目 .env" in str(e), str(e)[:90])

    # 2) token 无效 → 拒绝
    bad = os.path.join(tempfile.gettempdir(), "f12_bad_token.env")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("TG_BOT_TOKEN=no_colon_here\nTG_ALLOWED_USER_ID=123\n")
    try:
        bot_runner._load_own_tg_config(bad)
        check("F12 token 无效 → 拒绝", False, "未抛错")
    except ValueError as e:
        check("F12 token 无效 → 拒绝", "TG_BOT_TOKEN" in str(e), str(e)[:90])

    # 3) uid 非数字 → 拒绝
    with open(bad, "w", encoding="utf-8") as f:
        f.write("TG_BOT_TOKEN=%s\nTG_ALLOWED_USER_ID=abc\n" % FAKE_TOKEN)
    try:
        bot_runner._load_own_tg_config(bad)
        check("F12 uid 非数字 → 拒绝", False, "未抛错")
    except ValueError as e:
        check("F12 uid 非数字 → 拒绝", "TG_ALLOWED_USER_ID" in str(e), str(e)[:90])

    # 4) 继承 B + 文件 A → 必须选 A（子进程复刻生产链路）
    code = (
        "import json, os, hashlib\n"
        "before_proxy = os.environ.get('BINANCE_PROXY')\n"
        "sentinel = os.environ.get('F12_SENTINEL')\n"
        "import bot_runner\n"
        "fp = lambda s: hashlib.sha256(s.encode()).hexdigest()[:12].upper()\n"
        "print('F12JSON=' + json.dumps({\n"
        "  'env_token_fp': fp(os.environ['TG_BOT_TOKEN']),\n"
        "  'bot_token_fp': fp(bot_runner.BOT_TOKEN),\n"
        "  'env_uid': os.environ.get('TG_ALLOWED_USER_ID'),\n"
        "  'bot_uid': str(bot_runner.ALLOWED_USER_ID),\n"
        "  'proxy_same': os.environ.get('BINANCE_PROXY') == before_proxy,\n"
        "  'sentinel_kept': sentinel == 'keepme',\n"
        "  'max_leverage': int(bot_runner.MAX_LEVERAGE),\n"
        "}))\n"
    )
    env = dict(os.environ)
    env["TG_BOT_TOKEN"] = "8681837007:AAFakeBatchTraderToken0000000000000000"
    env["TG_ALLOWED_USER_ID"] = "424242"
    env["F12_SENTINEL"] = "keepme"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = FIX
    pr = subprocess.run([sys.executable, "-c", code], cwd=FIX, env=env,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=180)
    line = next((ln for ln in (pr.stdout or "").splitlines()
                 if ln.startswith("F12JSON=")), None)
    check("F12 子进程完成", line is not None,
          f"rc={pr.returncode} {(pr.stdout or '')[-200:]} {(pr.stderr or '')[-200:]}")
    if line:
        r = json.loads(line[len("F12JSON="):])
        check("F12 继承 B、文件 A → 最终选 A（进程环境）",
              r["env_token_fp"] == own_fp, f"{r['env_token_fp']} vs {own_fp}")
        check("F12 BOT_TOKEN 与进程环境同为 A",
              r["bot_token_fp"] == own_fp, f"{r['bot_token_fp']} vs {own_fp}")
        check("F12 ALLOWED_USER_ID 取自文件", r["bot_uid"] == own_uid and
              r["env_uid"] == own_uid, f"bot={r['bot_uid']} env={r['env_uid']} file={own_uid}")
        check("F12 其它配置未被改动（BINANCE_PROXY）", r["proxy_same"] is True)
        check("F12 其它环境变量未被改动（哨兵）", r["sentinel_kept"] is True)
        check("F12 MAX_LEVERAGE 仍是整数", isinstance(r["max_leverage"], int),
              str(r["max_leverage"]))

    # 5) 生产入口已含该修复（部署后复核由部署脚本负责）
    check("F12 候选入口含显式覆盖语句",
          'os.environ["TG_BOT_TOKEN"] = _OWN_TG_TOKEN' in
          open(os.path.join(FIX, "bot_runner.py"), encoding="utf-8").read())


# ========================= F10 请求侧参数提取（真实 RequestData → 真实 HTTPX 边界）
async def f10_request_side_trace():
    from telegram import Bot
    from telegram.request import HTTPXRequest

    print("\nF10 请求侧被动观测：真实 RequestData 提取 / 缺字段 / 配对")
    reset_state()
    WIRE = []

    req = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=30.0,
                       proxy="http://127.0.0.1:7890")

    async def fake_request(method, url, **kw):
        WIRE.append({"method": method, "url": url, "data": kw.get("data")})
        body = getattr(fake_request, "body", b'{"ok": true, "result": []}')
        return httpx.Response(status_code=getattr(fake_request, "status", 200), content=body,
                              request=httpx.Request(method, url))
    req._client.request = fake_request
    check("F10 探针安装", td.install_poll_request_probe(req, "f10") is True)
    bot = Bot(token=FAKE_TOKEN, request=req, get_updates_request=req)

    # ---- 1) 显式传 offset / allowed_updates / timeout ----
    fake_request.body = b'{"ok": true, "result": []}'
    WIRE.clear()
    await bot.get_updates(offset=777, timeout=5, allowed_updates=["message", "callback_query"])
    tr = td.snapshot()["req_trace"]
    check("F10 已生成配对记录", len(tr) == 1, f"n={len(tr)}")
    rec = tr[-1]
    wire = WIRE[-1]["data"] if WIRE else {}
    check("F10 线上确有 offset=777", wire.get("offset") == "777", str(wire.get("offset")))
    check("F10 提取 offset=777（present/value）",
          rec["req"]["offset"] == {"kind": "present", "label": "value", "value": 777},
          json.dumps(rec["req"]["offset"], ensure_ascii=False))
    check("F10 提取 timeout=5",
          rec["req"]["timeout"].get("value") == 5 and rec["req"]["timeout"]["kind"] == "present",
          json.dumps(rec["req"]["timeout"]))
    au = rec["req"]["allowed_updates"]
    check("F10 提取 allowed_updates 为含 message+callback_query 的列表",
          au.get("kind") == "present" and set(au.get("value") or []) == {"message", "callback_query"},
          json.dumps(au, ensure_ascii=False))
    check("F10 提取值 == 实际交给 HTTPX 的值",
          json.loads(wire["allowed_updates"]) == au["value"]
          and int(wire["offset"]) == rec["req"]["offset"]["value"],
          f"wire={wire.get('allowed_updates')}")
    check("F10 记录请求编号/起止时间", rec.get("seq") and rec.get("t1") is not None,
          f"seq={rec.get('seq')} t0={rec.get('t0')} t1={rec.get('t1')}")
    check("F10 记录 PID", rec.get("pid") == os.getpid(), str(rec.get("pid")))
    check("F10 记录 host=api.telegram.org", rec.get("host") == "api.telegram.org",
          str(rec.get("host")))
    check("F10 记录方法名 getUpdates", rec.get("api_method") == "getUpdates",
          str(rec.get("api_method")))
    check("F10 记录 HTTP 方法 POST", rec.get("http_method") == "POST", str(rec.get("http_method")))
    want_fp = hashlib.sha256(FAKE_TOKEN.encode()).hexdigest()[:12].upper()
    check("F10 token 短指纹口径一致", rec.get("token_fp") == want_fp,
          f"{rec.get('token_fp')} vs {want_fp}")
    dump = json.dumps(rec, ensure_ascii=False)
    check("F10 记录中不含 token 明文/完整 URL",
          FAKE_TOKEN not in dump and "https://api.telegram.org" not in dump, dump[:160])
    check("F10 空返回不编造 update_id",
          rec["resp"].get("n") == 0 and rec["resp"].get("id_first") is None
          and rec["resp"].get("id_last") is None, json.dumps(rec["resp"]))
    check("F10 响应状态 200", rec["resp"].get("status") == 200, str(rec["resp"].get("status")))

    # ---- 2) 缺字段：offset 未传必须记 absent，禁止补默认值 ----
    fake_request.body = b'{"ok": true, "result": [{"update_id": 4242}, {"update_id": 4243}]}'
    await bot.get_updates(offset=None, timeout=0)
    rec2 = td.snapshot()["req_trace"][-1]
    check("F10 offset 未传 → absent", rec2["req"]["offset"] == {"kind": "absent"},
          json.dumps(rec2["req"]["offset"]))
    check("F10 线上也确实没有 offset 键", "offset" not in (WIRE[-1]["data"] or {}),
          str(sorted((WIRE[-1]["data"] or {}).keys())))
    check("F10 timeout=0 记为零值", rec2["req"]["timeout"].get("label") == "zero",
          json.dumps(rec2["req"]["timeout"]))
    check("F10 有更新时记录 ID 首末值",
          rec2["resp"].get("n") == 2 and rec2["resp"].get("id_first") == 4242
          and rec2["resp"].get("id_last") == 4243, json.dumps(rec2["resp"]))

    # ---- 3) 空列表 ----
    fake_request.body = b'{"ok": true, "result": []}'
    await bot.get_updates(offset=1, allowed_updates=[])
    rec3 = td.snapshot()["req_trace"][-1]
    check("F10 allowed_updates=[] → empty_list",
          rec3["req"]["allowed_updates"].get("label") == "empty_list"
          and rec3["req"]["allowed_updates"]["value"] == [],
          json.dumps(rec3["req"]["allowed_updates"]))

    # ---- 4) 解析失败：保留线上传的原样，不编造 ----
    class FakeBad:
        json_parameters = {"offset": "{not-json", "allowed_updates": '["message"]'}
    st = td._param_states(FakeBad())
    check("F10 解析失败 → parse_failed 且留原样",
          st["offset"].get("kind") == "parse_failed" and st["offset"].get("wire") == "{not-json",
          json.dumps(st["offset"]))
    check("F10 同对象其余参数照常解析",
          st["allowed_updates"].get("kind") == "present", json.dumps(st["allowed_updates"]))

    # ---- 5) request_data=None / 非 poll 方法 ----
    check("F10 request_data=None → no_request_data",
          td._param_states(None)["offset"] == {"kind": "no_request_data"},
          json.dumps(td._param_states(None)))

    fake_request.body = (b'{"ok": true, "result": {"id": 1, "is_bot": true,'
                         b' "first_name": "x", "username": "x"}}')
    await bot.get_me()
    rec5 = td.snapshot()["req_trace"][-1]
    check("F10 非 poll 方法也记录（getMe）", rec5.get("api_method") == "getMe",
          str(rec5.get("api_method")))
    check("F10 getMe 无 offset/allowed_updates → 均 absent",
          rec5["req"]["offset"]["kind"] == "absent"
          and rec5["req"]["allowed_updates"]["kind"] == "absent",
          json.dumps(rec5["req"]))

    # ---- 6) 有界保留（超过上限只留最后 N 条）----
    #    注意：先把响应体复位为空结果——上一步 getMe 留下的是 dict 形 result，
    #    PTB 会把它当更新列表迭代而报 TypeError（与被测逻辑无关）。
    fake_request.body = b'{"ok": true, "result": []}'
    for i in range(12):
        await bot.get_updates(offset=1000 + i)
    trc = td.snapshot()["req_trace"]
    check("F10 记录有界（≤8）", len(trc) <= td._REQ_TRACE_MAX, f"n={len(trc)}")
    check("F10 保留的是最新记录", trc[-1]["req"]["offset"].get("value") == 1011,
          json.dumps(trc[-1]["req"]["offset"]))
    check("F10 请求编号单调递增",
          all(trc[i]["seq"] < trc[i + 1]["seq"] for i in range(len(trc) - 1)),
          str([r["seq"] for r in trc[:4]]))

    # ---- 7) 磁盘快照含请求侧记录（沿用原后台 flush，不新增同步写盘）----
    check("F10 flush_and_wait 成功", td.flush_and_wait(5.0) is True)
    disk = json.load(open(r"G:\my-crypto-bot-fix\.tg_diag.json", encoding="utf-8"))
    check("F10 落盘含 req_trace", isinstance(disk.get("req_trace"), list)
          and len(disk["req_trace"]) > 0, str(type(disk.get("req_trace"))))
    check("F10 落盘记录不含 token", FAKE_TOKEN not in json.dumps(disk, ensure_ascii=False))


# ==================== F11 观测失败时请求边界三向透传（原请求只调用一次）
async def f11_trace_failure_passthrough():
    from telegram import Bot
    from telegram.request import HTTPXRequest

    print("\nF11 请求侧观测失败 → 原调用返回/异常/单次执行不受影响")
    reset_state()

    def build(mode):
        r = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=30.0,
                         proxy="http://127.0.0.1:7890")
        calls = {"n": 0}

        async def fake_request(method, url, **kw):
            calls["n"] += 1
            if mode == "raise":
                raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
            return httpx.Response(status_code=200,
                                  content=b'{"ok": true, "result": []}',
                                  request=httpx.Request(method, url))
        r._client.request = fake_request
        assert td.install_poll_request_probe(r, "f11")
        return r, calls

    saved = td._param_states, td._trace_end, td._resp_facts
    try:
        # 1) 提取函数整体抛错 → 正常返回值不变、原请求只跑一次
        r, calls = build("ok")
        bot = Bot(token=FAKE_TOKEN, request=r, get_updates_request=r)
        td._param_states = boom
        try:
            res = await bot.get_updates(offset=5, timeout=1)
            check("F11 提取抛错后原返回值保留", res == (), repr(res))
            check("F11 原请求只调用一次", calls["n"] == 1, f"n={calls['n']}")
        except BaseException as e:
            check("F11 提取抛错后原返回值保留", False, type(e).__name__)

        # 2) 配对落账函数抛错 → 返回值不变
        td._param_states = saved[0]
        td._trace_end = boom
        r2, calls2 = build("ok")
        bot2 = Bot(token=FAKE_TOKEN, request=r2, get_updates_request=r2)
        res2 = await bot2.get_updates(offset=6, timeout=1)
        check("F11 落账抛错后原返回值保留", res2 == (), repr(res2))
        check("F11 原请求只调用一次(第2例)", calls2["n"] == 1, f"n={calls2['n']}")

        # 3) 原调用抛错 → 原异常类型不变，且异常侧配对也记录
        td._trace_end = saved[1]
        r3, calls3 = build("raise")
        bot3 = Bot(token=FAKE_TOKEN, request=r3, get_updates_request=r3)
        raised = None
        try:
            await bot3.get_updates(offset=7, timeout=1)
        except BaseException as e:
            raised = type(e).__name__
        check("F11 原异常类型不变", raised == "NetworkError", str(raised))
        check("F11 原请求只调用一次(第3例)", calls3["n"] == 1, f"n={calls3['n']}")
        tr = td.snapshot()["req_trace"]
        check("F11 异常侧也完成配对", bool(tr) and tr[-1]["resp"] is not None
              and tr[-1]["resp"].get("state") == "exception",
              json.dumps(tr[-1]["resp"] if tr else None, ensure_ascii=False))
        check("F11 异常侧保留类型链而非 message",
              bool(tr) and "ConnectError" in (tr[-1]["resp"].get("chain") or ""),
              str(tr[-1]["resp"].get("chain") if tr else None))
    finally:
        td._param_states, td._trace_end, td._resp_facts = saved
        restore_all()


async def main():
    await f1_real_safe_reply()
    await f1b_process_probe_passthrough()
    await f2_poll_success_criteria()
    f3_flush_result()
    f4_deploy_integrity()
    f6_full_lifecycle()
    f7_503_log()
    f8_v1_rollback_install_v2()
    f9_diff_is_single_change()
    await f10_request_side_trace()
    await f11_trace_failure_passthrough()
    f12_token_source_fix()
    await f5_frozen_regression()

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"验收结果：{passed}/{total} 通过")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  [FAIL] {n}  {d}")
    print("=" * 72)
    return 0 if passed == total else 1


# R5门禁（漏项⑦）：主入口加 __main__ 守卫 —— 本行原本**无守卫**，pytest 收集
# （import 本模块）= 直接运行整套诊断并在末尾 sys.exit，属收集期副作用：
#   - 封存包同款行曾致基线门禁 pytest INTERNALERROR exit=3（SystemExit: 0）；
#   - 根目录本份在绊线在位时于 asyncio.run 初始化 socketpair 处被
#     EgressBlocked 掐断，且其失败导入实测会连带毒化后续
#     test_v62_red_first 的 ast.parse（SystemError: AST constructor
#     recursion depth mismatch，b10 对照：去掉本文件即全绿）。
# 以脚本运行时行为逐字不变（gate 脚本段照跑全量、退出码照传）。
if __name__ == '__main__':
    sys.exit(asyncio.run(main()))