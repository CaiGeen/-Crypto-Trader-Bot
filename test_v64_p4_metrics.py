# -*- coding: utf-8 -*-
"""v6.4-P4（Phase 2 限流观测层）RED-first 测试。

冻结规格（Phase 1 r2 报告 + ChatGPT 批准）：
- _safe_api_call 单点极薄计数：endpoint → calls/min + 真实响应头优先
  （X-MBX-USED-WEIGHT-1M）+ 静态 weight 估值辅助
- 60s 节流周期汇总（日志行）
- 429/418 事发时输出前 60s 本程序调用面快照（归因证据链）
- 零交易逻辑改动：观测故障绝不外泄

R1 计数 / R2 响应头捕获+峰值 / R3 事发快照内容 / R4 汇总 60s 节流 /
R5 线程安全 / R6 成功路径接线 / R7 429 路径接线 / R8 观测故障零外泄
"""
import io
import contextlib
import threading
import time
import types
import unittest.mock as mock

import trader_260725

SYM = 'BTCUSDT'


# ────────────────── ApiMetrics 单元 ──────────────────

def r1_endpoint_counting():
    m = trader_260725.ApiMetrics()
    f = lambda: None
    f.__name__ = 'fetch_open_orders'
    g = lambda: None
    g.__name__ = 'fetch_positions'
    for _ in range(3):
        m.record(f, ok=True)
    m.record(g, ok=True)
    m.record(g, ok=True)
    snap = m.snapshot_last()
    assert snap.get('fetch_open_orders') == 3, snap
    assert snap.get('fetch_positions') == 2, snap


def r2_used_weight_header_capture_and_peak():
    m = trader_260725.ApiMetrics()
    f = lambda: None
    f.__name__ = 'fetch_positions'
    m.record(f, {'X-MBX-USED-WEIGHT-1M': '120'}, ok=True)
    m.record(f, {'x-mbx-used-weight-1m': '300'}, ok=True)  # 大小写不敏感 + 峰值更新
    assert m._weight_latest.get('fetch_positions') == 300.0, m._weight_latest
    assert m._weight_peak.get('fetch_positions') == 300.0, m._weight_peak
    # 事发行里必须带真实头证据
    line = m.format_incident(f, {'X-MBX-USED-WEIGHT-1M': '300'},
                             Exception('429 too many requests'))
    assert 'used-weight-1m=300' in line and '历史峰值=300' in line, line


def r3_incident_snapshot_content():
    m = trader_260725.ApiMetrics()
    f1 = lambda: None
    f1.__name__ = 'fetch_open_orders'
    f2 = lambda: None
    f2.__name__ = 'fetch_positions'
    for _ in range(4):
        m.record(f1, ok=True)
    m.record(f2, ok=True)
    inc = m.format_incident(f1, {}, Exception('418 I am a teapot banned'))
    assert '事发快照' in inc, inc
    assert 'fetch_open_orders×4' in inc, inc
    assert 'fetch_positions×1' in inc, inc
    assert '418' in inc, inc  # 原始错误片段留证


def r4_summary_throttled_60s():
    clock = {'t': 1000.0}
    m = trader_260725.ApiMetrics(time_fn=lambda: clock['t'])
    f = lambda: None
    f.__name__ = 'fetch_ticker'
    line1 = m.record(f, ok=True)
    assert line1 and '限流观测' in line1, line1
    clock['t'] += 10
    assert m.record(f, ok=True) is None, '窗口内不得重复汇总'
    clock['t'] += 55  # 距上次汇总 65s
    line2 = m.record(f, ok=True)
    assert line2 and 'fetch_ticker' in line2, line2


def r5_thread_safety():
    m = trader_260725.ApiMetrics()
    fs = []
    for i in range(4):
        f = lambda: None
        f.__name__ = f'ep{i}'
        fs.append(f)

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
    ex.last_response_headers = {'X-MBX-USED-WEIGHT-1M': '42'}

    def fetch_positions(*a, **k):  # 具名函数：_endpoint_of 取 __name__（对齐 ccxt 绑定方法）
        return [{'symbol': SYM}]
    ex.fetch_positions = fetch_positions
    t = _wire_trader(ex)
    m = trader_260725.ApiMetrics()
    with _MetricsKeeper(m):
        r = t._safe_api_call(ex.fetch_positions, [SYM])
    assert r == [{'symbol': SYM}], '返回值必须原样透传'
    assert m.snapshot_last().get('fetch_positions') == 1, m.snapshot_last()
    assert m._weight_latest.get('fetch_positions') == 42.0, '必须捕获真实响应头'


def r7_429_path_emits_incident_snapshot():
    ex = types.SimpleNamespace()
    ex.last_response_headers = {'Retry-After': '5', 'X-MBX-USED-WEIGHT-1M': '900'}

    def fetch_open_orders(*a, **k):
        raise Exception('binanceusdm 429 rate limit exceeded; please slow down')
    ex.fetch_open_orders = fetch_open_orders
    t = _wire_trader(ex)
    m = trader_260725.ApiMetrics()

    def _seed_fetch_positions():
        pass
    _seed_fetch_positions.__name__ = 'fetch_positions'
    for _ in range(5):
        m.record(_seed_fetch_positions, ok=True)
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
    assert '限流观测·事发快照' in out, f'429 路径必须输出事发快照:\n{out[-600:]}'
    assert 'fetch_positions×5' in out, f'快照必须含 429 前调用面:\n{out[-600:]}'
    assert 'used-weight-1m=900' in out, '快照必须带本次真实头证据'
    assert m.snapshot_last().get('fetch_open_orders') == 1, '失败调用本身也要入账'


def r8_metrics_failure_never_breaks_trading():
    """契约：ApiMetrics 内部任何故障静默（record→None / incident→采集失败串 /
    snapshot→{}），绝不外泄到交易路径。用会抛错的时钟注入故障。"""
    def bad_clock():
        raise RuntimeError('injected clock failure')
    m = trader_260725.ApiMetrics(time_fn=bad_clock)
    f = lambda: None
    f.__name__ = 'fetch_ticker'
    assert m.record(f, {'X-MBX-USED-WEIGHT-1M': '1'}, ok=True) is None
    assert m.snapshot_last() == {}
    inc = m.format_incident(f, {}, Exception('429'))
    assert isinstance(inc, str) and '事发快照' in inc, inc  # 优雅降级：空快照仍出事发行

    # 接线层：故障观测实例挂载后交易调用零影响
    ex = types.SimpleNamespace()
    ex.last_response_headers = {}

    def fetch_ticker(*a, **k):
        return {'last': 76000.0}
    ex.fetch_ticker = fetch_ticker
    t = _wire_trader(ex)
    with _MetricsKeeper(m):
        r = t._safe_api_call(ex.fetch_ticker, SYM)
    assert r == {'last': 76000.0}, '观测层故障绝不能影响交易调用'


TESTS = [r1_endpoint_counting,
         r2_used_weight_header_capture_and_peak,
         r3_incident_snapshot_content,
         r4_summary_throttled_60s,
         r5_thread_safety,
         r6_success_path_wiring,
         r7_429_path_emits_incident_snapshot,
         r8_metrics_failure_never_breaks_trading]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'❌ {fn.__name__}: {str(e)[:300]}')
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {str(e)[:300]}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
