import json
from decimal import Decimal

import pytest

from pnl_accounting import (
    AccountingEvidenceError,
    allocate_funding,
    build_single_direction_funding_allocation,
    authoritative_net,
    apply_authoritative_revision,
    convert_mark_minute,
    income_identity,
    mark_revision_pending,
    match_commissions,
    reconcile_income_facts,
    reserve_request,
    RequestBudgetError,
)


def test_income_identity_scopes_tran_id_by_type_and_quarantines_missing_id():
    realized, complete = income_identity("acct", {"incomeType": "REALIZED_PNL", "tranId": 7})
    rebate, complete2 = income_identity("acct", {"incomeType": "COMMISSION_REBATE", "tranId": 7})
    assert complete and complete2 and realized != rebate
    a = income_identity("acct", {"incomeType": "FUNDING_FEE", "income": "1", "time": 1})
    assert a[0].startswith("acct:FUNDING_FEE:missing-id:") and not a[1]


def test_commission_sources_match_once_and_signed_income_is_canonical():
    trades = [{"id": 51, "info": {"id": "51", "commission": "0.002", "commissionAsset": "BNB"}}]
    incomes = [{"incomeType": "COMMISSION", "tradeId": "51", "income": "-0.002", "asset": "BNB"}]
    assert match_commissions(trades, incomes) == {("51", "BNB"): Decimal("-0.002")}
    with pytest.raises(AccountingEvidenceError, match="counterpart"):
        match_commissions(trades, [])
    with pytest.raises(AccountingEvidenceError, match="amount_mismatch"):
        match_commissions(trades, [{**incomes[0], "income": "-0.001"}])


def test_commission_matching_preserves_multiple_assets_per_trade():
    trades = [
        {"id": "51", "info": {"id": "51", "commission": "0.1", "commissionAsset": "BNB"}},
        {"id": "51", "info": {"id": "51", "commission": "0.2", "commissionAsset": "USDT"}},
    ]
    incomes = [
        {"incomeType": "COMMISSION", "tradeId": "51", "income": "-0.1", "asset": "BNB"},
        {"incomeType": "COMMISSION", "tradeId": "51", "income": "-0.2", "asset": "USDT"},
    ]
    assert match_commissions(trades, incomes) == {
        ("51", "BNB"): Decimal("-0.1"), ("51", "USDT"): Decimal("-0.2")}


def test_mark_fx_requires_exact_closed_event_minute():
    event = 1_700_000_001_234
    minute = event // 60_000 * 60_000
    candles = [[minute, "1", "2", "0.5", "250", "100"]]
    value, proof = convert_mark_minute("-0.01", "BNB", event, candles,
                                       now_ms=minute + 120_000)
    assert value == Decimal("-2.50")
    assert proof["source"] == "binance_usdm_mark_1m_close"
    with pytest.raises(AccountingEvidenceError, match="missing_or_ambiguous"):
        convert_mark_minute("1", "BNB", event, [], now_ms=minute + 120_000)
    with pytest.raises(AccountingEvidenceError, match="not_closed"):
        convert_mark_minute("1", "BNB", event, candles, now_ms=minute + 59_999)


def test_funding_splits_direction_pools_before_pro_rata_allocation():
    allocated = allocate_funding(
        {"LONG": "-0.024", "SHORT": "0.008"},
        [
            {"batch_id": "L1", "position_side": "LONG", "verified_notional": "1"},
            {"batch_id": "L2", "position_side": "LONG", "verified_notional": "2"},
            {"batch_id": "S1", "position_side": "SHORT", "verified_notional": "1"},
        ],
    )
    assert allocated["L1"] == Decimal("-0.008")
    assert allocated["L2"] == Decimal("-0.016")
    assert allocated["S1"] == Decimal("0.008")
    assert sum(allocated.values()) == Decimal("-0.016")
    with pytest.raises(AccountingEvidenceError, match="direction_pools_incomplete"):
        allocate_funding({"NET": "-0.016"}, [])


@pytest.mark.parametrize("side,entry_side,reduce_side,position_side", [
    ("BUY", "BUY", "SELL", "LONG"),
    ("SELL", "SELL", "BUY", "SHORT"),
])
def test_funding_reconstructs_partial_exposure_from_durable_fill_events(
        side, entry_side, reduce_side, position_side):
    task = {
        "account_scope": "acct", "symbol": "BTCUSDT", "batch_id": "batch-a",
        "side": side, "income_start_ms": 100, "close_time_ms": 500,
        "event_evidence_complete": True, "prior_reduction_unknown": False,
        "funding_baseline_complete": True,
        "funding_position_baseline": {"captured_at_ms": 100,
                                      "long_qty": "0", "short_qty": "0"},
        "realized_reduce_amount": "0.4",
        "funding_events": [
            {"trade_id": "entry-1", "order_id": "entry-order", "symbol": "BTCUSDT",
             "side": entry_side, "position_side": position_side, "qty": "1",
             "time": 110, "role": "entry"},
            {"trade_id": "close-1", "order_id": "close-order", "symbol": "BTCUSDT",
             "side": reduce_side, "position_side": position_side, "qty": "0.6",
             "time": 500, "role": "exit"},
        ],
        "reduction_events": [
            {"trade_id": "reduce-1", "order_id": "reduce-order", "symbol": "BTCUSDT",
             "side": reduce_side, "position_side": position_side, "qty": "0.4",
             "time": 200},
        ],
    }
    restored_task = json.loads(json.dumps(task))  # persisted state survives process restart
    income = "-0.006" if side == "BUY" else "0.006"
    audit = {"complete": True, "start_ms": 100, "end_ms": 300, "trades": [
        {"trade_id": "entry-1", "order_id": "entry-order", "symbol": "BTCUSDT",
         "side": entry_side, "position_side": position_side, "qty": "1", "time": 110},
        {"trade_id": "reduce-1", "order_id": "reduce-order", "symbol": "BTCUSDT",
         "side": reduce_side, "position_side": position_side, "qty": "0.4", "time": 200},
    ]}
    result = build_single_direction_funding_allocation(
        restored_task, {"task-a": restored_task, "replayed-task": restored_task},
        {"time": 300, "income": income, "asset": "USDT"}, "100", "0.0001",
        account_trade_audit=audit,
        active_batch_ids=["batch-a"])
    assert {key: Decimal(value) for key, value in result["direction_pools"].items()} == {
        "LONG": Decimal("-0.006") if side == "BUY" else Decimal(0),
        "SHORT": Decimal(0) if side == "BUY" else Decimal("0.006"),
    }
    assert Decimal(result["batch_allocations"]["batch-a"]) == Decimal(income)
    assert result["batches"][0]["verified_notional"] == "60.0"

    audit["trades"].append({"trade_id": "manual-trade", "order_id": "manual-order",
                            "symbol": "BTCUSDT", "side": "BUY",
                            "position_side": position_side, "qty": "0.01", "time": 250})
    with pytest.raises(AccountingEvidenceError, match="unexplained_account_trade"):
        build_single_direction_funding_allocation(
            restored_task, {"task-a": restored_task},
            {"time": 300, "income": income, "asset": "USDT"}, "100", "0.0001",
            account_trade_audit=audit)


def test_funding_evidence_gap_or_active_batch_stays_unallocated():
    task = {
        "account_scope": "acct", "symbol": "BTCUSDT", "batch_id": "batch-a",
        "side": "BUY", "income_start_ms": 100, "close_time_ms": 500,
        "event_evidence_complete": False, "prior_reduction_unknown": True,
        "funding_baseline_complete": False,
        "funding_position_baseline": None,
        "realized_reduce_amount": "0", "funding_events": [], "reduction_events": [],
    }
    audit = {"complete": True, "start_ms": 100, "end_ms": 300, "trades": []}
    with pytest.raises(AccountingEvidenceError, match="active_batch"):
        build_single_direction_funding_allocation(
            {**task, "event_evidence_complete": True,
             "prior_reduction_unknown": False,
             "funding_baseline_complete": True,
             "funding_position_baseline": {"captured_at_ms": 100,
                                           "long_qty": "0", "short_qty": "0"}},
            {"task-a": {**task, "event_evidence_complete": True,
                        "prior_reduction_unknown": False,
                        "funding_baseline_complete": True,
                        "funding_position_baseline": {"captured_at_ms": 100,
                                                      "long_qty": "0", "short_qty": "0"}}},
            {"time": 300, "income": "0.1", "asset": "USDT"}, "100",
            "0.001", account_trade_audit=audit,
            active_batch_ids=["still-open"])
    with pytest.raises(AccountingEvidenceError, match="evidence_incomplete"):
        build_single_direction_funding_allocation(
            task, {"task-a": task}, {"time": 300, "income": "0.1", "asset": "USDT"}, "100",
            "0.001", account_trade_audit=audit)


def test_unknown_inputs_never_become_authoritative_zero():
    assert authoritative_net("1", "0", "0", "0", evidence_complete=False,
                             valuation_complete=True, allocation_complete=True) is None
    assert authoritative_net("1", "-0.1", "-0.02", "0.01",
                             evidence_complete=True, valuation_complete=True,
                             allocation_complete=True) == Decimal("0.89")


def test_revision_promotion_is_idempotent_and_preserves_history():
    stats = {"trades": [{"dedup_key": "BTC:close-1", "net_pnl": 0.5,
                         "pnl_not_authoritative": True}], "accounting_v2": {}}
    evidence = {"revision_id": "r1", "income_complete": True,
                "commission_reconciled": True, "funding_allocated": True,
                "fx_complete": True, "realized_usdt": "1",
                "funding_usdt": "0.1", "commission_usdt": "-0.2",
                "commission_entry_usdt": "-0.1", "commission_exit_usdt": "-0.1",
                "rebate_usdt": "0.01", "evidence_refs": ["income:1"]}
    assert apply_authoritative_revision(stats, "BTC:close-1", evidence) is True
    row = stats["trades"][0]
    assert row["net_pnl"] == pytest.approx(0.91)
    assert row["pnl_source"] == "binance_income_history"
    assert row["pnl_not_authoritative"] is False
    assert row["entry_fee_source"] == row["exit_fee_source"] == "actual"
    assert row["entry_fee"] == row["exit_fee"] == 0.1
    assert apply_authoritative_revision(stats, "BTC:close-1", evidence) is True
    assert len(stats["accounting_v2"]["revisions"]) == 1
    assert stats["accounting_v2"]["revisions"]["BTC:close-1:r1"]["prior_record"]["net_pnl"] == 0.5
    # Recovery may reload a durable revision ledger alongside a stale trade
    # projection; replay must repair the projection without adding a revision.
    stats["trades"][0].update(net_pnl=0.5, accounting_status="PENDING_RECONCILIATION",
                              pnl_source="local_provisional", pnl_not_authoritative=True)
    assert apply_authoritative_revision(stats, "BTC:close-1", evidence) is True
    assert stats["trades"][0]["net_pnl"] == 0.91
    assert stats["trades"][0]["accounting_status"] == "AUTHORITATIVE_FINAL"
    assert len(stats["accounting_v2"]["revisions"]) == 1


def test_incomplete_revision_cannot_promote_provisional_record():
    stats = {"trades": [{"dedup_key": "X:1", "net_pnl": 5.0}]}
    evidence = {"revision_id": "r1", "income_complete": True,
                "commission_reconciled": True, "funding_allocated": False,
                "fx_complete": True, "realized_usdt": "100",
                "funding_usdt": "0", "commission_usdt": "0", "rebate_usdt": "0"}
    assert apply_authoritative_revision(stats, "X:1", evidence) is False
    assert stats["trades"][0]["net_pnl"] == 5.0
    assert stats["trades"][0]["accounting_status"] == "PENDING_RECONCILIATION"


def test_income_reducer_requires_trade_matched_realized_and_both_commission_sources():
    task = {
        "account_scope": "acct", "batch_id": "B1", "symbol": "BTC/USDT:USDT",
        "trade_evidence": [
            {"trade_id": "e1", "role": "entry", "commission": "0.01",
             "commission_asset": "USDT", "time": 1000},
            {"trade_id": "x1", "role": "exit", "commission": "0.02",
             "commission_asset": "USDT", "time": 2000},
        ],
    }
    rows = [
        {"incomeType": "REALIZED_PNL", "tradeId": "x1", "tranId": 1,
         "symbol": "BTCUSDT", "income": "1.0", "asset": "USDT", "time": 1000},
        {"incomeType": "COMMISSION", "tradeId": "e1", "tranId": 2,
         "symbol": "BTCUSDT",
         "income": "-0.01", "asset": "USDT", "time": 1000},
        {"incomeType": "COMMISSION", "tradeId": "x1", "tranId": 3,
         "symbol": "BTCUSDT",
         "income": "-0.02", "asset": "USDT", "time": 2000},
    ]
    result = reconcile_income_facts(task, rows,
        fx_value=lambda amount, asset, event: (Decimal(str(amount)), {"asset": asset}),
        income_complete=True)
    assert result["realized_usdt"] == Decimal("1.0")
    assert result["commission_usdt"] == Decimal("-0.03")
    assert result["funding_usdt"] == Decimal(0)
    with pytest.raises(AccountingEvidenceError, match="counterpart"):
        reconcile_income_facts(task, rows[:-1],
            fx_value=lambda amount, asset, event: (Decimal(str(amount)), {}),
            income_complete=True)


def test_commission_and_late_rebate_use_their_own_event_minute_fx():
    task = {
        "account_scope": "acct", "batch_id": "B1", "symbol": "BTC/USDT:USDT",
        "trade_evidence": [
            {"trade_id": "e1", "role": "entry", "commission": "0.001",
             "commission_asset": "BNB", "time": 60_001},
            {"trade_id": "x1", "role": "exit", "commission": "0.002",
             "commission_asset": "BNB", "time": 120_001},
        ],
    }
    rows = [
        {"incomeType": "REALIZED_PNL", "tradeId": "x1", "tranId": 1,
         "symbol": "BTCUSDT", "income": "1", "asset": "USDT", "time": 120_001},
        {"incomeType": "COMMISSION", "tradeId": "e1", "tranId": 2,
         "symbol": "BTCUSDT", "income": "-0.001", "asset": "BNB", "time": 60_002},
        {"incomeType": "COMMISSION", "tradeId": "x1", "tranId": 3,
         "symbol": "BTCUSDT", "income": "-0.002", "asset": "BNB", "time": 120_002},
        {"incomeType": "COMMISSION_REBATE", "tradeId": "e1", "tranId": 4,
         "symbol": "BTCUSDT", "income": "0.0001", "asset": "BNB", "time": 180_001},
    ]
    prices = {1: Decimal("300"), 2: Decimal("310"), 3: Decimal("320")}

    def fx(amount, asset, event_ms):
        if asset == "USDT":
            return Decimal(str(amount)), {"asset": asset}
        minute = event_ms // 60_000
        return Decimal(str(amount)) * prices[minute], {"minute": minute}

    result = reconcile_income_facts(task, rows, fx_value=fx, income_complete=True)
    assert result["commission_entry_usdt"] == Decimal("-0.300")
    assert result["commission_exit_usdt"] == Decimal("-0.620")
    assert result["commission_usdt"] == Decimal("-0.920")
    assert result["rebate_usdt"] == Decimal("0.0320")
    assert [p["minute"] for p in result["fx_proofs"] if "minute" in p] == [1, 2, 3]


def test_income_reducer_funding_requires_conserved_direction_pools_and_batch_weights():
    task = {
        "account_scope": "acct", "batch_id": "L1", "symbol": "BTC/USDT:USDT",
        "income_start_ms": 1000, "close_time_ms": 4000,
        "trade_evidence": [
            {"trade_id": "e1", "role": "entry", "commission": "0", "commission_asset": "USDT", "time": 1000},
            {"trade_id": "x1", "role": "exit", "commission": "0", "commission_asset": "USDT", "time": 2000},
        ],
    }
    pools = {"LONG": "-0.024", "SHORT": "0.008"}
    batches = [
        {"batch_id": "L1", "position_side": "LONG", "verified_notional": "1"},
        {"batch_id": "L2", "position_side": "LONG", "verified_notional": "2"},
        {"batch_id": "S1", "position_side": "SHORT", "verified_notional": "1"},
    ]
    expected = allocate_funding(pools, batches)
    funding = {"incomeType": "FUNDING_FEE", "tranId": 10, "symbol": "BTCUSDT",
               "income": "-0.016", "asset": "USDT", "time": 3000}
    rows = [
        {"incomeType": "REALIZED_PNL", "tradeId": "x1", "tranId": 1,
         "symbol": "BTCUSDT", "income": "1", "asset": "USDT", "time": 1000},
        {"incomeType": "COMMISSION", "tradeId": "e1", "tranId": 2,
         "symbol": "BTCUSDT",
         "income": "0", "asset": "USDT", "time": 1000},
        {"incomeType": "COMMISSION", "tradeId": "x1", "tranId": 3,
         "symbol": "BTCUSDT",
         "income": "0", "asset": "USDT", "time": 2000}, funding,
    ]
    with pytest.raises(AccountingEvidenceError, match="funding_direction_unallocated"):
        reconcile_income_facts(task, rows,
            fx_value=lambda amount, asset, event: (Decimal(str(amount)), {}),
            income_complete=True)
    allocations = {"acct:FUNDING_FEE:10": {
        "direction_pools": pools, "batches": batches,
        "batch_allocations": {k: str(v) for k, v in expected.items()},
        "evidence_refs": ["position-snapshot:3000", "funding-pools:3000"],
    }}
    result = reconcile_income_facts(task, rows,
        fx_value=lambda amount, asset, event: (Decimal(str(amount)), {}),
        funding_allocations=allocations, income_complete=True)
    assert result["funding_usdt"] == Decimal("-0.008")
    allocations["acct:FUNDING_FEE:10"]["direction_pools"]["LONG"] = "-0.025"
    with pytest.raises(AccountingEvidenceError, match="not_conserved"):
        reconcile_income_facts(task, rows,
            fx_value=lambda amount, asset, event: (Decimal(str(amount)), {}),
            funding_allocations=allocations, income_complete=True)


def test_post_close_funding_is_not_charged_to_closed_batch_and_partial_window_blocks():
    task = {
        "account_scope": "acct", "batch_id": "B1", "symbol": "BTC/USDT:USDT",
        "income_start_ms": 1000, "close_time_ms": 2000,
        "trade_evidence": [
            {"trade_id": "e1", "role": "entry", "commission": "0",
             "commission_asset": "USDT", "time": 1000},
            {"trade_id": "x1", "role": "exit", "commission": "0",
             "commission_asset": "USDT", "time": 2000},
        ],
    }
    rows = [
        {"incomeType": "REALIZED_PNL", "tradeId": "x1", "tranId": 1,
         "symbol": "BTCUSDT", "income": "1", "asset": "USDT", "time": 2000},
        {"incomeType": "COMMISSION", "tradeId": "e1", "tranId": 2,
         "symbol": "BTCUSDT", "income": "0", "asset": "USDT", "time": 1000},
        {"incomeType": "COMMISSION", "tradeId": "x1", "tranId": 3,
         "symbol": "BTCUSDT", "income": "0", "asset": "USDT", "time": 2000},
        {"incomeType": "FUNDING_FEE", "tranId": 4, "symbol": "BTCUSDT",
         "income": "-0.1", "asset": "USDT", "time": 3000},
    ]
    fx = lambda amount, asset, event: (Decimal(str(amount)), {})
    result = reconcile_income_facts(task, rows, fx_value=fx, income_complete=True)
    assert result["funding_usdt"] == 0
    task["prior_reduction_unknown"] = True
    with pytest.raises(AccountingEvidenceError, match="partial_close_exposure_unknown"):
        reconcile_income_facts(task, rows, fx_value=fx, income_complete=True)


def test_request_budget_counts_failed_attempt_reservation_and_weight():
    budget = {"attempts": 0, "weight": 0}
    for _ in range(4):
        reserve_request(budget, 20)
    for _ in range(2):
        reserve_request(budget, 1)
    assert budget == {"attempts": 6, "weight": 82}
    with pytest.raises(RequestBudgetError, match="budget_exhausted"):
        reserve_request(budget, 1)
    heavy = {"attempts": 0, "weight": 0}
    for _ in range(3):
        reserve_request(heavy, 30)
    with pytest.raises(RequestBudgetError, match="budget_exhausted"):
        reserve_request(heavy, 1)


def test_later_unverifiable_rescan_revokes_authority_but_keeps_local_estimate():
    stats = {"trades": [{"dedup_key": "k", "net_pnl": 2.0,
                         "local_provisional_net_pnl": 0.5,
                         "accounting_status": "AUTHORITATIVE_FINAL",
                         "pnl_source": "binance_income_history"}]}
    assert mark_revision_pending(stats, "k", "funding_event_unallocated", "t")
    row = stats["trades"][0]
    assert row["net_pnl"] == 0.5
    assert row["pnl_not_authoritative"] is True
    assert row["accounting_status"] == "PENDING_RECONCILIATION"
    assert row["last_authoritative_projection"]["net_pnl"] == 2.0
