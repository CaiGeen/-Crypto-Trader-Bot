# -*- coding: utf-8 -*-
"""
D-010 Batch 2（trader/watchdog 写入侧 + AUTH_BLOCKED）离线验收测试
（不连真实 Telegram、不连交易所、零真实网络请求）

设计依据：
- .workbuddy/memory/discussions/D-010_通知链路加固与2015分流_设计确认稿_v3.md（v3.1 约束 C5）
- .workbuddy/memory/discussions/D-010_Batch2_实施方案_改动点清单.md
- ChatGPT 终审 2026-08-28 三条钉死约束：
  1. AUTH_BLOCKED 闸门位于 retry loop 与 try 之前，raise 于 try 外
  2. L920 load_time_difference 必须 AST + 动态 mock 零网络调用双验证
  3. BLIND-SAFE 恢复顺序锁死：probe → RECOVERING → reconcile → clear（Fail-Closed，绝不半恢复）

覆盖场景：
  S1  闸门位置（AST）：raise AuthBlockedError 在 for 循环之前、不在任何 try 内
  S2  -2015 触发：写锁 + 三通道告警（含"盲区"字样）+ 立即 raise，无 5 次重试
  S3  AUTH_BLOCKED 普通 API 零调用（6 个方法，mock 网络层计数=0）
  S4  连续 5 轮监控循环 → Binance API 调用 = 0
  S5  L920 动态验证：blocked + -1021 错误 → load_time_difference 零调用
  S6  auth_probe 唯一放行 + AST 全库普查（auth_probe=True 仅出现在 _attempt_auth_recovery）
  S7  恢复链成败两路：探活失败保持锁；reconcile 失败回 BLOCKED 不半恢复
  S8  BLIND-SAFE 顺序（AST + 运行时）：reconcile 先于 clear，clear 前无 UNBLOCKED
  S9  watchdog 写入格式闭环：watchdog_alert|{text}，消费端可解析，旧 .notify 不再产生
  S10 trader 入队格式闭环：ip_notify|{msg}，消费端可解析 + Email 通道（T5）
  S11 auth_blocked.json 损坏 → Fail-Closed（按 BLOCKED 处理）+ 损坏告警去重
  S12 持久锁存在时 CryptoTrader.__init__ 不崩溃（跳过启动初始化 API，零网络调用）
  S13 三通道真实语义（ChatGPT 复审补充）：AUTH_BLOCKED=三通道并行（队列无条件）；
      IP=双通道+队列 fallback-only（用户裁定 B，设计稿同步）
  S14 真实启动集成路径（ChatGPT 复审补充）：持久锁启动→探活恰1次→败=保持锁且
      recover 不可达；成=RECOVERING→reconcile→UNBLOCKED→READY（无普通 API 绕过）

用法: .venv\\Scripts\\python.exe test_auth_blocked.py
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile
from unittest import mock

import trader_260725
from trader_260725 import CryptoTrader

TRADER_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "trader_260725.py"), encoding="utf-8").read()
BOT_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bot_runner.py"), encoding="utf-8").read()
WATCHDOG_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "watchdog.py"), encoding="utf-8").read()

PASS, FAIL = 0, 0
RESULTS = []


def report(name, passed, detail=''):
    global PASS, FAIL
    if passed:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((passed, name, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")


ERR_2015 = ("binance APIError: code=-2015 Invalid API-key, IP, or permissions for action. "
            "Request IP: 1.2.3.4")
ERR_1021 = "binance APIError: code=-1021 Timestamp for this request is outside of the recvWindow."


def make_trader(tmp, pre_auth=None, configure=None):
    """全新真实 CryptoTrader 实例（=一次"重启"）。
    - STATE_FILE / AUTH_BLOCKED_FILE / NOTIFY_QUEUE_DIR_TRADER 模块级重定向
    - ccxt.binanceusdm → MagicMock（防 __init__ 联网）
    - _daily_report_loop → 空函数（防后台线程干扰）
    - send_tg_notification / _send_email_alert 实例级收集
    pre_auth: dict → 构造前预写 auth_blocked.json（模拟持久锁跨重启）"""
    state_file = os.path.join(str(tmp), 'trade_state.json')
    auth_file = os.path.join(str(tmp), 'auth_blocked.json')
    queue_dir = os.path.join(str(tmp), '.notify_queue')
    trader_260725.STATE_FILE = state_file
    trader_260725.AUTH_BLOCKED_FILE = auth_file
    trader_260725.NOTIFY_QUEUE_DIR_TRADER = queue_dir
    if pre_auth is not None:
        with open(auth_file, 'w', encoding='utf-8') as f:
            json.dump(pre_auth, f, ensure_ascii=False)
    with mock.patch.object(CryptoTrader, '_daily_report_loop', lambda self: None):
        with mock.patch.object(trader_260725.ccxt, 'binanceusdm') as mk:
            ex = mock.MagicMock()
            ex.load_time_difference.return_value = None
            ex.load_markets.return_value = {}
            ex.fetch_time.return_value = 1234567890.0
            ex.fetch_positions.return_value = []
            ex.fetch_balance.return_value = {}
            ex.set_leverage = mock.MagicMock(side_effect=lambda *a, **k: None)
            mk.return_value = ex
            t = CryptoTrader('k', 's')
    t._min_api_interval = 0
    t._api_cooldown_until = 0
    # 测试隔离：IP 持久化重定向 + 固定基线（S2/S10/S13 曾依赖项目根 last_ip.txt 的
    # 跨运行残留做去重判定，导致偶发 FAIL——2026-08-28 S13 复审时发现并根治）
    t.ip_file = os.path.join(str(tmp), 'last_ip.txt')
    t.last_known_ip = '0.0.0.0'
    if configure:
        configure(ex)
    t._sent = []
    t._emails = []
    t.send_tg_notification = lambda text, **k: t._sent.append(str(text))
    t._send_email_alert = lambda text, subject='交易告警': t._emails.append((str(text), subject))
    return t, ex


def auth_file_of(t):
    with open(t.auth_blocked_file, encoding='utf-8') as f:
        return json.load(f)


def write_auth(t, data):
    with open(t.auth_blocked_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


BLOCKED_JSON = {'blocked': True, 'state': 'BLOCKED',
                'reason': 'binance -2015', 'updated_at': '2026-08-28 09:00:00'}

EVENT_ID_RE = re.compile(r'^\d{8}_\d{6}_\d{6}_[0-9a-f]{8}$')


# ==================== S1 闸门位置（AST） ====================

def s1_gate_position_ast():
    tree = ast.parse(TRADER_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_safe_api_call')
    for_node = next(n for n in ast.walk(fn) if isinstance(n, ast.For))

    gate_raises = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Raise) and n.exc is not None
                   and 'AuthBlockedError' in ast.dump(n.exc)]
    if not gate_raises:
        report('S1 闸门位置（AST）', False, '未找到 raise AuthBlockedError')
        return
    # 闸门 raise = 不在任何 try 块内的那些（-2015 检测点的 raise 合法地位于 except 分支内，
    # 那是"检测后抛出"而非"入口闸门"；钉死约束针对的是入口闸门）
    try_nodes = [tn for tn in ast.walk(fn) if isinstance(tn, ast.Try)]
    gate_raises = [r for r in gate_raises
                   if not any(any(sub is r for sub in ast.walk(tn)) for tn in try_nodes)]
    if not gate_raises:
        report('S1 闸门位置（AST）', False, '未找到位于 try 外的闸门 raise')
        return
    ok_pos = all(r.lineno < for_node.lineno for r in gate_raises)
    report('S1 闸门位置（AST）', ok_pos,
           f'闸门 raise 行号 {[r.lineno for r in gate_raises]} < for 行号 {for_node.lineno}（try 外）')


# ==================== S2 -2015 触发 ====================

def s2_2015_triggers_block():
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    ex.fetch_positions.side_effect = Exception(ERR_2015)
    try:
        t._safe_api_call(ex.fetch_positions, ['BTCUSDT'])
        report('S2 -2015 触发', False, '未抛出异常')
        return
    except trader_260725.AuthBlockedError:
        pass
    except Exception as e:
        report('S2 -2015 触发', False, f'异常类型错误: {type(e).__name__}')
        return
    data = auth_file_of(t)
    # 队列事件（S13 复审补充）：AUTH_BLOCKED 告警的队列通道为无条件写入，不依赖 TG 成败。
    # 测试环境无 tg_bot → IP 事件也按 fallback 契约入队（ip_notify），故队列 = ip_notify + auth_blocked
    qdir = t.notify_queue_dir
    q_files = [f for f in os.listdir(qdir) if f.endswith('.notify')] if os.path.isdir(qdir) else []
    types_in_queue = []
    import bot_runner as _br
    for f in q_files:
        with open(os.path.join(qdir, f), encoding='utf-8') as fh:
            typ, msg = _br._parse_notify_content(fh.read())
        types_in_queue.append(typ)
    q_ok = (types_in_queue.count('auth_blocked') == 1
            and set(types_in_queue) <= {'auth_blocked', 'ip_notify'})
    ok = (data.get('blocked') is True
          and ex.fetch_positions.call_count == 1        # 无 5 次重试
          and any('盲区' in m for m in t._sent)          # 三通道告警含盲区字样
          and len(t._emails) >= 1                        # Email 通道（critical 级）
          and q_ok                                        # 队列事件无条件入队（三通道第3路）
          and t.last_known_ip == '1.2.3.4')              # IP 提取仍生效
    report('S2 -2015 触发', ok,
           f'blocked={data.get("blocked")} 调用数={ex.fetch_positions.call_count} '
           f'TG={len(t._sent)} Email={len(t._emails)} 队列={types_in_queue} ip={t.last_known_ip}')


# ==================== S3 普通 API 零调用（场景 7 核心） ====================

def s3_zero_network_calls():
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t, dict(BLOCKED_JSON))
    methods = [ex.fetch_positions, ex.fetch_open_orders, ex.fetch_balance,
               ex.create_order, ex.cancel_order, ex.set_leverage]
    all_raised = True
    for m in methods:
        try:
            t._safe_api_call(m, 'BTCUSDT')
            all_raised = False
        except trader_260725.AuthBlockedError:
            pass
        except Exception:
            all_raised = False
    total = sum(m.call_count for m in methods)
    report('S3 AUTH_BLOCKED 普通 API 零调用', all_raised and total == 0,
           f'6 方法网络调用总数 = {total}（应为 0），全部 AuthBlockedError = {all_raised}')


# ==================== S4 5 轮监控循环零 API ====================

def s4_five_monitor_cycles():
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t, dict(BLOCKED_JSON))
    for _ in range(5):
        try:
            t._safe_api_call(ex.fetch_open_orders, 'BTCUSDT')
        except trader_260725.AuthBlockedError:
            pass
    report('S4 5 轮监控循环零 API', ex.fetch_open_orders.call_count == 0,
           f'fetch_open_orders 调用数 = {ex.fetch_open_orders.call_count}（应为 0）')


# ==================== S5 L920 动态零调用（ChatGPT 钉死约束 2） ====================

def s5_l920_dynamic_zero():
    # 构造前预置锁（__init__ 的 2 次 load_time_difference 也被跳过 → 计数基线为 0，
    # 任何后续调用即为 -1021 分支 L920 泄漏）
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'), pre_auth=dict(BLOCKED_JSON))
    ex.fetch_positions.side_effect = Exception(ERR_1021)  # 触发 -1021 分支的输入
    try:
        t._safe_api_call(ex.fetch_positions, ['BTCUSDT'])
        report('S5 L920 动态零调用', False, '未抛出异常')
        return
    except trader_260725.AuthBlockedError:
        pass
    except Exception as e:
        report('S5 L920 动态零调用', False, f'异常类型错误: {type(e).__name__}')
        return
    report('S5 L920 动态零调用', ex.load_time_difference.call_count == 0,
           f'load_time_difference 调用数 = {ex.load_time_difference.call_count}（应为 0，含 __init__ 的 2 次）')


# ==================== S6 auth_probe 唯一放行 + AST 普查 ====================

def s6_auth_probe_whitelist():
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t, dict(BLOCKED_JSON))
    # 运行时：auth_probe=True 可通过闸门
    try:
        t._safe_api_call(ex.fetch_balance, auth_probe=True)
        probe_ok = ex.fetch_balance.call_count == 1
    except Exception:
        probe_ok = False

    # AST：auth_probe=True 关键字在全代码库仅出现在 trader._attempt_auth_recovery 内
    allowed_owner = '_attempt_auth_recovery'
    violations = []
    for src, fname in ((TRADER_SRC, 'trader'), (BOT_SRC, 'bot_runner'), (WATCHDOG_SRC, 'watchdog')):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == 'auth_probe' and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        # 向上找归属函数
                        owner = None
                        for fn in ast.walk(tree):
                            if isinstance(fn, ast.FunctionDef):
                                for sub in ast.walk(fn):
                                    if sub is node:
                                        owner = fn.name
                                        break
                        if owner != allowed_owner:
                            violations.append(f'{fname}:{owner or "<module>"} 行{node.lineno}')
    report('S6 auth_probe 唯一放行', probe_ok and not violations,
           f'运行时放行={probe_ok}；越界调用点={violations or "无"}')


# ==================== S7 恢复链成败两路 ====================

def s7_recovery_paths():
    # 路径 1：探活失败 → 保持锁
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t, dict(BLOCKED_JSON))
    ex.fetch_balance.side_effect = Exception(ERR_2015)
    ok, msg = t._attempt_auth_recovery()
    d1 = auth_file_of(t)
    r1 = (ok is False and d1.get('blocked') is True and t._auth_recovering is False)

    # 路径 2：探活成功 + reconcile 失败 → 回 BLOCKED（不半恢复）
    t2, ex2 = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t2, dict(BLOCKED_JSON))
    events2 = []
    ex2.fetch_balance.side_effect = lambda *a, **k: events2.append('probe') or {}
    t2.recover_active_batches = lambda: events2.append('reconcile') or False
    ok2, msg2 = t2._attempt_auth_recovery()
    d2 = auth_file_of(t2)
    r2 = (ok2 is False and d2.get('blocked') is True and d2.get('state') == 'BLOCKED'
          and t2._auth_recovering is False and events2 == ['probe', 'reconcile'])

    # 路径 3：全链成功 → UNBLOCKED + 顺序正确
    t3, ex3 = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t3, dict(BLOCKED_JSON))
    events3 = []
    ex3.fetch_balance.side_effect = lambda *a, **k: events3.append('probe') or {}
    t3.recover_active_batches = lambda: events3.append('reconcile') or True
    ok3, msg3 = t3._attempt_auth_recovery()
    d3 = auth_file_of(t3)
    r3 = (ok3 is True and d3.get('blocked') is False and d3.get('state') == 'UNBLOCKED'
          and events3 == ['probe', 'reconcile']
          and any('恢复' in m for m in t3._sent))

    report('S7 恢复链成败两路', r1 and r2 and r3, f'探活败={r1} reconcile败={r2} 全成功={r3}')


# ==================== S8 BLIND-SAFE 顺序（AST + 运行时） ====================

def s8_blind_safe_order():
    tree = ast.parse(TRADER_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_attempt_auth_recovery')

    reconcile_ln = None
    clear_lns = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name == 'recover_active_batches':
                reconcile_ln = node.lineno
            if name == '_save_auth_state' and node.args and isinstance(node.args[0], ast.Constant):
                if node.args[0].value in ('UNBLOCKED',):
                    clear_lns.append(node.lineno)
    ast_ok = (reconcile_ln is not None and clear_lns
              and all(cl > reconcile_ln for cl in clear_lns))

    # 运行时：clear 动作（UNBLOCKED 落盘）必须发生在 reconcile 之后
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    write_auth(t, dict(BLOCKED_JSON))
    trace = []

    orig_save = t._save_auth_state

    def traced_save(state, reason=''):
        trace.append(state)
        return orig_save(state, reason)

    t._save_auth_state = traced_save
    ex.fetch_balance.side_effect = lambda *a, **k: {}
    t.recover_active_batches = lambda: trace.append('reconcile') or True
    t._attempt_auth_recovery()
    # 期望顺序：RECOVERING → reconcile → UNBLOCKED（reconcile 先于 UNBLOCKED）
    rec_idx = trace.index('reconcile')
    un_idx = trace.index('UNBLOCKED')
    rt_ok = rec_idx < un_idx
    report('S8 BLIND-SAFE 顺序', ast_ok and rt_ok,
           f'AST: reconcile@{reconcile_ln} < clear@{clear_lns}；运行时 trace={trace}')


# ==================== S9 watchdog 写入格式闭环（W1/W2/E5） ====================

def s9_watchdog_queue_format():
    import watchdog
    tmp = tempfile.mkdtemp(prefix='d010b2_wd_')
    watchdog.BASE_DIR = str(tmp)
    watchdog.LOG_FILE = os.path.join(str(tmp), 'watchdog.log')
    import bot_runner

    watchdog.atomic_write_notify("crash_alert|测试崩溃")
    watchdog.send_tg_notification("系统级告警测试")  # W2：原写 {iso}|{text} 死格式
    qdir = os.path.join(str(tmp), '.notify_queue')
    files = sorted(f for f in os.listdir(qdir) if f.endswith('.notify'))
    ok_files = len(files) == 2 and all(not f.startswith('.tmp_') for f in files)
    ok_ids = all(EVENT_ID_RE.match(f[:-len('.notify')]) for f in files)

    contents = {}
    for f in files:
        with open(os.path.join(qdir, f), encoding='utf-8') as fh:
            c = fh.read()
        typ, msg = bot_runner._parse_notify_content(c)
        contents[typ] = msg
    ok_types = contents.get('crash_alert') == '测试崩溃' and contents.get('watchdog_alert') == '系统级告警测试'
    # E5：不再产生旧单文件 .notify
    legacy = os.path.exists(os.path.join(str(tmp), '.notify'))
    report('S9 watchdog 写入格式闭环', ok_files and ok_ids and ok_types and not legacy,
           f'文件数={len(files)} id格式={ok_ids} types={list(contents.keys())} 旧.notify存在={legacy}')


# ==================== S10 trader 入队格式闭环（T1/T5） ====================

def s10_trader_queue_format():
    import bot_runner
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    # 无 tg_bot → _try_async_send 返回 False → 走 _fallback_notify_file 入队
    t._record_ip_change('5.6.7.8', source='binance_error')
    qdir = t.notify_queue_dir
    files = [f for f in os.listdir(qdir) if f.endswith('.notify')]
    ok_files = len(files) == 1
    content = ''
    if files:
        with open(os.path.join(qdir, files[0]), encoding='utf-8') as fh:
            content = fh.read()
    typ, msg = bot_runner._parse_notify_content(content)
    ok_type = typ == 'ip_notify' and '5.6.7.8' in msg
    ok_id = EVENT_ID_RE.match(files[0][:-len('.notify')]) is not None if files else False
    legacy = os.path.exists(os.path.join(os.path.dirname(t.notify_queue_dir), '.notify'))
    # T5：Email 通道独立发送（不依赖 TG 成败）
    ok_email = len(t._emails) >= 1 and '5.6.7.8' in t._emails[0][0]
    report('S10 trader 入队格式闭环', ok_files and ok_type and ok_id and not legacy and ok_email,
           f'文件数={len(files)} type={typ} id格式={ok_id} 旧.notify存在={legacy} Email={len(t._emails)}')


# ==================== S11 锁文件损坏 Fail-Closed ====================

def s11_corrupt_fail_closed():
    t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_'))
    with open(t.auth_blocked_file, 'w', encoding='utf-8') as f:
        f.write("这不是JSON{{{")
    raised = False
    try:
        t._safe_api_call(ex.fetch_positions, ['BTCUSDT'])
    except trader_260725.AuthBlockedError:
        raised = True
    except Exception:
        raised = False
    # 损坏告警去重：调用两次，告警只发一次
    try:
        t._safe_api_call(ex.fetch_open_orders, 'BTCUSDT')
    except trader_260725.AuthBlockedError:
        pass
    corrupt_alerts = [m for m in t._sent if '损坏' in m]
    ok = (raised and ex.fetch_positions.call_count == 0 and len(corrupt_alerts) == 1
          and any('Fail-Closed' in m or '盲区' in m for m in corrupt_alerts))
    report('S11 锁文件损坏 Fail-Closed', ok,
           f'拒绝={raised} 网络=0 告警次数={len(corrupt_alerts)}（应为 1，去重）')


# ==================== S12 持久锁下 __init__ 不崩溃（B3 前置） ====================

def s12_init_survives_persistent_lock():
    tmp = tempfile.mkdtemp(prefix='d010b2_')
    t, ex = make_trader(tmp, pre_auth=dict(BLOCKED_JSON))
    locked = t._load_auth_state()
    ok = (locked.get('locked') is True
          and ex.load_time_difference.call_count == 0   # 启动初始化 API 全部跳过
          and ex.load_markets.call_count == 0
          and ex.fetch_time.call_count == 0)
    report('S12 持久锁下 __init__ 不崩溃', ok,
           f'locked={locked.get("locked")} init API 调用='
           f'{ex.load_time_difference.call_count + ex.load_markets.call_count + ex.fetch_time.call_count}（应为 0）')


# ==================== S13 三通道真实语义（ChatGPT 复审 2026-08-28 补充） ====================
# 钉死两路径正式契约（用户裁定 B，设计稿 v3.2 同步）：
#   AUTH_BLOCKED 事件 = 完整三通道并行（TG/Email 路径 + 队列事件**无条件写入**，互不依赖）
#   IP 变更事件     = TG + Email 双通道并行；队列仅 TG 失败时 fallback（安全网定位）

def s13_channel_semantics():
    import bot_runner

    # --- AUTH_BLOCKED 路径（_enter_auth_blocked）：三通道互不依赖 ---
    def run_blocked_case(tg_raises, queue_raises):
        t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_s13a_'))
        if tg_raises:
            def bad_tg(text, **k):
                raise RuntimeError('TG down')
            t.send_tg_notification = bad_tg
        else:
            t.send_tg_notification = lambda text, **k: t._sent.append(str(text))
        if queue_raises:
            def bad_queue(*a, **k):
                raise RuntimeError('queue down')
            t._enqueue_notify_event = bad_queue
        t._enter_auth_blocked('测试原因')
        qdir = t.notify_queue_dir
        files = [f for f in os.listdir(qdir) if f.endswith('.notify')] if os.path.isdir(qdir) else []
        if queue_raises:
            # 象限3 契约：队列写入失败 → 无文件产生、异常被 _enter_auth_blocked 吞掉
            # （不向外传播）、TG 已发出、锁仍写入——通道独立失败边界
            q_ok = len(files) == 0
        else:
            q_ok = len(files) == 1
            if q_ok:
                with open(os.path.join(qdir, files[0]), encoding='utf-8') as fh:
                    content = fh.read()
                typ, msg = bot_runner._parse_notify_content(content)
                q_ok = typ == 'auth_blocked' and '盲区' in msg
        # 锁写入与 TG 发出互不受另一通道失败影响
        locked = auth_file_of(t).get('blocked') is True
        tg_ok = (len(t._sent) == 0) if tg_raises else (len(t._sent) == 1)
        return q_ok and locked and tg_ok

    # 象限1：TG 成功 → 队列仍有 1 个 auth_blocked 事件（无条件，非 fallback）
    q1 = run_blocked_case(tg_raises=False, queue_raises=False)
    # 象限2：TG 失败 → 队列仍有 1 + 锁仍写入（Fail-Closed but not Fail-Silent）
    q2 = run_blocked_case(tg_raises=True, queue_raises=False)
    # 象限3：队列写入失败 → 不阻塞 TG 已发出 + 锁仍写入（通道独立失败边界）
    q3 = run_blocked_case(tg_raises=False, queue_raises=True)

    # --- IP 路径（_record_ip_change）：双通道并行 + 队列 fallback-only ---
    def run_ip_case(tg_ok):
        t, ex = make_trader(tempfile.mkdtemp(prefix='d010b2_s13b_'))
        t._try_async_send = lambda text: tg_ok
        t._record_ip_change('9.9.9.9', source='binance_error')
        qdir = t.notify_queue_dir
        files = [f for f in os.listdir(qdir) if f.endswith('.notify')] if os.path.isdir(qdir) else []
        return len(files), len(t._emails)

    n_tg_ok, mail_ok = run_ip_case(True)       # TG 成功 → 队列 0（fallback-only 契约）
    n_tg_fail, mail_fail = run_ip_case(False)  # TG 失败 → 队列 1（安全网兜住）
    ip_ok = (n_tg_ok == 0 and n_tg_fail == 1 and mail_ok >= 1 and mail_fail >= 1)

    report('S13 三通道真实语义', q1 and q2 and q3 and ip_ok,
           f'AUTH_BLOCKED象限: TG成功={q1} TG失败={q2} 队列失败={q3}；'
           f'IP路径: TG成功队列={n_tg_ok} TG失败队列={n_tg_fail} Email={mail_ok}/{mail_fail}')


# ==================== S14 真实启动 AUTH_BLOCKED 集成路径（ChatGPT 复审补充） ====================
# 覆盖 run_trader_recovery_on_startup 全链路（非函数级）：
# 持久锁启动 → 启动探活恰 1 次 → 失败保持 BLOCKED 且 recover_active_batches 不可达
# / 成功 → RECOVERING → reconcile → UNBLOCKED → READY；全程普通 API 不绕过闸门

def s14_startup_integration():
    import asyncio
    import bot_runner

    # --- A) 探活失败路径 ---
    tmp_a = tempfile.mkdtemp(prefix='d010b2_s14a_')
    t, ex = make_trader(tmp_a, pre_auth=dict(BLOCKED_JSON))
    ex.fetch_balance = mock.MagicMock(side_effect=Exception(ERR_2015))
    rec_calls = []
    t.recover_active_batches = lambda: rec_calls.append(1) or True
    trace_a = []
    orig_save_a = t._save_auth_state
    t._save_auth_state = lambda s, r='': trace_a.append(s) or orig_save_a(s, r)
    bot_runner.TRADER_LOCK = asyncio.Lock()    # 测试内换新锁（防跨 event loop 复用报错）
    asyncio.run(bot_runner.run_trader_recovery_on_startup(t))
    d_a = auth_file_of(t)
    a_ok = (ex.fetch_balance.call_count == 1          # 探活恰 1 次
            and d_a.get('blocked') is True and d_a.get('state') == 'BLOCKED'
            and len(rec_calls) == 0                    # 探活败 → 常规恢复不可达（不反复重试）
            and bool(t._not_ready_reason) and t._ready is False
            and ex.fetch_positions.call_count == 0)    # 锁定期普通 API = 0

    # --- B) 探活成功路径 ---
    tmp_b = tempfile.mkdtemp(prefix='d010b2_s14b_')
    t2, ex2 = make_trader(tmp_b, pre_auth=dict(BLOCKED_JSON))
    probe_log = []
    ex2.fetch_balance = mock.MagicMock(side_effect=lambda *a, **k: probe_log.append('probe') or {})
    rec_calls_b = []
    t2.recover_active_batches = lambda: rec_calls_b.append(1) or True
    trace_b = []
    orig_save_b = t2._save_auth_state
    t2._save_auth_state = lambda s, r='': trace_b.append(s) or orig_save_b(s, r)
    bot_runner.TRADER_LOCK = asyncio.Lock()
    asyncio.run(bot_runner.run_trader_recovery_on_startup(t2))
    d_b = auth_file_of(t2)
    b_ok = (len(probe_log) == 1                        # 整个启动流程只产生一次探活
            and d_b.get('blocked') is False and d_b.get('state') == 'UNBLOCKED'
            and trace_b == ['RECOVERING', 'UNBLOCKED']  # clear 前必经 RECOVERING（无半恢复）
            and len(rec_calls_b) == 2                  # 恢复链内 1 次 + 常规恢复幂等二跑 1 次（既有设计）
            and t2._ready is True and t2._not_ready_reason == '')

    report('S14 真实启动集成路径', a_ok and b_ok,
           f'探活败: probe={ex.fetch_balance.call_count} state={d_a.get("state")} '
           f'recover调用={len(rec_calls)} ready={t._ready}；'
           f'探活成: probe={len(probe_log)} state={d_b.get("state")} trace={trace_b} '
           f'recover调用={len(rec_calls_b)} ready={t2._ready}')


# ==================== 主流程 ====================

def main():
    for fn in (s1_gate_position_ast, s2_2015_triggers_block, s3_zero_network_calls,
               s4_five_monitor_cycles, s5_l920_dynamic_zero, s6_auth_probe_whitelist,
               s7_recovery_paths, s8_blind_safe_order, s9_watchdog_queue_format,
               s10_trader_queue_format, s11_corrupt_fail_closed, s12_init_survives_persistent_lock,
               s13_channel_semantics, s14_startup_integration):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            report(fn.__name__, False, f'测试自身异常: {e}')

    print("\n" + "=" * 60)
    print(f"D-010 Batch 2 专项测试: {PASS} 通过 / {FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
