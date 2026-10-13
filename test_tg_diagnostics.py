"""Offline regression tests for the optional Telegram diagnostics module."""

import asyncio
import threading

import pytest

import tg_diagnostics as td


@pytest.fixture(autouse=True)
def diagnostics_file_is_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(td, "DIAG_FILE", str(tmp_path / "tg_diag.json"))
    yield
    monkeypatch.setattr(td, "DIAG_FILE", str(tmp_path / "tg_diag_final.json"))
    assert td.flush_and_wait(timeout=3)


def test_log_counter_is_shared_across_threads(monkeypatch):
    class CountingQueue:
        def __init__(self):
            self.accepted = 0

        def put_nowait(self, item):
            self.accepted += 1

    monkeypatch.setattr(td, "_RATE_WINDOW", 3600.0)
    monkeypatch.setattr(td, "_RATE_MAX", 10)
    counted = CountingQueue()
    monkeypatch.setattr(td, "_log_q", counted)
    with td._log_lock:
        td._log_rate.update(last=0.0, count=0)
        td._log_dropped["n"] = 0

    threads = [threading.Thread(target=lambda: [td._log("parallel") for _ in range(100)])
               for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert counted.accepted == 10
    assert td.snapshot()["log_dropped"] == 1190


def test_process_probe_preserves_result_and_original_exceptions():
    class App:
        async def process_update(self, update):
            if update == "error":
                raise ValueError("original")
            return update

    app = App()
    assert td.install_process_probe(app)
    assert asyncio.run(app.process_update("ok")) == "ok"

    try:
        asyncio.run(app.process_update("error"))
    except ValueError as exc:
        assert str(exc) == "original"
    else:
        raise AssertionError("original exception was swallowed")


def test_request_probe_preserves_result_exception_and_poll_facts():
    class Request:
        async def do_request(self, url, method="POST", request_data=None):
            if "raise" in url:
                raise RuntimeError("original")
            return 200, b'{"ok": true, "result": []}'

    request = Request()
    assert td.install_poll_request_probe(request)
    result = asyncio.run(request.do_request(
        url="https://api.example/bot12345678:abcdefghijklmnopqrst/getUpdates"))
    assert result == (200, b'{"ok": true, "result": []}')
    snap = td.snapshot()
    assert snap["poll_req_total"] >= 1
    assert snap["poll_success"] >= 1
    assert snap["req_trace"][-1]["resp"]["status"] == 200
    assert "12345678:abcdefghijklmnopqrst" not in str(snap["req_trace"])

    try:
        asyncio.run(request.do_request("https://example/raise"))
    except RuntimeError as exc:
        assert str(exc) == "original"
    else:
        raise AssertionError("original exception was swallowed")


def test_flush_and_wait_reports_actual_write_result(tmp_path, monkeypatch):
    monkeypatch.setattr(td, "DIAG_FILE", str(tmp_path / "diag.json"))
    assert td.flush_and_wait(timeout=3)
    assert (tmp_path / "diag.json").is_file()

    monkeypatch.setattr(td, "DIAG_FILE", str(tmp_path / "missing" / "diag.json"))
    assert td.flush_and_wait(timeout=3) is False


def test_redaction_never_persists_token():
    token = "123456789:abcdefghijklmnopqrstuvwxyz"
    assert token not in td._redact(f"https://api.telegram.org/bot{token}/getUpdates")
