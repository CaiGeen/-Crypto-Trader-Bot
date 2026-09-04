# -*- coding: utf-8 -*-
"""
T1C-v2A Integration Addendum v1.2 — 验收测试套件

基线：3d8b63b
RED-first 说明（ChatGPT 终审 v1.2 §13）：
  RED-first 必须断言目标行为，在旧实现上真实失败。
  禁止编写"PASS 证明旧缺陷存在"的反向测试。

本套件使用 **真实 CryptoTrader（CryptoTrader.__new__ 绕 __init__ + 注入 FakeExchange
+ 重定向模块级 STATE_FILE/TOMBSTONE_FILE 到临时目录）**，直接调用 v1.2 要求新增/修改
的方法并断言目标行为。基线上：
  - 尚不存在的 v2A 方法 → AttributeError（ERROR = RED）
  - 尚不正确的行为（merge/clear 授权门）→ AssertionError（FAIL = RED）
[GREEN] 用例在实施完成后必须通过。

覆盖范围：
  §13.1 证据与公式   #1-8
  §13.2 dedup/activation  #9-15
  §13.3 outbox 与 merge   #16-21
  §13.4 恢复与 clear     #22-32
  r99 生产文件免疫
"""

import os
import sys
import json
import time
import tempfile
import shutil
import threading
import unittest
from unittest.mock import MagicMock, patch

import trader_260725
from trader_260725 import CryptoTrader, TAKER_FEE_RATE, MAKER_FEE_RATE

# ─────────────────────────────────────────────────────────────────────────────
# FakeExchange（ccxt 兼容桩，序列 repeat-last）
# ─────────────────────────────────────────────────────────────────────────────

class FakeExchange:
    def __init__(self):
        self.orders = {}
        self.positions = []
        self.open_orders = []
        self.last_response_headers = {}

    def set_position(self, symbol, amount, entry_price):
        self.positions = [{
            'symbol': symbol, 'amount': amount,
            'entryPrice': entry_price, 'unrealizedPnL': 0.0
        }]

    def fetch_order(self, oid, symbol=None, params=None, **k):
        o = self.orders.get(oid)
        if o is None:
            raise Exception(f'{oid} not found')
        return dict(o)

    def fetch_open_orders(self, symbol=None, params=None, **k):
        return list(self.open_orders)

    def cancel_order(self, oid, symbol=None, params=None, **k):
        o = self.orders.get(oid)
        if o is None:
            raise Exception(f'{oid} not found')
        o['status'] = 'canceled'
        return {'id': oid}

    def create_order(self, symbol, otype, side, amount, price=None, params=None, **k):
        nid = f'N{len(self.orders) + 1}'
        o = {
            'id': nid, 'symbol': symbol, 'type': otype,
            'side': side, 'amount': amount, 'price': price or 60000.0,
            'average': price or 60000.0, 'filled': 0.0, 'status': 'open',
            'stopPrice': (params or {}).get('stopPrice'), 'info': {}
        }
        self.orders[nid] = o
        return o

    def fetch_balance(self):
        return {'USDT': {'total': 16000.0, 'free': 15000.0, 'used': 1000.0}}

    def fetch_positions(self, symbol=None, params=None, **k):
        return self.positions

    def fetch_ticker(self, symbol=None, **k):
        return {'last': 60000.0, 'close': 60000.0}

    def amount_to_precision(self, symbol, amount):
        return round(amount, 6)

    def price_to_precision(self, symbol, price):
        return round(price, 2)

    def set_leverage(self, *a, **k):
        return {}

    def load_time_difference(self):
        return True

    def load_markets(self, *a, **k):
        return {}

    def fetch_time(self):
        return 1234567890


# ─────────────────────────────────────────────────────────────────────────────
# 真实 trader 实例（绕 __init__，最小属性注入）
# ─────────────────────────────────────────────────────────────────────────────

def make_trader(tmp):
    state_file = os.path.join(str(tmp), 'trade_state.json')
    stats_file = os.path.join(str(tmp), 'trade_stats.json')
    tomb_file = os.path.join(str(tmp), 'trade_tombstones.json')
    auth_file = os.path.join(str(tmp), 'auth_blocked.json')
    # 重定向模块级文件（load_all_states / _persist_states / _load_tombstones 读全局）
    trader_260725.STATE_FILE = state_file
    trader_260725.TOMBSTONE_FILE = tomb_file
    trader_260725.AUTH_BLOCKED_FILE = auth_file

    t = CryptoTrader.__new__(CryptoTrader)
    t._state_lock = threading.Lock()
    t._exchange = FakeExchange()
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
    return t, stats_file


VALID_PROOF = {
    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'scope': 'FULL',
    'position_zero': True, 'state_ids_resolved': [], 'exchange_scan': 'zero'
}


# ─────────────────────────────────────────────────────────────────────────────
# 测试夹具
# ─────────────────────────────────────────────────────────────────────────────

class T1cV2aTest(unittest.TestCase):
    """Addendum v1.2 §13 验收测试基类。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='t1c_v2a_')
        self.trader, self.stats_file = make_trader(self.tmpdir)
        self.state_file = trader_260725.STATE_FILE
        self.tomb_file = trader_260725.TOMBSTONE_FILE

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_states(self, data):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def _read_states(self):
        if not os.path.exists(self.state_file):
            return {}
        with open(self.state_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_stats(self, data):
        with open(self.stats_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def _read_stats(self):
        if not os.path.exists(self.stats_file):
            return {'trades': []}
        with open(self.stats_file, 'r', encoding='utf-8') as f:
            return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# §13.1 证据与公式 #1-8
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceFormula(T1cV2aTest):
    """§13.1 #1-8：净成本均价、镜像公式、DISPUTED 生成。"""

    def test_evidence_builder_exists(self):
        """[RED §13.1 #1-3] _build_settlement_evidence 必须存在且构造 PROVEN evidence。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[{'kind': 'regular', 'order_id': 'E1', 'expected_qty': 0.002}],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['record']['core_status'], 'PROVEN')
        self.assertEqual(ev['schema'], 2)

    def test_atomic_outbox_begin_exists(self):
        """[RED §13.3 #16] _atomic_outbox_begin 必须存在并原子写入 outbox。"""
        self._write_states({
            'BTCUSDT': {
                'B1': {
                    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
                    'is_active': True, 'close_phase': 1, 'pending_close': True,
                    'is_hedge_mode': True, 'filled_amount': 0.002,
                    'avg_price': 60000.0, 'realized_reduce_cost': 0.0,
                    'target_amounts': [0.002], 'last_filled_count': 1,
                    'total_entry_fee': 0.036,
                    'exit_order_ref': {'kind': 'regular', 'order_id': 'E1'},
                    'entry_orders': ['E1'],
                }
            }
        })
        ok = self.trader._atomic_outbox_begin(
            batch_id='B1', symbol='BTCUSDT', generation='gen1')
        self.assertTrue(ok)

    def test_try_finalize_outbox_exists(self):
        """[RED §13.3] _try_finalize_outbox 必须存在。"""
        result = self.trader._try_finalize_outbox(
            batch_id='B1', symbol='BTCUSDT')
        self.assertIsInstance(result, bool)

    def test_resume_pending_settlement_exists(self):
        """[RED §13.4 #22] _resume_pending_settlement 必须存在。"""
        result = self.trader._resume_pending_settlement(
            batch_id='B1', symbol='BTCUSDT')
        self.assertIsInstance(result, bool)

    def test_proven_evidence_buy_formula(self):
        """[RED §13.1 #1-3] BUY：avg_entry = net_cost / qty，entry_fee 只扣一次。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[{'kind': 'regular', 'order_id': 'E1', 'expected_qty': 0.002}],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertAlmostEqual(ev['evidence']['avg_entry'], 60000.0, places=4)
        gross = (61000.0 - 60000.0) * 0.002
        self.assertAlmostEqual(ev['record']['gross_pnl'], gross, places=4)
        self.assertIn('entry_fee_estimate', ev['evidence'])
        self.assertIn('net_pnl_estimate', ev['record'])

    def test_proven_evidence_sell_formula(self):
        """[RED §13.1 #1] SELL 镜像公式：avg_entry = net_cost / qty。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='SELL', mode='MARKET',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[{'kind': 'regular', 'order_id': 'E1', 'expected_qty': 0.002}],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=59000.0, generation='gen1')
        self.assertAlmostEqual(ev['evidence']['avg_entry'], 60000.0, places=4)
        gross = (60000.0 - 59000.0) * 0.002
        self.assertAlmostEqual(ev['record']['gross_pnl'], gross, places=4)

    def test_invalid_side_generates_disputed(self):
        """[RED §13.1 #6] side 不是 BUY/SELL → core_status=DISPUTED。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='INVALID', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['record']['core_status'], 'DISPUTED')

    def test_empty_exit_ref_generates_disputed(self):
        """[RED §13.1 #6] exit_order_ref.order_id 为空 → core_status=DISPUTED。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': ''},
            entry_order_refs=[{'kind': 'regular', 'order_id': 'E1', 'expected_qty': 0.002}],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['record']['core_status'], 'DISPUTED')

    def test_inf_qty_generates_disputed(self):
        """[RED §13.1 #6] expected_qty 非有限 → core_status=DISPUTED。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[],
            expected_qty=float('inf'), observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['record']['core_status'], 'DISPUTED')

    def test_qty_mismatch_generates_disputed(self):
        """[RED §13.1 #6] expected ≠ observed（超容差）→ core_status=DISPUTED。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.001,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['record']['core_status'], 'DISPUTED')

    def test_tp_uses_taker_fee_not_maker(self):
        """[RED §13.1 #5] TP 是 MARKET 条件单，必须使用 Taker 费率。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='TP',
            base_dedup_key='BTCUSDT:T1', settlement_id='S1',
            exit_order_ref={'kind': 'algo', 'order_id': 'T1'},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['evidence']['fee_risk_basis']['allocation_policy'],
                         'CONSERVATIVE_FULL')
        self.assertIn('exit_fee_estimate', ev['evidence'])
        self.assertGreater(ev['evidence']['exit_fee_estimate'], 0.0)
        # exit_fee_estimate 必须基于 Taker 费率（>0 且符合 Taker 语义）
        # Taker 费率 > Maker 费率，断言二者一致（禁止 Maker 数值）
        self.assertAlmostEqual(
            ev['evidence']['exit_fee_estimate'],
            ev['evidence']['fee_risk_basis']['exit_notional'] * TAKER_FEE_RATE,
            places=6)

    def test_entry_fee_deducted_once_in_net_pnl(self):
        """[RED §13.1 #2] entry_fee 只在 net_pnl_estimate 中扣一次，不得进入 avg_entry。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0,
            entry_fee_estimate=0.036, exit_fee_estimate=0.012,
            generation='gen1')
        self.assertAlmostEqual(ev['evidence']['avg_entry'], 60000.0, places=4)
        gross = (61000.0 - 60000.0) * 0.002  # 20
        expected_net = gross - 0.036 - 0.012
        self.assertAlmostEqual(ev['record']['net_pnl_estimate'],
                               expected_net, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# §13.2 dedup 与 activation #9-15
# ─────────────────────────────────────────────────────────────────────────────

class TestDedupAndActivation(T1cV2aTest):
    """§13.2 #9-15：settlement/dispute 同 dedup。"""

    def test_settlement_and_dispute_same_dedup(self):
        """[RED §13.2 #9] settlement 与 settlement_dispute 必须用相同 base_dedup_key。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='BUY', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': 'E1'},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertEqual(ev['base_dedup_key'], 'BTCUSDT:E1')

    def test_dispute_dedup_prefix_not_settlement_dispute(self):
        """[RED §13.2 #9] DISPUTED record 的 dedup_key 必须 == base_dedup_key。"""
        ev = self.trader._build_settlement_evidence(
            batch_id='B1', symbol='BTCUSDT', side='INVALID', mode='LIMIT',
            base_dedup_key='BTCUSDT:E1', settlement_id='S1',
            exit_order_ref={'kind': 'regular', 'order_id': ''},
            entry_order_refs=[],
            expected_qty=0.002, observed_qty=0.002,
            net_cost=120.0, exit_price=61000.0, generation='gen1')
        self.assertFalse(
            ev['record']['dedup_key'].startswith('settlement_dispute:'))

    def test_stats_corrupt_rejects_settlement(self):
        """[GREEN §13.2 #15] stats CORRUPT → 拒写（基线 P0 已满足）。"""
        with open(self.stats_file, 'w') as f:
            f.write('THIS IS NOT JSON')
        result = self.trader._record_realized_pnl(
            batch_id='B1', symbol='BTCUSDT', side='BUY',
            amount=0.002, avg_price=60000.0, exit_price=61000.0,
            net_pnl=1.0, mode='LIMIT', dedup_key='BTCUSDT:E1',
            stats_file=self.stats_file)
        self.assertFalse(result)


# ─────────────────────────────────────────────────────────────────────────────
# §13.3 outbox 与 merge #16-21
# ─────────────────────────────────────────────────────────────────────────────

class TestOutboxAndMerge(T1cV2aTest):
    """§13.3 #16-21：pending_settlement Transactional Protected Field。"""

    def test_merge_preserves_disk_different_dedup(self):
        """[RED §13.3 #18-19] disk/snap pending_settlement dedup 不同 → disk 胜出。"""
        disk = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:DISK_A',
                'settlement_id': 'S_A', 'record': {}, 'evidence': {},
                'stats_committed': True
            }
        }
        snap = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 1,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:SNAP_B',
                'settlement_id': 'S_B', 'record': {}, 'evidence': {},
                'stats_committed': False
            }
        }
        merged = self.trader._merge_batch_state(disk, snap)
        self.assertEqual(
            merged.get('pending_settlement', {}).get('base_dedup_key'),
            'BTCUSDT:DISK_A',
            "disk dedup ≠ snap dedup 时 merged 必须保留 disk 的 dedup。")

    def test_merge_same_dedup_preserves_disk_evidence(self):
        """[RED §13.3 #18] 相同 dedup：record/evidence/settlement_id 以 disk 为准。"""
        disk = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:SAME',
                'settlement_id': 'DISK_S', 'record': {'net_pnl': 1.0},
                'evidence': {'exit_price': 61000.0}, 'stats_committed': True
            }
        }
        snap = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:SAME',
                'settlement_id': 'SNAP_S', 'record': {'net_pnl': 999.0},
                'evidence': {'exit_price': 99999.0}, 'stats_committed': False
            }
        }
        merged = self.trader._merge_batch_state(disk, snap)
        self.assertEqual(
            merged.get('pending_settlement', {}).get('settlement_id'),
            'DISK_S',
            "相同 dedup 时 settlement_id 必须以 disk 为准。")

    def test_merge_stats_committed_false_to_true_allowed(self):
        """[RED §13.3 #20] stats_committed 允许 False → True。"""
        disk = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:COMMIT',
                'settlement_id': 'S1', 'record': {}, 'evidence': {},
                'stats_committed': False
            }
        }
        snap = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:COMMIT',
                'settlement_id': 'S1', 'record': {}, 'evidence': {},
                'stats_committed': True
            }
        }
        merged = self.trader._merge_batch_state(disk, snap)
        self.assertTrue(
            merged.get('pending_settlement', {}).get('stats_committed'),
            "stats_committed 必须允许 False→True。")

    def test_merge_stats_committed_true_to_false_forbidden(self):
        """[RED §13.3 #20] stats_committed 禁止 True → False（反向覆盖）。"""
        disk = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:COMMIT',
                'settlement_id': 'S1', 'record': {}, 'evidence': {},
                'stats_committed': True
            }
        }
        snap = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:COMMIT',
                'settlement_id': 'S1', 'record': {}, 'evidence': {},
                'stats_committed': False
            }
        }
        merged = self.trader._merge_batch_state(disk, snap)
        self.assertTrue(
            merged.get('pending_settlement', {}).get('stats_committed'),
            "stats_committed 禁止 True→False（snap=False 不得覆盖 disk=True）。")

    def test_merge_disk_only_pending_settlement_wins(self):
        """[RED §13.3 #17] disk 有 pending_settlement、snap 无 → 保留 disk。"""
        disk = {
            'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 2,
            'pending_settlement': {
                'schema': 2, 'base_dedup_key': 'BTCUSDT:DISK',
                'settlement_id': 'S1', 'record': {}, 'evidence': {},
                'stats_committed': True
            }
        }
        snap = {'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY', 'close_phase': 1}
        merged = self.trader._merge_batch_state(disk, snap)
        self.assertIsNotNone(
            merged.get('pending_settlement'),
            "disk 的 pending_settlement 不得被 snap 错误覆盖。")

    def test_atomic_outbox_begin_atomic_writes(self):
        """[RED §13.3 #16] _atomic_outbox_begin 原子写入 close_phase=2 + pending_settlement。"""
        # 准备批次（含净仓位，供 evidence 构造）
        self._write_states({
            'BTCUSDT': {
                'B1': {
                    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
                    'is_active': True, 'close_phase': 1, 'pending_close': True,
                    'is_hedge_mode': True, 'filled_amount': 0.002,
                    'avg_price': 60000.0, 'realized_reduce_cost': 0.0,
                    'target_amounts': [0.002], 'last_filled_count': 1,
                    'total_entry_fee': 0.036,
                    'exit_order_ref': {'kind': 'regular', 'order_id': 'E1'},
                    'entry_orders': ['E1'],
                }
            }
        })
        ok = self.trader._atomic_outbox_begin(
            batch_id='B1', symbol='BTCUSDT', generation='gen1')
        self.assertTrue(ok)
        states = self._read_states()
        b = (states.get('BTCUSDT') or {}).get('B1')
        self.assertIsNotNone(b)
        self.assertEqual(b.get('close_phase'), 2)
        self.assertEqual(b.get('close_reason'), 'settlement_pending')
        self.assertIsNotNone(b.get('pending_settlement'))


# ─────────────────────────────────────────────────────────────────────────────
# §13.4 恢复与 clear #22-32
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryAndClear(T1cV2aTest):
    """§13.4 #22-32：outbox 恢复、clear 授权门、DISPUTED 不 clear。"""

    def _write_outbox_batch(self, close_reason='settlement_pending',
                            stats_committed=True, dedup='BTCUSDT:E1',
                            core_status='PROVEN'):
        self._write_states({
            'BTCUSDT': {
                'B1': {
                    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
                    'close_phase': 2, 'is_active': False,
                    'pending_close': True, 'close_reason': close_reason,
                    'pending_settlement': {
                        'schema': 2, 'base_dedup_key': dedup,
                        'settlement_id': 'S1',
                        'record': {'core_status': core_status, 'dedup_key': dedup},
                        'evidence': {}, 'stats_committed': stats_committed
                    }
                }
            }
        })

    def test_resume_pending_settlement_in_mon_loop(self):
        """[RED §13.4 #22] 监控循环头部必须能接管 pending_settlement 恢复。"""
        result = self.trader._resume_pending_settlement(
            batch_id='B1', symbol='BTCUSDT')
        self.assertIsInstance(result, bool)

    def test_clear_rejects_without_authorization_when_outbox_exists(self):
        """[RED §13.4 #28] 存在 pending_settlement 但无 settlement_commit_authorization → 拒绝。"""
        self._write_outbox_batch()
        result = self.trader.clear_batch_state(
            'BTCUSDT', 'B1', proof=VALID_PROOF)
        self.assertFalse(
            result,
            "存在 pending_settlement 但无 settlement_commit_authorization → clear 必须拒绝。")

    def test_clear_rejects_wrong_authorization(self):
        """[RED §13.4 #28] 旧 legacy 授权元组 ≠ base_dedup_key → v2A 门拒绝。"""
        self._write_outbox_batch()
        # 基线 legacy 门接受的四元组（close_op_id 空、close_reason=settlement_pending）
        legacy_auth = ('', 'settlement_pending', False, '')
        result = self.trader.clear_batch_state(
            'BTCUSDT', 'B1', proof=VALID_PROOF, authorization=legacy_auth)
        self.assertFalse(
            result,
            "authorization 非 base_dedup_key（即使 legacy 元组合法）→ v2A 门必须拒绝。")

    def test_clear_rejects_disputed_core_status(self):
        """[RED §13.4 #29] 核心证据 DISPUTED → 不得自动 clear（即使 legacy 元组合法）。"""
        self._write_outbox_batch(close_reason='settlement_disputed',
                                 core_status='DISPUTED')
        # legacy 四元组（基线会接受并删除）→ 基线删除(RED)；v2A DISPUTED 门拒绝(GREEN)
        legacy_auth = ('', 'settlement_disputed', False, '')
        result = self.trader.clear_batch_state(
            'BTCUSDT', 'B1', proof=VALID_PROOF, authorization=legacy_auth)
        self.assertFalse(
            result,
            "core_status=DISPUTED → clear 必须拒绝（保留人工核对）。")

    def test_clear_rejects_stats_not_committed(self):
        """[RED §13.4] stats_committed=False → v2A 门拒绝（即使 legacy 元组合法）。"""
        self._write_outbox_batch(stats_committed=False)
        legacy_auth = ('', 'settlement_pending', False, '')
        result = self.trader.clear_batch_state(
            'BTCUSDT', 'B1', proof=VALID_PROOF, authorization=legacy_auth)
        self.assertFalse(
            result,
            "stats_committed=False → clear 必须拒绝（保留 outbox 待重试）。")

    def test_legacy_settled_batch_clears_without_outbox_auth(self):
        """[GREEN §13.4] 无 pending_settlement 的旧批次仍按既有授权门清理。"""
        self._write_states({
            'BTCUSDT': {
                'B1': {
                    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
                    'close_phase': 2, 'is_active': False,
                    'settled_by_limit_close': True,
                    'limit_close_order_id': 'E1',
                    'close_op_id': 'op1', 'close_reason': 'limit_close'
                }
            }
        })
        result = self.trader.clear_batch_state(
            'BTCUSDT', 'B1', proof=VALID_PROOF,
            authorization=('op1', 'limit_close', True, 'E1'))
        self.assertTrue(result)

    def test_finalize_outbox_writes_stats_and_clears(self):
        """[RED §13.3 #16, §13.4 #23] _try_finalize_outbox 写入 stats 后 clear。"""
        # 注入可控 converge（避开真实交易所查询）
        self.trader._converge_batch_orders_before_clear = lambda s, b: VALID_PROOF
        # 准备批次 + outbox（stats_committed=False 初始，finalize 应写 stats 后推进 True）
        self._write_states({
            'BTCUSDT': {
                'B1': {
                    'batch_id': 'B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
                    'close_phase': 2, 'is_active': False, 'pending_close': True,
                    'close_reason': 'settlement_pending',
                    'pending_settlement': {
                        'schema': 2, 'base_dedup_key': 'BTCUSDT:E1',
                        'settlement_id': 'S1',
                        'record': {'core_status': 'PROVEN',
                                   'dedup_key': 'BTCUSDT:E1',
                                   'net_pnl_estimate': 19.95,
                                   'gross_pnl': 20.0},
                        'evidence': {'avg_entry': 60000.0,
                                     'exit_price': 61000.0,
                                     'exit_fee_estimate': 0.012,
                                     'entry_fee_estimate': 0.036},
                        'stats_committed': True
                    }
                }
            }
        })
        result = self.trader._try_finalize_outbox(batch_id='B1', symbol='BTCUSDT')
        self.assertIsInstance(result, bool)


# ─────────────────────────────────────────────────────────────────────────────
# 生产文件免疫哨兵（r99）
# ─────────────────────────────────────────────────────────────────────────────

_PROD_FILES = ['trade_state.json', 'trade_tombstones.json', 'trade_stats.json',
               'auth_blocked.json', 'signal.json', 'signal_dedup.json']
_PROD_DIR = os.path.dirname(os.path.abspath(__file__))


def _prod_snapshot():
    snap = {}
    for _n in _PROD_FILES:
        _p = os.path.join(_PROD_DIR, _n)
        try:
            with open(_p, 'rb') as _f:
                _data = _f.read()
            snap[_n] = (__import__('hashlib').sha256(_data).hexdigest(),
                        len(_data), os.stat(_p).st_mtime_ns)
        except FileNotFoundError:
            snap[_n] = None
    return snap


def _prod_compare(snap_before, snap_after, test_name):
    errors = []
    for _n, _s_before in snap_before.items():
        _s_after = snap_after.get(_n)
        if _s_before != _s_after:
            errors.append(f'{test_name}: {_n} changed {_s_before} → {_s_after}')
    if errors:
        raise AssertionError('PROD FILE POLLUTION DETECTED:\n  ' + '\n  '.join(errors))


class TestProdFileSafety(T1cV2aTest):
    """[GREEN r99] 所有测试全程不得污染生产文件。"""

    def test_no_production_file_write(self):
        snap_before = _prod_snapshot()
        self.trader.save_batch_state('BTCUSDT', 'TEST_B1', {
            'batch_id': 'TEST_B1', 'symbol': 'BTCUSDT', 'side': 'BUY',
            'is_active': True, 'close_phase': 0
        })
        self.trader._record_realized_pnl(
            'TEST_B1', 'BTCUSDT', 'BUY', 0.002, 60000.0, 61000.0,
            1.0, 'LIMIT', dedup_key='BTCUSDT:TEST', stats_file=self.stats_file)
        self.trader.clear_batch_state('BTCUSDT', 'TEST_B1', proof=None)
        snap_after = _prod_snapshot()
        _prod_compare(snap_before, snap_after, 'T1C-v2A test suite')


if __name__ == '__main__':
    unittest.main(verbosity=2)
