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
from ct_fixtures import git, make_draft, make_repo


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

    def test_facts_reports_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            store = Path(tmp) / 'store'
            draft = make_draft(repo)
            common = ['--repo', str(repo), '--store', str(store), '--json']
            written = subprocess.run([sys.executable, ct.__file__, 'write', *common, str(draft)],
                                     capture_output=True, text=True, check=True)
            path = json.loads(written.stdout)['path']
            facts = subprocess.run([sys.executable, ct.__file__, 'facts', *common],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(facts.stdout)['checkpoints'], [path])

    def test_untracked_links_and_nested_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            external_file = outside / 'secret'
            external_file.write_text('external content')
            (repo / 'dir-link').symlink_to(outside, target_is_directory=True)
            (repo / 'file-link').symlink_to(external_file)
            nested = repo / 'nested'
            nested.mkdir()
            git(nested, 'init')
            (nested / 'inner').write_text('nested content')
            project = ct.collect_facts(repo)['project']
            digests = project['dirty_digests']
            for name, target in (('dir-link', outside), ('file-link', external_file)):
                self.assertEqual(digests[name], hashlib.sha256(os.fsencode(str(target))).hexdigest())
            self.assertEqual(digests['nested/'], 'directory')
            external_file.write_text('changed outside')
            self.assertEqual(ct.collect_facts(repo)['project']['dirty_digests'], digests)

    def test_worktree_slug_keeps_latest_pointers_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            first = Path(tmp) / 'a-b'
            second = Path(tmp) / 'a' / 'b'
            second.parent.mkdir()
            git(repo, 'worktree', 'add', '-b', 'first', str(first))
            git(repo, 'worktree', 'add', '-b', 'second', str(second))
            store = repo / '.git' / 'context-transfer'
            draft = make_draft(first)
            common = ['--store', str(store), '--json']
            subprocess.run([sys.executable, ct.__file__, 'write', '--repo', str(first),
                            *common, str(draft)], capture_output=True, text=True, check=True)
            result = subprocess.run([sys.executable, ct.__file__, 'resume-check', '--repo',
                                     str(second), *common], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(json.loads(result.stdout)['transfer_id'])
            self.assertEqual(json.loads(result.stdout)['verdict'], 'choose')
            self.assertNotEqual(ct.slug(first), ct.slug(second))
            for path in (first, second, Path('/')):
                resolved = str(path.resolve())
                expected = (path.resolve().name or 'root') + '-' + hashlib.sha256(os.fsencode(resolved)).hexdigest()[:12]
                self.assertEqual(ct.slug(path), expected)

    def test_long_worktree_basename_is_bounded_in_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 239 bytes: an unbounded latest-<slug> would exceed the 255-byte name limit.
            name = 'a' + 'я' * 119
            repo = make_repo(tmp)
            worktree = Path(tmp) / name
            git(repo, 'worktree', 'add', '-b', 'long', str(worktree))
            resolved = str(worktree.resolve())
            digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:12]
            self.assertEqual(ct.slug(worktree), 'a' + 'я' * 31 + '-' + digest)
            store = repo / '.git' / 'context-transfer'
            subprocess.run([sys.executable, ct.__file__, 'write', '--repo', str(worktree),
                            '--store', str(store), '--json', str(make_draft(worktree))],
                           capture_output=True, text=True, check=True)
            self.assertTrue((store / ('latest-' + ct.slug(worktree))).is_file())

    def test_remote_normalization(self):
        for remote in ('git@github.com:O/R.git', 'ssh://git@github.com:22/O/R',
                       'https://github.com/O/R'):
            self.assertEqual(ct.normalize_remote(remote), 'github.com/O/R')
        self.assertIsNone(ct.normalize_remote('/local/path'))

    def test_store_precedence_and_non_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Git reports the realpath; macOS TMPDIR goes through the /var symlink.
            tmp = os.path.realpath(tmp)
            repo = make_repo(tmp)
            args = argparse.Namespace(repo=str(repo), store=None)
            with mock.patch.dict(os.environ, {'CONTEXT_TRANSFER_DIR': tmp + '/env'}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/env'))
                args.store = tmp + '/explicit'
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/explicit'))
                self.assertEqual(ct.resolve_store(args).stat().st_mode & 0o777, 0o700)
            args.store = None
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ct.resolve_store(args), repo / 'docs/context-transfer')
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            args.repo = str(outside)
            with mock.patch.dict(os.environ, {'XDG_STATE_HOME': tmp + '/state'}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/state/context-transfer') / ct.slug(outside))
            with mock.patch.dict(os.environ, {'HOME': tmp}, clear=True):
                self.assertEqual(ct.resolve_store(args), Path(tmp + '/.local/state/context-transfer') / ct.slug(outside))

    def test_default_store_is_excluded_once_and_shared_by_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            repo = make_repo(tmp)
            args = argparse.Namespace(repo=str(repo), store=None)
            with mock.patch.dict(os.environ, {}, clear=True):
                store = ct.resolve_store(args)
                ct.resolve_store(args)
                self.assertEqual(store, repo / 'docs/context-transfer')
                exclude = (repo / '.git/info/exclude').read_text().splitlines()
                self.assertEqual(exclude.count('docs/context-transfer/'), 1)
                self.assertNotIn('docs/', git(repo, 'status', '--porcelain').decode())
                tree = Path(tmp) / 'linked'
                git(repo, 'worktree', 'add', '-q', str(tree), '-b', 'other')
                self.assertEqual(ct.resolve_store(argparse.Namespace(repo=str(tree), store=None)), store)
                self.assertEqual((repo / '.git/info/exclude').read_text().splitlines().count('docs/context-transfer/'), 1)

    def test_legacy_store_with_checkpoints_stays_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            repo = make_repo(tmp)
            legacy = repo / '.git/context-transfer'
            legacy.mkdir()
            (legacy / '20260101T000000Z-aaaaaaaa.md').write_text('x')
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(ct.resolve_store(argparse.Namespace(repo=str(repo), store=None)), legacy)
            self.assertFalse((repo / 'docs/context-transfer').exists())

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
