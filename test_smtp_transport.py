"""Offline SMTP tests: credentials/message must never precede verified STARTTLS."""
import smtplib
import ssl
from unittest import mock

import pytest

import bot_runner
import smtp_transport
import trader_260725
from test_email_channels import patrol


class FakeSMTP:
    def __init__(self, failure=None):
        self.failure = failure
        self.events = []
        self.ehlo_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.events.append('close')

    def ehlo(self):
        self.ehlo_count += 1
        self.events.append('ehlo')
        if self.failure == ('ehlo_pre' if self.ehlo_count == 1 else 'ehlo_post'):
            return 550, b'EHLO rejected'
        return 250, b'OK'

    def has_extn(self, name):
        assert name == 'starttls'
        self.events.append('has_starttls')
        return self.failure != 'unsupported'

    def starttls(self, *, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        self.events.append('starttls')
        if self.failure == 'certificate':
            raise ssl.SSLCertVerificationError('hostname/certificate rejected')
        if self.failure == 'tls':
            raise ssl.SSLError('handshake failed')
        if self.failure == 'starttls_rejected':
            raise smtplib.SMTPResponseException(454, b'TLS unavailable')
        self.events.append('verified_tls')
        return 220, b'TLS established'

    def login(self, user, code):
        self.events.append('login')
        assert self.events.index('verified_tls') < self.events.index('login')
        if self.failure == 'auth':
            raise smtplib.SMTPAuthenticationError(535, b'authentication rejected')

    def sendmail(self, sender, recipients, message):
        self.events.append('sendmail')
        assert 'verified_tls' in self.events and 'login' in self.events
        if self.failure == 'send':
            raise smtplib.SMTPServerDisconnected('response lost after submission')
        if self.failure == 'recipient':
            return {recipients[0]: (550, b'refused')}
        return {}


def test_single_attempt_order_and_verification(monkeypatch):
    server = FakeSMTP()
    constructor = mock.Mock(return_value=server)
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    with mock.patch.object(smtplib, 'SMTP_SSL') as implicit:
        smtp_transport.send_qq_mail('sender', 'not-a-real-code', 'recipient', 'message', timeout=15)
    constructor.assert_called_once_with('smtp.qq.com', 587, timeout=15)
    implicit.assert_not_called()
    assert server.events == ['ehlo', 'has_starttls', 'starttls', 'verified_tls',
                             'ehlo', 'login', 'sendmail', 'close']


@pytest.mark.parametrize('failure', ['ehlo_pre', 'unsupported', 'starttls_rejected',
                                     'tls', 'certificate', 'ehlo_post'])
def test_security_failure_never_authenticates_or_submits(monkeypatch, failure):
    server = FakeSMTP(failure)
    constructor = mock.Mock(return_value=server)
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    with mock.patch.object(smtplib, 'SMTP_SSL') as implicit:
        with pytest.raises((smtplib.SMTPException, ssl.SSLError)):
            smtp_transport.send_qq_mail('sender', 'secret', 'recipient', 'message')
    assert 'login' not in server.events and 'sendmail' not in server.events
    constructor.assert_called_once()
    implicit.assert_not_called()


@pytest.mark.parametrize('failure', ['auth', 'send', 'recipient'])
def test_late_failure_is_not_retried(monkeypatch, failure):
    server = FakeSMTP(failure)
    constructor = mock.Mock(return_value=server)
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    with pytest.raises(smtplib.SMTPException):
        smtp_transport.send_qq_mail('sender', 'secret', 'recipient', 'message')
    constructor.assert_called_once()
    assert server.events.count('login') == 1
    assert server.events.count('sendmail') == (0 if failure == 'auth' else 1)


def test_connection_failure_never_retries_or_falls_back(monkeypatch):
    constructor = mock.Mock(side_effect=TimeoutError('connection timed out'))
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    with mock.patch.object(smtplib, 'SMTP_SSL') as implicit:
        with pytest.raises(TimeoutError):
            smtp_transport.send_qq_mail('sender', 'secret', 'recipient', 'message')
    constructor.assert_called_once()
    implicit.assert_not_called()


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()

    def join(self, **kwargs):
        pass

    def is_alive(self):
        return False


@pytest.mark.parametrize('owner', ['trader', 'runner', 'patrol'])
@pytest.mark.parametrize('failure', [None, 'unsupported', 'certificate', 'auth', 'send'])
def test_all_owners_report_actual_result_with_same_secure_transport(monkeypatch, owner, failure):
    env = {'QQ_MAIL_USER': 'offline@example.com', 'QQ_MAIL_AUTH_CODE': 'offline',
           'QQ_MAIL_TO': 'recipient@example.com', 'EMAIL_ALERT_ENABLED': 'true'}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(trader_260725.threading, 'Thread', InlineThread)
    monkeypatch.setattr(patrol, 'log', mock.Mock())
    server = FakeSMTP(failure)
    constructor = mock.Mock(return_value=server)
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    if owner == 'trader':
        result = trader_260725.CryptoTrader._send_email_alert(object(), 'offline', event='critical', wait=True)
    elif owner == 'runner':
        result = bot_runner.send_email_alert('offline', event='crash', wait=True)
    else:
        result = patrol.send_email(env, 'offline', 'offline', event='health')
    assert result is (failure is None)
    constructor.assert_called_once_with('smtp.qq.com', 587, timeout=15 if owner == 'patrol' else 10)
    if failure in ('unsupported', 'certificate'):
        assert 'login' not in server.events and 'sendmail' not in server.events


@pytest.mark.parametrize('owner', ['trader', 'runner', 'patrol'])
def test_email_gate_still_precedes_connection(monkeypatch, owner):
    env = {'EMAIL_ALERT_ENABLED': 'false', 'QQ_MAIL_USER': 'offline@example.com',
           'QQ_MAIL_AUTH_CODE': 'offline', 'QQ_MAIL_TO': 'recipient@example.com'}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(patrol, 'log', mock.Mock())
    constructor = mock.Mock()
    monkeypatch.setattr(smtplib, 'SMTP', constructor)
    if owner == 'trader':
        result = trader_260725.CryptoTrader._send_email_alert(object(), 'offline', event='critical', wait=True)
    elif owner == 'runner':
        result = bot_runner.send_email_alert('offline', event='crash', wait=True)
    else:
        result = patrol.send_email(env, 'offline', 'offline', event='health')
    assert result is False
    constructor.assert_not_called()
