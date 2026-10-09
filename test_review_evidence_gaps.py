"""U1-U5 targeted offline regressions against the real engine, not helper copies."""
import copy
import tempfile
import unittest

import test_converge_post_cancel as pc


class EvidenceGaps(unittest.TestCase):
    def setUp(self):
        before = pc._prod_snapshot()
        self.addCleanup(lambda: self.assertEqual(pc._prod_snapshot(), before))
        self.tmp = tempfile.TemporaryDirectory(prefix='evidence_gaps_')
        self.addCleanup(self.tmp.cleanup)
        paths = ('STATE_FILE', 'AUTH_BLOCKED_FILE', 'NOTIFY_QUEUE_DIR_TRADER', 'TOMBSTONE_FILE')
        old = {key: getattr(pc.trader_260725, key) for key in paths}
        self.addCleanup(lambda: [setattr(pc.trader_260725, key, value)
                                 for key, value in old.items()])
        self.t, self.ex = pc.make_trader(self.tmp.name)

    def seed(self, **over):
        pc._state_write(self.t, {pc.SYM: {pc.BID: pc._batch(over)}})

    def batch(self):
        return pc._load(self.t)[pc.SYM][pc.BID]

    def cost_batch(self):
        self.seed(last_filled_count=1, filled_details=[0.0],
                  cost_pending_layers=[0], qty_reconcile_pending=[],
                  fill_evidence={'0': {'status': 'cost_pending', 'order_id': 'E1',
                                      'side': 'buy', 'qty': 1.0, 'planned': 1.0}})

    def setup_filled(self, new_fill=0.0):
        self.seed(entry_orders=['E1', 'E2'], target_amounts=[1.0, 1.0],
                  stop_steps=[76000.0, 77000.0], filled_details=[76001.0, 0.0],
                  last_filled_count=1, total_entry_fee=0.1)
        self.ex._mk('E1', status='closed', filled=1.0)
        self.ex._mk('E2')
        self.ex._mk('SL1')
        self.ex.open_orders = [self.ex.orders['E2'], self.ex.orders['SL1']]
        self.queried = []
        fetch = self.ex.fetch_order
        def query(oid, *a, **kw):
            self.queried.append(oid)
            return fetch(oid, *a, **kw)
        self.ex.fetch_order = query
        def cancel(oid):
            if oid == 'E2':
                self.ex.orders[oid]['filled'] = new_fill
        self.ex.on_cancel = cancel

    def test_filled_batch_new_partial_blocks_proof_and_keeps_sl(self):
        self.setup_filled(0.25)
        proof = self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID)
        self.assertIsNone(proof)
        self.assertIn('E2', self.queried)
        self.assertNotIn('SL1', self.ex.cancel_calls)
        self.assertTrue(self.batch()['is_active'])

    def test_filled_batch_unknown_entry_blocks(self):
        for filled in (None, True, float('nan'), -1.0):
            with self.subTest(filled=filled):
                self.setup_filled()
                self.ex.orders['E2']['filled'] = filled
                self.ex.on_cancel = None
                self.assertIsNone(self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID))
                self.assertNotIn('SL1', self.ex.cancel_calls)
                self.ex.cancel_calls.clear()

    def test_filled_batch_accounted_fill_and_empty_tail_can_clear(self):
        self.setup_filled()
        proof = self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID)
        self.assertIsInstance(proof, dict)
        self.assertIn('E1', self.queried)
        self.assertIn('E2', self.queried)
        self.assertTrue(self.t.clear_batch_state(pc.SYM, pc.BID, proof=proof))

    def test_accounted_algo_requires_exact_terminal_child_quantity(self):
        for filled, status, allowed in ((1.0, 'closed', True), (1.25, 'closed', False),
                                        (1.0, 'open', False), (None, 'closed', False)):
            with self.subTest(filled=filled, status=status):
                self.setup_filled()
                self.ex.orders.pop('E1')
                self.ex.conditional_missing.add('E1')
                self.ex.algo_orders['E1'] = dict(pc.DEFAULT_ALGO_E1,
                    algoStatus='FINISHED', actualOrderId='A1', triggerTime=1)
                self.ex._mk('A1', status=status, filled=filled)
                proof = self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID)
                if allowed:
                    self.assertIsInstance(proof, dict)
                else:
                    self.assertIsNone(proof)
                    self.assertNotIn('SL1', self.ex.cancel_calls)
                self.ex.cancel_calls.clear()

    def test_unknown_rescan_with_lingering_sl_never_clears(self):
        self.setup_filled()
        original_scan = self.ex.fetch_open_orders
        calls = []
        def scan(*a, **kw):
            calls.append(1)
            if len(calls) >= 3:
                return None
            return original_scan(*a, **kw)
        self.ex.fetch_open_orders = scan
        original_cancel = self.ex.cancel_order
        def cancel(oid, *a, **kw):
            if oid == 'SL1':
                self.ex.cancel_calls.append(oid)
                return {'id': oid}  # acknowledgment without disappearance
            return original_cancel(oid, *a, **kw)
        self.ex.cancel_order = cancel
        self.assertIsNone(self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID))
        self.assertEqual(self.ex.orders['SL1']['status'], 'open')
        self.assertTrue(self.batch()['is_active'])
        self.assertIn('critical', self.t.sent_tg_levels)

    def test_scan_invalid_responses_are_not_empty(self):
        for stage in ('initial', 'rescan'):
            for channel in (0, 1):
                for response in (None, {}, 'bad', [None], [{}]):
                    with self.subTest(stage=stage, channel=channel, response=response):
                        self.seed(entry_orders=[], target_amounts=[], filled_details=[],
                                  current_sl_id=None, protection_registry={})
                        calls = []
                        def scan(*a, **kw):
                            n = len(calls)
                            calls.append(n)
                            bad = channel + (2 if stage == 'rescan' else 0)
                            return copy.deepcopy(response) if n == bad else []
                        self.ex.fetch_open_orders = scan
                        self.t.sent_tg_levels.clear()
                        self.assertIsNone(self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID))
                        self.assertIn('critical', self.t.sent_tg_levels)
                        self.assertTrue(self.batch()['is_active'])
                        self.t._converge_alert_counts = {}

    def test_legal_empty_scans_can_clear(self):
        self.seed(entry_orders=[], target_amounts=[], filled_details=[],
                  current_sl_id=None, protection_registry={})
        proof = self.t._converge_batch_orders_before_clear(pc.SYM, pc.BID)
        self.assertIsInstance(proof, dict)
        self.assertTrue(self.t.clear_batch_state(pc.SYM, pc.BID, proof=proof))

    def test_backfill_rechecks_binding_quantity_and_state(self):
        for changed in ('binding', 'target', 'state', 'evidence_qty', 'side'):
            with self.subTest(changed=changed):
                self.cost_batch()
                def query(oid, *a, **kw):
                    b = self.batch()
                    if changed == 'binding':
                        reg = b['protection_registry'][pc.BID + '|ENTRY|L0|LONG']
                        reg.update(state='CONFIRMED', order_id='E9')
                    elif changed == 'target':
                        b['target_amounts'] = [2.0]
                    elif changed == 'state':
                        b['fill_evidence']['0']['status'] = 'qty_unverified'
                        b['qty_reconcile_pending'] = [0]
                    elif changed == 'evidence_qty':
                        b['fill_evidence']['0']['qty'] = 0.25
                    else:
                        b['side'] = 'SELL'
                    pc._state_write(self.t, {pc.SYM: {pc.BID: b}})
                    if changed == 'binding':
                        ids, ok = self.t._rebuild_entry_orders_from_registry(pc.SYM, pc.BID)
                        self.assertTrue(ok)
                        self.assertEqual(ids, ['E9'])
                    return {'id': oid, 'side': 'buy', 'filled': 1.0, 'average': 76001.25}
                self.ex.fetch_order = query
                self.assertEqual(self.t._backfill_entry_costs(pc.SYM, pc.BID), 0)
                b = self.batch()
                self.assertEqual(b['filled_details'], [0.0])
                self.assertEqual(b['total_entry_fee'], 0.0)
                self.assertEqual(b['cost_pending_layers'], [0])

    def test_backfill_stable_evidence_is_once_only(self):
        self.cost_batch()
        self.ex.fetch_order = lambda oid, *a, **kw: {
            'id': oid, 'side': 'buy', 'filled': 1.0, 'average': 76001.25}
        self.assertEqual(self.t._backfill_entry_costs(pc.SYM, pc.BID), 1)
        fee = self.batch()['total_entry_fee']
        self.assertGreater(fee, 0)
        self.assertEqual(self.t._backfill_entry_costs(pc.SYM, pc.BID), 0)
        self.assertEqual(self.batch()['total_entry_fee'], fee)

    def test_cost_exit_requires_observed_side_and_id(self):
        for missing in ('side', 'order_id'):
            with self.subTest(missing=missing):
                self.cost_batch()
                b = self.batch()
                b['fill_evidence']['0'].pop(missing)
                self.assertFalse(self.t._derive_close_txn_vars(b, pc.BID)[0])
                pc._state_write(self.t, {pc.SYM: {pc.BID: b}})
                self.assertEqual(self.t._survey_same_side_batches(pc.SYM, 'BUY', pc.BID),
                                 (-1, -1, -1))

    def test_cost_exit_valid_identity_still_allowed(self):
        self.cost_batch()
        self.assertTrue(self.t._derive_close_txn_vars(self.batch(), pc.BID)[0])
        self.assertEqual(self.t._survey_same_side_batches(pc.SYM, 'BUY', pc.BID),
                         (0, 1.0, 0))

    def test_save_preserves_quantity_unknown_and_derives_queues(self):
        self.cost_batch()
        b = self.batch()
        for price in (None, 76001.0):
            with self.subTest(price=price):
                b['fill_evidence']['0'].update(status='qty_unverified', price=price)
                b['cost_pending_layers'] = []
                b['qty_reconcile_pending'] = [0]
                pc._state_write(self.t, {pc.SYM: {pc.BID: b}})
                self.assertTrue(self.t.save_batch_state(pc.SYM, pc.BID, copy.deepcopy(b)))
                now = self.batch()
                self.assertEqual(now['fill_evidence']['0']['status'], 'qty_unverified')
                self.assertEqual(now['filled_details'], [0.0])
                self.assertEqual(now['cost_pending_layers'], [])
                self.assertEqual(now['qty_reconcile_pending'], [0])

    def test_backfill_quantity_mismatch_atomically_escalates(self):
        self.cost_batch()
        self.ex.fetch_order = lambda oid, *a, **kw: {
            'id': oid, 'side': 'buy', 'filled': 0.25, 'average': 76001.25}
        self.assertEqual(self.t._backfill_entry_costs(pc.SYM, pc.BID), 0)
        b = self.batch()
        self.assertEqual(b['fill_evidence']['0']['status'], 'qty_unverified')
        self.assertEqual(b['cost_pending_layers'], [])
        self.assertEqual(b['qty_reconcile_pending'], [0])
        self.assertEqual(b['filled_details'], [0.0])
        self.assertEqual(b['total_entry_fee'], 0.0)


if __name__ == '__main__':
    unittest.main()
