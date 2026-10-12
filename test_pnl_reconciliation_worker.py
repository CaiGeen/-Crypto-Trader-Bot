import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import ccxt
import pytest
import trader_260725
from pnl_accounting import RequestBudgetError, reserve_request
from trader_260725 import CryptoTrader


def _task(now_ms, *, due_index=7, authoritative=False):
    close_ms = now_ms - (72 * 3600 * 1000 + 10_000) if due_index >= 7 else now_ms - 1_000
    milestones = (0, 60, 300, 900, 3600, 21600, 86400, 72 * 3600, 7 * 24 * 3600)
    task = {
        "dedup_key": "BTC/USDT:USDT:B1:close-1", "account_scope": "acct",
        "batch_id": "B1", "symbol": "BTC/USDT:USDT", "side": "BUY",
        "mode": "market", "created_at_ms": now_ms - 72 * 3600 * 1000,
        "income_start_ms": close_ms - 1000, "close_time_ms": close_ms,
        "trade_evidence": [
            {"trade_id": "entry-1", "role": "entry", "commission": "0.01",
             "commission_asset": "USDT", "time": close_ms - 1000},
            {"trade_id": "exit-1", "role": "exit", "commission": "0.02",
             "commission_asset": "USDT", "time": close_ms},
        ],
        "status": "AUTHORITATIVE_FINAL" if authoritative else "PENDING",
        "attempts": 0, "retry_count": 0, "due_index": due_index,
        "due_times_ms": [close_ms + s * 1000 for s in milestones],
        "next_retry_at_ms": now_ms - 1,
        "finality_at_ms": close_ms + 72 * 3600 * 1000,
        "stop_auto_at_ms": close_ms + 7 * 24 * 3600 * 1000,
        "income_cursor_ms": close_ms - 1000, "income_scan_target_ms": None,
        "income_window_ms": 7 * 24 * 3600 * 1000,
        "income_facts": {}, "income_row_hashes": {}, "fx_facts": {},
        "revisions": 1 if authoritative else 0,
        "last_revision_id": "old" if authoritative else None,
        "notification_claimed": False, "blocking_reasons": [],
    }
    return task, close_ms


def _trader(stats_file, endpoint, *, markets=None, fetch_ohlcv=None):
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._state_lock = threading.Lock()

    def market(symbol):
        if symbol == "BTC/USDT:USDT":
            return {"id": "BTCUSDT", "symbol": symbol, "swap": True,
                    "linear": True, "settle": "USDT"}
        return (markets or {}).get(symbol)

    trader.exchange = SimpleNamespace(
        apiKey="offline-key",
        market=market,
        fapiPrivateGetIncome=endpoint,
        markets=markets or {},
        fetch_ohlcv=fetch_ohlcv,
    )
    def safe_api_call(func, *args, retries=5, attempt_observer=None,
                      request_weight=1, **kwargs):
        if attempt_observer is not None:
            attempt_observer(request_weight)
        return func(*args, **kwargs)
    trader._safe_api_call = safe_api_call
    trader.send_tg_notification = lambda message: trader.notices.append(message)
    trader.notices = []
    trader._test_stats_file = str(stats_file)
    return trader


def _write_stats(path, task, *, net_pnl=0.5, authoritative=False):
    path.write_text(json.dumps({
        "trades": [{"dedup_key": task["dedup_key"], "batch_id": "B1",
                    "symbol": task["symbol"], "net_pnl": net_pnl,
                    "local_provisional_net_pnl": 0.5,
                    "pnl_not_authoritative": not authoritative,
                    "accounting_status": "AUTHORITATIVE_FINAL" if authoritative
                    else "PENDING_RECONCILIATION",
                    "pnl_source": "binance_income_history" if authoritative
                    else "local_provisional"}],
        "accounting_v2": {"tasks": {task["dedup_key"]: task}},
    }), encoding="utf-8")


def _income_rows(close_ms, *, rebate="0", funding=None):
    rows = [
        {"incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "exit-1",
         "symbol": "BTCUSDT", "income": "1.0", "asset": "USDT",
         "time": close_ms},
        {"incomeType": "COMMISSION", "tranId": 2, "tradeId": "entry-1",
         "symbol": "BTCUSDT", "income": "-0.01", "asset": "USDT",
         "time": close_ms - 1000},
        {"incomeType": "COMMISSION", "tranId": 3, "tradeId": "exit-1",
         "symbol": "BTCUSDT", "income": "-0.02", "asset": "USDT",
         "time": close_ms},
    ]
    if rebate != "0":
        rows.append({"incomeType": "COMMISSION_REBATE", "tranId": 4,
                     "tradeId": "entry-1", "symbol": "BTCUSDT",
                     "income": rebate, "asset": "USDT", "time": close_ms + 1})
    if funding is not None:
        rows.append({"incomeType": "FUNDING_FEE", "tranId": 5,
                     "symbol": "BTCUSDT", "income": funding,
                     "asset": "USDT", "time": close_ms + 1})
    return rows


def test_worker_promotes_once_and_durable_notification_is_batch_scoped(tmp_path):
    now_ms = int(time.time() * 1000)
    task, close_ms = _task(now_ms)
    stats_file = tmp_path / "trade_stats.json"
    _write_stats(stats_file, task)
    calls = []
    rows = _income_rows(close_ms) + [
        {"incomeType": "TRANSFER", "income": "2", "asset": "USDT",
         "time": close_ms + 1000}
    ]

    def income(params):
        calls.append(params)
        return rows

    trader = _trader(stats_file, income)
    assert trader._run_one_pnl_reconciliation(str(stats_file)) is True
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    row = persisted["trades"][0]
    live = persisted["accounting_v2"]["tasks"][task["dedup_key"]]
    assert row["net_pnl"] == 0.97
    assert row["accounting_status"] == "AUTHORITATIVE_FINAL"
    assert live["status"] == "AUTHORITATIVE_FINAL"
    assert live["api_attempts_total"] == 1 and live["api_weight_total"] == 30
    assert calls[0]["symbol"] == "BTCUSDT"
    assert live["ignored_income_types"] == {"TRANSFER": 1}
    assert len(trader.notices) == 1
    trader._claim_pnl_completion_notice(task["dedup_key"], str(stats_file))
    assert len(trader.notices) == 1
    second_task = dict(live, dedup_key="BTC/USDT:USDT:B1:close-2",
                       notification_claimed=False)
    persisted["accounting_v2"]["tasks"][second_task["dedup_key"]] = second_task
    stats_file.write_text(json.dumps(persisted), encoding="utf-8")
    trader._claim_pnl_completion_notice(second_task["dedup_key"], str(stats_file))
    assert len(trader.notices) == 1  # max one automatic completion TG per batch


def test_worker_restarts_after_api_failure_and_does_not_promote_partial_scan(tmp_path):
    now_ms = int(time.time() * 1000)
    task, close_ms = _task(now_ms, due_index=0)
    stats_file = tmp_path / "trade_stats.json"
    _write_stats(stats_file, task)
    calls = {"n": 0}

    def fails_once(params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("response lost")
        return _income_rows(close_ms)

    first = _trader(stats_file, fails_once)
    assert first._run_one_pnl_reconciliation(str(stats_file)) is False
    after_failure = json.loads(stats_file.read_text(encoding="utf-8"))
    saved = after_failure["accounting_v2"]["tasks"][task["dedup_key"]]
    assert saved["retry_count"] == 1
    assert saved["api_attempts_total"] == 1 and saved["api_weight_total"] == 30
    assert after_failure["trades"][0].get("accounting_status") != "AUTHORITATIVE_FINAL"

    # Simulate process restart after the saved backoff deadline has passed.
    saved["next_retry_at_ms"] = 0
    after_failure["accounting_v2"]["tasks"][task["dedup_key"]] = saved
    stats_file.write_text(json.dumps(after_failure), encoding="utf-8")
    restarted = _trader(stats_file, fails_once)
    assert restarted._run_one_pnl_reconciliation(str(stats_file)) is False
    resumed = json.loads(stats_file.read_text(encoding="utf-8"))
    saved = resumed["accounting_v2"]["tasks"][task["dedup_key"]]
    assert saved["status"] == "AWAITING_FINALITY"
    assert saved["api_attempts_total"] == 2 and saved["api_weight_total"] == 60
    assert resumed["trades"][0].get("accounting_status") != "AUTHORITATIVE_FINAL"


def test_late_rebate_creates_revision_and_unallocatable_funding_revokes_old_final(tmp_path):
    now_ms = int(time.time() * 1000)
    task, close_ms = _task(now_ms, due_index=7, authoritative=True)
    stats_file = tmp_path / "trade_stats.json"
    _write_stats(stats_file, task, net_pnl=0.97, authoritative=True)
    prior = json.loads(stats_file.read_text(encoding="utf-8"))
    prior["accounting_v2"]["completion_notice_claims"] = {
        "acct:BTC/USDT:USDT:B1": {"claimed_at_ms": now_ms - 1000}}
    stats_file.write_text(json.dumps(prior), encoding="utf-8")
    rows = _income_rows(close_ms, rebate="0.005")
    trader = _trader(stats_file, lambda params: rows)
    assert trader._run_one_pnl_reconciliation(str(stats_file)) is True
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    assert persisted["trades"][0]["net_pnl"] == 0.975
    assert persisted["accounting_v2"]["tasks"][task["dedup_key"]]["revisions"] == 2
    assert len(trader.notices) == 0  # later revisions never send another TG

    # A new funding fact without independently verified direction pools cannot
    # leave the previous authoritative projection marked final.
    rows.append({"incomeType": "FUNDING_FEE", "tranId": 5, "symbol": "BTCUSDT",
                 "income": "-0.01", "asset": "USDT", "time": close_ms})
    task = persisted["accounting_v2"]["tasks"][task["dedup_key"]]
    task["due_index"] = 8
    task["due_times_ms"][8] = int(time.time() * 1000) - 1
    task["next_retry_at_ms"] = 0
    task["income_cursor_ms"] = task["income_start_ms"]
    task["income_scan_target_ms"] = None
    task["status"] = "AUTHORITATIVE_FINAL"
    persisted["accounting_v2"]["tasks"][task["dedup_key"]] = task
    stats_file.write_text(json.dumps(persisted), encoding="utf-8")
    assert trader._run_one_pnl_reconciliation(str(stats_file)) is False
    after_conflict = json.loads(stats_file.read_text(encoding="utf-8"))
    row = after_conflict["trades"][0]
    assert row["pnl_not_authoritative"] is True
    assert row["accounting_status"] == "PENDING_RECONCILIATION"
    assert row["net_pnl"] == row["local_provisional_net_pnl"]


def test_worker_caps_income_pages_by_attempts_and_weight(tmp_path):
    now_ms = int(time.time() * 1000)
    task, _ = _task(now_ms, due_index=0)
    stats_file = tmp_path / "trade_stats.json"
    _write_stats(stats_file, task)
    calls = []

    def full_page(params):
        calls.append(params)
        return [{}] * 1000

    trader = _trader(stats_file, full_page)
    assert trader._run_one_pnl_reconciliation(str(stats_file)) is False
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    saved = persisted["accounting_v2"]["tasks"][task["dedup_key"]]
    assert len(calls) == 3
    assert saved["api_attempts_total"] == 3
    assert saved["api_weight_total"] == 90
    assert saved["income_cursor_ms"] == task["income_start_ms"]
    assert persisted["trades"][0]["pnl_not_authoritative"] is True


def test_worker_converts_each_non_usdt_cashflow_with_exact_mark_minute(tmp_path):
    now_ms = int(time.time() * 1000)
    task, close_ms = _task(now_ms)
    task["trade_evidence"][0].update({"commission": "0.001", "commission_asset": "BNB"})
    task["trade_evidence"][1].update({"commission": "0.002", "commission_asset": "BNB"})
    stats_file = tmp_path / "trade_stats.json"
    _write_stats(stats_file, task)
    rows = _income_rows(close_ms, rebate="0.0001")
    for row, amount in zip((rows[1], rows[2]), ("-0.001", "-0.002")):
        row["asset"] = "BNB"
        row["income"] = amount
    rows[3]["asset"] = "BNB"
    markets = {"BNB/USDT:USDT": {
        "symbol": "BNB/USDT:USDT", "swap": True, "linear": True,
        "base": "BNB", "quote": "USDT", "settle": "USDT",
    }}
    mark_calls = []

    def fetch_mark(symbol, timeframe, since, limit, params):
        mark_calls.append((symbol, timeframe, since, limit, params))
        return [[since, "300", "300", "300", "300", "0"]]

    trader = _trader(stats_file, lambda params: rows,
                     markets=markets, fetch_ohlcv=fetch_mark)
    assert trader._run_one_pnl_reconciliation(str(stats_file)) is True
    persisted = json.loads(stats_file.read_text(encoding="utf-8"))
    assert persisted["trades"][0]["net_pnl"] == 0.13
    task_after = persisted["accounting_v2"]["tasks"][task["dedup_key"]]
    assert mark_calls and all(x[0] == "BNB/USDT:USDT" and x[1] == "1m"
                              and x[3] == 1 and x[4] == {"price": "mark"}
                              for x in mark_calls)
    assert task_after["api_attempts_total"] == 1 + len(mark_calls)
    assert task_after["api_weight_total"] == 30 + len(mark_calls)
    assert all(q["market_type"] == "USD-M-mark"
               and q.get("raw_response_hash") for q in task_after["fx_facts"].values())


def test_worker_start_is_singleton_and_stoppable_without_exchange_calls():
    trader = CryptoTrader.__new__(CryptoTrader)
    called = threading.Event()
    trader._run_one_pnl_reconciliation = lambda: called.set()
    assert trader.start_pnl_reconciliation() is True
    assert called.wait(1)
    assert trader.start_pnl_reconciliation() is False
    trader._pnl_reconcile_stop.set()
    trader._pnl_reconcile_thread.join(timeout=1)
    assert not trader._pnl_reconcile_thread.is_alive()


def test_safe_api_call_accounts_each_failed_retry_before_network_attempt(monkeypatch):
    class Metrics:
        @staticmethod
        def record(*args, **kwargs):
            return None

    class Exchange:
        last_response_headers = {}

    trader = CryptoTrader.__new__(CryptoTrader)
    trader.exchange = Exchange()
    trader._load_auth_state = lambda: {"locked": False, "state": "OPEN", "reason": ""}
    trader._wait_for_api_cooldown = lambda: None
    trader._api_semaphore = threading.Semaphore(1)
    trader._api_lock = threading.Lock()
    trader._min_api_interval = 0
    trader._last_api_call_time = 0
    monkeypatch.setattr(trader_260725, "_API_METRICS", Metrics())
    monkeypatch.setattr(trader_260725.time, "sleep", lambda _: None)

    budget = {"attempts": 0, "weight": 0}
    calls = []

    def fail_network():
        calls.append(1)
        raise ccxt.NetworkError("offline timeout")

    with pytest.raises(ccxt.NetworkError):
        trader._safe_api_call(
            fail_network, retries=3, delay=0, request_weight=30,
            attempt_observer=lambda weight: reserve_request(budget, weight))
    assert len(calls) == 3
    assert budget == {"attempts": 3, "weight": 90}

    budget = {"attempts": 0, "weight": 0}
    calls.clear()
    with pytest.raises(RequestBudgetError, match="budget_exhausted"):
        trader._safe_api_call(
            fail_network, retries=3, delay=0, request_weight=30,
            attempt_observer=lambda weight: reserve_request(
                budget, weight, max_attempts=2))
    assert len(calls) == 2  # third retry is blocked before making a request
    assert budget == {"attempts": 2, "weight": 60}


def test_safe_api_call_accounts_hidden_time_sync_request_and_retry(monkeypatch):
    class Metrics:
        @staticmethod
        def record(*args, **kwargs):
            return None

    class Exchange:
        last_response_headers = {}

        @staticmethod
        def load_time_difference():
            return 0

    trader = CryptoTrader.__new__(CryptoTrader)
    trader.exchange = Exchange()
    trader._load_auth_state = lambda: {"locked": False, "state": "OPEN", "reason": ""}
    trader._wait_for_api_cooldown = lambda: None
    trader._api_semaphore = threading.Semaphore(1)
    trader._api_lock = threading.Lock()
    trader._min_api_interval = 0
    trader._last_api_call_time = 0
    trader.last_time_sync = 0
    monkeypatch.setattr(trader_260725, "_API_METRICS", Metrics())
    monkeypatch.setattr(trader_260725.time, "sleep", lambda _: None)

    budget = {"attempts": 0, "weight": 0}
    calls = []

    def fail_timestamp_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("-1021 timestamp outside recvWindow")
        return "ok"

    result = trader._safe_api_call(
        fail_timestamp_once, retries=2, delay=0, request_weight=30,
        attempt_observer=lambda weight: reserve_request(budget, weight))
    assert result == "ok"
    assert len(calls) == 2
    assert budget == {"attempts": 3, "weight": 61}


def test_funding_fill_reader_requires_real_unique_ids_and_execution_times():
    trader = CryptoTrader.__new__(CryptoTrader)
    fills = [{"id": "trade-1", "order": "order-1", "symbol": "BTC/USDT:USDT",
              "side": "buy", "amount": 0.4, "timestamp": 1234,
              "info": {"id": "trade-1", "orderId": "order-1",
                       "symbol": "BTCUSDT", "side": "BUY",
                       "positionSide": "LONG", "qty": "0.4", "time": 1234}}]
    trader.exchange = SimpleNamespace(
        fetch_my_trades=lambda *args: fills,
        market=lambda symbol: {"id": "BTCUSDT"})
    def direct_api_call(func, *args, **kwargs):
        kwargs.pop('retries', None)
        return func(*args, **kwargs)
    trader._safe_api_call = direct_api_call
    events = trader._fetch_funding_fill_events(
        "BTCUSDT", "order-1", "entry", "BUY", "LONG", 0.4)
    assert events == [{"trade_id": "trade-1", "order_id": "order-1",
                       "symbol": "BTCUSDT", "side": "BUY",
                       "position_side": "LONG", "qty": "0.4",
                       "time": 1234, "role": "entry"}]
    assert CryptoTrader._merge_funding_fill_events(events, events) == events
    with pytest.raises(ValueError, match="identity_conflict"):
        CryptoTrader._merge_funding_fill_events(
            events, [{**events[0], "qty": "0.5"}])

    fills[0]["info"].pop("time")
    fills[0].pop("timestamp")
    with pytest.raises(ValueError, match="identity_incomplete"):
        trader._fetch_funding_fill_events(
            "BTCUSDT", "order-1", "entry", "BUY", "LONG", 0.4)


def test_entry_fill_accepts_observed_algo_to_actual_order_mapping():
    trader = CryptoTrader.__new__(CryptoTrader)
    status, price, qty, _why = trader._resolve_entry_fill_evidence(
        {"id": "actual-1", "side": "buy", "average": 100,
         "filled": 0.4,
         "info": {"algoId": "algo-1", "actualOrderId": "actual-1",
                  "executedQty": "0.4", "side": "BUY"}},
        0.4, expected_side="buy", expected_order_id="algo-1")
    assert status == "confirmed"
    assert price == 100
    assert qty == 0.4


def test_funding_fill_evidence_update_preserves_cost_pending_exit_proof():
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._state_lock = threading.RLock()
    state = {"BTC/USDT:USDT": {"B1": {
        "side": "BUY", "entry_orders": ["algo-1"], "target_amounts": [0.001],
        "last_filled_count": 1, "filled_details": [0.0],
        "total_entry_fee": 0.00005,
        "cost_pending_layers": [], "qty_reconcile_pending": [],
        "fill_evidence": {"0": {
            "status": "confirmed", "why": "backfill_confirmed",
            "qty": 0.001, "side": "buy", "order_id": "algo-1",
            "identity_observed": True,
        }},
    }}}
    trader._load_all_states_ex = lambda: (state, False, "")
    trader._persist_states = lambda *_a, **_k: True
    before = state["BTC/USDT:USDT"]["B1"]["fill_evidence"]["0"]
    assert before["status"] == "confirmed"

    CryptoTrader._record_fill_evidence(
        trader, "BTC/USDT:USDT", "B1", 0, "algo-1",
        {"status": "cost_pending", "why": "stale_cost_pending_snapshot",
         "funding_events": [{"trade_id": "t1"}],
         "funding_event_complete": True}, alert=False)
    after = state["BTC/USDT:USDT"]["B1"]["fill_evidence"]["0"]
    assert after["status"] == "confirmed"
    assert after["why"] == "backfill_confirmed"
    assert after["qty"] == 0.001 and after["side"] == "buy"
    assert after["funding_events"] == [{"trade_id": "t1"}]
    assert state["BTC/USDT:USDT"]["B1"]["cost_pending_layers"] == []
    assert state["BTC/USDT:USDT"]["B1"]["total_entry_fee"] == pytest.approx(0.00005)

    # A newer quantity hold must likewise survive a stale Funding completion.
    after.update(status="qty_unverified", why="new_qty_conflict")
    state["BTC/USDT:USDT"]["B1"]["qty_reconcile_pending"] = [0]
    CryptoTrader._record_fill_evidence(
        trader, "BTC/USDT:USDT", "B1", 0, "algo-1",
        {"status": "cost_pending", "why": "stale_cost_pending_snapshot",
         "funding_events": [{"trade_id": "t1"}],
         "funding_event_complete": True}, alert=False)
    latest = state["BTC/USDT:USDT"]["B1"]["fill_evidence"]["0"]
    assert latest["status"] == "qty_unverified"
    assert state["BTC/USDT:USDT"]["B1"]["qty_reconcile_pending"] == [0]
    batch = state["BTC/USDT:USDT"]["B1"]
    batch["entry_orders"][0] = "algo-2"
    old_record = dict(latest)
    persisted, changed = CryptoTrader._record_fill_evidence(
        trader, "BTC/USDT:USDT", "B1", 0, "algo-1",
        {"funding_events": [{"trade_id": "stale"}],
         "funding_event_complete": True}, alert=False)
    assert (persisted, changed) == (False, False)
    assert batch["fill_evidence"]["0"] == old_record

    pending_batch = {
        "side": "BUY", "entry_orders": ["algo-p"], "target_amounts": [0.001],
        "last_filled_count": 1, "filled_details": [0.0],
        "cost_pending_layers": [0], "qty_reconcile_pending": [],
        "fill_evidence": {"0": {
            "status": "cost_pending", "why": "cost_unavailable", "qty": 0.001,
            "side": "buy", "order_id": "algo-p", "identity_observed": True,
        }},
    }
    state["BTC/USDT:USDT"]["B2"] = pending_batch
    assert trader_260725._entry_quantity_evidence_valid(
        pending_batch, 0, pending_batch["fill_evidence"]["0"])
    CryptoTrader._record_fill_evidence(
        trader, "BTC/USDT:USDT", "B2", 0, "algo-p",
        {"status": "confirmed", "why": "stale_status",
         "funding_events": [], "funding_event_complete": False}, alert=False)
    pending_record = pending_batch["fill_evidence"]["0"]
    assert pending_record["status"] == "cost_pending"
    assert trader_260725._entry_quantity_evidence_valid(pending_batch, 0, pending_record)


def test_closecancel_attribution_commits_without_funding_query():
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._state_lock = threading.RLock()
    batch = {"close_op_id": "op-1", "close_phase": 1,
             "close_reason": "limit_pending_normal", "limit_close_order_id": "ord-1",
             "side": "BUY", "is_hedge_mode": False, "is_active": True,
             "target_amounts": [0.002], "filled_details": [100.0],
             "entry_orders": ["entry-1"], "last_filled_count": 1,
             "realized_reduce_amount": 0.0, "funding_reduction_events": []}
    state = {"BTC/USDT:USDT": {"B1": batch}}
    trader._load_all_states_ex = lambda: (state, False, "")
    trader._persist_states = lambda *_a, **_k: True
    trader._batch_net_position = lambda _batch: (0.002, 0.2)
    trader._fetch_funding_fill_events = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("Funding query ran before attribution/protection"))

    ok, reason = CryptoTrader._commit_closecancel_attribution(
        trader, "BTC/USDT:USDT", "B1", "op-1", 0.001)
    assert ok and reason == "attributed"
    assert batch["realized_reduce_amount"] == pytest.approx(0.001)
    assert batch["close_reason"] == "limit_cancel_restore_pending"
    assert "ord-1" in batch["funding_event_missing_orders"]


def test_production_funding_builder_uses_rate_history_and_full_trade_audit():
    entry = {"trade_id": "entry-1", "order_id": "entry-order", "symbol": "BTCUSDT",
             "side": "BUY", "position_side": "LONG", "qty": "1",
             "time": 110, "role": "entry"}
    reduction = {"trade_id": "reduce-1", "order_id": "reduce-order",
                 "symbol": "BTCUSDT", "side": "SELL", "position_side": "LONG",
                 "qty": "0.4", "time": 200}
    close = {"trade_id": "close-1", "order_id": "close-order", "symbol": "BTCUSDT",
             "side": "SELL", "position_side": "LONG", "qty": "0.6",
             "time": 500, "role": "exit"}
    task = {
        "account_scope": "acct", "symbol": "BTC/USDT:USDT", "batch_id": "B1",
        "side": "BUY", "income_start_ms": 100, "close_time_ms": 500,
        "funding_baseline_complete": True,
        "funding_position_baseline": {"captured_at_ms": 100,
                                      "long_qty": "0", "short_qty": "0"},
        "event_evidence_complete": True, "prior_reduction_unknown": False,
        "funding_events": [entry, close], "reduction_events": [reduction],
        "realized_reduce_amount": "0.4",
    }
    raw_trades = [
        {"id": "entry-1", "order": "entry-order", "symbol": "BTC/USDT:USDT",
         "side": "buy", "amount": 1, "timestamp": 110,
         "info": {"id": "entry-1", "orderId": "entry-order", "symbol": "BTCUSDT",
                  "side": "BUY", "positionSide": "LONG", "qty": "1", "time": 110}},
        {"id": "reduce-1", "order": "reduce-order", "symbol": "BTC/USDT:USDT",
         "side": "sell", "amount": 0.4, "timestamp": 200,
         "info": {"id": "reduce-1", "orderId": "reduce-order", "symbol": "BTCUSDT",
                  "side": "SELL", "positionSide": "LONG", "qty": "0.4", "time": 200}},
        {"id": "entry-2", "order": "entry-order-2", "symbol": "BTC/USDT:USDT",
         "side": "buy", "amount": 0.5, "timestamp": 250,
         "info": {"id": "entry-2", "orderId": "entry-order-2", "symbol": "BTCUSDT",
                  "side": "BUY", "positionSide": "LONG", "qty": "0.5", "time": 250}},
    ]
    active_batch = {
        "is_active": True, "batch_id": "B2", "symbol": "BTC/USDT:USDT",
        "side": "BUY", "is_hedge_mode": True,
        "funding_position_baseline": {"captured_at_ms": 100,
                                      "long_qty": "0", "short_qty": "0"},
        "funding_baseline_complete": True,
        "funding_event_evidence_complete": True,
        "funding_fill_events": [{**entry, "trade_id": "entry-2",
                                  "order_id": "entry-order-2", "qty": "0.5",
                                  "time": 250}],
        "funding_reduction_events": [], "realized_reduce_amount": 0,
    }
    trader = CryptoTrader.__new__(CryptoTrader)
    trader.exchange = SimpleNamespace(
        market=lambda symbol: {"id": "BTCUSDT"},
        fetch_my_trades=lambda *args: raw_trades,
        fetch_funding_rate_history=lambda *args: [
            {"timestamp": 300, "fundingRate": 0.0001,
             "info": {"symbol": "BTCUSDT", "fundingTime": "300",
                      "fundingRate": "0.0001", "markPrice": "100"}}],
    )
    trader.load_all_states = lambda: {"BTC/USDT:USDT": {"B2": active_batch}}
    budget = {"attempts": 0, "weight": 0}

    def safe_call(func, *args, **kwargs):
        observer = kwargs.pop("attempt_observer", None)
        weight = kwargs.pop("request_weight", 1)
        kwargs.pop("retries", None)
        if observer:
            observer(weight)
        return func(*args, **kwargs)

    trader._safe_api_call = safe_call
    trader._pnl_consume_budget = lambda b, weight: reserve_request(b, weight)
    result = trader._pnl_build_funding_allocations(
        task, {"task-1": task},
        [{"incomeType": "FUNDING_FEE", "tranId": 8, "symbol": "BTCUSDT",
            "income": "-0.011", "asset": "USDT", "time": 300}], budget)
    proof = next(iter(result.values()))
    assert proof["direction_pools"]["LONG"] == "-0.01100"
    assert proof["batch_allocations"]["B1"] == "-0.00600"
    assert proof["batch_allocations"]["B2"] == "-0.00500"
    assert proof["account_trade_ids"] == ["entry-1", "entry-2", "reduce-1"]
    assert budget == {"attempts": 2, "weight": 6}


def test_funding_baseline_capture_is_read_only_and_side_complete():
    trader = CryptoTrader.__new__(CryptoTrader)
    trader.exchange = SimpleNamespace(
        market=lambda symbol: {"id": "BTCUSDT"},
        fetch_positions=lambda symbols: [
            {"symbol": "BTC/USDT:USDT", "contracts": 0.25,
             "info": {"symbol": "BTCUSDT", "positionAmt": "0.25",
                      "positionSide": "LONG"}},
            {"symbol": "BTC/USDT:USDT", "contracts": 0.1,
             "info": {"symbol": "BTCUSDT", "positionAmt": "0.1",
                      "positionSide": "SHORT"}},
        ])
    trader._safe_api_call = lambda func, *args, **kwargs: func(*args)
    baseline = trader._capture_funding_position_baseline("BTC/USDT:USDT", True)
    assert baseline["long_qty"] == "0.25"
    assert baseline["short_qty"] == "0.1"
    assert baseline["market_id"] == "BTCUSDT"
    assert baseline["captured_at_ms"] > 0
