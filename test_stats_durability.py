# -*- coding: utf-8 -*-
"""P0-stats-durability 验收测试（S1–S10，v2.2 §10.1，RED-first）。

契约（v2.2 §6）：
- 三态读取 MISSING/VALID/CORRUPT；
- CORRUPT：原文件字节不变、不执行 stats={}、不 os.replace、无法验证 dedup
  时不追加、返回 False；
- MISSING：可创建新账本；
- VALID：锁内 read→validate→dedup→append→tempfile→fsync(file)→os.replace
  →尽力 fsync(directory)；
- BUY/SELL 枚举与金额有限性校验；
- P0 绝不写 schema_activation 记录。
"""
import json
import os
import tempfile
import threading

from trader_260725 import CryptoTrader

TMP = tempfile.mkdtemp(prefix='p0stats_')


def _trader():
    t = CryptoTrader.__new__(CryptoTrader)
    t._state_lock = threading.Lock()
    return t


def _rec(**kw):
    base = dict(batch_id='b1', symbol='BTC/USDT:USDT', side='BUY',
                amount=0.001, avg_price=100.0, exit_price=101.0,
                net_pnl=0.9, mode='市价平仓')
    base.update(kw)
    return base


def _sf(name):
    return os.path.join(TMP, name)


def _bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def _dedup_count(path, key):
    if not os.path.exists(path):
        return 0
    stats = json.load(open(path, encoding='utf-8'))
    return len([r for r in stats.get('trades', [])
                if r.get('dedup_key') == key])


# S1：文件不存在 → 首次写入成功
def s1_missing_file_first_write():
    t = _trader()
    sf = _sf('s1.json')
    assert t._record_realized_pnl(stats_file=sf, **_rec()) is True
    stats = json.load(open(sf, encoding='utf-8'))
    assert len(stats['trades']) == 1 and stats['trades'][0]['side'] == 'BUY'


# S2：合法文件 → 正常追加（既有记录保留）
def s2_valid_file_append():
    t = _trader()
    sf = _sf('s2.json')
    t._record_realized_pnl(stats_file=sf, **_rec(batch_id='a'))
    t._record_realized_pnl(stats_file=sf, **_rec(batch_id='b'))
    stats = json.load(open(sf, encoding='utf-8'))
    assert len(stats['trades']) == 2, len(stats['trades'])


# S3：相同 dedup 重试 → 只保留一条，返回 True（幂等成功）
def s3_dedup_retry_single_record():
    t = _trader()
    sf = _sf('s3.json')
    k = 'BTC/USDT:USDT:L1'
    assert t._record_realized_pnl(stats_file=sf, dedup_key=k, **_rec()) is True
    assert t._record_realized_pnl(stats_file=sf, dedup_key=k, **_rec()) is True
    assert _dedup_count(sf, k) == 1


# S4：非法 JSON → False，原文件逐字节不变
def s4_invalid_json_rejected_bytes_unchanged():
    t = _trader()
    sf = _sf('s4.json')
    raw = b'{"trades": [{oops'
    open(sf, 'wb').write(raw)
    before = _bytes(sf)
    assert t._record_realized_pnl(stats_file=sf, **_rec()) is False
    assert _bytes(sf) == before, 'CORRUPT 文件字节必须保持不变'


# S5：根/schema 非法 → False，原文件不变
def s5_invalid_schema_rejected():
    t = _trader()
    sf5a = _sf('s5a.json')
    open(sf5a, 'wb').write(b'[1, 2, 3]')          # 根不是 dict
    b5a = _bytes(sf5a)
    assert t._record_realized_pnl(stats_file=sf5a, **_rec()) is False
    assert _bytes(sf5a) == b5a
    sf5b = _sf('s5b.json')
    open(sf5b, 'w', encoding='utf-8').write('{"trades": "not-a-list"}')
    b5b = _bytes(sf5b)
    assert t._record_realized_pnl(stats_file=sf5b, **_rec()) is False
    assert _bytes(sf5b) == b5b


# S6：写盘失败 → 不报告成功
def s6_write_failure_reported_false():
    t = _trader()
    sf = _sf('s6.json')
    t._record_realized_pnl(stats_file=sf, **_rec())   # 先建合法账本
    import trader_260725 as mod
    real_replace = mod.os.replace

    def _boom(*a, **k):
        raise OSError('injected replace failure')
    mod.os.replace = _boom
    try:
        rc = t._record_realized_pnl(stats_file=sf, **_rec(batch_id='c'))
    finally:
        mod.os.replace = real_replace
    assert rc is False, f'写盘失败必须返回 False: {rc}'


# S7：BUY/SELL 枚举与金额有限性校验
def s7_side_enum_and_finite_amounts():
    t = _trader()
    sf = _sf('s7.json')
    assert t._record_realized_pnl(stats_file=sf, **_rec(side='buy')) is False
    assert t._record_realized_pnl(stats_file=sf, **_rec(side='LONG')) is False
    assert t._record_realized_pnl(stats_file=sf, **_rec(amount=float('nan'))) is False
    assert t._record_realized_pnl(stats_file=sf, **_rec(net_pnl=float('inf'))) is False
    assert t._record_realized_pnl(stats_file=sf, **_rec(avg_price='x')) is False
    # 合法 SELL 必须通过
    assert t._record_realized_pnl(stats_file=sf, **_rec(side='SELL')) is True


# S8：P0 永不写 schema_activation 记录
def s8_no_activation_record_from_p0():
    t = _trader()
    sf = _sf('s8.json')
    for i in range(3):
        t._record_realized_pnl(stats_file=sf, **_rec(batch_id=f'b{i}'))
    stats = json.load(open(sf, encoding='utf-8'))
    assert all(r.get('record_type') is None for r in stats['trades']), (
        'P0 不得写 schema_activation')


# S9：CORRUPT → activation 与 settlement 均不得写入
def s9_corrupt_blocks_everything():
    t = _trader()
    sf = _sf('s9.json')
    raw = b'not json at all'
    open(sf, 'wb').write(raw)
    before = _bytes(sf)
    assert t._record_realized_pnl(stats_file=sf,
                                  dedup_key='K1', **_rec()) is False
    assert _bytes(sf) == before


# S10：P0 单独部署时绝不主动追加 activation（多次写入后仍为零）
def s10_p0_never_activates():
    t = _trader()
    sf = _sf('s10.json')
    for i in range(5):
        t._record_realized_pnl(stats_file=sf, dedup_key=f'K{i}', **_rec())
    stats = json.load(open(sf, encoding='utf-8'))
    assert not any('record_type' in r for r in stats['trades'])


TESTS = [s1_missing_file_first_write,
         s2_valid_file_append,
         s3_dedup_retry_single_record,
         s4_invalid_json_rejected_bytes_unchanged,
         s5_invalid_schema_rejected,
         s6_write_failure_reported_false,
         s7_side_enum_and_finite_amounts,
         s8_no_activation_record_from_p0,
         s9_corrupt_blocks_everything,
         s10_p0_never_activates]


def main():
    fails = []
    for fn in TESTS:
        try:
            fn()
            print(f'✅ {fn.__name__}')
        except AssertionError as e:
            fails.append(fn.__name__)
            print(f'❌ {fn.__name__}: {e}')
        except Exception as e:
            fails.append(fn.__name__)
            print(f'💥 {fn.__name__}: {type(e).__name__}: {e}')
    print(f'GREEN: {len(TESTS) - len(fails)}/{len(TESTS)}')
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
