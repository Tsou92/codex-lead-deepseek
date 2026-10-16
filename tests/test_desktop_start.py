import unittest
from unittest.mock import MagicMock, patch

from leadseek.desktop_start import ensure_running
from leadseek import runner


class DesktopStartTests(unittest.TestCase):
    def test_existing_backend_is_reused_without_launch(self):
        with patch('leadseek.desktop.server_identity', return_value={'server_pid': 42}) as identity, \
             patch('leadseek.desktop_start.subprocess.run') as run:
            result = ensure_running('http://127.0.0.1:3080')
        self.assertEqual(result, {'server_pid': 42, 'desktop_auto_started': False})
        identity.assert_called_once()
        run.assert_not_called()

    def test_absent_backend_opens_the_same_app_and_waits_for_api(self):
        fake = MagicMock()
        fake.url = 'http://127.0.0.1:3080'
        fake.__enter__.return_value = fake
        with patch('leadseek.desktop.server_identity', side_effect=[ValueError('absent'), ValueError('starting'), {'server_pid': 43}]), \
             patch('leadseek.desktop_start.listener_present', return_value=False), \
             patch('leadseek.desktop_start.DesktopClient', return_value=fake), \
             patch('leadseek.desktop_start.time.sleep'), \
             patch('leadseek.desktop_start.subprocess.run') as run:
            run.return_value.returncode = 0
            result = ensure_running('http://127.0.0.1:3080')
        self.assertTrue(result['desktop_auto_started'])
        self.assertEqual(result['server_pid'], 43)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-g', '-a', 'DSH'])
        fake.rpc.assert_called_once_with('session/modelCatalog')

    def test_unrecognised_occupied_port_does_not_launch_or_authenticate(self):
        with patch('leadseek.desktop.server_identity', side_effect=ValueError('not DSH')), \
             patch('leadseek.desktop_start.listener_present', return_value=True), \
             patch('leadseek.desktop_start.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'not DSH'):
                ensure_running('http://127.0.0.1:3080')
        run.assert_not_called()

    def test_disabled_autostart_never_opens_app(self):
        with patch('leadseek.desktop.server_identity', side_effect=ValueError('absent')), \
             patch('leadseek.desktop_start.listener_present', return_value=False), \
             patch('leadseek.desktop_start.subprocess.run') as run:
            with self.assertRaises(ValueError):
                ensure_running('http://127.0.0.1:3080', auto_start=False)
        run.assert_not_called()

    def test_missing_app_reports_failure_without_cli_fallback(self):
        with patch('leadseek.desktop.server_identity', side_effect=ValueError('absent')), \
             patch('leadseek.desktop_start.listener_present', return_value=False), \
             patch('leadseek.desktop_start.subprocess.run') as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, '安装桌面客户端'):
                ensure_running('http://127.0.0.1:3080')
        self.assertEqual(run.call_count, 1)

    def test_readiness_timeout_is_bounded(self):
        with patch('leadseek.desktop.server_identity', side_effect=ValueError('absent')), \
             patch('leadseek.desktop_start.listener_present', return_value=False), \
             patch('leadseek.desktop_start.time.monotonic', side_effect=[0, 31]), \
             patch('leadseek.desktop_start.subprocess.run') as run:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, '未在等待时间内就绪'):
                ensure_running('http://127.0.0.1:3080')

    def test_remote_url_is_rejected_before_launch(self):
        with patch('leadseek.desktop_start.subprocess.run') as run:
            with self.assertRaises(ValueError):
                ensure_running('http://example.org:3080')
        run.assert_not_called()


class DesktopDoctorTests(unittest.TestCase):
    def test_unstarted_desktop_reports_pending_without_launch_or_authentication(self):
        with patch.object(runner, 'load_config', return_value={'execution_backend': 'desktop'}), \
             patch('leadseek.desktop.server_identity', side_effect=ValueError('absent')), \
             patch('leadseek.desktop_start.listener_present', return_value=False), \
             patch('leadseek.desktop_client.DesktopClient') as client:
            result = runner.doctor()
        self.assertEqual(result['connection'], 'pending-start')
        self.assertIsNone(result['server_pid'])
        self.assertIn('skill_linked', result)
        self.assertIn('cli_linked', result)
        client.assert_not_called()

    def test_unrecognised_listener_is_rejected_before_authentication(self):
        with patch.object(runner, 'load_config', return_value={'execution_backend': 'desktop'}), \
             patch('leadseek.desktop.server_identity', side_effect=ValueError('not DSH')), \
             patch('leadseek.desktop_start.listener_present', return_value=True), \
             patch('leadseek.desktop_client.DesktopClient') as client:
            with self.assertRaisesRegex(ValueError, 'not DSH'):
                runner.doctor()
        client.assert_not_called()

    def test_verified_server_is_reused_without_model_or_plugin_overrides(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        selection = {'model': 'native', 'reasoningEffort': 'max'}
        fake.rpc.return_value = {'default': selection}
        with patch.object(runner, 'load_config', return_value={'execution_backend': 'desktop'}), \
             patch('leadseek.desktop.server_identity', return_value={'server_pid': 42}), \
             patch('leadseek.desktop_client.DesktopClient', return_value=fake):
            result = runner.doctor()
        self.assertEqual(result['model_selection'], selection)
        self.assertEqual(result['connection'], 'connected')
        fake.rpc.assert_called_once_with('session/modelCatalog')
