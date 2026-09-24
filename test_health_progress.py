import json
import os
import tempfile
import time
import unittest
from unittest import mock

import health_progress
import importlib.util
spec = importlib.util.spec_from_file_location("patrol", os.path.join(os.path.dirname(__file__), "健康巡检.py"))
patrol = importlib.util.module_from_spec(spec); spec.loader.exec_module(patrol)

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = health_progress.HEALTH_DIR
        health_progress.HEALTH_DIR = self.tmp.name
    def tearDown(self):
        health_progress.HEALTH_DIR = self.old; self.tmp.cleanup()
    def test_roundtrip_and_instance_mismatch(self):
        health_progress.write_progress("control", "i1", sequence=2)
        self.assertEqual(health_progress.read_progress("control", "i1")["sequence"], 2)
        self.assertIsNone(health_progress.read_progress("control", "i2"))
    def test_batch_cleanup_only_current_instance(self):
        health_progress.write_progress("batch", "i1", "b1", "BTCUSDT", 1)
        health_progress.remove_batch("i2", "b1")
        self.assertIsNotNone(health_progress.read_progress("batch", "i1", "b1"))
        health_progress.remove_batch("i1", "b1")
        self.assertIsNone(health_progress.read_progress("batch", "i1", "b1"))

class PatrolTests(unittest.TestCase):
    def run_check_with(self, states, control, batch=None):
        data = {}
        def fake_read(path):
            name = os.path.basename(path)
            if name == ".heartbeat.json": return {"bot_alive": True, "watchdog_pid": 1, "ts": time.time()}
            if name == "trade_state.json": return states
            if name == "control.json": return control
            if name == "batch_b1.json": return batch
            return None
        with mock.patch.object(patrol, "read_json", side_effect=fake_read), mock.patch.object(patrol, "log"), mock.patch.object(patrol, "alert") as alert_mock, mock.patch.object(patrol, "proxy_ready", return_value=True):
            patrol.run_check({}, True)
            return 1 if alert_mock.called else 0
    def test_empty_is_quiet(self):
        self.assertEqual(self.run_check_with({}, {"instance_id":"i", "ts":time.time()}), 0)
    def test_missing_control_is_state_unknown(self):
        self.assertEqual(self.run_check_with({}, None), 1)
    def test_active_batch_requires_current_progress(self):
        rc = self.run_check_with({"BTC": {"b1": {"is_active": True}}}, {"instance_id":"i", "ts":time.time()})
        self.assertEqual(rc, 1)
    def test_stale_control_alerts(self):
        rc = self.run_check_with({}, {"instance_id":"i", "ts":time.time()-1000})
        self.assertEqual(rc, 1)

if __name__ == "__main__": unittest.main()
