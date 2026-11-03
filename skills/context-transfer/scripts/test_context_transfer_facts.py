"""Tests for repository facts and store selection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import context_transfer as ct
from ct_fixtures import git, make_repo


class FactsTest(unittest.TestCase):
    def test_facts_and_cli_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, 'https://fixtureuser:fixturesecret@host/x.git')
            args = [sys.executable, ct.__file__, 'facts', '--repo', str(repo), '--json']
            result = subprocess.run(args, capture_output=True, text=True, check=True)
            self.assertNotIn('fixturesecret', result.stdout)
            self.assertNotIn('fixtureuser', result.stdout)
            facts = json.loads(result.stdout)
            p = facts['project']
            self.assertEqual(p['repository'], 'host/x')
            self.assertEqual(p['root'], str(repo.resolve()))
            self.assertEqual(p['worktree'], str(repo.resolve()))
            self.assertEqual(p['git_common_dir'], str((repo / '.git').resolve()))
            self.assertEqual(p['head'], git(repo, 'rev-parse', 'HEAD').decode().strip())
            self.assertEqual(p['dirty'], ['tracked'])
            self.assertEqual(p['untracked'], ['untracked'])
            self.assertEqual(p['dirty_digests']['tracked'], hashlib.sha256(b'changed').hexdigest())
            self.assertTrue(p['branch'])

    def test_remote_normalization(self):
        for remote in ('git@github.com:O/R.git', 'ssh://git@github.com:22/O/R',
                       'https://github.com/O/R'):
            self.assertEqual(ct.normalize_remote(remote), 'github.com/O/R')
        self.assertIsNone(ct.normalize_remote('/local/path'))

    def test_store_precedence_and_non_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            args = argparse.Namespace(repo=str(repo), store=None)
            with mock.patch.dict(os.environ, {'CONTEXT_TRANSFER_DIR': tmp + '/env'}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/env'))
                args.store = tmp + '/explicit'
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/explicit'))
                self.assertEqual(ct.resolve_store(args).stat().st_mode & 0o777, 0o700)
            args.store = None
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ct.resolve_store(args), repo / '.git/context-transfer')
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            args.repo = str(outside)
            with mock.patch.dict(os.environ, {'XDG_STATE_HOME': tmp + '/state'}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/state/context-transfer') / ct.slug(outside))
            with mock.patch.dict(os.environ, {'HOME': tmp}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/.local/state/context-transfer') / ct.slug(outside))

    def test_unborn_detached_rename_and_special_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'space repo'
            repo.mkdir()
            git(repo, 'init')
            self.assertIsNone(ct.collect_facts(repo)['project']['head'])
            name = 'new\nфайл'
            (repo / 'old').write_text('old')
            git(repo, 'add', 'old')
            git(repo, 'commit', '-m', 'first')
            git(repo, 'checkout', '--detach')
            git(repo, 'mv', 'old', name)
            p = ct.collect_facts(repo)['project']
            self.assertIsNone(p['branch'])
            self.assertEqual(set(p['dirty']), {'old', name})
            self.assertEqual(p['dirty_digests']['old'], 'deleted')
