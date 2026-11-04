"""Canonical credential-free local/CI checks; never reaches native agent CLIs."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUN_SUITE = """
import sys
import unittest

suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern='test_*.py')
if suite.countTestCases() == 0:
    print('No tests discovered: ' + sys.argv[1], file=sys.stderr)
    raise SystemExit(1)
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def main():
    with tempfile.TemporaryDirectory(prefix='cross-agent-checks-') as temporary:
        scratch = Path(temporary).resolve()
        private_bin = scratch / 'bin'
        private_bin.mkdir()
        (private_bin / 'python3').symlink_to(Path(sys.executable).resolve())
        home = scratch / 'home'
        home.mkdir(mode=0o700)
        env = {'PATH': str(private_bin), 'HOME': str(home), 'TMPDIR': str(scratch),
               'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null'}
        for name in ('codex', 'claude', 'agy'):
            if shutil.which(name, path=env['PATH']) is not None:
                raise RuntimeError('Native agent is reachable in test environment: ' + name)
        # No host/provider/auth environment is inherited. Individual fixtures may
        # add fake executables only inside their own temporary bin directories.
        for folder in ('skills/cross-agent/scripts', 'skills/context-transfer/scripts', 'scripts'):
            result = subprocess.run([sys.executable, '-B', '-c', RUN_SUITE, folder],
                                    cwd=ROOT, env=env, check=False)
            if result.returncode:
                return result.returncode
        print('Standalone checks passed with private HOME/PATH; native agents unreachable.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
