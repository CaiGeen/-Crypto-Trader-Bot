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
    ok = (data.get('blocked') is True
          and ex.fetch_positions.call_count == 1        # 无 5 次重试
          and any('盲区' in m for m in t._sent)          # 三通道告警含盲区字样
          and len(t._emails) >= 1                        # Email 通道（critical 级）
          and t.last_known_ip == '1.2.3.4')              # IP 提取仍生效
    report('S2 -2015 触发', ok,
           f'blocked={data.get("blocked")} 调用数={ex.fetch_positions.call_count} '
           f'TG={len(t._sent)} Email={len(t._emails)} ip={t.last_known_ip}')


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


# ==================== 主流程 ====================

def main():
    for fn in (s1_gate_position_ast, s2_2015_triggers_block, s3_zero_network_calls,
               s4_five_monitor_cycles, s5_l920_dynamic_zero, s6_auth_probe_whitelist,
               s7_recovery_paths, s8_blind_safe_order, s9_watchdog_queue_format,
               s10_trader_queue_format, s11_corrupt_fail_closed, s12_init_survives_persistent_lock):
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
