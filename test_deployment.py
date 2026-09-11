"""Deployment contract tests. These do not start daemons or touch ledgers."""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read(ROOT / 'deploy/multihedge.supervisord.conf')

    def test_supervisor_control_socket_and_rpc_registered(self):
        self.assertTrue(self.cfg.has_section('unix_http_server'))
        self.assertEqual(self.cfg.get('unix_http_server', 'file'), '/var/run/supervisor.sock')
        self.assertTrue(self.cfg.has_section('rpcinterface:supervisor'))
        self.assertEqual(self.cfg.get('supervisorctl', 'serverurl'), 'unix:///var/run/supervisor.sock')

    def test_all_deployed_programs_are_preserved(self):
        expected = {'loom', 'news', 'reasoner', 'dash', 'grid', 'whale', 'whale_trader', 'pump_monitor', 'memecoin_trader'}
        actual = {x.split(':', 1)[1] for x in self.cfg.sections() if x.startswith('program:')}
        self.assertTrue(expected.issubset(actual), expected - actual)
        for name in expected:
            self.assertEqual(self.cfg.get('program:' + name, 'directory', fallback=''), '/app')

    def test_supervisor_does_not_embed_credentials(self):
        text = (ROOT / 'deploy/multihedge.supervisord.conf').read_text()
        self.assertNotIn('api-key=', text)
        self.assertNotIn('restartdelay', text)
        self.assertNotIn('restartsecs', text)

    def test_build_context_excludes_databases_and_secrets(self):
        path = ROOT / '.dockerignore'
        self.assertTrue(path.exists())
        text = path.read_text().splitlines()
        for item in ('audit/', 'deploy/data/', '*.db', '.env', '.venv/'):
            self.assertIn(item, text)

if __name__ == '__main__':
    unittest.main()
