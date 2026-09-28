# -*- coding: utf-8 -*-
"""P1：结算报告取价失败不得冒充（离线、零网络、生产免疫）。

背景（ChatGPT 2026-09-27 复审；本机 `2b4cc17` 源码核验）
================================================================
`_start_monitoring` 持仓归零结算块：

    L8190  if batch_filled_amount > 0 and self._claim_settlement_reported(...)
                 ↑ 先 CAS 认领：写 settlement_reported=True + _persist_states 落账本
    L8206  try:  exit_price = float(ticker['last'] or ticker['close'] or 0.0)
    L8209  except: exit_price = avg_price_net       ← 取价失败回退【成本价】
    L8214  gross_pnl = (exit_price - avg_price_net) * qty   ← 恒为 0
    L8244  print + L8245 send_tg_notification       ← 发出失真数字
    L8263  converge → L8264 clear_batch_state → break

结论：
1. 报告发完批次即被 clear（L8263-8265），且 L8190 已把认领标记落账本
   → **失真报告是终局，不存在「下轮重发正确报告」的通道**。
   因此正确修法不是把认领挪到取价之后（挪了会因 clear 而永久丢报告），
   而是让取价失败时的报告**如实标注**。
2. 复审未指出的更坏分支：`or 0.0` 在 last/close 均为 0 时兜出 `0.0` 且
   **不抛异常** → 走不到 except → `gross_pnl = (0 − 均价) × 数量`
   → 报出**巨额假亏损**，比回退成本价更坏。

修正（trader L8205+）
================================================================
取价失败或非正价 → `exit_price = None`，报告改走「不可用/不可计算」四行；
**有价路径输出与修正前逐字节相同**（防 `test_v64_p3_lifecycle` 基线漂移）。

测试基建说明
================================================================
复用 `test_v64_p3_lifecycle` 的 `_make_runner/_start/_settle_tg/_bind_fn`
（真跑 `_start_monitoring` 线程 + 全端点计数桩）。

⚠️ 必须**惰性 import**：`test_v64_p3_lifecycle` 在模块顶层执行
`H.NS['time'] = _H_FAKE_TIME`，会污染 `test_v64_partial_close` 的共享
exec 命名空间。若在本文件顶层 import，污染位置会从字典序的
`test_v64_p3_*` 提前到 `test_p1_*`，从而改变其后一批测试的行为，
可能触发 `test_v64_p3_lifecycle (3,9)` 的 BASELINE-DRIFT。
pytest 先完成全部收集再运行测试 → 运行期 import 零顺序影响；
脚本模式下每个文件独立进程 → 同样安全。

运行：.venv/Scripts/python.exe test_p1_settlement_price.py
预期：GREEN、退出码 0。
"""
import hashlib
import os
import threading
import time
import traceback

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), str(detail)))
    print("  [%s] %s" % ('PASS' if passed else 'FAIL', name)
          + ((("\n        -> " + str(detail)) if detail else "")))
    return bool(passed)


# ────────────────── 生产免疫快照（模块导入时采集）──────────────────

_PROD_DIR = r'G:\my-crypto-bot'
_PROD_FILES = ['trade_state.json', 'trade_state.json.bak', 'trade_tombstones.json',
               'trade_stats.json', 'signal.json', 'signal_dedup.json', '.env']


def _prod_snapshot():
    snap = {}
    for _n in _PROD_FILES:
        _p = os.path.join(_PROD_DIR, _n)
        try:
            with open(_p, 'rb') as _f:
                _d = _f.read()
            snap[_n] = (hashlib.sha256(_d).hexdigest(), len(_d),
                        os.stat(_p).st_mtime_ns)
        except FileNotFoundError:
            snap[_n] = ('<missing>', 0, 0)
    return snap


_PROD_AT_IMPORT = _prod_snapshot()


# ────────────────── 惰性夹具导入 ──────────────────

def _P3():
    """惰性导入（原因见模块 docstring「测试基建说明」）。

    首次调用时记录共享 exec 命名空间 H.NS 的键快照，供 T98 断言
    「私有 ns 零泄漏」——这是本测试不触发 test_v64_p3_lifecycle (3,9)
    基线漂移的直接证据。
    """
    global _NS_KEYS_BASE
    import test_v64_p3_lifecycle as _m
    if _NS_KEYS_BASE is None:
        _NS_KEYS_BASE = set(_m.H.NS.keys())
    return _m


_NS_KEYS_BASE = None


def _ticker_raise(symbol, params=None, **k):
    raise Exception('simulated ticker outage: binanceusdm -1000')


def _ticker_zero(symbol, params=None, **k):
    """last/close 均为 0：修复前 `or 0.0` 兜出 0.0 且不抛异常。"""
    return {'last': 0.0, 'close': 0.0}


class _DummyKb:
    """InlineKeyboard 的占位类（仅当该分支被执行到时用）。"""

    def __init__(self, *a, **k):
        pass

    def add_row(self, *a, **k):
        return self


def _private_ns():
    """拷贝共享 NS，补齐 _start_monitoring 引用但共享 NS 未登记的模块级名字。

    为什么必须用【副本】而不是直接写 P3.H.NS：
      test_v64_partial_close.NS 是被 test_v64_p3_lifecycle 共用的 exec 命名空间，
      直接登记会改变其后续用例的行为，触发 test_v64_p3_lifecycle (3,9) 的
      BASELINE-DRIFT → 门禁失败。

    write_progress / remove_batch 用 no-op 而非真实现：
      真 write_progress 会写 .bot_health 进度文件（生产目录路径），本测试只断言
      TG 报告内容，不需要这些副作用，也不能产生任何生产写入。

    AST 实测（scan_ns_gap.py）：_start_monitoring L7698-9933 共 8 个 NS 缺失名，
    其中 AUTH_BLIND_SLEEP_SECONDS / AuthBlockedError / ccxt 已由 p3 模块级
    setdefault 补齐（本函数先惰性 import p3 故已生效），余下 5 个在此补齐。
    """
    P3 = _P3()
    ns = dict(P3.H.NS)
    ns.setdefault('current_instance_id', lambda: 'p1-test-instance')
    ns.setdefault('write_progress', lambda *a, **k: None)
    ns.setdefault('remove_batch', lambda *a, **k: None)
    ns.setdefault('InlineKeyboardButton', _DummyKb)
    ns.setdefault('InlineKeyboardMarkup', _DummyKb)
    return P3, ns


def _patch_fixture_gaps(t):
    """补齐 p3 夹具（_make_runner）未绑定的三处，消除与被测面无关的噪声。

    实测（test_p1_settlement_price 首轮运行）三类噪声：
      1) `with self._conservation_event_lock:` —— p3 未绑定 → AutoStubTrader
         __getattr__ 返回并缓存 _noop 函数 → `with <function>` 抛
         'function' object does not support the context manager protocol
         → 被 trader L4408 守恒检测兜底吞掉，每轮打印一次（实测 54 次）。
      2) `self._conservation_events.pop(...)` (trader L4347) —— 未绑定 →
         AttributeError 'function' object has no attribute 'pop'，
         同样被 L4408 吞掉（补完 1 后 2 才暴露）。
      3) `_fin_decision, _fin_snap = self._finally_cleanup_decision(...)`
         (trader L9794, finally 块) —— 未绑定 → _noop 返回 None → 解包
         TypeError → 线程带 traceback 死在 finally（stderr 噪声）。
         实测 AST 全量比对：夹具缺口共 10 个（scan_lock_gap.py）。

    边界：只补【上下文管理器】与【finally 清理授权】两类，
    不碰其它自动桩（避免改变被测面）。
    - 锁绑 threading.RLock()：无争用，语义等价于生产锁，不引入新分支。
    - _finally_cleanup_decision 返回 ('skip', None)：fail-closed 短路两段
      旧清理（两者均以 _fin_decision == 'allow' 门控，见 L9831 / L9871），
      只剩 L9868 一次 load_all_states（走桩，零 API）。本测试只断言结算
      报告内容，不测 finalizer 路由；(('skip', None)) 与 settled 批次的
      真实 fail-closed 方向一致。
    """
    for _n in ('_conservation_event_lock', '_api_lock', '_api_semaphore',
               '_gate_alert_lock', '_limit_close_monitor_lock',
               '_resize_inflight_lock', '_auth_recovery_lock'):
        if _n not in t.__dict__:
            t.__dict__[_n] = threading.RLock()
    # 守恒观测器的事件表（dict）。不绑则 L4347 `self._conservation_events.pop`
    # 打在自动桩函数上 → AttributeError 'function' object has no attribute 'pop'
    # → 被 L4408 兜底吞掉，每轮打印一次（实测 54 次）。
    # 绑空 dict 后本夹具只有单批次 → L4346 `len(same_side) < 2` 立即 return，
    # 不产生任何告警，零副作用。
    if '_conservation_events' not in t.__dict__:
        t.__dict__['_conservation_events'] = {}
    if '_finally_cleanup_decision' not in t.__dict__:
        t.__dict__['_finally_cleanup_decision'] = \
            lambda symbol, batch_id: ('skip', None)


def _run(ticker_factory, timeout=0.6):
    """起真监控线程跑到结算分支，返回报告消息与状态。"""
    P3, ns = _private_ns()
    sym = P3.SYM
    t = P3._make_runner({sym: {'batch_A': P3._disk_batch()}},
                        position=0.0, converge_result=None)
    if ticker_factory is not None:
        t.exchange.fetch_ticker = ticker_factory
    # 用私有 ns 提取，而非 P3.H.ex_t（后者绑定共享 NS）
    _fn = P3.H._extract(P3.H.TREE, P3.H.SRC, '_start_monitoring', ns)
    P3._bind_fn(t, _fn)
    _patch_fixture_gaps(t)
    th = P3._start(t)
    try:
        time.sleep(timeout)
        msgs = P3._settle_tg(t)
        st = (t._states.get(sym) or {}).get('batch_A') or {}
        return {
            'msgs': list(msgs),
            'reported': st.get('settlement_reported'),
            'converge': int(t.converge_calls['converge']),
            'clear': int(t.clear_calls['clear']),
        }
    finally:
        t.converge_result = {'ok': True}   # 人工收敛成功 → clear → 线程退出
        th.join(timeout=5)
        if th.is_alive():
            P3._force_stop(th)


# ────────────────── T1：取价抛异常 ──────────────────

def t1_price_fetch_failure_report_is_honest():
    r = _run(_ticker_raise)
    msgs = r['msgs']
    ok = check('T1 取价失败仍恰好发出 1 条结算报告（at-most-once 未破）',
               len(msgs) == 1, 'n=%d converge=%d' % (len(msgs), r['converge']))
    if not msgs:
        check('T1 报告内容如实标注不可用', False, '无报告')
        return ok
    msg = msgs[0]
    check('T1 平仓价格标注为不可用', '⚠️ 不可用' in msg,
          msg[msg.find('💵'):msg.find('💵') + 60] if '💵' in msg else msg[:160])
    check('T1 名义盈亏标注为不可计算',
          '📊 **名义盈亏**：⚠️ 不可计算（缺平仓价）' in msg, '')
    check('T1 不得出现失真的「名义盈亏 +0.00」',
          '📊 **名义盈亏**：`+0.00`' not in msg, '修复前必现（成本价冒充 → 毛盈亏恒 0）')
    check('T1 明示未以成本价冒充', '未以成本价冒充' in msg, '')
    return ok


# ────────────────── T2：last/close 均为 0 ──────────────────

def t2_zero_price_not_reported_as_huge_loss():
    r = _run(_ticker_zero)
    msgs = r['msgs']
    ok = check('T2 零价仍恰好发出 1 条结算报告', len(msgs) == 1,
               'n=%d' % len(msgs))
    if not msgs:
        check('T2 不得报出巨额假亏损', False, '无报告')
        return ok
    msg = msgs[0]
    check('T2 不得把平仓价显示成 0.00',
          '💵 **平仓价格**：`0.00`' not in msg,
          '修复前 → gross_pnl=(0-均价)*qty = 巨额假亏损')
    check('T2 报告改为不可用/不可计算',
          '⚠️ 不可用' in msg and '⚠️ **最终净盈亏**：不可计算' in msg, '')
    return ok


# ────────────────── T3：有价路径回归护栏 ──────────────────

def t3_normal_price_path_unchanged():
    """有价时输出必须与修复前一致——否则 test_v64_p3_lifecycle 基线会漂移。"""
    r = _run(None)   # CountingExchange 默认 {'last': 76500.0}
    msgs = r['msgs']
    ok = check('T3 正常价仍恰好发出 1 条结算报告', len(msgs) == 1,
               'n=%d' % len(msgs))
    if not msgs:
        check('T3 正常价报告格式未变', False, '无报告')
        return ok
    msg = msgs[0]
    check('T3 平仓价格仍为真实市价 76500.00',
          '💵 **平仓价格**：`76500.00`' in msg, '')
    check('T3 名义盈亏仍是带符号数字（走原口径）',
          '📊 **名义盈亏**：`+' in msg or '📊 **名义盈亏**：`-' in msg, '')
    check('T3 有价路径不得出现不可用告警', '⚠️ 不可用' not in msg, '')
    check('T3 结算数量行不变（r7 同款断言）',
          '🔢 **平仓数量**：' in msg, '')
    return ok


# ────────────────── T4：认领落账本 + 重试期间不重复发 ──────────────────

def t4_claim_ratchets_ledger_and_no_duplicate():
    """口径更正的证据：认领确实落账本（ChatGPT P1 所指），
    但取价失败 + converge 反复重试期间报告仍只发 1 次。"""
    r = _run(_ticker_raise, timeout=0.9)
    a = check('T4 settlement_reported=True 确实落账本（口径更正）',
              r['reported'] is True, 'reported=%r' % (r['reported'],))
    b = check('T4 收敛持续重试期间结算报告仍恰好 1 次',
              len(r['msgs']) == 1 and r['converge'] >= 2,
              'n=%d converge=%d' % (len(r['msgs']), r['converge']))
    return a and b


# ────────────────── T98：共享 exec 命名空间零泄漏 ──────────────────
def t98_shared_exec_namespace_untouched():
    """本测试用 dict(H.NS) 私有副本注入缺失名，绝不写共享 H.NS。

    若泄漏：test_v64_p3_lifecycle 的用例行为会被改变，其 (3,9) 登记
    基线随之漂移 → 门禁 BASELINE-DRIFT ≠ 0 失败。本断言即该风险的守卫。

    基线扣除说明：p3 自己的 `_make_runner` 会经 `H.ex_t(name)`
    （= _extract(..., NS)）把若干函数提取进**共享** NS，实测足迹
    {'_claim_settlement_reported', '_get_current_position_amt',
     '_merge_batch_state', '_monitor_lifecycle_check', 'save_batch_state'}
    —— 那是 p3 的固有行为，不是本测试泄漏。这里先量出该足迹，再断言
    跑完 _run（含私有 ns 提取）后不出现**任何额外**新键。
    """
    P3 = _P3()
    base = set(_NS_KEYS_BASE or set())
    # 量 _make_runner 自有足迹（T1-T4 已触发过，此处幂等）
    P3._make_runner({P3.SYM: {'batch_A': P3._disk_batch()}},
                    position=0.0, converge_result=None)
    after_maker = set(P3.H.NS.keys())
    maker_footprint = after_maker - base
    # 再完整跑一次 _run（走私有 ns 提取路径）
    _run(_ticker_raise, timeout=0.3)
    final = set(P3.H.NS.keys())
    leaked = sorted(final - base - maker_footprint)
    check('T98 已量出 _make_runner 自有提取足迹（p3 固有行为，非本测试泄漏）',
          len(maker_footprint) == 5, 'footprint=%s' % sorted(maker_footprint))
    return check('T98 私有 ns 未向共享 H.NS 泄漏任何新键（防 (3,9) 基线漂移）',
                 not leaked, 'leaked=%s' % leaked)


# ────────────────── T99 生产免疫 ──────────────────

def t99_production_files_untouched():
    after = _prod_snapshot()
    diff = [k for k in _PROD_AT_IMPORT if after.get(k) != _PROD_AT_IMPORT[k]]
    ok = check('T99 生产账本/墓碑/统计/信号/.env 零变化（内容+mtime_ns）',
               not diff, 'diff=%s' % diff)
    try:
        import trader_260725 as _tr
        in_prod = os.path.abspath(str(getattr(_tr, 'STATE_FILE', ''))) \
            .startswith(_PROD_DIR + os.sep)
        check('T99 未把生产目录写进被测模块 STATE_FILE', not in_prod,
              'STATE_FILE=%s' % getattr(_tr, 'STATE_FILE', '<未设置>'))
    except Exception as e:
        check('T99 被测模块可读', False, repr(e))
    return ok


def main():
    print('=' * 68)
    print('P1 结算报告取价失败不得冒充（离线负测）')
    print('=' * 68)
    for fn in (t1_price_fetch_failure_report_is_honest,
               t2_zero_price_not_reported_as_huge_loss,
               t3_normal_price_path_unchanged,
               t4_claim_ratchets_ledger_and_no_duplicate,
               t98_shared_exec_namespace_untouched,
               t99_production_files_untouched):
        print('\n---- %s ----' % fn.__name__)
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(fn.__name__ + ' 异常', False, '见 stderr')

    npass = sum(1 for _, p, _ in RESULTS if p)
    print('\n' + '=' * 68)
    print('GREEN: %d/%d' % (npass, len(RESULTS)))
    print('=' * 68)
    for n, p, d in RESULTS:
        if not p:
            print('  FAIL: %s  %s' % (n, d))
    return 0 if npass == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
