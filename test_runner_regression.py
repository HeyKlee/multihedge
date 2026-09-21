import unittest, sys
from pathlib import Path
sys.path.insert(0, '.')
class RunnerRegression(unittest.TestCase):
    def test_runner_has_argparse_and_no_hardcoded_output(self):
        import run_sampled_replay_full as r
        self.assertTrue(hasattr(r, 'run'))
        import inspect
        src = inspect.getsource(r)
        self.assertNotIn('backtest-results/sampled-price-replay-20260917-1200', src)
        self.assertIn('argparse', src)
        self.assertIn('if __name__', src)
if __name__ == '__main__':
    unittest.main()
