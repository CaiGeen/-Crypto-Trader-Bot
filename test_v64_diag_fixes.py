# -*- coding: utf-8 -*-
"""v6.4 竞态修复 + 429 诊断增强的最小决定性测试（ChatGPT 授权范围）。

① signal snapshot：快照解析与磁盘文件完全独立（连发覆写竞态根因的钉死）；
② 429 诊断：证据串包含 endpoint/Retry-After/used-weight/原始错误，headers 缺失不炸。
"""
import ast
import json
import os
import tempfile
import textwrap

import parser as parser_mod

TRADER_PATH = r'G:\my-crypto-bot\trader_260725.py'
BOTRUNNER_PATH = r'G:\my-crypto-bot\bot_runner.py'

SRC = open(TRADER_PATH, encoding='utf-8').read()
BR = open(BOTRUNNER_PATH, encoding='utf-8').read()


def _sig_dict(trigger):
    return {
        "symbol": "BTCUSDT", "side": "BUY", "leverage": 100,
        "entries": [{"trigger_price": trigger, "amount": 0.001, "stop_loss": 75001}],
        "take_profit": 80000, "initial_stop_loss": 75000,
    }


# ── ① signal snapshot：快照解析与磁盘文件完全独立 ────────────────────────
def t01_snapshot_parse_independent_of_file():
    """竞态场景本体：先发快照 77300，磁盘已被后发 77290 覆盖 → 快照互不污染，
    文件路径读到的是覆盖后的 77290。"""
    tmp = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(_sig_dict(77290.0), tmp)  # disk 已被第二条覆盖
    tmp.close()
    try:
        # 两个 dispatch 时刻的快照——竞态场景本体
        snap_first = _sig_dict(77300.0)   # 先发
        snap_second = _sig_dict(77290.0)  # 后发（覆盖了磁盘）
        s_first = parser_mod.parse_signal_from_dict(snap_first)
        s_second = parser_mod.parse_signal_from_dict(snap_second)
        # 快照互不污染：各自解析出各自的 trigger（旧实现重读文件 → 两个都是 77290 ❌）
        assert float(s_first.entries[0][0]) == 77300.0, s_first.entries
        assert float(s_second.entries[0][0]) == 77290.0, s_second.entries
        # 文件路径路径行为不变（读到覆盖后的 77290）
        s_file = parser_mod.parse_signal_from_json(tmp.name)
        assert float(s_file.entries[0][0]) == 77290.0
    finally:
        os.unlink(tmp.name)


# ── ② 429 诊断证据串 ────────────────────────────────────────────────────
def _extract_diag():
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_format_429_diagnostics':
            ns = {}
            exec(textwrap.dedent(ast.get_source_segment(SRC, node)), ns)
            return ns['_format_429_diagnostics']
    return None


def t02_429_diag_evidence_string():
    fmt = _extract_diag()
    assert fmt is not None, '_format_429_diagnostics 未实现'
    fake_fn = lambda: None  # noqa: E731
    fake_fn.__name__ = 'fetch_balance'
    # 真实 Binance header 名（order-count 是独立类别，非 used-weight 变体）
    headers = {'retry-after': '12',
               'x-mbx-used-weight-1m': '2380',
               'x-mbx-order-count-10s': '295',
               'x-mbx-order-count-1m': '1195'}
    out = fmt(fake_fn, headers, 'binanceusdm GET /fapi/v2/balance 429 Too Many Requests')
    assert 'endpoint=fetch_balance' in out, out
    assert 'Retry-After=12' in out, out
    assert 'used-weight=' in out and '2380' in out, out
    assert 'order-count=' in out, out
    assert '295' in out and '1195' in out, out
    assert '429 Too Many Requests' in out, out
    # headers 缺失（部分异常不带 last_response_headers）→ 不炸，字段置 None
    out2 = fmt(fake_fn, None, '429')
    assert 'Retry-After=None' in out2 and 'used-weight={}' in out2 \
        and 'order-count={}' in out2, out2


def t03_wiring_structural():
    """接线结构：5 个分发点全部快照；任务用快照分支；429 分支接诊断。"""
    assert BR.count('signal_snapshot=dict(signal_data))') == 5, \
        BR.count('signal_snapshot=dict(signal_data))')
    assert 'signal = parse_signal_from_dict(signal_snapshot)' in BR
    assert 'async def run_trader_execution(update: Update, context: ContextTypes.DEFAULT_TYPE,\n                               signal_snapshot: dict = None):' in BR
    assert SRC.count('_format_429_diagnostics(') >= 2  # 定义 + 429 分支调用
    assert "getattr(self.exchange, 'last_response_headers', None)" in SRC


def _extract_t(name):
    """从 trader 提取函数；注入其引用的模块级常量（EPS 等）。"""
    tree = ast.parse(SRC)
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets and \
                isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = eval(
                    compile(ast.Expression(node.value), '<m>', 'eval'))
            except Exception:
                pass
    ns = dict(consts)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(textwrap.dedent(ast.get_source_segment(SRC, node)), ns)
            return ns[name]
    return None


def t04_429_cooldown_respects_retry_after():
    ec = _extract_t('_effective_429_cooldown')
    assert ec is not None, '_effective_429_cooldown 未实现'
    assert abs(ec(36.6, '221') - 222.0) < 1e-9, 'Retry-After 主导（221+1）'   # 418 事件的真实量级
    assert abs(ec(36.6, None) - 36.6) < 1e-9, '缺失 → 保持基础值'
    assert abs(ec(36.6, 'abc') - 36.6) < 1e-9, '非法 → 保持基础值'
    assert abs(ec(36.6, '-5') - 36.6) < 1e-9, '负数 → 保持基础值'
    assert abs(ec(50.0, '10') - 50.0) < 1e-9, '基础值更大 → 基础值'


# ── ⑤ SG2 双通道 + 净仓位（实盘误拒加仓的根因修复）──────────────────────
def t05_sg2_dual_channel_and_net_position():
    import types
    cov = _extract_t('_check_sl_coverage')
    netpos = _extract_t('_batch_net_position')
    assert cov is not None and netpos is not None

    class StubEx:
        def __init__(self, normal, stop):
            self.normal, self.stop = normal, stop

        def fetch_open_orders(self, symbol, params=None, **k):
            return list(self.stop) if (params or {}).get('stop') else list(self.normal)

    class T:
        pass

    t = T()
    t._batch_net_position = types.MethodType(netpos, t)
    b = {'is_active': True, 'batch_id': 'batch_A', 'side': 'BUY',
         'target_amounts': [1.0], 'filled_details': [100.0],
         'last_filled_count': 1, 'current_sl_id': 'S1',
         'realized_reduce_amount': 0.5}  # gross 1.0 → net 0.5
    states = {'BTC/USDT:USDT': {'batch_A': b}}

    # A：SL 只在条件单通道（普通通道看不到——今天实盘误拒的场景本体）
    t._safe_api_call = lambda fn, *a, **k: fn(*a, **k)
    t.exchange = StubEx(normal=[], stop=[{'id': 'S1'}])
    ok, msg = types.MethodType(cov, t)('BTC/USDT:USDT', states, 0.5)
    assert ok is True, f'条件单通道里的 SL 必须被找到: {msg}'

    # 反向对照：两个通道都没有 → 拒绝（判据仍活着）
    t.exchange = StubEx(normal=[], stop=[])
    ok, msg = types.MethodType(cov, t)('BTC/USDT:USDT', states, 0.5)
    assert ok is False and '缺少有效止损单' in msg, (ok, msg)

    # B：partial 后净仓位累计——actual 0.5 == Σnet 0.5 → 不得假报台账不一致
    t.exchange = StubEx(normal=[], stop=[{'id': 'S1'}])
    ok, msg = types.MethodType(cov, t)('BTC/USDT:USDT', states, 0.5)
    assert ok is True, f'净仓位判据: {msg}'
    # gross 会得 1.0 > actual 0.5 → 假「台账 > 交易所」（旧实现回归哨兵）
    ok2, msg2 = types.MethodType(cov, t)('BTC/USDT:USDT', states, 0.4)
    assert ok2 is False and '台账' in msg2, msg2

    # Fail-Closed：任一通道异常 → 拒绝
    class BoomEx(StubEx):
        def fetch_open_orders(self, symbol, params=None, **k):
            if (params or {}).get('stop'):
                raise Exception('network down')
            return []
    t.exchange = BoomEx([], [])
    ok, msg = types.MethodType(cov, t)('BTC/USDT:USDT', states, 0.5)
    assert ok is False and '条件单通道' in msg, msg


# ── ⑥ PROVEN-CLEAN：骨架停用 + D-005 释放（零 create 可证明场景）────────
def t06_proven_clean_reject_release():
    import types
    # D 侧：_release_dedup_clean 从 bot_runner 源提取（桩掉 dedup 依赖）
    tree = ast.parse(BR)
    rel = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_release_dedup_clean':
            ns = {'time': __import__('time'), '_load_dedup': None, '_save_dedup': None,
                  'DedupCorruptedError': Exception, 'print': print}
            exec(textwrap.dedent(ast.get_source_segment(BR, node)), ns)
            rel = ns['_release_dedup_clean']
    assert rel is not None, '_release_dedup_clean 未实现'
    store = {'fp_77640': {'status': 'EXECUTING', 'batch_id': None}}
    ns['_load_dedup'] = lambda fp=None, now=None, p=None: dict(store)
    ns['_save_dedup'] = lambda data, p=None: (store.clear(), store.update(data))
    rel('fp_77640')
    assert 'fp_77640' not in store, 'PROVEN-CLEAN 必须删除指纹记录（可立即重发）'

    # C 侧结构：零 create 分支含骨架停用 + CLEAN_REJECT 哨兵
    assert "return 'CLEAN_REJECT'" in SRC
    assert "_b['is_active'] = False" in SRC
    # D 接线：bot_runner 拦截哨兵并释放 + 明确 TG 回执（防「唤起后无下文」），
    # 其余 None 仍走 mark（Fail-Closed 不变）
    assert "if batch_id == 'CLEAN_REJECT':" in BR and '_release_dedup_clean(fingerprint)' in BR
    assert "await safe_reply(" in BR.split("if batch_id == 'CLEAN_REJECT':")[1][:900], \
        'CLEAN_REJECT 分支必须有 TG 回执'
    assert "_mark_dedup_result(fingerprint, batch_id)" in BR


TESTS = [t01_snapshot_parse_independent_of_file,
         t02_429_diag_evidence_string,
         t03_wiring_structural,
         t04_429_cooldown_respects_retry_after,
         t05_sg2_dual_channel_and_net_position,
         t06_proven_clean_reject_release]


def main():
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
            passed += 1
        except Exception as e:
            print(f'❌ {fn.__name__}: {type(e).__name__}: {e}')
    print(f'\nGREEN: {passed}/{len(TESTS)}')
    return 0 if passed == len(TESTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
