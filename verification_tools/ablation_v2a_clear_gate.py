# -*- coding: utf-8 -*-
"""
T1C-v2A §13.6 消融验证：绕过 clear authorization（§9 财务授权门）。

方法：真实 CryptoTrader（绕 __init__ + 重定向模块级 STATE_FILE/TOMBSTONE_FILE/AUTH_BLOCKED_FILE
到临时目录）。先确认基线（无突变）下存在 pending_settlement 但缺/错 authorization 时 clear 被拒；
再注入「绕过授权门」突变（outbox 批次一律用 base_dedup_key 自动满足授权），断言同一调用现在
被清批（返回 True）——即对应反例 test_clear_rejects_without_authorization_when_outbox_exists /
test_clear_rejects_wrong_authorization 确定性变红。

判定：
  - 基线被拒 (False) 且 突变后被清 (True) ⇒ 授权门必要且消融有效 ⇒ 本脚本 GREEN。
  - 任一不成立 ⇒ 本脚本 RED（代码已坏 / 突变无效）。

不修改 trader_260725.py（仅内存 monkeypatch），可安全反复运行。
"""
import os
import sys
import json
import tempfile
import shutil
import threading

# 自定位项目根（verification_tools/ 上级），确保从任意目录可跑
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import trader_260725
from trader_260725 import CryptoTrader
from unittest.mock import MagicMock

VALID_PROOF = {
    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'scope': 'FULL',
    'position_zero': True, 'state_ids_resolved': [], 'exchange_scan': 'zero'
}


def make_trader(tmp):
    state_file = os.path.join(str(tmp), 'trade_state.json')
    stats_file = os.path.join(str(tmp), 'trade_stats.json')
    tomb_file = os.path.join(str(tmp), 'trade_tombstones.json')
    auth_file = os.path.join(str(tmp), 'auth_blocked.json')
    trader_260725.STATE_FILE = state_file
    trader_260725.TOMBSTONE_FILE = tomb_file
    trader_260725.AUTH_BLOCKED_FILE = auth_file
    t = CryptoTrader.__new__(CryptoTrader)
    t._state_lock = threading.Lock()
    t._exchange = MagicMock()
    t.tg_bot = MagicMock()
    t.send_tg_notification = MagicMock()
    t._notify_snapshot = MagicMock()
    t._converge_alert_counts = {}
    t._converge_alert_lock = threading.Lock()
    t._alert_counts = {}
    t._alert_lock = threading.Lock()
    t._stats_alert_counts = {}
    t._stats_alert_lock = threading.Lock()
    t._tombstone_alerted = set()
    t._state_corrupted = False
    t._state_corruption_detail = ''
    t._stats_file = stats_file
    t._min_api_interval = 0
    t.sent_tg = []
    return t


def _write_outbox_batch(t, state_file, dedup='BTCUSDT:E1',
                        stats_committed=True, core_status='PROVEN'):
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump({'BTCUSDT': {'B1': {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
            'close_phase': 2, 'is_active': False,
            'pending_close': True, 'close_reason': 'settlement_pending',
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': dedup, 'settlement_id': 'S1',
                'record': {'core_status': core_status, 'dedup_key': dedup},
                'evidence': {}, 'stats_committed': stats_committed,
            },
        }}}, f)


def _write_legacy_batch(t, state_file):
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump({'BTCUSDT': {'B1': {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
            'close_phase': 2, 'is_active': False,
            'settled_by_limit_close': True, 'limit_close_order_id': 'E1',
            'close_op_id': 'op1', 'close_reason': 'limit_close',
        }}}, f)


def apply_bypass_mutant(t):
    """绕过 §9 授权门：outbox 批次一律以 base_dedup_key 自动满足授权 → 任何调用方都能清批。"""
    orig = t.clear_batch_state

    def _mutant(symbol, batch_id, proof=None, authorization=None):
        b = (t.load_all_states().get(symbol) or {}).get(batch_id)
        ob = b.get('pending_settlement') if isinstance(b, dict) else None
        if isinstance(ob, dict) and ob.get('base_dedup_key'):
            # 门失效：无视调用方 authorization，直接以正确 dedup 满足
            return orig(symbol, batch_id, proof=proof,
                        authorization=ob['base_dedup_key'])
        return orig(symbol, batch_id, proof=proof, authorization=authorization)

    t.clear_batch_state = _mutant


def main():
    tmp = tempfile.mkdtemp(prefix='ablation_v2a_')
    state_file = os.path.join(tmp, 'trade_state.json')
    try:
        t = make_trader(tmp)
        passed = 0
        total = 0

        # ① 基线（无突变）：缺 authorization → 必须被拒
        _write_outbox_batch(t, state_file)
        total += 1
        r0 = t.clear_batch_state('BTCUSDT', 'B1', proof=VALID_PROOF)
        assert r0 is False, f'基线授权门失效（缺 authorization 竟被清批）: {r0}'
        passed += 1
        print('✅ 基线：缺 authorization → clear 被拒（授权门存在）')

        # ② 基线（无突变）：错 authorization（legacy 元组） → 必须被拒
        total += 1
        r0b = t.clear_batch_state('BTCUSDT', 'B1', proof=VALID_PROOF,
                                  authorization=('', 'settlement_pending', False, ''))
        assert r0b is False, f'基线授权门失效（错 authorization 竟被清批）: {r0b}'
        passed += 1
        print('✅ 基线：错 authorization → clear 被拒（授权门存在）')

        # ③ 注入「绕过授权门」突变：缺 authorization → 必须被清（反例变红）
        apply_bypass_mutant(t)
        _write_outbox_batch(t, state_file)
        total += 1
        r1 = t.clear_batch_state('BTCUSDT', 'B1', proof=VALID_PROOF)
        assert r1 is True, f'消融突变无效：绕过授权门后缺 authorization 仍被拒: {r1}'
        passed += 1
        print('✅ 消融：绕过授权门 → 缺 authorization 也被清批（反例 test_clear_rejects_without_authorization 确定性变红）')

        # ④ 突变下：错 authorization → 仍被清（test_clear_rejects_wrong_authorization 变红）
        _write_outbox_batch(t, state_file)
        total += 1
        r2 = t.clear_batch_state('BTCUSDT', 'B1', proof=VALID_PROOF,
                                 authorization=('WRONG', 'x', False, 'y'))
        assert r2 is True, f'消融突变无效：错 authorization 仍被拒: {r2}'
        passed += 1
        print('✅ 消融：绕过授权门 → 错 authorization 也被清批（反例 test_clear_rejects_wrong_authorization 确定性变红）')

        # ⑤ 突变不得破坏旧路径：无 outbox 的 legacy 批次仍按既有授权门清理
        _write_legacy_batch(t, state_file)
        total += 1
        r3 = t.clear_batch_state('BTCUSDT', 'B1', proof=VALID_PROOF,
                                 authorization=('op1', 'limit_close', True, 'E1'))
        assert r3 is True, f'突变误伤 legacy 路径：无 outbox 批次应仍可被既有授权门清理: {r3}'
        passed += 1
        print('✅ 对照：无 outbox 的 legacy 批次仍按既有授权门清理（突变仅影响 §9 v2A 门）')

        print(f'\nGREEN: {passed}/{total} — §13.6 消融（绕过 clear authorization）有效：'
              f'授权门为必要承重结构，对应反例确定性变红。')
        return 0
    except AssertionError as e:
        print(f'❌ 消融验证失败: {e}')
        print('GREEN: 0/5 — 消融无法证明授权门必要性（代码已坏或突变无效）。')
        return 1
    except Exception as e:
        print(f'❌ 消融验证异常: {type(e).__name__}: {e}')
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
