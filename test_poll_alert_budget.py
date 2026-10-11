# -*- coding: utf-8 -*-
"""Durable notification-budget tests; no TG/SMTP/network or exchange access."""
import concurrent.futures
import os
import tempfile
import threading

import trader_260725
from poll_alert_state import PollAlertBudget
from trader_260725 import CryptoTrader


def _manager(tmp_path):
    return PollAlertBudget(os.path.join(str(tmp_path), ".poll_alert.state.json"))


def test_poll_incident_caps_email_and_tg_including_recovery(tmp_path):
    budget = _manager(tmp_path)
    event_id, is_new = budget.open_or_update("batch-a", now=1000)
    assert event_id and is_new
    assert budget.open_or_update("batch-b", now=1001) == (event_id, False)
    assert budget.open_batches() == {"batch-a", "batch-b"}

    email_id = budget.reserve(event_id, "email", "initial", now=1002)
    assert email_id
    assert budget.finish(event_id, "email", email_id, "SUBMITTED", now=1003)
    assert budget.reserve(event_id, "email", "initial", now=5000) is None

    tg1 = budget.reserve(event_id, "tg", "initial", now=1002)
    assert tg1
    assert budget.finish(event_id, "tg", tg1, "ACCEPTED", now=1004)
    assert not budget.record_recovery_success(event_id, "batch-a", "full recovery", now=1880)
    assert not budget.record_recovery_success(event_id, "batch-b", "full recovery", now=1880)
    assert not budget.record_recovery_success(event_id, "batch-a", "full recovery", now=2000)
    assert budget.record_recovery_success(event_id, "batch-b", "full recovery", now=2000)

    tg2 = budget.reserve(event_id, "tg", "recovery", now=2001)
    assert tg2
    assert budget.finish(event_id, "tg", tg2, "ACCEPTED", now=2002)
    assert budget.reserve(event_id, "tg", "recovery", now=2003) is None
    assert len(budget.attempts(event_id, "email")) == 1
    assert len(budget.attempts(event_id, "tg")) == 2


def test_failed_tg_retry_is_cooled_and_restart_does_not_reset_budget(tmp_path):
    path = os.path.join(str(tmp_path), ".poll_alert.state.json")
    budget = PollAlertBudget(path)
    event_id, _ = budget.open_or_update("batch-a", now=10)
    tg1 = budget.reserve(event_id, "tg", "initial", now=20)
    assert tg1
    assert budget.finish(event_id, "tg", tg1, "FAILED", now=21)
    assert budget.reserve(event_id, "tg", "retry", now=919) is None

    restarted = PollAlertBudget(path)
    same_id, is_new = restarted.open_or_update("batch-a", now=30)
    assert same_id == event_id and not is_new
    assert restarted.reserve(event_id, "tg", "retry", now=920)


def test_inflight_or_unknown_attempt_is_never_reissued(tmp_path):
    budget = _manager(tmp_path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    tg = budget.reserve(event_id, "tg", "initial", now=2)
    assert tg
    assert budget.finish(event_id, "tg", tg, "UNKNOWN", now=3)
    assert not budget.finish(event_id, "tg", tg, "ACCEPTED", now=4)
    assert budget.attempts(event_id, "tg")[0]["status"] == "UNKNOWN"
    assert budget.reserve(event_id, "tg", "retry", now=5000) is None
    assert not budget.record_recovery_success(event_id, "batch-a", "recovered", now=6000)
    assert budget.record_recovery_success(event_id, "batch-a", "recovered", now=6120)
    assert budget.reserve(event_id, "tg", "recovery", now=6001) is None


def test_corrupt_or_unexpectedly_missing_state_fails_closed(tmp_path):
    path = os.path.join(str(tmp_path), ".poll_alert.state.json")
    budget = PollAlertBudget(path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    assert event_id

    with open(path, "w", encoding="utf-8") as stream:
        stream.write("not-json")
    damaged = PollAlertBudget(path)
    assert not damaged.available
    assert damaged.open_or_update("batch-a", now=2) == (None, False)

    os.remove(path)
    missing = PollAlertBudget(path)
    assert not missing.available


def test_concurrent_initial_reservation_is_single_winner(tmp_path):
    budget = _manager(tmp_path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    gate = threading.Barrier(8)

    def reserve(_):
        gate.wait()
        return budget.reserve(event_id, "email", "initial", now=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    winners = [item for item in results if item]
    assert len(winners) == 1
    assert len(budget.attempts(event_id, "email")) == 1


def test_real_poll_alert_entry_does_not_repeat_email_and_caps_tg(tmp_path, monkeypatch):
    state_path = os.path.join(str(tmp_path), ".poll_alert.state.json")
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._poll_alert_lock = threading.Lock()
    trader._poll_alert_active = False
    trader._poll_alert_attempted = {}
    trader._poll_degraded_batches = {"batch-a"}
    trader._poll_alert_budget = PollAlertBudget(state_path)
    trader._send_email_alert_calls = []
    trader._send_email_alert = lambda *a, **kw: (
        trader._send_email_alert_calls.append(kw) or True)
    trader._tg_results = []

    def fail_tg(_text):
        trader._tg_results.append("FAILED")
        return "FAILED"
    trader._send_poll_degraded_tg = fail_tg

    clock = [1000.0]
    monkeypatch.setattr(trader_260725.time, "time", lambda: clock[0])
    for _ in range(33):
        CryptoTrader._alert_poll_degraded(trader, 3, 120, "batch-a")
        clock[0] += 60

    assert len(trader._send_email_alert_calls) == 1
    assert len(trader._tg_results) == 2  # initial + one retry after 900s
    event_id = trader._poll_alert_budget.open_event_id()
    assert event_id
    assert len(trader._poll_alert_budget.attempts(event_id, "email")) == 1
    assert len(trader._poll_alert_budget.attempts(event_id, "tg")) == 2


def test_recovery_uses_remaining_tg_slot_and_never_email(tmp_path):
    budget = _manager(tmp_path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    tg1 = budget.reserve(event_id, "tg", "initial", now=2)
    assert tg1 and budget.finish(event_id, "tg", tg1, "ACCEPTED", now=3)
    assert not budget.record_recovery_success(event_id, "batch-a", "full recovery", now=4)
    assert budget.record_recovery_success(event_id, "batch-a", "full recovery", now=124)
    recovery = budget.reserve(event_id, "tg", "recovery", now=5)
    assert recovery
    assert budget.finish(event_id, "tg", recovery, "ACCEPTED", now=6)
    assert budget.reserve(event_id, "email", "initial", now=7) is None


def test_trader_recovery_path_uses_budgeted_plain_tg_only(tmp_path, monkeypatch):
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._poll_alert_lock = threading.Lock()
    trader._poll_alert_active = True
    trader._poll_degraded_batches = {"batch-a"}
    trader._poll_alert_budget = _manager(tmp_path)
    trader._send_email_alert_calls = []
    trader._send_email_alert = lambda *a, **kw: trader._send_email_alert_calls.append(kw)
    trader._tg_messages = []
    trader._send_poll_degraded_tg = lambda text: (
        trader._tg_messages.append(text) or "ACCEPTED")

    event_id, _ = trader._poll_alert_budget.open_or_update("batch-a", now=1)
    first = trader._poll_alert_budget.reserve(event_id, "tg", "initial", now=2)
    assert first and trader._poll_alert_budget.finish(
        event_id, "tg", first, "ACCEPTED", now=3)
    trader._poll_degraded_batches.clear()
    clock = [100.0]
    monkeypatch.setattr(trader_260725.time, "time", lambda: clock[0])
    CryptoTrader._finish_poll_alert_event(
        trader, "full existing recovery predicate passed", send_recovery=True,
        batch_id="batch-a")
    assert trader._poll_alert_budget.open_event_id() == event_id
    assert trader._tg_messages == []
    clock[0] = 220.0
    CryptoTrader._finish_poll_alert_event(
        trader, "full existing recovery predicate passed", send_recovery=True,
        batch_id="batch-a")

    history = trader._poll_alert_budget.attempts(event_id, "tg")
    assert [item["purpose"] for item in history] == ["initial", "recovery"]
    assert all(item["status"] == "ACCEPTED" for item in history)
    assert len(trader._tg_messages) == 1
    assert trader._send_email_alert_calls == []


def test_open_incident_rehydrates_degraded_batches_after_restart(tmp_path):
    state_path = os.path.join(str(tmp_path), ".poll_alert.state.json")
    first = PollAlertBudget(state_path)
    event_id, _ = first.open_or_update("batch-a", now=1)
    assert first.reserve(event_id, "email", "initial", now=2)

    restarted = CryptoTrader.__new__(CryptoTrader)
    restarted._poll_alert_lock = threading.Lock()
    restarted._poll_alert_budget = PollAlertBudget(state_path)
    restarted._poll_degraded_batches = set()
    restarted._poll_fail_streak = {}
    restarted._poll_first_fail_time = {}
    CryptoTrader._reconcile_poll_alert_batches(
        restarted, {"BTCUSDT": {"batch-a": {"is_active": True}}})

    assert restarted._poll_degraded_batches == {"batch-a"}
    assert restarted._poll_fail_streak["batch-a"] >= 1
    assert restarted._poll_alert_budget.open_event_id() == event_id


def test_failure_before_alert_threshold_interrupts_recovery_and_preserves_budget(
        tmp_path, monkeypatch):
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._poll_alert_lock = threading.Lock()
    trader._poll_alert_active = True
    trader._poll_degraded_batches = {"batch-a"}
    trader._poll_alert_attempted = {}
    trader._poll_alert_budget = _manager(tmp_path)
    trader._send_email_alert_calls = []
    trader._send_email_alert = lambda *a, **kw: (
        trader._send_email_alert_calls.append(kw) or True)
    trader._tg_messages = []
    trader._send_poll_degraded_tg = lambda text: (
        trader._tg_messages.append(text) or "ACCEPTED")
    clock = [1000.0]
    monkeypatch.setattr(trader_260725.time, "time", lambda: clock[0])

    CryptoTrader._alert_poll_degraded(trader, 3, 120, "batch-a")
    event_id = trader._poll_alert_budget.open_event_id()
    assert event_id
    assert len(trader._send_email_alert_calls) == 1
    assert len(trader._tg_messages) == 1

    # First complete success starts observation, but does not resolve yet.
    trader._poll_degraded_batches.clear()
    clock[0] = 2000.0
    CryptoTrader._finish_poll_alert_event(
        trader, "full success", send_recovery=True, batch_id="batch-a")
    assert trader._poll_alert_budget.open_event_id() == event_id
    assert len(trader._tg_messages) == 1

    # A first failed query breaks observation immediately, before the normal
    # three-round external-alert threshold is reached.
    clock[0] = 2010.0
    CryptoTrader._interrupt_poll_alert_recovery(trader, "batch-a")
    clock[0] = 2070.0
    CryptoTrader._interrupt_poll_alert_recovery(trader, "batch-a")
    clock[0] = 2130.0
    CryptoTrader._interrupt_poll_alert_recovery(trader, "batch-a")
    CryptoTrader._alert_poll_degraded(trader, 3, 130, "batch-a")
    assert trader._poll_alert_budget.open_event_id() == event_id
    assert len(trader._send_email_alert_calls) == 1
    assert len(trader._tg_messages) == 1

    # Positive control: two complete rounds spanning 120 stable seconds finish
    # the same event. A later failure after that stable recovery may open a new
    # event and consume a fresh email slot.
    trader._poll_degraded_batches.clear()
    clock[0] = 2200.0
    CryptoTrader._finish_poll_alert_event(
        trader, "full success", send_recovery=True, batch_id="batch-a")
    assert trader._poll_alert_budget.open_event_id() == event_id
    clock[0] = 2320.0
    CryptoTrader._finish_poll_alert_event(
        trader, "full success", send_recovery=True, batch_id="batch-a")
    assert trader._poll_alert_budget.open_event_id() is None
    assert len(trader._tg_messages) == 2

    clock[0] = 2441.0
    new_id, is_new = trader._poll_alert_budget.open_or_update("batch-a")
    assert is_new and new_id != event_id
    CryptoTrader._alert_poll_degraded(trader, 3, 130, "batch-a")
    assert len(trader._send_email_alert_calls) == 2


def test_cross_batch_failure_resets_window_and_each_batch_needs_two_rounds(tmp_path):
    budget = _manager(tmp_path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    assert budget.open_or_update("batch-b", now=2) == (event_id, False)
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=10)
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=130)
    assert budget.open_event_id() == event_id

    # B's first failure is below its normal alert threshold but still joins the
    # incident and resets A's apparent recovery.
    assert budget.note_poll_failure("batch-b", now=131)
    assert "batch-b" in budget.open_batches()
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=200)
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=320)
    assert not budget.record_recovery_success(event_id, "batch-b", "ok", now=321)
    assert budget.open_event_id() == event_id
    assert budget.record_recovery_success(event_id, "batch-b", "ok", now=440)
    assert budget.open_event_id() is None


def test_restart_resets_recovery_proof_but_keeps_attempt_budget(tmp_path):
    path = os.path.join(str(tmp_path), ".poll_alert.state.json")
    budget = PollAlertBudget(path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    email = budget.reserve(event_id, "email", "initial", now=2)
    assert email and budget.finish(event_id, "email", email, "SUBMITTED", now=3)
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=10)

    restarted = PollAlertBudget(path)
    assert restarted.reconcile_batches({"batch-a"}, now=500)
    state = restarted._find_event(restarted._state, event_id)
    assert state["recovery_observation"] is None
    assert len(restarted.attempts(event_id, "email")) == 1
    assert not restarted.record_recovery_success(event_id, "batch-a", "ok", now=501)
    assert restarted.record_recovery_success(event_id, "batch-a", "ok", now=621)
    assert restarted.open_event_id() is None
    assert restarted.reserve(event_id, "email", "initial", now=622) is None


def test_failure_shortly_after_stable_resolution_reopens_same_budget(tmp_path):
    budget = _manager(tmp_path)
    event_id, _ = budget.open_or_update("batch-a", now=1)
    email = budget.reserve(event_id, "email", "initial", now=2)
    assert email and budget.finish(event_id, "email", email, "SUBMITTED", now=3)
    assert not budget.record_recovery_success(event_id, "batch-a", "ok", now=10)
    assert budget.record_recovery_success(event_id, "batch-a", "ok", now=130)
    assert budget.open_event_id() is None

    # The first failure arrives within the existing 120-second quiet interval;
    # it reopens the same event immediately, even though alerting may be delayed.
    assert budget.note_poll_failure("batch-a", now=140)
    assert budget.open_event_id() == event_id
    assert budget.reserve(event_id, "email", "initial", now=270) is None
