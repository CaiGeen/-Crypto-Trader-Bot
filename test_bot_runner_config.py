import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import bot_runner


class ProjectTelegramConfigTests(unittest.TestCase):
    PROJECT_TOKEN = '123456:Abc_def-123'
    INHERITED_TOKEN = '654321:Inherited_token-456'

    def _write_config(self, contents):
        temp = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False)
        self.addCleanup(lambda: os.path.exists(temp.name) and os.unlink(temp.name))
        temp.write(contents)
        temp.close()
        return temp.name

    def test_reads_project_credentials(self):
        path = self._write_config(
            f'TG_BOT_TOKEN={self.PROJECT_TOKEN}\nTG_ALLOWED_USER_ID=-42\n')
        self.assertEqual(bot_runner._load_own_tg_config(path), (self.PROJECT_TOKEN, '-42'))

    def test_rejects_malformed_token(self):
        for token in (':', 'no-colon', '123:bad token', 'abc:secret'):
            with self.subTest(token=token):
                path = self._write_config(f'TG_BOT_TOKEN={token}\nTG_ALLOWED_USER_ID=42\n')
                with self.assertRaisesRegex(ValueError, 'TG_BOT_TOKEN'):
                    bot_runner._load_own_tg_config(path)

    def test_rejects_malformed_user_id(self):
        for user_id in ('--5', '-', '', '4.2', 'abc'):
            with self.subTest(user_id=user_id):
                path = self._write_config(
                    f'TG_BOT_TOKEN={self.PROJECT_TOKEN}\nTG_ALLOWED_USER_ID={user_id}\n')
                with self.assertRaisesRegex(ValueError, 'TG_ALLOWED_USER_ID'):
                    bot_runner._load_own_tg_config(path)

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '缺少本项目 .env'):
                bot_runner._load_own_tg_config(os.path.join(directory, '.env'))

    def test_main_pins_project_credentials_before_every_startup_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '.env').write_text(
                f'TG_BOT_TOKEN={self.PROJECT_TOKEN}\nTG_ALLOWED_USER_ID=-42\n', encoding='utf-8')
            boundaries = []

            def assert_pinned(label):
                self.assertEqual(os.environ['TG_BOT_TOKEN'], self.PROJECT_TOKEN)
                self.assertEqual(os.environ['TG_ALLOWED_USER_ID'], '-42')
                self.assertEqual(bot_runner.BOT_TOKEN, self.PROJECT_TOKEN)
                self.assertEqual(bot_runner.ALLOWED_USER_ID, -42)
                self.assertEqual(os.environ['CONFIG_TEST_SENTINEL'], 'unchanged')
                self.assertEqual(os.environ['BINANCE_PROXY'], 'http://127.0.0.1:7890')
                boundaries.append(label)

            app = mock.Mock()
            builder = mock.Mock()
            for name in ('token', 'request', 'get_updates_request', 'post_init'):
                getattr(builder, name).return_value = builder
            builder.token.side_effect = lambda token: (assert_pinned('app.token'), builder)[1]
            builder.build.side_effect = lambda: (assert_pinned('app.build'), app)[1]
            app.run_polling.side_effect = lambda **kwargs: assert_pinned('run_polling')
            with mock.patch.dict(os.environ, {
                    'TG_BOT_TOKEN': self.INHERITED_TOKEN, 'TG_ALLOWED_USER_ID': '999',
                    'BINANCE_PROXY': 'http://127.0.0.1:7890', 'CONFIG_TEST_SENTINEL': 'unchanged'}), \
                    mock.patch.object(bot_runner, 'BASE_DIR', directory), \
                    mock.patch.object(bot_runner, 'BOT_TOKEN', self.INHERITED_TOKEN), \
                    mock.patch.object(bot_runner, 'ALLOWED_USER_ID', 999), \
                    mock.patch.object(bot_runner, 'acquire_instance_lock',
                                      side_effect=lambda: assert_pinned('instance_lock')), \
                    mock.patch.object(bot_runner, 'log_effective_config',
                                      side_effect=lambda: assert_pinned('config_banner')), \
                    mock.patch.object(bot_runner, 'HTTPXRequest',
                                       side_effect=lambda **kwargs: (assert_pinned('request'), mock.Mock())[1]), \
                    mock.patch.object(bot_runner.Application, 'builder', return_value=builder), \
                    mock.patch('tg_diagnostics.install_updater_watch') as updater_watch, \
                    mock.patch.object(bot_runner, 'CryptoTrader') as trader:
                bot_runner.main()
                builder.token.assert_called_once_with(self.PROJECT_TOKEN)
                app.run_polling.assert_called_once_with(bootstrap_retries=-1)
                updater_watch.assert_called_once_with(app, interval=15.0)
                trader.assert_not_called()  # mock polling does not run handlers/post_init
            self.assertEqual(boundaries, [
                'instance_lock', 'config_banner', 'request', 'request',
                'app.token', 'app.build', 'run_polling'])

    def test_main_invalid_config_stops_before_any_startup_side_effect(self):
        for contents in (None, 'TG_BOT_TOKEN=:\nTG_ALLOWED_USER_ID=42\n',
                         f'TG_BOT_TOKEN={self.PROJECT_TOKEN}\nTG_ALLOWED_USER_ID=--5\n'):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                if contents is not None:
                    Path(directory, '.env').write_text(contents, encoding='utf-8')
                with mock.patch.dict(os.environ, {
                        'TG_BOT_TOKEN': self.INHERITED_TOKEN, 'TG_ALLOWED_USER_ID': '999'}), \
                        mock.patch.object(bot_runner, 'BASE_DIR', directory), \
                        mock.patch.object(bot_runner, 'BOT_TOKEN', self.INHERITED_TOKEN), \
                        mock.patch.object(bot_runner, 'ALLOWED_USER_ID', 999), \
                        mock.patch.object(bot_runner, 'acquire_instance_lock') as lock, \
                        mock.patch.object(bot_runner, 'log_effective_config') as banner, \
                        mock.patch.object(bot_runner, 'HTTPXRequest') as request, \
                        mock.patch.object(bot_runner.Application, 'builder') as builder, \
                        mock.patch.object(bot_runner, 'CryptoTrader') as trader:
                    with self.assertRaises(ValueError):
                        bot_runner.main()
                    for boundary in (lock, banner, request, builder, trader):
                        boundary.assert_not_called()
                    self.assertEqual(os.environ['TG_BOT_TOKEN'], self.INHERITED_TOKEN)
                    self.assertEqual(os.environ['TG_ALLOWED_USER_ID'], '999')
                    self.assertEqual(bot_runner.BOT_TOKEN, self.INHERITED_TOKEN)
                    self.assertEqual(bot_runner.ALLOWED_USER_ID, 999)

    def test_import_needs_no_project_env_or_valid_inherited_user_id(self):
        # Fresh processes exercise module initialization, not an already-cached import.
        source_dir = str(Path(bot_runner.__file__).resolve().parent)
        code = (f'import sys; sys.path.insert(0, {source_dir!r}); '
                'import bot_runner; assert bot_runner.ALLOWED_USER_ID is None; '
                'print("IMPORT_OK")')
        for inherited_id in (None, '--5'):
            with self.subTest(inherited_id=inherited_id), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ)
                env.pop('TG_BOT_TOKEN', None)
                env.pop('TG_ALLOWED_USER_ID', None)
                if inherited_id is not None:
                    env['TG_ALLOWED_USER_ID'] = inherited_id
                result = subprocess.run([sys.executable, '-B', '-c', code], cwd=directory,
                                        env=env, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('IMPORT_OK', result.stdout)

    def test_auth_handler_fixture_restores_missing_and_configured_global(self):
        import test_auth_blocked as scenarios
        for original in (None, 7):
            with self.subTest(original=original), \
                    mock.patch.object(bot_runner, 'ALLOWED_USER_ID', original), \
                    mock.patch.object(scenarios, 'report') as report:
                scenarios.s15_auth_reset_command()
                self.assertEqual(bot_runner.ALLOWED_USER_ID, original)
                report.assert_called_once()
                self.assertIs(report.call_args.args[1], True)

    def test_auth_handler_fixture_restores_global_when_scenario_raises(self):
        import test_auth_blocked as scenarios
        with mock.patch.object(bot_runner, 'ALLOWED_USER_ID', None), \
                mock.patch.object(scenarios, '_s15_auth_reset_command_configured',
                                  side_effect=RuntimeError('scenario failed')):
            with self.assertRaisesRegex(RuntimeError, 'scenario failed'):
                scenarios.s15_auth_reset_command()
            self.assertIsNone(bot_runner.ALLOWED_USER_ID)


if __name__ == '__main__':
    unittest.main()
