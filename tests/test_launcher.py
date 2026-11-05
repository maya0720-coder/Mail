import errno
import tempfile
import unittest
from unittest.mock import patch

from mailbrain.launcher import address_in_use, launch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {"data_dir": self.temp.name, "port": 18765,
                    "user_email": "me@example.com", "engine": "rules"}

    def test_duplicate_opens_existing_app_without_starting_or_writing_data(self):
        with patch('mailbrain.launcher.running_instance', return_value=True), \
             patch('mailbrain.launcher.Store') as store, \
             patch('mailbrain.web.serve') as serve, \
             patch('mailbrain.launcher.webbrowser.open') as browser:
            r = launch(self.cfg, open_browser=True)
        self.assertTrue(r['reused'])
        browser.assert_called_once_with('http://127.0.0.1:18765')
        store.assert_not_called()
        serve.assert_not_called()

    def test_occupied_port_uses_next_without_mutating_config_or_killing_owner(self):
        attempts = []
        def serve(cfg, store, pipeline, on_ready, health):
            attempts.append(cfg['port'])
            if cfg['port'] == 18765:
                raise OSError(errno.EADDRINUSE, 'occupied')
            on_ready()
        with patch('mailbrain.launcher.running_instance', return_value=False), \
             patch('mailbrain.web.serve', side_effect=serve), \
             patch('mailbrain.launcher.webbrowser.open') as browser:
            r = launch(self.cfg, open_browser=True)
        self.assertEqual(attempts, [18765, 18766])
        self.assertEqual(self.cfg['port'], 18765)
        self.assertFalse(r['reused'])
        browser.assert_called_once_with('http://127.0.0.1:18766')

    def test_unrelated_errors_propagate_and_windows_10048_recognized(self):
        error = OSError('occupied')
        error.winerror = 10048
        self.assertTrue(address_in_use(error))
        with patch('mailbrain.launcher.running_instance', return_value=False), \
             patch('mailbrain.web.serve', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                launch(self.cfg)

    def test_exhausted_ports_have_actionable_message_and_port_bounds(self):
        cfg = {**self.cfg, 'port': 65534}
        with patch('mailbrain.launcher.running_instance', return_value=False), \
             patch('mailbrain.web.serve', side_effect=OSError(errno.EADDRINUSE, 'occupied')) as serve:
            with self.assertRaisesRegex(ValueError, 'config.json'):
                launch(cfg)
        self.assertEqual(serve.call_count, 2)
        with self.assertRaises(ValueError):
            launch({**self.cfg, 'port': 65536})
