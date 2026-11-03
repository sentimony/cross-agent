"""Checkpoint writing and validation tests."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import context_transfer as ct
from ct_fixtures import make_repo, make_draft


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        self.store = Path(self.tmp.name) / 'store'
        self.args = SimpleNamespace(repo=str(self.repo), store=str(self.store), budget=12000,
                                    statusline=False, transcript=None, rollout=None,
                                    prepare=None, refresh=None, rotate=None)
        self.draft = make_draft(self.repo)

    def test_write_idempotent_and_previous(self):
        first = ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(first, ct.write_checkpoint(self.draft, self.args))
        meta, body = ct.parse_checkpoint(first.read_text())
        self.assertIsNone(meta['previous_transfer_id'])
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertIn(str(first), body)
        text = self.draft.read_text().replace('next step', 'different step')
        self.draft.write_text(text)
        second = ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(ct.parse_checkpoint(second.read_text())[0]['previous_transfer_id'], first.stem)

    def test_failure_preserves_latest_and_removes_new_file(self):
        first = ct.write_checkpoint(self.draft, self.args)
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        self.draft.write_text(self.draft.read_text().replace('next step', 'different step'))
        real = ct.os.replace
        def replace(src, dst):
            if Path(dst).name.startswith('latest-'):
                raise OSError('simulated failure')
            return real(src, dst)
        with mock.patch.object(ct.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError):
                ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.iterdir()})
        self.assertTrue(first.exists())

    def test_missing_and_secret_rejected(self):
        meta, body = ct.parse_checkpoint(self.draft.read_text())
        meta['artifacts'] = ['missing']
        errors, _ = ct.validate_checkpoint(meta, body, self.repo, 'draft')
        self.assertTrue(any('missing' in e for e in errors))
        errors, _ = ct.validate_checkpoint(meta, body + '\nghp_exampleSecret', self.repo, 'draft')
        self.assertTrue(any('ghp_' in e and 'exampleSecret' not in e for e in errors))


    def test_each_secret_and_ordinary_words(self):
        values = ['ghp_', 'github_pat_', 'glpat-', 'sk-', 'AKIA', 'xoxa-', 'xoxb-',
                  'xoxp-', 'xoxr-', 'xoxs-']
        for prefix in values:
            with self.subTest(prefix=prefix):
                self.assertTrue(ct._secret_errors('line one\n' + prefix + 'EXAMPLE'))
        for value in ('-----BEGIN PRIVATE KEY-----', 'password=example', 'token=example',
                      '"token": "example"'):
            self.assertTrue(ct._secret_errors(value))
        self.assertFalse(ct._secret_errors('task-1 risk-2'))

    def test_validation_and_budget(self):
        meta, body = ct.parse_checkpoint(self.draft.read_text())
        for key in ('task', 'verification', 'continuation', 'artifacts'):
            broken = dict(meta)
            del broken[key]
            self.assertTrue(ct.validate_checkpoint(broken, body, self.repo, 'draft')[0])
        for section in ct.SECTIONS:
            broken = body.replace('## ' + section, '## Removed', 1)
            self.assertTrue(any(section in e for e in ct.validate_checkpoint(meta, broken, self.repo, 'draft')[0]))
        meta['project'] = {}
        self.assertTrue(ct.validate_checkpoint(meta, body, self.repo, 'draft')[0])
        self.args.budget = 1
        path = ct.write_checkpoint(self.draft, self.args)
        saved, saved_body = ct.parse_checkpoint(path.read_text())
        saved['schema_version'] = 2
        self.assertTrue(any('update' in e for e in ct.validate_checkpoint(saved, saved_body, self.repo, 'checkpoint')[0]))
        saved['schema_version'] = 1
        saved['artifacts'] = ['missing']
        errors, warnings = ct.validate_checkpoint(saved, saved_body, self.repo, 'checkpoint')
        self.assertFalse(errors)
        self.assertTrue(warnings)

    def test_first_replace_failure_and_existing_resume_block(self):
        first = ct.write_checkpoint(self.draft, self.args)
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        self.draft.write_text(self.draft.read_text().replace('next step', 'other step'))
        with mock.patch.object(ct.os, 'replace', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.iterdir()})
        meta, body = ct.parse_checkpoint(first.read_text())
        model = {k: meta[k] for k in ('task', 'verification', 'continuation', 'artifacts')}
        model['source'] = {'model': meta['source']['model']}
        self.draft.write_text('---\n' + json.dumps(model) + '\n---\n' + body)
        self.assertEqual(first, ct.write_checkpoint(self.draft, self.args))
        self.assertEqual(body.count(ct.RESUME_MARKER), 2)

    def test_symlink_store_is_rejected(self):
        target = Path(self.tmp.name) / 'target'
        target.mkdir()
        self.store.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            ct.write_checkpoint(self.draft, self.args)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
