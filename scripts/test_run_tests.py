"""Canonical runner must reject an empty suite on every Python version."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class NonemptyDiscoveryTests(unittest.TestCase):
    def fixture(self, core=False, package=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        scripts = root / 'scripts'
        core_scripts = root / 'skills/cross-agent/scripts'
        scripts.mkdir()
        core_scripts.mkdir(parents=True)
        transfer_scripts = root / 'skills/transfer-context/scripts'
        transfer_scripts.mkdir(parents=True)
        (transfer_scripts / 'test_fixture.py').write_text('import unittest\nclass Fixture(unittest.TestCase):\n def test_fixture(self): self.assertTrue(True)\n')
        runner = scripts / 'run_tests.py'
        runner.write_bytes(Path(__file__).with_name('run_tests.py').read_bytes())
        test = 'import unittest\nclass Fixture(unittest.TestCase):\n def test_fixture(self):self.assertTrue(True)\n'
        if core:
            (core_scripts / 'test_fixture.py').write_text(test)
        if package:
            (scripts / 'test_fixture.py').write_text(test)
        private_bin = root / 'bin'
        private_bin.mkdir()
        (private_bin / 'python3').symlink_to(Path(sys.executable).resolve())
        home = root / 'home'
        home.mkdir(mode=0o700)
        env = {'PATH': str(private_bin), 'HOME': str(home), 'TMPDIR': str(root),
               'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1'}
        for name in ('codex', 'claude', 'agy'):
            self.assertIsNone(shutil.which(name, path=env['PATH']))
        return subprocess.run([sys.executable, '-B', str(runner)], cwd=root,
                              env=env, text=True, capture_output=True, check=False)

    def test_empty_core_suite_rejected_explicitly(self):
        result = self.fixture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No tests discovered: skills/cross-agent/scripts', result.stderr)
        self.assertNotIn('Standalone checks passed', result.stdout)

    def test_empty_package_suite_rejected_after_nonempty_core(self):
        result = self.fixture(core=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No tests discovered: scripts', result.stderr)
        self.assertNotIn('Standalone checks passed', result.stdout)

    def test_both_nonempty_suites_run(self):
        result = self.fixture(core=True, package=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Standalone checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
