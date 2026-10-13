"""Offline regression tests for the optional Telegram diagnostics module."""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest import mock

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


@pytest.fixture
def runner(monkeypatch):
    # Import the actual PR18 runner with synthetic config, never machine credentials.
    monkeypatch.setenv('TG_BOT_TOKEN', '123456789:OfflineDiagnosticsTestToken000000')
    monkeypatch.setenv('TG_ALLOWED_USER_ID', '1')
    import bot_runner
    return bot_runner


def configure_recording(monkeypatch, mode):
    if mode == 'absent':
        monkeypatch.delitem(sys.modules, 'tg_diagnostics', raising=False)
        return
    monkeypatch.setitem(sys.modules, 'tg_diagnostics', td)
    failure = RuntimeError('recording failed') if mode == 'error' else asyncio.CancelledError()
    monkeypatch.setattr(td, 'record_reply', mock.Mock(side_effect=failure))
    monkeypatch.setattr(td, 'record_error_callback', mock.Mock(side_effect=failure))


@pytest.mark.parametrize('mode', ['absent', 'error', 'cancel'])
@pytest.mark.parametrize('callback', [False, True])
def test_actual_safe_reply_preserves_object_when_recording_is_unavailable(runner, monkeypatch, mode, callback):
    configure_recording(monkeypatch, mode)
    reply_object = object()
    message = SimpleNamespace(reply_text=mock.AsyncMock(return_value=reply_object))
    update = SimpleNamespace(message=None if callback else message,
                             callback_query=SimpleNamespace(message=message) if callback else None)
    result = asyncio.run(runner.safe_reply(update, 'original text', parse_mode='Markdown'))
    assert result is reply_object
    message.reply_text.assert_awaited_once_with('original text', parse_mode='Markdown', reply_markup=None)


@pytest.mark.parametrize('mode', ['absent', 'error', 'cancel'])
def test_actual_safe_reply_preserves_original_cancellation(runner, monkeypatch, mode):
    configure_recording(monkeypatch, mode)
    cancelled = asyncio.CancelledError('original cancellation')
    message = SimpleNamespace(reply_text=mock.AsyncMock(side_effect=cancelled))
    update = SimpleNamespace(message=message, callback_query=None)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(runner.safe_reply(update, 'text'))
    assert caught.value is cancelled
    message.reply_text.assert_awaited_once()


@pytest.mark.parametrize('mode', ['absent', 'error', 'cancel'])
def test_actual_error_handler_still_logs_and_replies(runner, monkeypatch, mode):
    configure_recording(monkeypatch, mode)
    business_error = RuntimeError('original application error')
    update = SimpleNamespace(message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=object())),
                             callback_query=None)
    with mock.patch.object(runner.logging, 'error') as log_error:
        asyncio.run(runner.error_handler(update, SimpleNamespace(error=business_error)))
    log_error.assert_called_once_with('❌ 未处理的异常:', exc_info=business_error)
    update.message.reply_text.assert_awaited_once()
    assert 'original application error' in update.message.reply_text.call_args.args[0]


@pytest.mark.parametrize('failed_probe', ['none', 'getUpdates', 'api', 'watch', 'process', 'import'])
def test_actual_main_keeps_independent_probes_and_polling(runner, monkeypatch, capsys,
                                                           failed_probe, tmp_path):
    calls = []
    config_dir = tmp_path / 'project'
    config_dir.mkdir()
    (config_dir / '.env').write_text(
        'TG_BOT_TOKEN=123456789:OfflineDiagnosticsTestToken000000\n'
        'TG_ALLOWED_USER_ID=1\n', encoding='utf-8')
    monkeypatch.setattr(runner, 'BASE_DIR', str(config_dir))
    app = mock.Mock()
    builder = mock.Mock()
    for method in ('token', 'request', 'get_updates_request', 'post_init'):
        getattr(builder, method).return_value = builder
    builder.build.return_value = app
    monkeypatch.setattr(runner, 'acquire_instance_lock', mock.Mock())
    monkeypatch.setattr(runner, 'log_effective_config', mock.Mock())
    requests = [object(), object()]
    request_constructor = mock.Mock(side_effect=requests)
    monkeypatch.setattr(runner, 'HTTPXRequest', request_constructor)
    monkeypatch.setattr(runner.Application, 'builder', mock.Mock(return_value=builder))
    trader = mock.Mock()
    monkeypatch.setattr(runner, 'CryptoTrader', trader)

    def attempt(label):
        calls.append(label)
        if failed_probe == label:
            raise RuntimeError('installation failed')

    if failed_probe == 'import':
        monkeypatch.setitem(sys.modules, 'tg_diagnostics', None)
    else:
        monkeypatch.setitem(sys.modules, 'tg_diagnostics', td)
        monkeypatch.setattr(td, 'install_poll_request_probe', lambda request, label: attempt(label))
        monkeypatch.setattr(td, 'install_updater_watch', lambda app, interval: attempt('watch'))
        monkeypatch.setattr(td, 'install_process_probe', lambda app: attempt('process'))
    runner.main()

    assert calls == ([] if failed_probe == 'import' else ['getUpdates', 'api', 'watch', 'process'])
    builder.request.assert_called_once_with(requests[0])
    builder.get_updates_request.assert_called_once_with(requests[1])
    app.add_error_handler.assert_called_once_with(runner.error_handler)
    app.run_polling.assert_called_once_with(
        bootstrap_retries=-1, allowed_updates=['message', 'callback_query'])
    trader.assert_not_called()  # fake polling does not run post_init/handlers
    output = capsys.readouterr().out
    if failed_probe == 'import':
        assert 'module unavailable' in output
        assert 'updater watch unavailable' not in output and 'process probe unavailable' not in output
