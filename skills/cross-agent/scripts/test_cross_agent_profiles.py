import unittest

import cross_agent_profiles as p


class ProfileTests(unittest.TestCase):
    def test_canonical_names_and_legacy_alias(self):
        self.assertEqual(p.canonical_profile('execution'), 'no-network')
        for name in p.PROFILES:
            self.assertEqual(p.canonical_profile(name), name)
        for bad in ('', 'FULL', 'full-access', None, 3, ['full']):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                p.canonical_profile(bad)

    def test_role_profile_pairs(self):
        self.assertTrue(p.profile_matches_role('execution', 'full'))
        self.assertTrue(p.profile_matches_role('execution', 'execution'))
        self.assertTrue(p.profile_matches_role('review', 'read-only'))
        self.assertFalse(p.profile_matches_role('execution', 'read-only'))
        self.assertFalse(p.profile_matches_role('consultation', 'no-network'))
        self.assertFalse(p.profile_matches_role('writer', 'full'))
        self.assertFalse(p.profile_matches_role('execution', 'bogus'))

    def test_defaults_follow_the_role(self):
        self.assertEqual(p.effective_role_profile('execution'), ('execution', 'full'))
        self.assertEqual(p.effective_role_profile('consultation'), ('consultation', 'read-only'))
        self.assertEqual(p.effective_role_profile('review'), ('review', 'read-only'))

    def test_host_profile_is_the_ceiling(self):
        self.assertEqual(p.effective_role_profile('execution', 'execution'), ('execution', 'no-network'))
        self.assertEqual(p.effective_role_profile('execution', 'no-network', 'no-network'),
                         ('execution', 'no-network'))
        with self.assertRaisesRegex(ValueError, 'widens'):
            p.effective_role_profile('execution', 'no-network', 'full')

    def test_mode_only_narrows(self):
        self.assertEqual(p.effective_role_profile('execution', None, 'full'), ('execution', 'full'))
        self.assertEqual(p.effective_role_profile('execution', None, 'no-network'), ('execution', 'no-network'))
        self.assertEqual(p.effective_role_profile('execution', None, 'read-only'), ('consultation', 'read-only'))
        self.assertEqual(p.effective_role_profile('review', None, 'read-only'), ('review', 'read-only'))
        for role in ('consultation', 'review'):
            for mode in ('no-network', 'full'):
                with self.subTest(role=role, mode=mode), self.assertRaisesRegex(ValueError, 'widens'):
                    p.effective_role_profile(role, None, mode)

    def test_role_profile_conflicts_and_unknown_values(self):
        for args in (('execution', 'read-only'), ('consultation', 'full'),
                     ('review', 'execution'), ('writer', None), ('execution', 'bogus')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                p.effective_role_profile(*args)
        with self.assertRaises(ValueError):
            p.effective_role_profile('execution', None, 'bogus')

    def test_grants_follow_the_launcher_flags(self):
        full = p.profile_grants('codex', 'full')
        self.assertEqual((full['write'], full['network'], full['git_write'], full['mcp']),
                         ('unrestricted', 'allowed', 'allowed', 'denied'))
        self.assertEqual(p.profile_grants('codex', 'execution')['network'], 'denied')
        self.assertEqual(p.profile_grants('codex', 'no-network')['git_write'], 'denied')
        self.assertEqual(p.profile_grants('claude-code', 'no-network')['git_write'], 'allowed')
        self.assertEqual(p.profile_grants('claude-code', 'full')['write'], 'workspace')
        self.assertEqual(p.profile_grants('claude-code', 'read-only')['write'], 'none')
        self.assertEqual(full['basis'], 'launcher flags, not observed')
        self.assertIsNone(p.profile_grants('antigravity', 'read-only'))
        full['write'] = 'changed'
        self.assertEqual(p.profile_grants('codex', 'full')['write'], 'unrestricted')


if __name__ == '__main__':
    unittest.main()
