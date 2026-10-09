#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tg_diagnostics —— Telegram 取件链路诊断（**只观测，不改变任何行为**）

v2 修正（依 ChatGPT 独立复审的 5 项阻断 + 3 项准确性缺口）：

  B1 回复观测抛错会吞掉原返回值   → record_* 内部全 try/except；调用点亦隔离
  B2 已取消任务 task.exception() 抛 CancelledError（BaseException，非 Exception）
                                   → 先判 cancelled()，并改捕 BaseException
  B3 _task_state 保存未脱敏 repr(task) → 移除 repr，只留结构字段 + 异常类型链
  B4 同步 print 阻塞事件循环        → 日志改后台有界队列，满则丢弃计数，绝不阻塞
  B5 部署脚本基线可绕过            → 见 deploy_tgdiag.py，严格三态
  G1 处理进度未真正接入            → install_process_probe 包装真实 Application.process_update
  G2 HTTP 返回 ≠ 轮询成功          → 分离 poll_http_response / poll_success，保存状态码
  G3 flush_and_wait 语义错误        → 改用完成确认（代次计数），真正等到写完

设计边界（不变）：
  * 诊断落在**实际请求边界**（包装本进程自己的 HTTPXRequest.do_request）
  * 不新增第二个 getUpdates 客户端 / 不改代理 / 不改 webhook / 不改自动重启
  * 不改 bot_alive 的进程存活含义
  * 诊断自身出任何故障，都不得改变原调用的返回 / 异常 / 取消
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIAG_FILE = os.path.join(BASE_DIR, ".tg_diag.json")

_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")
_MAX_EXC_CHAIN = 8


def _redact(text) -> str:
    if text is None:
        return None
    s = str(text)
    s = _TOKEN_RE.sub("<TOKEN_REDACTED>", s)
    s = re.sub(r"https?://[^\s\"']*bot\d+:[^\s\"']*", "<URL_REDACTED>", s)
    return s[:300]


def _exc_chain(e, depth=0):
    """异常**类型名链**（不输出可能含 token 的 message）。全程吞异常。"""
    chain, cur, guard = [], e, 0
    try:
        while cur is not None and guard < _MAX_EXC_CHAIN:
            chain.append(type(cur).__name__)
            cur = cur.__cause__ or cur.__context__
            guard += 1
    except BaseException:
        pass
    return " <- ".join(chain)


# ============================================================ 日志：异步有界
_log_q: "queue.Queue" = queue.Queue(maxsize=2000)
_log_dropped = {"n": 0}
_log_rate = {"last": 0.0, "count": 0}
_RATE_WINDOW = 5.0          # 5 秒内最多 10 条常规日志，超出则丢弃并计数
_RATE_MAX = 10


def _log_worker():
    while True:
        try:
            line = _log_q.get()
            print(line, flush=True)
        except BaseException:
            pass


threading.Thread(target=_log_worker, daemon=True, name="tgdiag_log").start()


def _log(msg: str, force: bool = False) -> None:
    """异步入队，**绝不阻塞调用方**；队列满则丢弃并计数。"""
    try:
        if not force:
            now = time.time()
            with threading.Lock():
                if now - _log_rate["last"] < _RATE_WINDOW:
                    _log_rate["count"] += 1
                    if _log_rate["count"] > _RATE_MAX:
                        _log_dropped["n"] += 1
                        return
                else:
                    _log_rate["last"] = now
                    _log_rate["count"] = 1
        _log_q.put_nowait(f"[TGDIAG] {msg}")
    except queue.Full:
        _log_dropped["n"] += 1
    except BaseException:
        pass


# ================================================================ 状态与落盘
_state_lock = threading.Lock()
_state = {
    "installed_at": time.time(),
    # ⚠️ 语义：进入 do_request 的次数，**≠ 请求已发出**。
    #    判断是否真的发出必须结合 poll_http_response / poll_success /
    #    poll_last_error_cause / poll_last_duration。
    "poll_req_total_meaning": "进入do_request次数;≠请求已发出",
    "poll_req_total": 0,
    "poll_http_response": 0,      # 拿到 HTTP 响应（含 5xx）
    "poll_success": 0,            # 有效轮询成功（HTTP 200 且 ok=true）
    "poll_http_error": 0,         # 拿到响应但状态码非 200 / ok=false
    "poll_req_err": 0,            # 未拿到响应（异常）
    "poll_empty_ok": 0,
    "poll_with_updates": 0,
    "poll_unparsed": 0,
    "poll_last_status": None,
    "poll_last_start": None,
    "poll_last_end": None,
    "poll_last_duration": None,
    "poll_last_error": None,
    "poll_last_error_cause": None,
    "updates_returned": 0,
    "updates_processed": 0,
    "updates_process_fail": 0,
    "reply_ok": 0,
    "reply_fail": 0,
    "reply_last_error": None,
    "task_snapshots": [],
    "log_dropped": 0,
    # ---- 请求侧被动观测（ChatGPT 第4轮锁定范围）----
    "req_seq": 0,             # 请求编号（递增，用于请求↔响应配对）
    "req_trace": [],          # 有界：最后 _REQ_TRACE_MAX 条完整往返记录
}
_t0 = time.time()


def _upd(**kw):
    try:
        with _state_lock:
            _state.update(kw)
    except BaseException:
        pass


def _bump(key, n=1):
    try:
        with _state_lock:
            _state[key] = _state.get(key, 0) + n
    except BaseException:
        pass


def snapshot() -> dict:
    try:
        with _state_lock:
            d = dict(_state)
        d["uptime_s"] = round(time.time() - _t0, 1)
        d["log_dropped"] = _log_dropped["n"]
        return d
    except BaseException:
        return {}


# ---- 落盘：后台线程 + 代次确认（修正 flush_and_wait 语义，G3） -------------
_flush_ev = threading.Event()
_flush_done = threading.Event()
_flush_gen = {"n": 0}
_flush_result = {"gen": -1, "ok": None}


def _write_diag_file() -> bool:
    """真正落盘（同步）。**任何失败都静默吞掉**，返回是否成功。"""
    try:
        tmp = DIAG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, DIAG_FILE)
        return True
    except BaseException:
        return False


def _flush_worker():
    while True:
        try:
            _flush_ev.wait()
            _flush_ev.clear()
            gen = _flush_gen["n"]
            ok = _write_diag_file()
            _flush_result["gen"] = gen
            _flush_result["ok"] = bool(ok)     # 记录该代次的**实际写入结果**
            _flush_done.set()
        except BaseException:
            pass


threading.Thread(target=_flush_worker, daemon=True, name="tgdiag_flush").start()


def flush(force: bool = False):
    """请求落盘。**立即返回，绝不阻塞调用方（尤其事件循环）**。返回 True=已排队。"""
    try:
        _flush_done.clear()
        _flush_gen["n"] += 1
        _flush_ev.set()
        return True
    except BaseException:
        return False


def flush_and_wait(timeout: float = 5.0) -> bool:
    """等到该代次写入结束，并返回**该代次的实际写入结果**（失败返回 False）。"""
    try:
        if not flush():
            return False
        want = _flush_gen["n"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.01)
            if _flush_done.is_set() and _flush_result["gen"] >= want:
                return bool(_flush_result["ok"])
        return False
    except BaseException:
        return False


# ============================ ⓪ 请求侧事实提取（ChatGPT 第4轮锁定范围）
_REQ_TRACE_MAX = 8
_TRACE_KEYS = ("offset", "allowed_updates", "timeout")


def _url_facts(url) -> dict:
    """从实际请求 URL 提取 host / Bot API 方法名 / Token 短指纹。

    **绝不返回 Token 或完整 URL。** 全程吞异常。
    """
    out = {"host": None, "api_method": None, "token_fp": None, "parse": "ok"}
    try:
        if url is None:
            out["parse"] = "absent"
            return out
        p = urllib.parse.urlsplit(str(url))
        out["host"] = p.hostname
        segs = [s for s in p.path.split("/") if s]
        if segs and segs[0].lower().startswith("bot") and ":" in segs[0]:
            # 与订阅重置工具同一口径：sha256(token) 前 12 位大写十六进制
            out["token_fp"] = hashlib.sha256(segs[0][3:].encode("utf-8")).hexdigest()[:12].upper()
            if len(segs) > 1:
                out["api_method"] = segs[1]
        else:
            out["parse"] = "no_bot_segment"
    except BaseException as e:
        out["parse"] = type(e).__name__
    return out


def _param_states(request_data) -> dict:
    """从**真实 RequestData** 提取 PTB 交给 HTTPX 的参数（`json_parameters`）。

    严格区分：未传(absent) / 值为0(zero) / 空列表(empty_list) / 解析失败(parse_failed)。
    **禁止在缺失时补默认值**——缺失就记缺失。
    """
    keys = {k: {"kind": "init_failed"} for k in _TRACE_KEYS}
    try:
        if request_data is None:
            return {k: {"kind": "no_request_data"} for k in _TRACE_KEYS}
        jp = getattr(request_data, "json_parameters", None)
        if not isinstance(jp, dict):
            return {k: {"kind": "json_parameters_unavailable"} for k in _TRACE_KEYS}
        out = {}
        for k in _TRACE_KEYS:
            if k not in jp:
                out[k] = {"kind": "absent"}          # 未传 ≠ 传了默认值
                continue
            raw = jp[k]
            wire = raw if isinstance(raw, str) else str(raw)
            try:
                val = json.loads(raw) if isinstance(raw, str) else raw
            except BaseException:
                # 解析失败也要留下**线上传的原样**（截断），禁止编造
                out[k] = {"kind": "parse_failed", "wire": wire[:120]}
                continue
            label = "value"
            if val in (0, "0") and not isinstance(val, bool):
                label = "zero"                     # 值为 0（offset/timeout 都可能）
            if k == "allowed_updates" and val == []:
                label = "empty_list"
            out[k] = {"kind": "present", "label": label, "value": val}
        return out
    except BaseException as e:
        keys["_error"] = type(e).__name__            # type: ignore[index]
        return keys


def _trace_begin(args, kwargs) -> dict:
    """请求侧记录（**任何异常都不得影响原调用**，故内部全程兜住）。"""
    try:
        url = http_method = request_data = None
        try:
            if kwargs:
                url = kwargs.get("url")
                http_method = kwargs.get("method")
                request_data = kwargs.get("request_data")
        except BaseException:
            pass
        try:
            if url is None and args:
                url = args[0]
            if http_method is None and len(args) > 1:
                http_method = args[1]
            if request_data is None and len(args) > 2:
                request_data = args[2]
        except BaseException:
            pass
        facts = _url_facts(url)
        with _state_lock:
            _state["req_seq"] += 1
            seq = _state["req_seq"]
        return {
            "seq": seq,
            "t0": round(time.time(), 3),
            "t1": None,
            "pid": os.getpid(),
            "http_method": http_method,
            "host": facts["host"],
            "api_method": facts["api_method"],
            "token_fp": facts["token_fp"],
            "url_parse": facts["parse"],
            "req": _param_states(request_data),
            "resp": None,
        }
    except BaseException as e:
        return {"seq": None, "t0": round(time.time(), 3), "t1": None,
                "pid": os.getpid(), "trace_error": type(e).__name__, "resp": None}


def _resp_facts(status, body) -> dict:
    """响应侧事实：状态、条数、更新 ID 首末值。**空返回不编造 ID。**"""
    out = {"status": status, "ok": None, "n": None,
           "id_first": None, "id_last": None, "parse": "ok"}
    try:
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        j = json.loads(raw)
        if not isinstance(j, dict):
            out["parse"] = "not_object"
            return out
        out["ok"] = j.get("ok")
        res = j.get("result")
        if isinstance(res, list):
            out["n"] = len(res)
            ids = [u.get("update_id") for u in res
                   if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
            if ids:                                # 空列表 → 保持 None，不编造
                out["id_first"], out["id_last"] = ids[0], ids[-1]
        else:
            out["parse"] = "result_not_list"
    except BaseException as e:
        out["parse"] = type(e).__name__
    return out


def _trace_end(rec, resp) -> None:
    """配对落账：同一 seq 关联起止时间与响应；有界保留，后台 flush。**吞掉一切异常。**"""
    try:
        if rec is None:
            return
        rec["t1"] = round(time.time(), 3)
        rec["resp"] = resp
        with _state_lock:
            _state["req_trace"] = (_state["req_trace"] + [rec])[-_REQ_TRACE_MAX:]
        flush()
    except BaseException:
        pass


# =============================================== ① 请求边界探针（核心不变式）
def install_poll_request_probe(request_obj, label: str = "get_updates") -> bool:
    """包装本进程自己的 HTTPXRequest.do_request。

    **不变式**：原调用必须被执行一次；其返回值 / 异常 / CancelledError 原样透传。
    诊断记账的成败绝不影响原调用。
    """
    try:
        if getattr(request_obj, "_tgdiag_installed", False):
            return False
        original = request_obj.do_request

        async def wrapped(*args, **kwargs):
            start = time.time()
            is_poll = False
            rec = None
            try:                                    # 前置记账失败 → 降级，不影响原调用
                is_poll = "getUpdates" in str(kwargs.get("url", ""))
                if is_poll:
                    _bump("poll_req_total")
                    _upd(poll_last_start=round(start, 3))
                    _log(f"[REQ] {label} 进入 do_request（尚未知是否发出）")
            except BaseException:
                is_poll = False
            try:                                    # 请求侧提取单独兜住：
                rec = _trace_begin(args, kwargs)    # 它失败不得改写 is_poll 记账
            except BaseException:
                rec = None

            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                try:                                # 记账失败也必须吞掉
                    if is_poll:
                        _bump("poll_req_err")
                        _dt = time.time() - start
                        chain = _exc_chain(exc)
                        _upd(
                            poll_last_end=round(time.time(), 3),
                            poll_last_duration=round(_dt, 3),
                            poll_last_error=type(exc).__name__,
                            poll_last_error_cause=chain,
                        )
                        hint = ("【请求未发出】" if "PoolTimeout" in chain else "【已尝试往返】")
                        _log(f"[REQ] {label} 异常{hint} 类型链={chain} "
                             f"耗时={_dt:.2f}s msg={_redact(exc)}", force=True)
                except BaseException:
                    pass
                try:                                # 请求↔响应配对（异常/取消也记录）
                    _trace_end(rec, {
                        "state": ("cancelled" if type(exc).__name__ == "CancelledError"
                                  else "exception"),
                        "chain": _exc_chain(exc),
                        "dt": round(time.time() - start, 3),
                    })
                except BaseException:
                    pass
                raise                                 # ← 原异常（含 CancelledError）原样透传

            try:                                    # 成功路径记账同样不得外溢
                if is_poll:
                    status, body = (result if isinstance(result, tuple) else (None, None))
                    _dt = time.time() - start
                    _bump("poll_http_response")      # 拿到响应（含 5xx）
                    _upd(poll_last_end=round(time.time(), 3),
                         poll_last_duration=round(_dt, 3), poll_last_status=status)
                    parsed, ok_flag, n = _parse(body)
                    # **有效轮询成功**的判据（三条缺一不可）：
                    #   status==200  且  JSON 可解析  且  ok=true  且  result 是列表
                    # 200 + 非法 JSON 时，真实 Bot.get_updates 会抛 TelegramError ——
                    # 因此那**不是**成功轮询，只记 HTTP 响应 + 解析失败。
                    if status == 200 and parsed and ok_flag is True and n is not None:
                        _bump("poll_success")
                        _upd(poll_last_error=None, poll_last_error_cause=None)
                        if n == 0:
                            _bump("poll_empty_ok")
                        else:
                            _bump("poll_with_updates")
                            _bump("updates_returned", n)
                        _log(f"[REQ] {label} 轮询成功 状态={status} 耗时={_dt:.2f}s 更新数={n}")
                    elif status == 200 and not parsed:
                        # 拿到 HTTP 响应，但**解析失败** → 不是有效轮询
                        _bump("poll_unparsed")
                        _upd(poll_last_error="UnparsableResponse",
                             poll_last_error_cause="UnparsableResponse")
                        _log(f"[REQ] {label} 状态=200 但**响应不可解析**"
                             f"→ 不是有效轮询成功（真实调用会抛 TelegramError）", force=True)
                    else:
                        # 收到 HTTP 响应 ≠ 轮询成功（G2）
                        _bump("poll_http_error")
                        _upd(poll_last_error=f"HTTP {status}",
                             poll_last_error_cause=f"HttpStatus {status}")
                        _log(f"[REQ] {label} **收到响应但非成功轮询** 状态={status} "
                             f"ok={ok_flag} 耗时={_dt:.2f}s", force=True)
            except BaseException:
                pass
            try:                                    # 成功路径配对（所有方法都记）
                if isinstance(result, tuple) and len(result) == 2:
                    _trace_end(rec, _resp_facts(result[0], result[1]))
                else:
                    _trace_end(rec, {"state": "non_tuple_result"})
            except BaseException:
                pass
            return result                           # ← 原返回值原样透传

        request_obj.do_request = wrapped
        request_obj._tgdiag_installed = True
        _log(f"[REQ] 请求探针已安装: {label} -> {type(original).__name__}", force=True)
        flush()
        return True
    except BaseException as exc:
        _log(f"[REQ] 探针安装失败（不影响业务）: {type(exc).__name__}: {_redact(exc)}", force=True)
        return False


def _parse(body):
    """返回 (是否可解析, ok 标志, 更新条数)。

    · 不可解析            → (False, None, None)
    · 可解析但 ok 非 true → (True, ok, None)
    · ok=true 且 result 是列表 → (True, True, len)
    · ok=true 但 result 不是列表 → (True, True, None)（异常形状，不算有效轮询）
    """
    try:
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        j = json.loads(raw)
        if not isinstance(j, dict):
            return True, None, None
        ok_flag = bool(j.get("ok"))
        res = j.get("result")
        return True, ok_flag, (len(res) if isinstance(res, list) else None)
    except BaseException:
        return False, None, None


# ================================================ ② 任务巡检（已取消安全）
def _task_state(task):
    """只读任务状态。**不含任何未脱敏 repr**（B3）。已取消任务不读异常（B2）。"""
    out = {"exists": task is not None}
    if task is None:
        return out
    try:
        out["done"] = bool(task.done())
    except BaseException as e:
        out["done"] = None
        out["done_error"] = type(e).__name__
    cancelled = None
    try:
        cancelled = bool(task.cancelled())
    except BaseException as e:
        out["cancelled_error"] = type(e).__name__
    out["cancelled"] = cancelled
    # 已取消的任务调 exception() 会抛 CancelledError（BaseException）→ 必须先判
    if cancelled:
        out["exception"] = "<cancelled>"
    elif out.get("done"):
        try:
            exc = task.exception()
            out["exception"] = _exc_chain(exc) if exc is not None else None
        except BaseException as e:
            out["exception"] = f"<读取失败 {type(e).__name__}>"
    else:
        out["exception"] = None
    # 当前等待位置（文件名:行号:函数名），不含局部变量
    try:
        frames = task.get_stack(limit=6)
        out["wait_at"] = [f"{f.f_code.co_filename.split(os.sep)[-1]}:{f.f_lineno}:{f.f_code.co_name}"
                          for f in frames][-4:]
    except BaseException:
        out["wait_at"] = []
    return out


def install_updater_watch(app, interval: float = 15.0) -> threading.Thread:
    """后台只读巡检。线程体**永不因任何异常退出**（含 BaseException）。"""

    def _loop():
        while True:
            try:
                time.sleep(interval)
            except BaseException:
                pass
            try:
                upd = getattr(app, "updater", None)
                pol = None
                if upd is not None:
                    for attr in ("_Updater__polling_task", "__polling_task"):
                        try:
                            pol = getattr(upd, attr, None)
                        except BaseException:
                            pol = None
                        if pol is not None:
                            break
                snap = {
                    "t": round(time.time(), 3),
                    "updater_exists": upd is not None,
                    "updater_running": getattr(upd, "running", None) if upd else None,
                    "app_running": getattr(app, "running", None),
                    "polling_task": _task_state(pol),
                }
                with _state_lock:
                    _state["task_snapshots"] = (_state["task_snapshots"] + [snap])[-5:]
                pt = snap["polling_task"]
                _log(f"[TASK] updater.running={snap['updater_running']} "
                     f"app.running={snap['app_running']} "
                     f"task.exists={pt.get('exists')} done={pt.get('done')} "
                     f"cancelled={pt.get('cancelled')} exc={pt.get('exception')} "
                     f"wait_at={pt.get('wait_at')}", force=True)
                flush()
            except BaseException as exc:            # 绝不因 BaseException 退出线程
                _log(f"[TASK] 巡检异常（已忽略）: {type(exc).__name__}: {_redact(exc)}")

    t = threading.Thread(target=_loop, daemon=True, name="tgdiag_watch")
    t.start()
    _log(f"[TASK] 巡检线程已启动 interval={interval}s", force=True)
    return t


# ============================================ ③ 更新处理 / 回复结果（不抛错）
def record_update_processed(ok: bool = True, detail: str = "") -> None:
    """**绝不允许抛异常**——调用点在业务路径上。"""
    try:
        _bump("updates_processed" if ok else "updates_process_fail")
        _log(f"[UPD] 处理一条更新 ok={ok} {_redact(detail) if detail else ''}")
        flush()
    except BaseException:
        pass


def record_reply(ok: bool, detail: str = "") -> None:
    """**绝不允许抛异常**（B1：观测抛错会吞掉 safe_reply 的返回值）。"""
    try:
        if ok:
            _bump("reply_ok")
            _log("[REPLY] 回复成功")
        else:
            _bump("reply_fail")
            _upd(reply_last_error=_redact(detail))
            _log(f"[REPLY] 回复失败 {_redact(detail)}", force=True)
        flush()
    except BaseException:
        pass


def record_error_callback(kind: str, exc) -> None:
    """观察应用 error_handler 到底被不被调用（对比请求层异常）。"""
    try:
        _log(f"[ERR] 应用错误回调被调用 kind={kind} 类型链={_exc_chain(exc)}", force=True)
        flush()
    except BaseException:
        pass


def install_process_probe(app) -> bool:
    """包装**真实** Application.process_update，接入处理进度（G1）。

    只读计数与计时，原 update 照常处理；异常原样透传。
    """
    try:
        if getattr(app, "_tgdiag_proc_installed", False):
            return False
        original = app.process_update

        async def wrapped(update, *a, **k):
            # 不变式（两个方向各自隔离）：
            #   原调用异常 → 观测单独兜住，**原异常/取消**原样透传
            #   原调用成功 → 观测单独兜住，**原返回值**原样返回
            #   （曾因观测与原调用同处一个 try，观测抛错会顶掉原返回值）
            try:
                result = await original(update, *a, **k)
            except BaseException as e:
                try:
                    record_update_processed(False, f"{type(e).__name__}")
                except BaseException:
                    pass
                raise
            try:
                record_update_processed(True)
            except BaseException:
                pass
            return result
        app.process_update = wrapped
        app._tgdiag_proc_installed = True
        _log("[UPD] 处理进度探针已安装（包装真实 process_update）", force=True)
        return True
    except BaseException as exc:
        _log(f"[UPD] 处理探针安装失败（不影响业务）: {type(exc).__name__}: {_redact(exc)}", force=True)
        return False


def dump_summary() -> None:
    try:
        _log("SUMMARY " + json.dumps(snapshot(), ensure_ascii=False), force=True)
    except BaseException:
        pass