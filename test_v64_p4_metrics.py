# -*- coding: utf-8 -*-
"""v6.4-P4b（Phase 2 观测层数据模型修正）RED-first 测试。

冻结规格（Phase 1 r2 + ChatGPT P4 复审裁定）：
- 事件模型：(ts, endpoint, status, used_weight_1m, order_count_10s, order_count_1m)
- USED-WEIGHT / ORDER-COUNT-10S / ORDER-COUNT-1M 三指标：latest=时间序最后有效值，
  peak=60s 窗口内最大值（绝不能用进程生命周期 max 冒充「最新」）
- 全部真实到达 Binance 的响应入账（含 503/-1021/网络错误等失败；-1021 重同步直连也要入账）
- weight 估值修正：cancel_order IP weight=1（官方实锤），create_order IP=0（order count 由真实 header 记录）

R1 计数 / R2 三指标 header 捕获 / R3 事发快照内容 / R4 汇总 60s 节流 /
R5 线程安全 / R6 成功路径接线 / R7 429 路径接线 / R8 观测故障零外泄 /
R9 ORDER-COUNT 生命周期 / R10 窗口语义（900 过期→50 生效）/
R11 失败响应入账（503 + -1021 重同步）/ R12 weight 估值表修正
"""
import io
import contextlib
import threading
import time
import types

import trader_260725

SYM = 'BTCUSDT'


def _named(name, fn=None):
    def f(*a, **k):
        return fn(*a, **k) if fn else None
    f.__name__ = name
    return f


# ────────────────── ApiMetrics 单元 ──────────────────

def r1_endpoint_counting():
    m = trader_260725.ApiMetrics()
    f = _named('fetch_open_orders')
    g = _named('fetch_positions')
    for _ in range(3):
        m.record(f, ok=True)
    m.record(g, ok=True)
    m.record(g, ok=True)
    snap = m.snapshot_last()
    assert snap.get('fetch_open_orders') == 3, snap
    assert snap.get('fetch_positions') == 2, snap


def r2_header_capture_all_three():
    m = trader_260725.ApiMetrics()
    m.record(_named('create_order'),
             {'X-MBX-USED-WEIGHT-1M': '42', 'X-MBX-ORDER-COUNT-10S': '3',
              'X-MBX-ORDER-COUNT-1M': '7'}, ok=True)
    line = m.format_summary()
    assert 'USED-WEIGHT 最新=42' in line, line
    assert 'ORDER-10S 最新=3' in line, line
    assert 'ORDER-1M 最新=7' in line, line
    assert '峰值60s=42' in line and '峰值60s=3' in line and '峰值60s=7' in line, line


def r3_incident_snapshot_content():
    m = trader_260725.ApiMetrics()
    for _ in range(4):
        m.record(_named('fetch_open_orders'), ok=True)
    m.record(_named('fetch_positions'), ok=True)
    inc = m.format_incident(_named('fetch_open_orders'), {},
                            Exception('418 I am a teapot banned'))
    assert '事发快照' in inc, inc
    assert 'fetch_open_orders×4' in inc, inc
    assert 'fetch_positions×1' in inc, inc
    assert '418' in inc, inc  # 原始错误片段留证
    assert 'ORDER-10S=N/A' in inc and 'ORDER-1M=N/A' in inc, inc  # 错误响应缺 header → N/A
    assert 'USED-WEIGHT=N/A' in inc, inc


def r4_summary_throttled_60s():
    clock = {'t': 1000.0}
    m = trader_260725.ApiMetrics(time_fn=lambda: clock['t'])
    f = _named('fetch_ticker')
    line1 = m.record(f, ok=True)
    assert line1 and '限流观测' in line1, line1
    clock['t'] += 10
    assert m.record(f, ok=True) is None, '窗口内不得重复汇总'
    clock['t'] += 55  # 距上次汇总 65s
    line2 = m.record(f, ok=True)
    assert line2 and 'fetch_ticker' in line2, line2


def r5_thread_safety():
    m = trader_260725.ApiMetrics()
    fs = [_named(f'ep{i}') for i in range(4)]

    def worker(idx):
        for _ in range(50):
            m.record(fs[idx % 4], ok=True)
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    total = sum(m.snapshot_last().values())
    assert total == 400, total


# ────────────────── _safe_api_call 接线（真函数 + 轻量 stub self） ──────────────────

def _wire_trader(exchange):
    t = types.SimpleNamespace()
    t.exchange = exchange
    t._load_auth_state = lambda: {'locked': False, 'state': 'OK', 'reason': ''}
    t._wait_for_api_cooldown = lambda: None
    t._api_semaphore = threading.BoundedSemaphore(5)
    t._api_lock = threading.Lock()
    t._min_api_interval = 0
    t._last_api_call_time = 0.0
    t.last_time_sync = 0.0
    t.api_cooldown_lock = threading.Lock()
    t.api_cooldown_until = 0.0
    t.alerts = []
    t._alert_cooldown_start = lambda *a, **k: t.alerts.append(a)
    # _safe_api_call 内经 self 访问的静态方法（生产桩契约）
    t._format_429_diagnostics = trader_260725.CryptoTrader._format_429_diagnostics
    t._effective_429_cooldown = trader_260725.CryptoTrader._effective_429_cooldown
    t._safe_api_call = types.MethodType(trader_260725.CryptoTrader._safe_api_call, t)
    return t


class _MetricsKeeper:
    """测试期替换模块级单例，退出恢复。"""

    def __init__(self, replacement):
        self.replacement = replacement
        self._orig = None

    def __enter__(self):
        self._orig = trader_260725._API_METRICS
        trader_260725._API_METRICS = self.replacement
        return self.replacement

    def __exit__(self, *exc):
        trader_260725._API_METRICS = self._orig
        return False


def r6_success_path_wiring():
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_positions(*a, **k):
        # ccxt 契约：响应 header 在请求调用内部写入（P4d 清栏后桩必须建模此行为）
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '42'}
        return [{'symbol': SYM}]
    ex.fetch_positions = fetch_positions
    t = _wire_trader(ex)
    m = trader_260725.ApiMetrics()
    with _MetricsKeeper(m):
        r = t._safe_api_call(ex.fetch_positions, [SYM])
    assert r == [{'symbol': SYM}], '返回值必须原样透传'
    assert m.snapshot_last().get('fetch_positions') == 1, m.snapshot_last()
    line = m.format_summary()
    assert 'USED-WEIGHT 最新=42' in line, line


def r7_429_path_emits_incident_snapshot():
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_open_orders(*a, **k):
        ex.last_response_headers = {'Retry-After': '5', 'X-MBX-USED-WEIGHT-1M': '900'}
        raise Exception('binanceusdm 429 rate limit exceeded; please slow down')
    ex.fetch_open_orders = fetch_open_orders
    t = _wire_trader(ex)
    m = trader_260725.ApiMetrics()
    for _ in range(5):
        m.record(_named('fetch_positions'), ok=True)
    buf = io.StringIO()
    raised = False
    with _MetricsKeeper(m):
        with contextlib.redirect_stdout(buf):
            try:
                t._safe_api_call(ex.fetch_open_orders, SYM, retries=1)
            except Exception:
                raised = True
    out = buf.getvalue()
    assert raised, '429 必须照常抛出（观测层不得吞错）'
    assert '限流观测·事发快照' in out, f'429 路径必须输出事发快照:\n{out[-800:]}'
    assert 'fetch_positions×5' in out, f'快照必须含 429 前调用面:\n{out[-800:]}'
    assert '本次错误 header: USED-WEIGHT=900' in out, '快照必须带本次真实头证据'
    assert m.snapshot_last().get('fetch_open_orders') == 1, '失败调用本身也要入账'


def r8_metrics_failure_never_breaks_trading():
    """契约：ApiMetrics 内部任何故障静默（record→None / incident→优雅降级 /
    snapshot→{}），绝不外泄到交易路径。用会抛错的时钟注入故障。"""
    def bad_clock():
        raise RuntimeError('injected clock failure')
    m = trader_260725.ApiMetrics(time_fn=bad_clock)
    f = _named('fetch_ticker')
    assert m.record(f, {'X-MBX-USED-WEIGHT-1M': '1'}, ok=True) is None
    assert m.snapshot_last() == {}
    inc = m.format_incident(f, {}, Exception('429'))
    assert isinstance(inc, str) and '事发快照' in inc, inc  # 优雅降级

    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_ticker(*a, **k):
        return {'last': 76000.0}
    ex.fetch_ticker = fetch_ticker
    t = _wire_trader(ex)
    with _MetricsKeeper(m):
        r = t._safe_api_call(ex.fetch_ticker, SYM)
    assert r == {'last': 76000.0}, '观测层故障绝不能影响交易调用'


def r9_order_count_lifecycle():
    """R9：成功 create_order 的 ORDER-COUNT-10S/1M 必须保存并出现在 incident；
    失败订单响应缺 header → 本次错误 header 显示 N/A（官方：失败订单不保证带计数）。"""
    m = trader_260725.ApiMetrics()
    m.record(_named('create_order'),
             {'X-MBX-USED-WEIGHT-1M': '0', 'X-MBX-ORDER-COUNT-10S': '4',
              'X-MBX-ORDER-COUNT-1M': '9'}, ok=True)
    m.record(_named('create_order'),
             {'X-MBX-USED-WEIGHT-1M': '0', 'X-MBX-ORDER-COUNT-10S': '5',
              'X-MBX-ORDER-COUNT-1M': '10'}, ok=True)
    inc = m.format_incident(_named('create_order'),
                            {'X-MBX-USED-WEIGHT-1M': '0'},  # 失败响应无 order-count
                            Exception('429 order rate limit'))
    assert 'ORDER-10S 窗口最新=5 峰值60s=5' in inc, inc
    assert 'ORDER-1M 窗口最新=10 峰值60s=10' in inc, inc
    assert 'USED-WEIGHT 窗口最新=0' in inc, inc
    assert 'ORDER-10S=N/A' in inc and 'ORDER-1M=N/A' in inc, inc


def r10_window_semantics():
    """R10：时间序 latest 与 60s 窗口 peak——900 过期后绝不能冒充最新/窗口峰值。"""
    clock = {'t': 1.0}
    m = trader_260725.ApiMetrics(time_fn=lambda: clock['t'])
    m.record(_named('fetch_positions'), {'X-MBX-USED-WEIGHT-1M': '900'}, ok=True)
    clock['t'] = 70.0  # 900 已出 60s 窗口
    line = m.record(_named('fetch_ticker'), {'X-MBX-USED-WEIGHT-1M': '50'}, ok=True)
    assert line is not None
    assert 'USED-WEIGHT 最新=50' in line, line
    assert '峰值60s=50' in line, line
    assert '900' not in line, f'过期值不得出现在窗口统计: {line}'


def r11_failed_response_recorded():
    """R11：503 等带 header 的失败响应也必须留下调用+weight 证据；
    -1021 重同步的直连 load_time_difference 也必须入账。"""
    # 场景 A：503 带头失败（ccxt 契约：header 在调用内部写入）
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_positions(*a, **k):
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '500'}
        raise Exception('503 Service Unavailable')
    ex.fetch_positions = fetch_positions
    t = _wire_trader(ex)
    m = trader_260725.ApiMetrics()
    with _MetricsKeeper(m):
        raised = False
        try:
            t._safe_api_call(ex.fetch_positions, [SYM], retries=1)
        except Exception:
            raised = True
    assert raised
    assert m.snapshot_last().get('fetch_positions') == 1, \
        f'失败响应必须入账: {m.snapshot_last()}'
    line = m.format_summary()
    assert 'USED-WEIGHT 最新=500' in line, line

    # 场景 B：-1021 → 重同步直连调用入账
    ex2 = types.SimpleNamespace()
    ex2.last_response_headers = {}

    def fetch_balance(*a, **k):
        raise Exception('binance -1021 Timestamp for this request is outside of the recvWindow.')

    def load_time_difference():
        return True
    ex2.fetch_balance = fetch_balance
    ex2.load_time_difference = load_time_difference
    t2 = _wire_trader(ex2)
    m2 = trader_260725.ApiMetrics()
    with _MetricsKeeper(m2):
        raised2 = False
        try:
            t2._safe_api_call(ex2.fetch_balance, SYM, retries=1)
        except Exception:
            raised2 = True
    assert raised2
    snap = m2.snapshot_last()
    assert snap.get('load_time_difference') == 1, \
        f'-1021 重同步直连必须入账: {snap}'
    assert snap.get('fetch_balance') == 1, snap


def r12_weight_map():
    """R12：cancel_order IP weight=1（官方实锤）；create_order IP=0
    （order 计数由真实 header 记录，不靠估值）。"""
    w = trader_260725.ApiMetrics.WEIGHT_ESTIMATES
    assert w.get('cancel_order') == 1, w
    assert w.get('create_order') == 0, w
    m = trader_260725.ApiMetrics()
    m.record(_named('cancel_order'), ok=True)
    m.record(_named('cancel_order'), ok=True)
    line = m.format_summary()
    assert '估算weight≈2' in line, f'cancel_order×2 必须估 2 weight: {line}'


class _RaceSemaphore:
    """A 的 with 退出时（semaphore 释放前一刻）同步执行 on_release——
    确定性模拟「A 请求已结束、B 覆盖共享 header 栏、A 才去记录」的并发竞态。"""

    def __init__(self, on_release):
        self._inner = threading.BoundedSemaphore(1)
        self._on_release = on_release

    def __enter__(self):
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._on_release()
        self._inner.release()
        return False


def _race_setup(m):
    """A=fetch_positions 失败(900)；on_release 模拟 B=fetch_ticker 成功(50) 覆盖共享栏。"""
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_positions(*a, **k):
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '900'}
        raise Exception('binanceusdm 429 rate limit exceeded')
    ex.fetch_positions = fetch_positions
    t = _wire_trader(ex)

    def on_release():
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '50'}
        m.record(_named('fetch_ticker'), {'X-MBX-USED-WEIGHT-1M': '50'}, ok=True)
    t._api_semaphore = _RaceSemaphore(on_release)
    return t


def r13_concurrent_header_ownership():
    """R13：失败事件的 header 必须属于本次尝试——A 失败(900)不能被 B 成功(50)覆盖；
    429 事发行「本次错误 header」也必须=900。"""
    m = trader_260725.ApiMetrics()
    t = _race_setup(m)
    buf = io.StringIO()
    raised = False
    with _MetricsKeeper(m):
        with contextlib.redirect_stdout(buf):
            try:
                t._safe_api_call(t.exchange.fetch_positions, SYM, retries=1)
            except Exception:
                raised = True
    assert raised
    fails = [e for e in m._events if e[1] == 'fetch_positions' and e[2] == 'fail']
    assert fails and fails[-1][3] == 900.0, \
        f'失败事件 header 归属错误（被并发成功覆盖）: {fails}'
    oks = [e for e in m._events if e[1] == 'fetch_ticker' and e[2] == 'ok']
    assert oks and oks[-1][3] == 50.0, oks
    out = buf.getvalue()
    assert '本次错误 header: USED-WEIGHT=900' in out, \
        f'429 事发行必须消费 frozen headers:\n{out[-600:]}'


def r14_event_ordering():
    """R14：响应完成顺序 = 事件记录顺序——A 响应先完成、B 后完成（A 线程
    在记录前被调度暂停），_events 仍必须 [A, B]，latest 语义不被线程调度破坏。"""
    m = trader_260725.ApiMetrics()
    t = _race_setup(m)
    with _MetricsKeeper(m):
        try:
            t._safe_api_call(t.exchange.fetch_positions, SYM, retries=1)
        except Exception:
            pass
    eps = [(e[1], e[2]) for e in m._events]
    assert eps == [('fetch_positions', 'fail'), ('fetch_ticker', 'ok')], \
        f'事件顺序必须与响应完成顺序一致: {eps}'


def r15_sync_failure_ownership():
    """R15：-1021 重同步 load_time_difference 自身失败时，其 header 快照+record
    必须同样在 semaphore 临界区内完成——A 同步产生 900 后失败 + sibling 覆盖 50
    → sync fail event 必须=900，绝不能是 50。"""
    m = trader_260725.ApiMetrics()
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_balance(*a, **k):
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '100'}
        raise Exception('binance -1021 Timestamp for this request is outside of the recvWindow.')

    def load_time_difference():
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '900'}
        raise Exception('sync network error')
    ex.fetch_balance = fetch_balance
    ex.load_time_difference = load_time_difference
    t = _wire_trader(ex)

    def on_release():
        ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '50'}
        m.record(_named('fetch_ticker'), {'X-MBX-USED-WEIGHT-1M': '50'}, ok=True)
    t._api_semaphore = _RaceSemaphore(on_release)
    with _MetricsKeeper(m):
        raised = False
        try:
            t._safe_api_call(ex.fetch_balance, SYM, retries=1)
        except Exception:
            raised = True
    assert raised
    sync_fails = [e for e in m._events if e[1] == 'load_time_difference' and e[2] == 'fail']
    assert sync_fails and sync_fails[-1][3] == 900.0, \
        f'重同步失败 header 归属错误（被 sibling 覆盖）: {sync_fails}'


def r16_stale_header_rejection():
    """R16：线程安全读取共享 header ≠ 证明 header 属于本次 attempt——
    上一笔遗留 777 + 本地 pre-network 抛错（未产生新响应）→ 本次 event
    header 必须 None/N/A，绝不能把 777 重新归属为新的窗口事件。"""
    m = trader_260725.ApiMetrics()
    ex = types.SimpleNamespace()
    ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '777'}  # 上一笔遗留

    def fetch_balance(*a, **k):
        raise ValueError('local pre-network error')  # 本地抛错，不碰 Binance
    ex.fetch_balance = fetch_balance
    t = _wire_trader(ex)
    with _MetricsKeeper(m):
        raised = False
        try:
            t._safe_api_call(ex.fetch_balance, SYM, retries=1)
        except Exception:
            raised = True
    assert raised
    fails = [e for e in m._events if e[1] == 'fetch_balance' and e[2] == 'fail']
    assert fails and fails[-1][3] is None, \
        f'遗留 header 777 不得归属本次 attempt: {fails}'


TESTS = [r1_endpoint_counting,
         r2_header_capture_all_three,
         r3_incident_snapshot_content,
         r4_summary_throttled_60s,
         r5_thread_safety,
         r6_success_path_wiring,
         r7_429_path_emits_incident_snapshot,
         r8_metrics_failure_never_breaks_trading,
         r9_order_count_lifecycle,
         r10_window_semantics,
         r11_failed_response_recorded,
         r12_weight_map,
         r13_concurrent_header_ownership,
         r14_event_ordering,
         r15_sync_failure_ownership,
         r16_stale_header_rejection]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {str(e)[:400]}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {str(e)[:400]}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
