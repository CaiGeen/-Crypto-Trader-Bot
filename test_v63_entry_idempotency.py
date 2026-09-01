#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v6.3 开仓幂等（D-005）RED-first 测试——6 个决定性用例，预算锁死不扩。

被测：trader_260725.py 的 _compute_signal_fingerprint（新增）+
      _check_existing_conflicts（加 active 同指纹拒绝，entry_orders=[] 不豁免）。
纯函数离线：ast 提取方法源码 + StubTrader 绑定，不实例化真实交易所连接。
"""
import ast
import textwrap
import traceback

SRC = open('trader_260725.py', encoding='utf-8').read()
TREE = ast.parse(SRC)


def extract_fn(name):
    """从 trader 源码提取单个方法（文本段 exec），返回未绑定函数。"""
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {}
            exec(textwrap.dedent(ast.get_source_segment(SRC, node)), ns)
            return ns[name]
    return None


class StubExchange:
    """确定性规范化桩：price 1 位小数 / amount 3 位小数（与真实调用语义一致性无关，
    只要求同值同输出）。fetch_open_orders 必须存在——放行路径的实参求值会访问它。"""

    def price_to_precision(self, symbol, v):
        return str(round(float(v), 1))

    def amount_to_precision(self, symbol, v):
        return str(round(float(v), 3))

    def fetch_open_orders(self, *a, **k):
        return []


class StubTrader:
    pass


class Sig:
    """最小 signal 桩——字段与 parser 产物对齐。"""

    def __init__(self, entries, stops, tp, symbol='BTC/USDT:USDT', side='buy'):
        self.symbol = symbol
        self.side = side
        self.entries = entries
        self.stop_loss_steps = stops
        self.take_profit = tp


def make_trader(api_calls=None):
    t = StubTrader()
    t.exchange = StubExchange()
    t._api_calls = api_calls if api_calls is not None else []

    def _safe_api_call(fn, *args, **kwargs):
        t._api_calls.append(getattr(fn, '__name__', str(fn)))
        return []

    def _notify(*a, **k):
        pass

    t._safe_api_call = _safe_api_call
    t.send_tg_notification = _notify
    return t


SYM = 'BTC/USDT:USDT'
SIG_A = Sig(entries=[(77820.0, 0.001), (80000.0, 0.001)],
            stops=[75001.0, 75002.0], tp=80000.0)


def _states(*batches):
    return {SYM: {b['batch_id']: b for b in batches}}


def _active_batch(bid, fp=None, entry_orders=None, pending=False):
    b = {'batch_id': bid, 'is_active': True, 'close_phase': 0,
         'entry_orders': entry_orders if entry_orders is not None else ['E1'],
         'last_filled_count': 0}
    if fp is not None:
        b['signal_fingerprint'] = fp
    if pending:
        b['pending_close'] = False
        b['protection_registry'] = {'x': {'role': 'ENTRY', 'state': 'PENDING_CREATE'}}
    return b


# ── 6 个决定性用例 ──────────────────────────────────────────────

def t01_first_signal_pass():
    """首次信号（无任何批次）→ 放行，且流经完整扫描（API 扫描被触达）。"""
    t = make_trader()
    fp = FP(t, SIG_A)
    states = _states()
    ret = CHECK(t, SYM, 'batch_new_1', states, fp)
    assert ret is False, f'首次信号应放行: {ret!r}'
    assert len(t._api_calls) >= 2, '放行路径必须走完双通道 API 扫描'


def t02_active_same_fp_reject():
    """active 同指纹批次 → 拒绝，且零 API 调用（本地判据先于交易所扫描）。"""
    t = make_trader()
    fp = FP(t, SIG_A)
    states = _states(_active_batch('batch_other', fp=fp))
    ret = CHECK(t, SYM, 'batch_new_1', states, fp)
    assert ret is True, f'active 同指纹必须拒绝: {ret!r}'
    assert t._api_calls == [], '指纹拒绝必须零 API 调用（本地判据前置）'


def t03_active_same_fp_empty_skeleton_reject():
    """active 同指纹 + entry_orders=[]（PENDING_CREATE）→ 仍拒绝。
    空 skeleton 不能证明未发单（create 可能已发出、order_id 未落盘）。"""
    t = make_trader()
    fp = FP(t, SIG_A)
    states = _states(_active_batch('batch_other', fp=fp,
                                   entry_orders=[], pending=True))
    ret = CHECK(t, SYM, 'batch_new_1', states, fp)
    assert ret is True, f'空骨架 PENDING_CREATE 必须仍拒绝: {ret!r}'
    assert t._api_calls == []


def t04_fp_market_drift_stable():
    """指纹与市价/跳层无关：同原始信号 → 同指纹；且原始两层都在指纹里
    （即使其中一层在当时市价下会被 skip，重发仍识别为同一意图）。"""
    t = make_trader()
    fp1 = FP(t, SIG_A)
    fp2 = FP(t, Sig(entries=[(77820.0, 0.001), (80000.0, 0.001)],
                    stops=[75001.0, 75002.0], tp=80000.0))
    assert fp1 == fp2, f'同意图必须同指纹:\n  {fp1}\n  {fp2}'
    assert '77820.0' in fp1 and '80000.0' in fp1, \
        f'指纹必须包含全部原始层（与 skip 无关）: {fp1}'


def t05_different_intent_pass():
    """entries/SL 实质不同 → 不同指纹 → 放行。"""
    t = make_trader()
    fp_a = FP(t, SIG_A)
    sig_b = Sig(entries=[(77930.0, 0.001), (80000.0, 0.001)],
                stops=[75001.0, 75002.0], tp=80000.0)
    fp_b = FP(t, sig_b)
    assert fp_a != fp_b, '不同意图不得同指纹'
    states = _states(_active_batch('batch_other', fp=fp_a))
    ret = CHECK(t, SYM, 'batch_new_1', states, fp_b)
    assert ret is False, f'不同意图应放行: {ret!r}'


def t06_cleared_batch_reentry_pass():
    """原批次已 clear（is_active=False / 已移除）→ 相同信号合法重入 → 放行。"""
    t = make_trader()
    fp = FP(t, SIG_A)
    cleared = _active_batch('batch_old', fp=fp)
    cleared['is_active'] = False
    states = _states(cleared)
    ret = CHECK(t, SYM, 'batch_new_1', states, fp)
    assert ret is False, f'已 clear 批次不得阻塞合法重入: {ret!r}'
    states2 = _states()  # 批次已移除的形态
    assert CHECK(t, SYM, 'batch_new_1', states2, fp) is False


# ── 运行器 ──────────────────────────────────────────────────────

CHECK = extract_fn('_check_existing_conflicts')
FP = extract_fn('_compute_signal_fingerprint')

TESTS = [t01_first_signal_pass, t02_active_same_fp_reject,
         t03_active_same_fp_empty_skeleton_reject, t04_fp_market_drift_stable,
         t05_different_intent_pass, t06_cleared_batch_reentry_pass]

if __name__ == '__main__':
    ok = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            ok += 1
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {e}')
            if 'AttributeError' in type(e).__name__ or 'TypeError' in type(e).__name__:
                traceback.print_exc(limit=1)
    print(f'\n{"GREEN" if ok == len(TESTS) else "RED"}: {ok}/{len(TESTS)}')
    raise SystemExit(0 if ok == len(TESTS) else 1)
