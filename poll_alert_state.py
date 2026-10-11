# -*- coding: utf-8 -*-
"""Durable per-incident notification budget for poll-degradation alerts.

This module deliberately owns only the notification budget. It does not alter
polling, trading, protection, or the exchange state.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import uuid


POLL_ALERT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".poll_alert.state.json")
POLL_ALERT_MARKER_FILE = POLL_ALERT_STATE_FILE + ".initialized"
POLL_ALERT_SCHEMA = 1
POLL_ALERT_MAX_EMAIL_ATTEMPTS = 1
POLL_ALERT_MAX_TG_ATTEMPTS = 2
POLL_ALERT_TG_RETRY_SECONDS = 900
POLL_ALERT_RECOVERY_QUIET_SECONDS = 120
POLL_ALERT_RECOVERY_REQUIRED_ROUNDS = 2


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".poll_alert_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class PollAlertBudget:
    """Single-process, fail-closed state for one account-wide open incident."""

    def __init__(self, state_path: str | None = None):
        self.path = state_path or POLL_ALERT_STATE_FILE
        self.marker_path = self.path + ".initialized"
        self._lock = threading.RLock()
        self._available = True
        self._error: str | None = None
        self._state = {"schema_version": POLL_ALERT_SCHEMA, "events": []}
        self._load()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def error(self) -> str | None:
        return self._error

    def _fail(self, reason: str) -> None:
        self._available = False
        self._error = reason

    def _load(self) -> None:
        state_exists = os.path.exists(self.path)
        marker_exists = os.path.exists(self.marker_path)
        if not state_exists:
            if marker_exists:
                self._fail("state file missing after initialization")
            return
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            if (not isinstance(data, dict)
                    or data.get("schema_version") != POLL_ALERT_SCHEMA
                    or not isinstance(data.get("events"), list)):
                raise ValueError("unsupported or invalid schema")
            for event in data["events"]:
                if (not isinstance(event, dict)
                        or not isinstance(event.get("event_id"), str)
                        or event.get("status") not in ("OPEN", "RESOLVED", "CLOSED")
                        or not isinstance(event.get("batches"), list)
                        or not isinstance(event.get("attempts"), dict)
                        or not isinstance(event["attempts"].get("email", []), list)
                        or not isinstance(event["attempts"].get("tg", []), list)):
                    raise ValueError("invalid event record")
            if sum(e.get("status") == "OPEN" for e in data["events"]) > 1:
                raise ValueError("multiple open incidents")
            self._state = data
            if not marker_exists:
                _atomic_write(self.marker_path, "poll-alert-state-v1\n")
        except Exception as exc:
            self._fail(f"state unreadable: {type(exc).__name__}: {exc}")

    def _persist_candidate(self, candidate: dict) -> bool:
        if not self._available:
            return False
        try:
            # Establish the marker before the first state file. If the process
            # stops between these writes, startup fails closed rather than
            # treating a missing state file as a fresh budget.
            if not os.path.exists(self.marker_path):
                if os.path.exists(self.path):
                    raise RuntimeError("state exists without initialization marker")
                _atomic_write(self.marker_path, "poll-alert-state-v1\n")
            _atomic_write(self.path, json.dumps(
                candidate, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._state = candidate
            return True
        except Exception as exc:
            self._fail(f"state persistence failed: {type(exc).__name__}: {exc}")
            return False

    def open_or_update(self, batch_id: str, now: float | None = None) -> tuple[str | None, bool]:
        """Return (event_id, is_new); persist newly opened incidents/batch links."""
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available:
                return None, False
            candidate = copy.deepcopy(self._state)
            event = next((e for e in reversed(candidate["events"])
                          if e.get("status") == "OPEN"), None)
            if event is None and candidate["events"]:
                last = candidate["events"][-1]
                resolved_at = last.get("resolved_at")
                if (last.get("status") == "RESOLVED"
                        and isinstance(resolved_at, (int, float))
                        and now - resolved_at <= POLL_ALERT_RECOVERY_QUIET_SECONDS):
                    # Treat a brief relapse as the same incident; do not reset
                    # channel budgets or turn one network flap into new emails.
                    event = last
                    event["status"] = "OPEN"
                    event["reopened_at"] = now
                    event["recovery_observation"] = None
            is_new = event is None
            if event is None:
                event = {
                    "event_id": uuid.uuid4().hex,
                    "status": "OPEN",
                    "created_at": now,
                    "updated_at": now,
                    "batches": [],
                    "attempts": {"email": [], "tg": []},
                    "channel_skipped": {},
                    "recovery_observation": None,
                }
                candidate["events"].append(event)
            batch_id = str(batch_id) if batch_id is not None else "unknown"
            changed = is_new or batch_id not in event["batches"] or event.get("reopened_at") == now
            if batch_id not in event["batches"]:
                event["batches"].append(batch_id)
            event["updated_at"] = now
            if changed and not self._persist_candidate(candidate):
                return None, False
            return event["event_id"], is_new

    def open_batches(self) -> set[str]:
        with self._lock:
            if not self._available:
                return set()
            event = next((e for e in reversed(self._state["events"])
                          if e.get("status") == "OPEN"), None)
            return set(event.get("batches", [])) if event else set()

    def open_event_id(self) -> str | None:
        with self._lock:
            if not self._available:
                return None
            event = next((e for e in reversed(self._state["events"])
                          if e.get("status") == "OPEN"), None)
            return event.get("event_id") if event else None

    def attempts(self, event_id: str, channel: str) -> list[dict]:
        with self._lock:
            event = self._find_event(self._state, event_id)
            return copy.deepcopy((event or {}).get("attempts", {}).get(channel, []))

    @staticmethod
    def _find_event(state: dict, event_id: str) -> dict | None:
        return next((e for e in state.get("events", [])
                     if e.get("event_id") == event_id), None)

    def mark_skipped(self, event_id: str, channel: str, reason: str,
                     now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if event is None or channel not in ("email", "tg"):
                return False
            if event.get("channel_skipped", {}).get(channel):
                return True
            event.setdefault("channel_skipped", {})[channel] = {
                "reason": str(reason)[:200], "at": now}
            event["updated_at"] = now
            return self._persist_candidate(candidate)

    def reserve(self, event_id: str, channel: str, purpose: str,
                now: float | None = None) -> str | None:
        """Durably consume an attempt before external I/O; None means no send."""
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available or channel not in ("email", "tg"):
                return None
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if event is None:
                return None
            if purpose == "recovery":
                if event.get("status") != "RESOLVED":
                    return None
            elif event.get("status") != "OPEN":
                return None
            if event.get("channel_skipped", {}).get(channel):
                return None
            history = event.setdefault("attempts", {}).setdefault(channel, [])
            limit = (1 if channel == "email" else 2)
            if len(history) >= limit:
                return None
            if any(a.get("status") in ("RESERVED", "IN_FLIGHT", "UNKNOWN")
                   for a in history):
                return None
            if purpose == "initial" and history:
                return None
            if purpose == "retry":
                if channel != "tg" or not history or history[-1].get("status") != "FAILED":
                    return None
                if now - float(history[-1].get("at", now)) < POLL_ALERT_TG_RETRY_SECONDS:
                    return None
            elif purpose == "recovery":
                pass
            elif purpose != "initial":
                return None
            attempt_id = uuid.uuid4().hex
            history.append({"attempt_id": attempt_id, "purpose": purpose,
                            "status": "RESERVED", "at": now})
            event["updated_at"] = now
            return attempt_id if self._persist_candidate(candidate) else None

    def finish(self, event_id: str, channel: str, attempt_id: str,
               outcome: str, now: float | None = None) -> bool:
        """Record a result for an attempt still RESERVED/IN_FLIGHT.

        UNKNOWN is deliberately terminal in this version: late transport results
        are not callback-reconciled, so no second delivery is risked.
        """
        now = time.time() if now is None else float(now)
        if outcome not in ("IN_FLIGHT", "SUBMITTED", "ACCEPTED", "FAILED", "UNKNOWN", "SKIPPED"):
            return False
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if event is None:
                return False
            attempt = next((a for a in event.get("attempts", {}).get(channel, [])
                            if a.get("attempt_id") == attempt_id), None)
            if attempt is None or attempt.get("status") not in ("RESERVED", "IN_FLIGHT"):
                return False
            attempt["status"] = outcome
            attempt["result_at"] = now
            event["updated_at"] = now
            return self._persist_candidate(candidate)

    def resolve(self, event_id: str, reason: str,
                now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if event is None or event.get("status") != "OPEN":
                return False
            observation = event.get("recovery_observation")
            rounds = observation.get("success_rounds", {}) if isinstance(observation, dict) else {}
            if (not isinstance(observation, dict)
                    or now - float(observation.get("started_at", now))
                    < POLL_ALERT_RECOVERY_QUIET_SECONDS
                    or not event.get("batches")
                    or any(int(rounds.get(batch, 0)) < POLL_ALERT_RECOVERY_REQUIRED_ROUNDS
                           for batch in event["batches"])):
                return False
            event["status"] = "RESOLVED"
            event["resolution"] = str(reason)[:200]
            event["resolved_at"] = now
            event["updated_at"] = now
            return self._persist_candidate(candidate)

    def note_poll_failure(self, batch_id: str,
                          now: float | None = None) -> bool:
        """Interrupt a pending notification-recovery window on any poll failure."""
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = next((e for e in reversed(candidate["events"])
                          if e.get("status") == "OPEN"), None)
            reopened = False
            if event is None and candidate["events"]:
                last = candidate["events"][-1]
                resolved_at = last.get("resolved_at")
                if (last.get("status") == "RESOLVED"
                        and isinstance(resolved_at, (int, float))
                        and now - resolved_at <= POLL_ALERT_RECOVERY_QUIET_SECONDS):
                    # A relapse during the quiet interval belongs to the same
                    # incident even if its alert threshold is reached later.
                    event = last
                    event["status"] = "OPEN"
                    event["reopened_at"] = now
                    event["recovery_observation"] = None
                    reopened = True
            if event is None:
                return False
            batch_id = str(batch_id) if batch_id is not None else "unknown"
            added_batch = batch_id not in event.get("batches", [])
            if added_batch:
                event.setdefault("batches", []).append(batch_id)
            observation = event.get("recovery_observation")
            if not isinstance(observation, dict) and not added_batch and not reopened:
                return True
            event["recovery_observation"] = None
            event["last_recovery_interruption_at"] = now
            event["updated_at"] = now
            return self._persist_candidate(candidate)

    def record_recovery_success(self, event_id: str, batch_id: str,
                                reason: str, now: float | None = None,
                                quiet_seconds: float = POLL_ALERT_RECOVERY_QUIET_SECONDS,
                                required_rounds: int = POLL_ALERT_RECOVERY_REQUIRED_ROUNDS
                                ) -> bool:
        """Record a complete successful poll; resolve only after stable all-batch proof.

        Returns True only when every batch in the incident has at least the
        required consecutive full-success rounds and the uninterrupted
        observation window has elapsed. Any poll failure resets the window.
        """
        now = time.time() if now is None else float(now)
        batch_id = str(batch_id) if batch_id is not None else "unknown"
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if (event is None or event.get("status") != "OPEN"
                    or batch_id not in event.get("batches", [])):
                return False
            observation = event.get("recovery_observation")
            if not isinstance(observation, dict):
                observation = {"started_at": now, "success_rounds": {}}
            rounds = observation.setdefault("success_rounds", {})
            rounds[batch_id] = int(rounds.get(batch_id, 0)) + 1
            observation["last_success_at"] = now
            event["recovery_observation"] = observation
            event["updated_at"] = now

            all_batches = event.get("batches", [])
            stable = (now - float(observation.get("started_at", now))
                      >= float(quiet_seconds))
            enough_rounds = bool(all_batches) and all(
                int(rounds.get(affected_batch, 0)) >= int(required_rounds)
                for affected_batch in all_batches)
            if stable and enough_rounds:
                event["status"] = "RESOLVED"
                event["resolution"] = str(reason)[:200]
                event["resolved_at"] = now
                event["updated_at"] = now
                return self._persist_candidate(candidate)
            self._persist_candidate(candidate)
            return False

    def finish_batch_without_recovery(self, event_id: str, batch_id: str,
                                      reason: str,
                                      now: float | None = None) -> bool:
        """Retire one terminal batch without closing siblings' incident budget."""
        now = time.time() if now is None else float(now)
        batch_id = str(batch_id) if batch_id is not None else "unknown"
        with self._lock:
            if not self._available:
                return False
            candidate = copy.deepcopy(self._state)
            event = self._find_event(candidate, event_id)
            if event is None or event.get("status") != "OPEN":
                return False
            batches = event.get("batches", [])
            if batch_id not in batches:
                return True
            event["batches"] = [item for item in batches if item != batch_id]
            observation = event.get("recovery_observation")
            if isinstance(observation, dict):
                observation.get("success_rounds", {}).pop(batch_id, None)
            if not event["batches"]:
                event["status"] = "CLOSED"
                event["resolution"] = str(reason)[:200]
                event["resolved_at"] = now
            else:
                event.setdefault("terminal_batches", []).append({
                    "batch_id": batch_id,
                    "reason": str(reason)[:200],
                    "at": now,
                })
            event["updated_at"] = now
            return self._persist_candidate(candidate)

    def reconcile_batches(self, active_batch_ids: set[str],
                          now: float | None = None) -> set[str]:
        """Reattach an open event after restart; close it if none remain active."""
        now = time.time() if now is None else float(now)
        with self._lock:
            if not self._available:
                return set()
            candidate = copy.deepcopy(self._state)
            event = next((e for e in reversed(candidate["events"])
                          if e.get("status") == "OPEN"), None)
            if event is None:
                return set()
            retained = [b for b in event.get("batches", []) if b in active_batch_ids]
            if retained:
                if (retained != event.get("batches")
                        or isinstance(event.get("recovery_observation"), dict)):
                    event["batches"] = retained
                    # A restart or changed active-batch set breaks proof of a
                    # continuous, currently observed recovery window. Budgets
                    # and attempt history remain untouched.
                    event["recovery_observation"] = None
                    event["updated_at"] = now
                    if not self._persist_candidate(candidate):
                        return set()
                return set(retained)
            event["status"] = "CLOSED"
            event["resolution"] = "no affected active batches at startup reconciliation"
            event["resolved_at"] = now
            event["updated_at"] = now
            return set() if self._persist_candidate(candidate) else set()
