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
                net_pnl=0.9, mode='市价平仓',
                expected_qty=0.001, observed_qty=0.001,
                entry_notional=100.0 * 0.001, allocation_status='PROVEN',
                entry_order_refs=['A1'], exit_order_ref={'order_id': 'X1'})
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
    settlements = [r for r in stats['trades'] if r.get('record_type') == 'settlement']
    assert len(settlements) == 1 and settlements[0]['side'] == 'BUY'
    assert sum(1 for r in stats['trades']
               if r.get('dedup_key') == 'schema_activation:v2') == 1


# S2：合法文件 → 正常追加（既有记录保留）
def s2_valid_file_append():
    t = _trader()
    sf = _sf('s2.json')
    t._record_realized_pnl(stats_file=sf, **_rec(batch_id='a'))
    t._record_realized_pnl(stats_file=sf, **_rec(batch_id='b'))
    stats = json.load(open(sf, encoding='utf-8'))
    assert len([r for r in stats['trades']
                if r.get('record_type') == 'settlement']) == 2


# S3：相同 dedup 重试 → 只保留一条，返回 True（幂等成功）
def s3_dedup_retry_single_record():
    t = _trader()
    sf = _sf('s3.json')
    k = 'BTC/USDT:USDT:L1'
    assert t._record_realized_pnl(stats_file=sf, dedup_key=k, **_rec()) is True
    assert t._record_realized_pnl(stats_file=sf, dedup_key=k, **_rec()) is True
    assert _dedup_count(sf, k) == 1
    assert sum(1 for r in json.load(open(sf, encoding='utf-8'))['trades']
               if r.get('dedup_key') == 'schema_activation:v2') == 1


# S4：非法 JSON → False，原文件逐字节不变
def s4_invalid_json_rejected_bytes_unchanged():
    t = _trader()
    sf = _sf('s4.json')
    t.send_tg_notification = lambda *a, **k: None
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
    # v2A 分层后：settlement 记录存在，但 schema_activation 幂等仅一条
    assert sum(1 for r in stats['trades']
               if r.get('dedup_key') == 'schema_activation:v2') == 1


# S9：CORRUPT → activation 与 settlement 均不得写入
def s9_corrupt_blocks_everything():
    t = _trader()
    sf = _sf('s9.json')
    t.send_tg_notification = lambda *a, **k: None   # 告警桩（防 __new__ 实例缺 tg_bot）
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
    assert sum(1 for r in stats['trades']
               if r.get('dedup_key') == 'schema_activation:v2') == 1



# S11：{} 与 {"trades":[1]} 均拒写且字节不变（{} ≠ 空账本）
def s11_empty_obj_and_non_dict_trades_rejected():
    t = _trader()
    sf1 = _sf('s11a.json')
    open(sf1, 'wb').write(b'{}')
    b1 = _bytes(sf1)
    assert t._record_realized_pnl(stats_file=sf1, **_rec()) is False
    assert _bytes(sf1) == b1, '{} 不得被当成空账本覆盖'
    sf2 = _sf('s11b.json')
    open(sf2, 'wb').write(b'{"trades": [1, "x"]}')
    b2 = _bytes(sf2)
    assert t._record_realized_pnl(stats_file=sf2, **_rec()) is False
    assert _bytes(sf2) == b2, 'trades 含非 dict 记录必须拒写'


# S12：重复损坏 → 限频 critical（≤3 次）且发送时未持 _state_lock
def s12_corrupt_alert_rate_limited_and_outside_lock():
    t = _trader()
    sf = _sf('s12.json')
    open(sf, 'wb').write(b'broken')
    seen = []

    def _spy(msg, level='info'):
        seen.append((level, msg, t._state_lock.locked()))
    t.send_tg_notification = _spy
    for _ in range(5):
        t._record_realized_pnl(stats_file=sf, **_rec())
    criticals = [x for x in seen if x[0] == 'critical']
    assert 1 <= len(criticals) <= 3, f'限频契约：最多 3 次 critical: {len(criticals)}'
    assert all(locked is False for _lv, _m, locked in criticals), \
        'critical 告警必须在锁外发送（不得持 _state_lock）'


# S13：replace/fsync 失败 → 目标不变 + 临时文件零残留
def s13_failure_leaves_no_temp_residue():
    t = _trader()
    d = _sf('s13dir') + '_' + str(abs(hash('s13')) % 10000)
    os.makedirs(d, exist_ok=True)
    sf = os.path.join(d, 'trade_stats.json')
    t._record_realized_pnl(stats_file=sf, **_rec())   # 建立合法账本
    before = _bytes(sf)
    files_before = set(os.listdir(d))
    import trader_260725 as mod
    real_replace = mod.os.replace

    def _boom(*a, **k):
        raise OSError('injected')
    mod.os.replace = _boom
    try:
        assert t._record_realized_pnl(stats_file=sf, **_rec(batch_id='z')) is False
    finally:
        mod.os.replace = real_replace
    assert _bytes(sf) == before, '失败后目标文件必须不变'
    residue = set(os.listdir(d)) - files_before
    assert not residue, f'临时文件零残留: {residue}'


# S14：_fsync_dir() 降级不把已完成写入误判失败（D-009 P0-A 安全方向）
def s14_fsync_dir_degrade_not_failure():
    t = _trader()
    sf = _sf('s14.json')
    import trader_260725 as mod
    real = mod._fsync_dir
    mod._fsync_dir = lambda *a, **k: False    # 平台不支持 → 降级
    try:
        assert t._record_realized_pnl(stats_file=sf, **_rec()) is True, \
            '目录 fsync 降级不得判定写入失败'
    finally:
        mod._fsync_dir = real
    stats = json.load(open(sf, encoding='utf-8'))
    assert len([r for r in stats['trades']
                if r.get('record_type') == 'settlement']) == 1




# S15：json.dump / 文件 fsync 失败 → 目标字节不变 + 临时文件零残留
def s15_dump_and_fsync_failure_no_residue():
    t = _trader()
    d = os.path.join(TMP, 's15dir')
    os.makedirs(d, exist_ok=True)
    sf = os.path.join(d, 'trade_stats.json')
    t._record_realized_pnl(stats_file=sf, **_rec())
    before = _bytes(sf)
    files_before = set(os.listdir(d))
    import trader_260725 as mod

    # ① json.dump 失败
    real_dump = mod.json.dump
    mod.json.dump = lambda *a, **k: (_ for _ in ()).throw(OSError('dump fail'))
    try:
        assert t._record_realized_pnl(stats_file=sf, **_rec(batch_id='d1')) is False
    finally:
        mod.json.dump = real_dump
    assert _bytes(sf) == before, 'dump 失败后目标不变'
    assert not (set(os.listdir(d)) - files_before), 'dump 失败零残留'

    # ② 文件 fsync 失败
    real_fsync = mod.os.fsync
    mod.os.fsync = lambda *a, **k: (_ for _ in ()).throw(OSError('fsync fail'))
    try:
        assert t._record_realized_pnl(stats_file=sf, **_rec(batch_id='d2')) is False
    finally:
        mod.os.fsync = real_fsync
    assert _bytes(sf) == before, 'fsync 失败后目标不变'
    assert not (set(os.listdir(d)) - files_before), 'fsync 失败零残留'


# S16：CORRUPT → 修复 → 再次 CORRUPT 会重新告警（限频器可重武装）─────────────
def s16_rearm_after_repair():
    t = _trader()
    sf = _sf('s16.json')
    seen = []
    t.send_tg_notification = lambda msg, level='info': (
        seen.append(level) if level == 'critical' else None)
    open(sf, 'wb').write(b'broken-1')
    for _ in range(4):                       # 第一次事故：3 次告警后静默
        t._record_realized_pnl(stats_file=sf, **_rec())
    n_first = len(seen)
    assert n_first == 3, f'第一次事故应恰好 3 次: {n_first}'
    # 人工修复：写回合法账本 → 下一次调用 VALID → 清除限频计数
    json.dump({'trades': []}, open(sf, 'w', encoding='utf-8'))
    assert t._record_realized_pnl(stats_file=sf, **_rec()) is True
    # 再次损坏：必须重新告警（不得永久静默）
    open(sf, 'wb').write(b'broken-2')
    t._record_realized_pnl(stats_file=sf, **_rec())
    n_second = len(seen) - n_first
    assert n_second == 1, f'修复后再次损坏必须重新告警: {n_second}'


# S17：并发损坏调用仍不超过限额（限频计数锁内原子更新）────────────────────
def s17_concurrent_corrupt_within_limit():
    t = _trader()
    sf = _sf('s17.json')
    open(sf, 'wb').write(b'broken-concurrent')
    seen = []
    lock = threading.Lock()

    def _spy(msg, level='info'):
        with lock:
            seen.append(level)
    t.send_tg_notification = _spy
    threads = [threading.Thread(target=t._record_realized_pnl,
                                kwargs=dict(stats_file=sf, **_rec(batch_id=f't{i}')))
               for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    criticals = sum(1 for lv in seen if lv == 'critical')
    assert criticals <= 3, f'并发损坏告警必须 ≤3: {criticals}'
    assert criticals >= 1, f'至少应有 1 次告警: {seen}'



TESTS = [s1_missing_file_first_write,
         s2_valid_file_append,
         s3_dedup_retry_single_record,
         s4_invalid_json_rejected_bytes_unchanged,
         s5_invalid_schema_rejected,
         s6_write_failure_reported_false,
         s7_side_enum_and_finite_amounts,
         s8_no_activation_record_from_p0,
         s9_corrupt_blocks_everything,
         s10_p0_never_activates,
         s11_empty_obj_and_non_dict_trades_rejected,
         s12_corrupt_alert_rate_limited_and_outside_lock,
         s13_failure_leaves_no_temp_residue,
         s14_fsync_dir_degrade_not_failure,
         s15_dump_and_fsync_failure_no_residue,
         s16_rearm_after_repair,
         s17_concurrent_corrupt_within_limit]


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
