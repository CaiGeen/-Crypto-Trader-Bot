import ast
import os
import unittest
from pathlib import Path
from unittest import mock

import bot_runner
import email_gate
import trader_260725


class NotificationEmailPolicyTests(unittest.TestCase):
    def test_ordinary_and_unknown_events_never_email(self):
        for position_gate in ('true', 'false'):
            for event in ('generic', 'ip_change', 'unknown', 'htalth', '', None):
                with self.subTest(event=event, position_gate=position_gate):
                    allowed, _ = email_gate.should_send_email(
                        env={'EMAIL_ALERT_ONLY_WITH_POSITION': position_gate},
                        event=event)
                    self.assertFalse(allowed)

    def test_serious_events_and_explicit_exceptions_remain_available(self):
        for event in email_gate.FATAL_EVENTS | {'daily_report', 'selftest'}:
            with self.subTest(event=event):
                allowed, _ = email_gate.should_send_email(
                    env={'EMAIL_ALERT_ONLY_WITH_POSITION': 'true'},
                    state_path='does-not-exist.json', event=event)
                self.assertTrue(allowed)
                allowed, _ = email_gate.should_send_email(
                    env={'EMAIL_ALERT_ENABLED': 'false'}, event=event)
                self.assertFalse(allowed)

    def test_ip_rotation_only_records_without_any_external_notification(self):
        fake = mock.MagicMock()
        fake.last_known_ip = '119.237.242.94'
        fake._save_last_ip.side_effect = lambda ip: setattr(fake, 'last_known_ip', ip)
        fake._try_async_send.return_value = True
        for ip in ('219.76.133.122', '119.237.242.94', '219.76.133.122'):
            trader_260725.CryptoTrader._record_ip_change(fake, ip, 'periodic_check')
        self.assertEqual(fake.last_known_ip, '219.76.133.122')
        self.assertEqual(fake._save_last_ip.call_count, 3)
        fake._try_async_send.assert_not_called()
        fake._fallback_notify_file.assert_not_called()
        fake._enqueue_notify_event.assert_not_called()
        fake._send_email_alert.assert_not_called()

    def test_legacy_ip_fallback_never_enqueues(self):
        fake = mock.MagicMock()
        trader_260725.CryptoTrader._fallback_notify_file(fake, '119.237.242.94', 'periodic_check')
        fake._enqueue_notify_event.assert_not_called()
        fake._send_email_alert.assert_not_called()

    def test_warning_does_not_email_but_critical_does(self):
        fake = mock.MagicMock(tg_bot=None, chat_id=None, loop=None)
        trader_260725.CryptoTrader.send_tg_notification(fake, 'ordinary rejection', level='warning')
        fake._send_email_alert.assert_not_called()
        trader_260725.CryptoTrader.send_tg_notification(fake, 'auth blocked', level='critical')
        self.assertEqual(fake._send_email_alert.call_args.kwargs['event'], 'critical')

    def test_all_three_smtp_owners_block_unclassified_before_io(self):
        from test_email_channels import patrol
        with mock.patch.dict(os.environ, {'EMAIL_ALERT_ENABLED': 'true',
                'EMAIL_ALERT_ONLY_WITH_POSITION': 'false',
                'QQ_MAIL_USER': 'test@example.com', 'QQ_MAIL_AUTH_CODE': 'test'}), \
                mock.patch('smtplib.SMTP_SSL') as smtp, \
                mock.patch.object(trader_260725.threading, 'Thread') as thread, \
                mock.patch.object(patrol, 'log'):
            self.assertFalse(trader_260725.CryptoTrader._send_email_alert(object(), 'info'))
            self.assertFalse(bot_runner.send_email_alert('info'))
            self.assertFalse(patrol.send_email({'EMAIL_ALERT_ONLY_WITH_POSITION': 'false'}, 'info', 'info'))
            smtp.assert_not_called()
            thread.assert_not_called()

    def test_manual_selftest_is_explicit_exception_not_health_failure(self):
        from test_email_channels import patrol
        with mock.patch.object(patrol, 'load_env', return_value={}), \
                mock.patch.object(patrol, 'send_tg', return_value=True), \
                mock.patch.object(patrol, 'send_email', return_value=True) as mail, \
                mock.patch.object(patrol, 'get_egress_ip', return_value=None), \
                mock.patch.object(patrol, 'log'):
            self.assertEqual(patrol.main(['patrol', '--selftest']), 0)
        mail.assert_called_once()
        self.assertEqual(mail.call_args.kwargs['event'], 'selftest')

    def test_safe_rejections_are_not_critical(self):
        tree = ast.parse(Path(trader_260725.__file__).read_text(encoding='utf-8'))
        markers = ('成本未就绪', '已拒绝设置保本损', '账户可用余额',)
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, 'attr', '') != 'send_tg_notification':
                continue
            source = ast.unparse(node)
            matched = {m for m in markers if m in source}
            if matched:
                found.update(matched)
                levels = [k.value.value for k in node.keywords if k.arg == 'level']
                self.assertEqual(levels, ['warning'])
        self.assertEqual(found, set(markers))

    def test_entry_validation_rejection_paths_keep_feedback_without_email(self):
        tree = ast.parse(Path(trader_260725.__file__).read_text(encoding='utf-8'))
        expected = {'not is_valid', 'not tp_is_valid',
                    'usdt_free < total_required_margin + used_margin'}
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or ast.unparse(node.test) not in expected:
                continue
            if not any(isinstance(n, ast.Call) and
                       getattr(n.func, 'attr', '') == 'send_tg_notification'
                       for n in ast.walk(node)):
                continue
            condition = ast.unparse(node.test)
            found.add(condition)
            fake = mock.MagicMock(tg_bot=None, chat_id=None, loop=None)
            fake.send_tg_notification.side_effect = lambda *a, **k: (
                trader_260725.CryptoTrader.send_tg_notification(fake, *a, **k))
            # Execute the actual rejection branch with controlled data, not a
            # duplicate of its notification call or a live exchange request.
            fn = ast.FunctionDef(name='reject', args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg='self')], kwonlyargs=[],
                kw_defaults=[], defaults=[]), body=node.body, decorator_list=[])
            module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
            context = dict(msg='invalid stop', tp_msg='invalid take profit',
                           usdt_free=0, total_required_margin=1, used_margin=0)
            exec(compile(module, '<actual-rejection-branch>', 'exec'), context)
            context['reject'](fake)
            fake.send_tg_notification.assert_called_once()
            self.assertEqual(fake.send_tg_notification.call_args.kwargs['level'], 'warning')
            fake._send_email_alert.assert_not_called()
        self.assertEqual(found, expected)


if __name__ == '__main__':
    unittest.main()
