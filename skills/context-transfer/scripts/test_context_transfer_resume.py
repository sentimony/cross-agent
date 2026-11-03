"""Resume selection, identity and drift tests."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import context_transfer as ct
from ct_fixtures import git, make_draft, make_repo


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name, 'git@github.com:O/R.git')
        self.store = Path(self.tmp.name) / 'store'
        self.args = SimpleNamespace(repo=str(self.repo), store=str(self.store), budget=12000,
                                    statusline=False, transcript=None, rollout=None,
                                    prepare=None, refresh=None, rotate=None)
        self.draft = make_draft(self.repo)

    def check(self, target=None):
        return ct.resume_check(target, self.args)

    def test_fresh_end_to_end_and_head(self):
        self.draft.write_text(self.draft.read_text().replace('## Verification\n\nNone.',
                              '## Verification\n\nTests passed.').replace(
                              '## Next actions\n\nNone.', '## Next actions\n\nFinish implementation.'))
        path = ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(self.check()['verdict'], 'fresh')
        self.assertEqual(self.check(path.stem)['path'], str(path))
        self.assertIn('## Verification\n\nTests passed.', path.read_text())
        self.assertIn('## Next actions\n\nFinish implementation.', path.read_text())
        git(self.repo, 'add', 'tracked')
        git(self.repo, 'commit', '-m', 'progress')
        result = self.check(str(path))
        self.assertEqual(result['verdict'], 'drifted')
        self.assertIn('head', [d['kind'] for d in result['drift']])

    def test_branch_dirty_content_deleted_and_rename(self):
        ct.write_checkpoint(self.draft, self.args)
        git(self.repo, 'checkout', '-b', 'other')
        self.assertIn('branch', [d['kind'] for d in self.check()['drift']])
        (self.repo / 'tracked').write_text('different')
        self.assertIn('content', [d['kind'] for d in self.check()['drift']])
        (self.repo / 'untracked').unlink()
        result = self.check()
        self.assertIn('dirty-set', [d['kind'] for d in result['drift']])
        self.assertIn('untracked', [d['detail'] for d in result['drift'] if d['kind'] == 'content'])
        git(self.repo, 'add', '-A')
        git(self.repo, 'mv', 'tracked', 'renamed')
        details = [d['detail'] for d in self.check()['drift'] if d['kind'] == 'content']
        self.assertIn('renamed', details)
        self.assertIn('tracked', details)

    def test_missing_and_identity(self):
        artifact = self.repo / 'artifact'
        artifact.write_text('data')
        self.draft = make_draft(self.repo, artifacts=['artifact'])
        path = ct.write_checkpoint(self.draft, self.args)
        artifact.unlink()
        self.assertEqual(self.check()['missing'], ['artifact'])
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        git(other, 'init')
        self.args.repo = str(other)
        self.assertEqual(self.check(str(path))['verdict'], 'blocked')
        git(other, 'remote', 'add', 'origin', 'https://github.com/O/R')
        self.assertNotEqual(self.check(str(path))['verdict'], 'blocked')

    def test_selection_invalid_and_exit_codes(self):
        self.assertEqual(self.check()['verdict'], 'none')
        path = ct.write_checkpoint(self.draft, self.args)
        (self.store / ('latest-' + ct.slug(self.repo))).unlink()
        self.assertEqual(self.check()['verdict'], 'choose')
        self.assertIn(str(path), self.check()['candidates'])
        path.write_text('invalid')
        self.assertEqual(self.check(str(path))['verdict'], 'invalid')
        for target in ([], [str(path)]):
            result = subprocess.run([sys.executable, ct.__file__, 'resume-check', '--json',
                                     '--repo', str(self.repo), '--store', str(self.store), *target],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(json.loads(result.stdout)['verdict'], ('choose', 'invalid'))


if __name__ == '__main__':
    unittest.main()
