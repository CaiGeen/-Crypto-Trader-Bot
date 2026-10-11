"""Pure reconciliation rules for Binance USDⓈ-M realized PnL evidence.

This module intentionally performs no network or persistence operations. Callers
must only promote its results after proving API-window completeness and durable
evidence ownership.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from typing import Iterable, Mapping


# Income History examples use eight decimal places. This permits at most one
# final displayed USDT unit of rounding drift; it is not scaled by batch count.
FUNDING_CONSERVATION_TOLERANCE = Decimal("0.00000001")
import copy


class AccountingEvidenceError(ValueError):
    """Evidence is malformed, incomplete, or cannot support authoritative PnL."""


class RequestBudgetError(RuntimeError):
    """The reconciliation pass reached its bounded request/weight budget."""


def reserve_request(budget: dict, weight: int, *, max_attempts: int = 6,
                    max_weight: int = 90) -> None:
    """Reserve one physical-attempt slot and endpoint weight before calling CCXT."""
    if not isinstance(budget, dict):
        raise RequestBudgetError("request_budget_missing")
    if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
        raise RequestBudgetError("request_weight_invalid")
    attempts = int(budget.get("attempts") or 0)
    used_weight = int(budget.get("weight") or 0)
    if attempts + 1 > max_attempts or used_weight + weight > max_weight:
        raise RequestBudgetError("pnl_api_budget_exhausted")
    budget["attempts"] = attempts + 1
    budget["weight"] = used_weight + weight


def decimal_value(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AccountingEvidenceError(f"{field}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AccountingEvidenceError(f"{field}_invalid")
    if not result.is_finite():
        raise AccountingEvidenceError(f"{field}_non_finite")
    return result


def income_identity(account_id: str, row: Mapping) -> tuple[str, bool]:
    """Return source key and whether the exchange supplied a complete identity.

    Hashes for rows missing tranId are quarantine/dedup hints only; they never
    establish uniqueness or query completeness.
    """
    if not isinstance(row, Mapping):
        raise AccountingEvidenceError("income_row_invalid")
    income_type = str(row.get("incomeType") or "").strip()
    if not income_type:
        raise AccountingEvidenceError("income_type_missing")
    tran_id = row.get("tranId")
    if tran_id not in (None, ""):
        return f"{account_id}:{income_type}:{tran_id}", True
    stable = {key: row.get(key) for key in (
        "symbol", "incomeType", "asset", "income", "time", "info", "tradeId"
    )}
    digest = hashlib.sha256(json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    return f"{account_id}:{income_type}:missing-id:{digest}", False


def convert_mark_minute(amount, asset: str, event_time_ms: int,
                        candles: Iterable, *, now_ms: int,
                        quote_asset: str = "USDT") -> tuple[Decimal, dict]:
    """Convert an amount using the exact completed UTC event-minute mark candle.

    CCXT OHLCV rows are [timestamp, open, high, low, close, volume]. No adjacent
    minute, spot-market, or current-price fallback is permitted.
    """
    qty = decimal_value(amount, "amount")
    asset = str(asset or "").upper()
    quote_asset = str(quote_asset or "").upper()
    if not asset or not quote_asset:
        raise AccountingEvidenceError("asset_missing")
    if asset == quote_asset:
        return qty, {"source": "identity", "event_time_ms": int(event_time_ms)}
    minute = (int(event_time_ms) // 60_000) * 60_000
    if int(now_ms) < minute + 60_000:
        raise AccountingEvidenceError("fx_candle_not_closed")
    exact = []
    for candle in candles or ():
        if not isinstance(candle, (list, tuple)) or len(candle) < 5:
            continue
        try:
            stamp = int(candle[0])
        except (TypeError, ValueError):
            continue
        if stamp == minute:
            exact.append(candle)
    if len(exact) != 1:
        raise AccountingEvidenceError("fx_event_minute_missing_or_ambiguous")
    close = decimal_value(exact[0][4], "fx_close")
    if close <= 0:
        raise AccountingEvidenceError("fx_close_non_positive")
    return qty * close, {
        "source": "binance_usdm_mark_1m_close",
        "base_asset": asset,
        "quote_asset": quote_asset,
        "event_time_ms": int(event_time_ms),
        "candle_timestamp_ms": minute,
        "close": str(close),
    }


def match_commissions(user_trades: Iterable[Mapping], income_rows: Iterable[Mapping],
                      *, tolerance: Decimal = Decimal("0")) -> dict[tuple[str, str], Decimal]:
    """Match native commission facts; return one signed cashflow per trade.

    userTrades commission is positive cost; Income COMMISSION is signed cashflow
    and must be negative. Both sources are required and must agree in asset and
    amount before the canonical fee is accepted.
    """
    tolerance = decimal_value(tolerance, "commission_tolerance")
    if tolerance < 0:
        raise AccountingEvidenceError("commission_tolerance_negative")
    trades_by_key = {}
    for row in user_trades or ():
        if not isinstance(row, Mapping):
            raise AccountingEvidenceError("user_trade_invalid")
        info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
        trade_id = str(info.get("id") or row.get("id") or "")
        asset = str(info.get("commissionAsset") or "").upper()
        qty = decimal_value(info.get("commission"), "trade_commission")
        if not trade_id or not asset or qty < 0:
            raise AccountingEvidenceError("trade_commission_identity_invalid")
        key = (trade_id, asset)
        if key in trades_by_key:
            raise AccountingEvidenceError("duplicate_user_trade")
        trades_by_key[key] = qty

    income_by_key = {}
    for row in income_rows or ():
        if not isinstance(row, Mapping) or row.get("incomeType") != "COMMISSION":
            continue
        trade_id = str(row.get("tradeId") or "")
        asset = str(row.get("asset") or "").upper()
        cashflow = decimal_value(row.get("income"), "income_commission")
        if not trade_id or not asset or cashflow > 0:
            raise AccountingEvidenceError("income_commission_identity_invalid")
        key = (trade_id, asset)
        if key in income_by_key:
            raise AccountingEvidenceError("duplicate_income_commission")
        income_by_key[key] = cashflow

    if set(trades_by_key) != set(income_by_key):
        raise AccountingEvidenceError("commission_counterpart_missing_or_unmatched")
    canonical = {}
    for key, cost in trades_by_key.items():
        cashflow = income_by_key[key]
        if abs(cost + cashflow) > tolerance:
            raise AccountingEvidenceError("commission_amount_mismatch")
        canonical[key] = cashflow
    return canonical


def allocate_funding(direction_income: Mapping[str, object], batches: Iterable[Mapping],
                     *, tolerance: Decimal = Decimal("0.00000001")) -> dict[str, Decimal]:
    """Allocate independently evidenced LONG/SHORT pools within same direction.

    direction_income must contain both LONG and SHORT entries, including an
    explicit zero for an empty pool. A net account amount is not sufficient.
    Each batch needs a unique id and positive, verified event-time notional.
    """
    tolerance = decimal_value(tolerance, "funding_tolerance")
    if not isinstance(direction_income, Mapping) or set(direction_income) != {"LONG", "SHORT"}:
        raise AccountingEvidenceError("funding_direction_pools_incomplete")
    pool_amounts = {side: decimal_value(direction_income[side], f"funding_{side.lower()}")
                    for side in ("LONG", "SHORT")}
    grouped = {"LONG": [], "SHORT": []}
    seen = set()
    for batch in batches or ():
        if not isinstance(batch, Mapping):
            raise AccountingEvidenceError("funding_batch_invalid")
        batch_id = str(batch.get("batch_id") or "")
        side = str(batch.get("position_side") or "").upper()
        if not batch_id or batch_id in seen or side not in grouped:
            raise AccountingEvidenceError("funding_batch_identity_invalid")
        seen.add(batch_id)
        notional = decimal_value(batch.get("verified_notional"), "funding_notional")
        if notional <= 0:
            raise AccountingEvidenceError("funding_notional_non_positive")
        grouped[side].append((batch_id, notional))

    result = {}
    for side in ("LONG", "SHORT"):
        amount = pool_amounts[side]
        positions = grouped[side]
        if not positions:
            if abs(amount) > tolerance:
                raise AccountingEvidenceError("funding_pool_without_batches")
            continue
        total = sum((notional for _, notional in positions), Decimal(0))
        allocated = []
        for batch_id, notional in sorted(positions):
            allocated.append((batch_id, amount * notional / total))
        # Deterministic exact residue to the lexically first batch; preserves
        # pool sign and conservation without mixing long/short exposures.
        residue = amount - sum((value for _, value in allocated), Decimal(0))
        first_id, first_value = allocated[0]
        result[first_id] = first_value + residue
        for batch_id, value in allocated[1:]:
            result[batch_id] = value
    if abs(sum(result.values(), Decimal(0)) - sum(pool_amounts.values(), Decimal(0))) > tolerance:
        raise AccountingEvidenceError("funding_allocation_not_conserved")
    return result


def build_single_direction_funding_allocation(
        task: Mapping, all_tasks: Mapping[str, Mapping], income_row: Mapping,
        mark_price, funding_rate, *, account_trade_audit: Mapping,
        active_batch_ids: Iterable[str] = (),
        tolerance: Decimal = FUNDING_CONSERVATION_TOLERANCE) -> dict:
    """Rebuild account exposures, derive direction pools from rate, then reconcile.

    Account-net Income is only a conservation check. A zero account-position
    baseline plus a complete userTrades audit excludes unexplained/manual fills.
    The conservation tolerance is one 1e-8 USDT Income History unit, never
    multiplied by the number of batches. Larger drift remains unallocated.

    KNOWN_LIMITATION: account-wide trade audit is capped at seven days and one
    response of fewer than 1000 fills; longer/denser histories remain
    UNALLOCATED. UPGRADE_TRIGGER: any confirmed batch baseline older than seven
    days at funding time, or an audit response reaching 1000 rows, requires a
    separately tested bounded cursor/pagination implementation before widening.
    """
    if not isinstance(task, Mapping) or not isinstance(all_tasks, Mapping):
        raise AccountingEvidenceError("funding_task_ledger_invalid")
    if not isinstance(income_row, Mapping):
        raise AccountingEvidenceError("funding_income_row_invalid")
    event_ms = int(income_row.get("time"))
    amount = decimal_value(income_row.get("income"), "funding_income")
    if str(income_row.get("asset") or "").upper() != "USDT":
        raise AccountingEvidenceError("funding_income_asset_not_quote")
    price = decimal_value(mark_price, "funding_mark_price")
    rate = decimal_value(funding_rate, "funding_rate")
    tolerance = decimal_value(tolerance, "funding_tolerance")
    if price <= 0 or tolerance < 0:
        raise AccountingEvidenceError("funding_mark_or_tolerance_invalid")
    active = {str(x) for x in (active_batch_ids or ())}
    expected_scope = str(task.get("account_scope") or "")
    expected_symbol = str(task.get("symbol") or "").replace("/", "").split(":", 1)[0]
    if not expected_scope or not expected_symbol:
        raise AccountingEvidenceError("funding_task_scope_missing")

    audit = account_trade_audit
    if not isinstance(audit, Mapping) or audit.get("complete") is not True:
        raise AccountingEvidenceError("funding_account_trade_audit_incomplete")
    try:
        audit_start = int(audit.get("start_ms"))
        audit_end = int(audit.get("end_ms"))
    except (TypeError, ValueError):
        raise AccountingEvidenceError("funding_account_trade_audit_window_missing")
    if audit_end != event_ms or audit_start >= audit_end \
            or audit_end - audit_start > 7 * 24 * 3600 * 1000:
        raise AccountingEvidenceError("funding_account_trade_audit_window_invalid")
    audited_ids = set()
    for row in audit.get("trades", ()):
        if not isinstance(row, Mapping):
            raise AccountingEvidenceError("funding_account_trade_audit_row_invalid")
        trade_id = str(row.get("trade_id") or "")
        order_id = str(row.get("order_id") or "")
        row_symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").upper()
        position_side = str(row.get("position_side") or "").upper()
        try:
            stamp = int(row.get("time"))
        except (TypeError, ValueError):
            raise AccountingEvidenceError("funding_account_trade_time_missing")
        if (not trade_id or not order_id or row_symbol != expected_symbol
                or side not in ("BUY", "SELL")
                or position_side not in ("LONG", "SHORT", "BOTH")
                or stamp < audit_start or stamp > event_ms or trade_id in audited_ids):
            raise AccountingEvidenceError("funding_account_trade_identity_invalid")
        if stamp == event_ms:
            raise AccountingEvidenceError("funding_account_trade_event_time_ambiguous")
        audited_ids.add(trade_id)

    batches_by_id: dict[str, dict] = {}
    earliest_baseline = None
    for task_key, candidate in all_tasks.items():
        if not isinstance(candidate, Mapping):
            continue
        if (str(candidate.get("account_scope") or "") != expected_scope
                or str(candidate.get("symbol") or "").replace("/", "").split(":", 1)[0]
                != expected_symbol):
            continue
        try:
            start = int(candidate.get("income_start_ms"))
            close = int(candidate.get("close_time_ms"))
        except (TypeError, ValueError):
            raise AccountingEvidenceError("funding_batch_lifetime_missing")
        if start > event_ms:
            continue
        batch_id = str(candidate.get("batch_id") or "")
        if not batch_id:
            raise AccountingEvidenceError("funding_batch_id_missing")
        if candidate.get("event_evidence_complete") is not True \
                or candidate.get("prior_reduction_unknown") is True \
                or candidate.get("funding_baseline_complete") is not True:
            raise AccountingEvidenceError("funding_batch_event_evidence_incomplete")
        baseline = candidate.get("funding_position_baseline")
        if not isinstance(baseline, Mapping):
            raise AccountingEvidenceError("funding_position_baseline_missing")
        try:
            baseline_ms = int(baseline.get("captured_at_ms"))
            long_qty = decimal_value(baseline.get("long_qty"), "funding_baseline_long")
            short_qty = decimal_value(baseline.get("short_qty"), "funding_baseline_short")
        except (TypeError, ValueError):
            raise AccountingEvidenceError("funding_position_baseline_invalid")
        if min(long_qty, short_qty) < 0 or baseline_ms >= event_ms:
            raise AccountingEvidenceError("funding_position_baseline_invalid")
        if earliest_baseline is None or baseline_ms < earliest_baseline[0]:
            earliest_baseline = (baseline_ms, long_qty, short_qty)
        group = batches_by_id.setdefault(batch_id, {
            "events": {}, "tasks": [], "start_ms": start, "close_ms": close,
            "reduction_total": None, "baseline": baseline,
        })
        if group["baseline"] != baseline:
            raise AccountingEvidenceError("funding_position_baseline_conflict")
        group["tasks"].append((str(task_key), candidate))
        group["start_ms"] = min(group["start_ms"], start)
        group["close_ms"] = max(group["close_ms"], close)
        reduction_total = decimal_value(
            candidate.get("realized_reduce_amount", "0"), "funding_reduction_total")
        group["reduction_total"] = reduction_total if group["reduction_total"] is None \
            else max(group["reduction_total"], reduction_total)
        fills = candidate.get("funding_events")
        reductions = candidate.get("reduction_events")
        if not isinstance(fills, list) or not isinstance(reductions, list):
            raise AccountingEvidenceError("funding_event_lists_missing")
        for ledger_role, source in (("fill", fills), ("reduction", reductions)):
            for raw in source:
                if not isinstance(raw, Mapping):
                    raise AccountingEvidenceError("funding_fill_invalid")
                trade_id = str(raw.get("trade_id") or "")
                order_id = str(raw.get("order_id") or "")
                row_symbol = str(raw.get("symbol") or "")
                side = str(raw.get("side") or "").upper()
                position_side = str(raw.get("position_side") or "").upper()
                try:
                    qty = decimal_value(raw.get("qty"), "funding_fill_qty")
                    time_ms = int(raw.get("time"))
                except (TypeError, ValueError):
                    raise AccountingEvidenceError("funding_fill_time_or_qty_invalid")
                if (not trade_id or not order_id or row_symbol != expected_symbol
                        or side not in ("BUY", "SELL")
                        or position_side not in ("LONG", "SHORT", "BOTH")
                        or time_ms <= 0 or qty <= 0):
                    raise AccountingEvidenceError("funding_fill_identity_incomplete")
                if time_ms == event_ms:
                    raise AccountingEvidenceError("funding_fill_event_time_ambiguous")
                payload = dict(raw)
                payload["_ledger_role"] = ledger_role
                previous = group["events"].get(trade_id)
                if previous is not None and previous != payload:
                    raise AccountingEvidenceError("funding_fill_identity_conflict")
                group["events"][trade_id] = payload

    if earliest_baseline is None:
        raise AccountingEvidenceError("funding_position_baseline_missing")
    known_batch_ids = set(batches_by_id)
    if not active.issubset(known_batch_ids):
        raise AccountingEvidenceError("funding_active_batch_evidence_missing")
    audit_start = int(audit.get("start_ms"))
    if audit_start != earliest_baseline[0]:
        raise AccountingEvidenceError("funding_account_trade_audit_start_mismatch")
    if earliest_baseline[1] != 0 or earliest_baseline[2] != 0:
        raise AccountingEvidenceError("funding_opening_baseline_nonzero_unattributed")

    # All exchange fills in this interval must be accounted for by the durable
    # bot ledger; unexpected trades are external/manual exposure, never guessed.
    expected_trade_ids = set()
    for group in batches_by_id.values():
        for trade_id, event in group["events"].items():
            if audit_start <= int(event["time"]) <= event_ms:
                if int(event["time"]) == audit_start:
                    raise AccountingEvidenceError("funding_baseline_trade_time_ambiguous")
                expected_trade_ids.add(trade_id)
    if audited_ids != expected_trade_ids:
        raise AccountingEvidenceError("funding_unexplained_account_trade")

    def _aggregate_at(cutoff_ms):
        aggregate = {"LONG": Decimal(0), "SHORT": Decimal(0)}
        for batch_id, group in batches_by_id.items():
            candidate = group["tasks"][0][1]
            batch_side = str(candidate.get("side") or "").upper()
            if batch_side not in ("BUY", "SELL"):
                raise AccountingEvidenceError("funding_batch_side_missing")
            direction = "LONG" if batch_side == "BUY" else "SHORT"
            entry_seen = False
            for event in sorted(group["events"].values(),
                                key=lambda x: (int(x["time"]), str(x["trade_id"]))):
                stamp = int(event["time"])
                if stamp >= cutoff_ms:
                    continue
                role = event.get("role") if event["_ledger_role"] == "fill" else "exit"
                side = str(event["side"]).upper()
                position_side = str(event["position_side"]).upper()
                qty = decimal_value(event["qty"], "funding_fill_qty")
                if position_side in ("LONG", "SHORT") and position_side != direction:
                    raise AccountingEvidenceError("funding_position_side_conflict")
                if role == "entry":
                    if side != ("BUY" if direction == "LONG" else "SELL"):
                        raise AccountingEvidenceError("funding_entry_side_conflict")
                    aggregate[direction] += qty
                    entry_seen = True
                elif role == "exit":
                    if side != ("SELL" if direction == "LONG" else "BUY") \
                            or aggregate[direction] < qty:
                        raise AccountingEvidenceError("funding_exit_not_conserved")
                    aggregate[direction] -= qty
                else:
                    raise AccountingEvidenceError("funding_fill_role_invalid")
            if group["start_ms"] <= event_ms:
                reductions_qty = sum((decimal_value(x["qty"], "funding_reduction_qty")
                                      for x in group["events"].values()
                                      if x["_ledger_role"] == "reduction"), Decimal(0))
                if reductions_qty != group["reduction_total"]:
                    raise AccountingEvidenceError("funding_reduction_ledger_mismatch")
                if cutoff_ms > group["start_ms"] and not entry_seen:
                    raise AccountingEvidenceError("funding_entry_evidence_missing")
        return aggregate

    # Every later account-wide baseline must match the fills reconstructed from
    # the earliest verified flat snapshot.
    checked_baselines = set()
    for group in batches_by_id.values():
        baseline = group["baseline"]
        baseline_ms = int(baseline["captured_at_ms"])
        baseline_key = (baseline_ms, baseline["long_qty"], baseline["short_qty"])
        if baseline_key in checked_baselines:
            continue
        checked_baselines.add(baseline_key)
        expected = _aggregate_at(baseline_ms)
        if (expected["LONG"] != decimal_value(baseline["long_qty"], "baseline_long")
                or expected["SHORT"] != decimal_value(baseline["short_qty"], "baseline_short")):
            raise AccountingEvidenceError("funding_position_baseline_mismatch")

    event_positions = _aggregate_at(event_ms)
    if event_positions["LONG"] == 0 and event_positions["SHORT"] == 0:
        raise AccountingEvidenceError("funding_empty_event_exposure")
    long_notional = event_positions["LONG"] * price
    short_notional = event_positions["SHORT"] * price
    pools = {"LONG": -long_notional * rate, "SHORT": short_notional * rate}
    if abs(sum(pools.values(), Decimal(0)) - amount) > tolerance:
        raise AccountingEvidenceError("funding_rate_pool_not_conserved")
    allocation_batches = []
    evidence_refs = []
    for batch_id, group in batches_by_id.items():
        candidate = group["tasks"][0][1]
        direction = "LONG" if str(candidate.get("side") or "").upper() == "BUY" else "SHORT"
        qty = Decimal(0)
        for event in group["events"].values():
            if int(event["time"]) >= event_ms:
                continue
            event_role = event.get("role") if event["_ledger_role"] == "fill" else "exit"
            delta = decimal_value(event["qty"], "funding_fill_qty")
            qty += delta if event_role == "entry" else -delta
            evidence_refs.append(f"trade:{batch_id}:{event['trade_id']}")
        if qty > 0:
            allocation_batches.append({"batch_id": batch_id,
                                      "position_side": direction,
                                      "verified_notional": str(qty * price)})
    allocations = allocate_funding(pools, allocation_batches)
    return {
        "direction_pools": {key: str(value) for key, value in pools.items()},
        "batches": allocation_batches,
        "batch_allocations": {key: str(value) for key, value in allocations.items()},
        "evidence_refs": sorted(set(evidence_refs)),
        "mark_price": str(price), "funding_rate": str(rate),
        "event_time_ms": event_ms,
        "account_trade_ids": sorted(audited_ids),
        "account_trade_audit_start_ms": audit_start,
    }


def authoritative_net(realized, funding, commission, rebate, *, evidence_complete: bool,
                      valuation_complete: bool, allocation_complete: bool) -> Decimal | None:
    if not (evidence_complete and valuation_complete and allocation_complete):
        return None
    values = [decimal_value(v, "pnl_component") for v in
              (realized, funding, commission, rebate)]
    return sum(values, Decimal(0))


def reconcile_income_facts(task: Mapping, income_rows: Iterable[Mapping], *,
                           fx_value, funding_allocations: Mapping | None = None,
                           income_complete: bool) -> dict:
    """Reduce one close's durable fill evidence and Income History rows.

    ``fx_value(amount, asset, event_time_ms)`` must return a signed USDT Decimal
    and persisted quote evidence. Funding rows require caller-provided direction
    pool allocations; an account net amount is deliberately insufficient.
    """
    if not isinstance(task, Mapping):
        raise AccountingEvidenceError("task_invalid")
    fills = task.get("trade_evidence")
    if not isinstance(fills, list) or not fills:
        raise AccountingEvidenceError("trade_evidence_missing")
    trades = [row for row in fills if isinstance(row, Mapping)
              and row.get("trade_id") and row.get("role") in ("entry", "exit")]
    if len(trades) != len(fills):
        raise AccountingEvidenceError("trade_evidence_identity_incomplete")
    entry_ids = {str(row["trade_id"]) for row in trades if row.get("role") == "entry"}
    exit_ids = {str(row["trade_id"]) for row in trades if row.get("role") == "exit"}
    all_ids = entry_ids | exit_ids
    if not entry_ids or not exit_ids or len(all_ids) != len(trades):
        raise AccountingEvidenceError("trade_identity_or_role_incomplete")

    account_scope = str(task.get("account_scope") or "")
    if not account_scope:
        raise AccountingEvidenceError("account_scope_missing")
    expected_symbol = str(task.get("symbol") or "").replace("/", "").split(":", 1)[0]
    if not expected_symbol:
        raise AccountingEvidenceError("task_symbol_missing")
    rows = []
    source_rows = {}
    for row in (income_rows or ()):
        if not isinstance(row, Mapping):
            raise AccountingEvidenceError("income_row_invalid")
        source_key, identity_complete = income_identity(account_scope, row)
        if not identity_complete:
            raise AccountingEvidenceError("income_tran_id_missing")
        if row.get("incomeType") not in (
                "REALIZED_PNL", "FUNDING_FEE", "COMMISSION", "COMMISSION_REBATE"):
            raise AccountingEvidenceError("income_type_unrecognized")
        if str(row.get("symbol") or "") != expected_symbol:
            raise AccountingEvidenceError("income_symbol_unverified")
        prior = source_rows.get(source_key)
        if prior is not None:
            if prior != row:
                raise AccountingEvidenceError("income_identity_conflict")
            continue
        source_rows[source_key] = row
        rows.append(row)
    by_type = {}
    for row in rows:
        by_type.setdefault(str(row.get("incomeType") or ""), []).append(row)

    realized_rows = [row for row in by_type.get("REALIZED_PNL", ())
                     if str(row.get("tradeId") or "") in exit_ids]
    for row in realized_rows:
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol != expected_symbol:
            raise AccountingEvidenceError("realized_pnl_symbol_unverified")
        trade = next(x for x in trades if str(x["trade_id"]) == str(row.get("tradeId")))
        try:
            income_minute = int(row["time"]) // 60_000
            trade_minute = int(trade["time"]) // 60_000
        except (KeyError, TypeError, ValueError):
            raise AccountingEvidenceError("realized_pnl_time_missing")
        if income_minute != trade_minute:
            raise AccountingEvidenceError("realized_pnl_time_unverified")
    realized_by_trade = {str(row.get("tradeId")): row for row in realized_rows}
    if len(realized_by_trade) != len(realized_rows):
        raise AccountingEvidenceError("duplicate_realized_pnl_trade")
    if set(realized_by_trade) != exit_ids:
        raise AccountingEvidenceError("realized_pnl_counterpart_missing")
    realized_usdt = Decimal(0)
    fx_proofs = []
    for row in realized_rows:
        amount, proof = fx_value(row.get("income"), row.get("asset"), int(row["time"]))
        realized_usdt += amount
        fx_proofs.append(proof)

    relevant_fills = [dict(row, info={
        "id": row.get("trade_id"), "commission": row.get("commission"),
        "commissionAsset": row.get("commission_asset"),
    }) for row in trades]
    own_commission_rows = [row for row in by_type.get("COMMISSION", ())
                           if str(row.get("tradeId") or "") in all_ids]
    for row in own_commission_rows:
        if str(row.get("symbol") or "") != expected_symbol:
            raise AccountingEvidenceError("commission_symbol_unverified")
        trade = next(x for x in trades if str(x["trade_id"]) == str(row.get("tradeId")))
        try:
            income_minute = int(row["time"]) // 60_000
            trade_minute = int(trade["time"]) // 60_000
        except (KeyError, TypeError, ValueError):
            raise AccountingEvidenceError("commission_time_missing")
        if income_minute != trade_minute:
            raise AccountingEvidenceError("commission_time_unverified")
    commission_by_trade = match_commissions(relevant_fills, own_commission_rows)
    commission_usdt = Decimal(0)
    commission_entry_usdt = Decimal(0)
    commission_exit_usdt = Decimal(0)
    role_by_trade = {str(row["trade_id"]): row["role"] for row in trades}
    for (trade_id, asset), cashflow in commission_by_trade.items():
        row = next(r for r in own_commission_rows
                   if str(r.get("tradeId")) == trade_id
                   and str(r.get("asset") or "").upper() == asset)
        value, proof = fx_value(cashflow, row.get("asset"), int(row["time"]))
        commission_usdt += value
        if role_by_trade.get(trade_id) == "entry":
            commission_entry_usdt += value
        elif role_by_trade.get(trade_id) == "exit":
            commission_exit_usdt += value
        else:
            raise AccountingEvidenceError("commission_trade_role_missing")
        fx_proofs.append(proof)

    rebate_usdt = Decimal(0)
    for row in by_type.get("COMMISSION_REBATE", ()):
        trade_id = str(row.get("tradeId") or "")
        if not trade_id:
            raise AccountingEvidenceError("rebate_trade_identity_missing")
        if trade_id not in all_ids:
            continue
        value, proof = fx_value(row.get("income"), row.get("asset"), int(row["time"]))
        rebate_usdt += value
        fx_proofs.append(proof)

    funding_rows = []
    try:
        close_time_ms = int(task.get("close_time_ms"))
        income_start_ms = int(task.get("income_start_ms"))
    except (TypeError, ValueError):
        close_time_ms = income_start_ms = None
    for row in by_type.get("FUNDING_FEE", ()):
        try:
            event_ms = int(row.get("time"))
        except (TypeError, ValueError):
            raise AccountingEvidenceError("funding_event_time_missing")
        if close_time_ms is None or income_start_ms is None:
            raise AccountingEvidenceError("funding_exposure_window_missing")
        if event_ms < income_start_ms:
            continue
        if event_ms > close_time_ms:
            if task.get("prior_reduction_unknown"):
                raise AccountingEvidenceError("funding_after_partial_close_exposure_unknown")
            continue
        funding_rows.append(row)
    funding_usdt = Decimal(0)
    if funding_rows:
            if not isinstance(funding_allocations, Mapping):
                raise AccountingEvidenceError("funding_direction_unallocated")
            by_income_key = {str(k): v for k, v in funding_allocations.items()}
            for row in funding_rows:
                key, identified = income_identity(account_scope, row)
                allocation = by_income_key.get(key)
                if not identified or not isinstance(allocation, Mapping):
                    raise AccountingEvidenceError("funding_event_unallocated")
                pools = allocation.get("direction_pools")
                batch_allocations = allocation.get("batch_allocations")
                batches = allocation.get("batches")
                refs = allocation.get("evidence_refs")
                if (not isinstance(pools, Mapping)
                        or set(pools) != {"LONG", "SHORT"}
                        or not isinstance(batch_allocations, Mapping)
                        or not isinstance(batches, list)
                        or not isinstance(refs, list) or not refs):
                    raise AccountingEvidenceError("funding_direction_evidence_incomplete")
                total_native = sum((decimal_value(pools[s], "funding_pool")
                                    for s in ("LONG", "SHORT")), Decimal(0))
                event_native = decimal_value(row.get("income"), "funding_income")
                if total_native != event_native:
                    raise AccountingEvidenceError("funding_direction_pool_not_conserved")
                expected_allocations = allocate_funding(pools, batches)
                normalized_allocations = {
                    str(batch_id): decimal_value(value, "funding_batch_allocation")
                    for batch_id, value in batch_allocations.items()
                }
                if expected_allocations != normalized_allocations:
                    raise AccountingEvidenceError("funding_batch_allocation_unverified")
                batch_id = str(task.get("batch_id") or "")
                if batch_id not in normalized_allocations:
                    continue
                allocated_native = normalized_allocations[batch_id]
                allocated_usdt, proof = fx_value(
                    allocated_native, row.get("asset"), int(row["time"]))
                funding_usdt += allocated_usdt
                fx_proofs.append(proof)

    if not income_complete:
        raise AccountingEvidenceError("income_window_incomplete")
    return {
        "realized_usdt": realized_usdt,
        "funding_usdt": funding_usdt,
        "commission_usdt": commission_usdt,
        "commission_entry_usdt": commission_entry_usdt,
        "commission_exit_usdt": commission_exit_usdt,
        "rebate_usdt": rebate_usdt,
        "income_complete": True,
        "commission_reconciled": True,
        "funding_allocated": True,
        "fx_complete": True,
        "fx_proofs": fx_proofs,
    }


def apply_authoritative_revision(stats: dict, dedup_key: str, revision: Mapping) -> bool:
    """Idempotently promote one provisional trade row from verified evidence.

    The caller must persist the returned stats atomically. An incomplete revision
    is retained for audit but never replaces the display/risk projection.
    """
    if not isinstance(stats, dict) or not isinstance(revision, Mapping):
        raise AccountingEvidenceError("revision_input_invalid")
    revision_id = str(revision.get("revision_id") or "")
    if not revision_id:
        raise AccountingEvidenceError("revision_id_missing")
    trades = stats.get("trades")
    if not isinstance(trades, list):
        raise AccountingEvidenceError("trade_stats_invalid")
    matches = [row for row in trades if isinstance(row, dict)
               and row.get("dedup_key") == dedup_key]
    if len(matches) != 1:
        raise AccountingEvidenceError("trade_record_missing_or_ambiguous")
    record = matches[0]
    accounting = stats.setdefault("accounting_v2", {})
    if not isinstance(accounting, dict):
        raise AccountingEvidenceError("accounting_state_invalid")
    revisions = accounting.setdefault("revisions", {})
    if not isinstance(revisions, dict):
        raise AccountingEvidenceError("revision_ledger_invalid")
    revision_key = f"{dedup_key}:{revision_id}"
    if revision_key in revisions:
        return True
    snapshot = copy.deepcopy(record)
    evidence_complete = revision.get("income_complete") is True
    commission_ok = revision.get("commission_reconciled") is True
    funding_ok = revision.get("funding_allocated") is True
    fx_ok = revision.get("fx_complete") is True
    net = None
    entry_commission = exit_commission = None
    if evidence_complete and commission_ok and funding_ok and fx_ok:
        commission_split_ok = all(
            key in revision for key in ("commission_entry_usdt", "commission_exit_usdt"))
        if not commission_split_ok:
            raise AccountingEvidenceError("commission_role_totals_missing")
        entry_commission = decimal_value(
            revision["commission_entry_usdt"], "commission_entry_usdt")
        exit_commission = decimal_value(
            revision["commission_exit_usdt"], "commission_exit_usdt")
        total_commission = decimal_value(revision.get("commission_usdt"),
                                         "commission_usdt")
        if entry_commission > 0 or exit_commission > 0 \
                or entry_commission + exit_commission != total_commission:
            raise AccountingEvidenceError("commission_role_totals_conflict")
        net = authoritative_net(
            revision.get("realized_usdt"), revision.get("funding_usdt"),
            revision.get("commission_usdt"), revision.get("rebate_usdt"),
            evidence_complete=True, valuation_complete=True,
            allocation_complete=True)
    rounded_net = (net.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
                   if net is not None else None)
    revisions[revision_key] = {
        "dedup_key": dedup_key,
        "revision_id": revision_id,
        "received_at": revision.get("received_at"),
        "status": "AUTHORITATIVE_FINAL" if net is not None else "PENDING_RECONCILIATION",
        "prior_record": snapshot,
        "evidence_refs": list(revision.get("evidence_refs") or []),
        "fx_proofs": copy.deepcopy(revision.get("fx_proofs") or []),
        "components": {
            "realized_usdt": str(revision.get("realized_usdt")),
            "funding_usdt": str(revision.get("funding_usdt")),
            "commission_usdt": str(revision.get("commission_usdt")),
            "rebate_usdt": str(revision.get("rebate_usdt")),
        },
        "valuation_label": revision.get("valuation_label") or "USDT",
    }
    if net is None:
        record["pnl_not_authoritative"] = True
        record["accounting_status"] = "PENDING_RECONCILIATION"
        return False
    record.update({
        "net_pnl": float(rounded_net),
        "net_pnl_unrounded": str(net),
        "pnl_source": "binance_income_history",
        "accounting_status": "AUTHORITATIVE_FINAL",
        "income_complete": True,
        "commission_reconciled": True,
        "funding_allocated": True,
        "fx_complete": True,
        "accounting_revision": revision_id,
        "pnl_not_authoritative": False,
        "realized_usdt": str(decimal_value(revision["realized_usdt"], "realized_usdt")),
        "funding_usdt": str(decimal_value(revision["funding_usdt"], "funding_usdt")),
        "commission_cashflow_usdt": str(decimal_value(revision["commission_usdt"], "commission_usdt")),
        "rebate_usdt": str(decimal_value(revision["rebate_usdt"], "rebate_usdt")),
        "valuation_label": revision.get("valuation_label") or "USDT",
        "entry_fee": float(-entry_commission),
        "entry_fee_total": float(-entry_commission),
        "entry_fee_source": "actual",
        "entry_note": "Income History matched to userTrades",
        "exit_fee": float(-exit_commission),
        "exit_fee_source": "actual",
        "exit_note": "Income History matched to userTrades",
        "fee_note": "",
    })
    record.pop("fee_metadata_error", None)
    return True


def mark_revision_pending(stats: dict, dedup_key: str, reason: str,
                         observed_at: str | None = None) -> bool:
    """Revoke a stale authoritative projection when a later rescan conflicts."""
    if not isinstance(stats, dict) or not isinstance(stats.get("trades"), list):
        raise AccountingEvidenceError("trade_stats_invalid")
    matches = [row for row in stats["trades"] if isinstance(row, dict)
               and row.get("dedup_key") == dedup_key]
    if len(matches) != 1:
        raise AccountingEvidenceError("trade_record_missing_or_ambiguous")
    record = matches[0]
    if record.get("accounting_status") == "AUTHORITATIVE_FINAL":
        record["last_authoritative_projection"] = {
            "net_pnl": record.get("net_pnl"),
            "accounting_revision": record.get("accounting_revision"),
            "pnl_source": record.get("pnl_source"),
        }
    if "local_provisional_net_pnl" in record:
        record["net_pnl"] = record["local_provisional_net_pnl"]
    record["pnl_source"] = "local_provisional"
    record["pnl_not_authoritative"] = True
    record["accounting_status"] = "PENDING_RECONCILIATION"
    record["last_reconciliation_error"] = str(reason)[:200]
    accounting = stats.setdefault("accounting_v2", {})
    if not isinstance(accounting, dict):
        raise AccountingEvidenceError("accounting_state_invalid")
    pending = accounting.setdefault("pending_revisions", {})
    if not isinstance(pending, dict):
        raise AccountingEvidenceError("pending_revision_ledger_invalid")
    pending[f"{dedup_key}:{hashlib.sha256(str(reason).encode()).hexdigest()[:16]}"] = {
        "dedup_key": dedup_key, "reason": str(reason)[:200],
        "observed_at": observed_at,
    }
    return True
