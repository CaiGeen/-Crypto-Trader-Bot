"""Offline coverage: observation silence never weakens real risk notifications."""
import asyncio
import json
import ssl
from types import SimpleNamespace
from unittest import mock

import pytest

import bot_runner
import trader_260725
from trader_260725 import CryptoTrader


@pytest.mark.parametrize('enqueue_result', ['event-id', None])
def test_auth_alert_has_one_tg_owner_and_independent_email(enqueue_result):
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._enqueue_notify_event = mock.Mock(return_value=enqueue_result)
    trader._send_email_alert = mock.Mock(return_value=False)
    trader.send_tg_notification = mock.Mock(return_value=False)
    trader._save_auth_state = mock.Mock()

    trader._enter_auth_blocked('binance -2015 request ip: 119.237.242.181')

    trader._save_auth_state.assert_called_once_with(
        'BLOCKED', 'binance -2015 request ip: 119.237.242.181')
    trader._enqueue_notify_event.assert_called_once()
    kind, text = trader._enqueue_notify_event.call_args.args
    assert kind == 'auth_blocked' and '119.237.242.181' in text
    if enqueue_result:
        trader.send_tg_notification.assert_not_called()
        trader._send_email_alert.assert_called_once()
        assert trader._send_email_alert.call_args.kwargs['event'] == 'auth_blocked'
    else:
        trader.send_tg_notification.assert_called_once_with(text, level='critical')


def test_queue_exception_falls_back_without_unblocking():
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._enqueue_notify_event = mock.Mock(side_effect=OSError('disk unavailable'))
    trader.send_tg_notification = mock.Mock(side_effect=RuntimeError('TG unavailable'))
    trader._save_auth_state = mock.Mock()
    trader._enter_auth_blocked('binance -2015')
    trader._save_auth_state.assert_called_once_with('BLOCKED', 'binance -2015')
    trader.send_tg_notification.assert_called_once()


def test_email_failure_does_not_remove_durable_auth_event():
    trader = CryptoTrader.__new__(CryptoTrader)
    trader._enqueue_notify_event = mock.Mock(return_value='durable-event')
    trader._send_email_alert = mock.Mock(side_effect=RuntimeError('TLS failed'))
    trader.send_tg_notification = mock.Mock()
    trader._notify_auth_blocked('actual rejection')
    trader._enqueue_notify_event.assert_called_once_with('auth_blocked', 'actual rejection')
    trader.send_tg_notification.assert_not_called()


@pytest.mark.parametrize('tls_failure', [False, True])
def test_real_email_sender_reports_smtp_result_without_live_io(monkeypatch, tls_failure):
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self, **kwargs):
            pass

        def is_alive(self):
            return False

    monkeypatch.setenv('EMAIL_ALERT_ENABLED', 'true')
    monkeypatch.setenv('QQ_MAIL_USER', 'offline@example.com')
    monkeypatch.setenv('QQ_MAIL_AUTH_CODE', 'offline')
    monkeypatch.setenv('QQ_MAIL_TO', 'offline@example.com')
    monkeypatch.setattr(trader_260725.threading, 'Thread', InlineThread)
    # Patch both transports so this policy test remains safe before and after
    # the legacy SMTP_SSL sender is replaced by the required-STARTTLS helper.
    with mock.patch('smtplib.SMTP_SSL') as implicit_tls, \
            mock.patch('smtplib.SMTP') as starttls:
        implicit_server = implicit_tls.return_value.__enter__.return_value
        starttls_server = starttls.return_value.__enter__.return_value
        starttls_server.ehlo.return_value = (250, b'OK')
        starttls_server.has_extn.return_value = True
        starttls_server.sendmail.return_value = {}
        if tls_failure:
            implicit_tls.side_effect = ssl.SSLError('legacy TLS failure')
            starttls_server.starttls.side_effect = ssl.SSLError('TLS verification failed')
        result = CryptoTrader._send_email_alert(
            object(), 'offline risk', event='auth_blocked', wait=True)
        assert result is (not tls_failure)
        if starttls.called:
            starttls.assert_called_once_with('smtp.qq.com', 587, timeout=10)
            implicit_tls.assert_not_called()
            if tls_failure:
                starttls_server.login.assert_not_called()
                starttls_server.sendmail.assert_not_called()
            else:
                starttls_server.login.assert_called_once()
                starttls_server.sendmail.assert_called_once()
        else:
            implicit_tls.assert_called_once_with('smtp.qq.com', 465, timeout=10)
            starttls.assert_not_called()
            if tls_failure:
                implicit_server.login.assert_not_called()


@pytest.mark.parametrize('code', [-2015, -2014])
def test_real_api_wrapper_still_blocks_once_then_stops_network(tmp_path, monkeypatch, code):
    from test_auth_blocked import make_trader

    # make_trader redirects these globals; register originals for automatic restore.
    for name in ('STATE_FILE', 'AUTH_BLOCKED_FILE', 'NOTIFY_QUEUE_DIR_TRADER'):
        monkeypatch.setattr(trader_260725, name, getattr(trader_260725, name))
    trader, exchange = make_trader(tmp_path)
    exchange.fetch_positions.side_effect = Exception(
        f'binance code={code} invalid api-key, ip, or permissions request ip: 119.237.242.181')
    with pytest.raises(trader_260725.AuthBlockedError):
        trader._safe_api_call(exchange.fetch_positions)
    assert exchange.fetch_positions.call_count == 1
    assert trader._load_auth_state()['locked'] is True
    with pytest.raises(trader_260725.AuthBlockedError):
        trader._safe_api_call(exchange.create_order)
    exchange.create_order.assert_not_called()
    assert len(trader._emails) == 1
    assert trader._sent == []
    events = list((tmp_path / '.notify_queue').glob('*.notify'))
    assert len(events) == 1
    content = events[0].read_text(encoding='utf-8')
    assert content.startswith('auth_blocked|') and str(code) in content
    assert '119.237.242.181' in content


@pytest.mark.parametrize('already_silenced', [False, True])
def test_legacy_ip_queue_is_silent_and_evidence_retained(tmp_path, already_silenced):
    queue = tmp_path / 'queue'
    queue.mkdir()
    event = queue / 'old-ip.notify'
    content = 'ip_notify|observed IP changed, not an auth verdict'
    event.write_text(content, encoding='utf-8')
    state_file = tmp_path / 'state.json'
    if already_silenced:
        state_file.write_text(json.dumps({'old-ip': {'status': 'SILENCED'}}), encoding='utf-8')
    bot = SimpleNamespace(send_message=mock.AsyncMock())
    email = mock.Mock()
    for _ in range(2):
        asyncio.run(bot_runner._process_notify_queue_once(
            bot, 1, state_file=str(state_file), queue_dir=str(queue),
            audit_log=str(tmp_path / 'audit.log'), email_cb=email))
    bot.send_message.assert_not_called()
    email.assert_not_called()
    assert event.read_text(encoding='utf-8') == content
    state = json.loads(state_file.read_text(encoding='utf-8'))
    assert state['old-ip']['status'] == 'SILENCED'
    if not already_silenced:
        assert state['old-ip']['silenced_reason'] == 'non_actionable_ip_change'
        assert (tmp_path / 'audit.log').read_text(encoding='utf-8').count('POLICY_SILENCED') == 1


def test_auth_queue_delivers_once_and_preserves_ip(tmp_path):
    queue = tmp_path / 'queue'
    queue.mkdir()
    (queue / 'auth.notify').write_text('auth_blocked|binance -2015 request ip: 119.237.242.181', encoding='utf-8')
    bot = SimpleNamespace(send_message=mock.AsyncMock())
    email = mock.Mock()
    for _ in range(2):
        asyncio.run(bot_runner._process_notify_queue_once(
            bot, 1, state_file=str(tmp_path / 'state.json'), queue_dir=str(queue),
            audit_log=str(tmp_path / 'audit.log'), email_cb=email))
    bot.send_message.assert_awaited_once()
    assert '119.237.242.181' in bot.send_message.call_args.kwargs['text']
    email.assert_not_called()


@pytest.mark.parametrize('stop_channel', [False, True])
@pytest.mark.parametrize('auth_failure', [False, True])
def test_conflict_scan_keeps_fail_closed_without_duplicate_auth_alert(stop_channel, auth_failure):
    trader = CryptoTrader.__new__(CryptoTrader)
    trader.exchange = SimpleNamespace(fetch_open_orders=mock.Mock())
    error = (trader_260725.AuthBlockedError('already blocked') if auth_failure
             else RuntimeError('order data unknown'))
    trader._safe_api_call = mock.Mock(side_effect=[[], error] if stop_channel else error)
    trader.send_tg_notification = mock.Mock()
    assert trader._check_existing_conflicts('BTCUSDT', 'offline-batch', {}) is True
    assert trader._safe_api_call.call_count == (2 if stop_channel else 1)
    if auth_failure:
        trader.send_tg_notification.assert_not_called()
    else:
        trader.send_tg_notification.assert_called_once()
        assert trader.send_tg_notification.call_args.kwargs['level'] == 'critical'


@pytest.mark.parametrize('locked', [True, False])
def test_actual_rejected_signal_reply_identifies_auth_lock(locked):
    trader = SimpleNamespace(_ready=True, execute_signal=mock.Mock(return_value=None),
                             _load_auth_state=mock.Mock(return_value={'locked': locked}))
    context = SimpleNamespace(bot_data={'global_trader': trader})
    signal = SimpleNamespace(symbol='BTCUSDT', side='BUY', leverage=1,
                             take_profit=100, initial_stop_loss=90,
                             entries=[(95, 1)], stop_loss_steps=[90])
    reply = mock.AsyncMock()
    with mock.patch.object(bot_runner, 'TRADER_LOCK', asyncio.Lock()), \
            mock.patch.object(bot_runner, 'parse_signal_from_dict', return_value=signal), \
            mock.patch.object(bot_runner, '_signal_fingerprint', return_value='offline'), \
            mock.patch.object(bot_runner, '_check_and_record_dedup', return_value=(True, 'allowed')), \
            mock.patch.object(bot_runner, 'safe_reply', reply):
        asyncio.run(bot_runner.run_trader_execution(object(), context, signal_snapshot={}))
    trader.execute_signal.assert_called_once_with(signal)
    reply.assert_awaited_once()
    text = reply.call_args.args[1]
    if locked:
        assert '/auth_reset' in text and '鉴权封锁' in text and '可能触发' not in text
    else:
        assert '可能触发' in text and '/auth_reset' not in text
