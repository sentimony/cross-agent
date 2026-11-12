import json
import re
import tempfile
import unittest
from pathlib import Path
import cross_agent
import cross_agent_request as r


def document():
    return {"schema_version": 1, "agents": {
        family: {"models": {model: ["low", "medium"]}, "default_model": model,
                 "default_effort": "low", "profiles": ["read-only"],
                 "capabilities": {"read-only": "supported"}}
        for family, model in [("codex", "gpt-6.1-sol"),
                              ("claude-code", "claude-opus-5-5"),
                              ("antigravity", "gemini-4-argon")]}}


class RequestTests(unittest.TestCase):
    def config(self, data=None, role="review", profile="read-only"):
        return r.load_config(host_config=data or document(), role=role, profile=profile)

    def test_orders_equal(self):
        self.assertEqual(r.parse_request('claude opus low [cr-request --auto]'),
                         r.parse_request('[cr-request --auto] claude opus low'))

    def test_assignment_exact_nested_escaped(self):
        text = '  consult [nested] \\[escaped\\] $(echo x); "quoted"  '
        self.assertEqual(r.parse_request('[' + text + '] codex').assignment, text)

    def test_invalid_syntax(self):
        for text in ('[a] [b]', '[a', 'a]', '[]', '[a] --auto', '[a] wat',
                     '[a] --new --session x', '[a] --task new --session x',
                     '[a] --task x --task y', '[a] codex low sol'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                r.parse_request(text)
        for text in ('[a] [b]', 'claude opus low ]', 'claude opus low []'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, re.escape(r.FORM_HINT)):
                r.parse_request(text)
        with self.assertRaisesRegex(ValueError, re.escape(r.FORM_HINT)):
            r.parse_request('claude opus low [a]', 'brief from file')

    def test_every_parse_error_ends_with_form_hint(self):
        cases = [('claude opus low', 'bad\x00text'), ('--new --new claude [a]', None),
                 ('--task x --task y claude [a]', None), ('--bogus claude [a]', None),
                 ('claude [a] --task', None), ('opus [a]', None),
                 ('claude low low [a]', None), ('claude opus low extra [a]', None),
                 ('--session x --new claude [a]', None), ('claude a\x01b [a]', None),
                 ('claude [a] "unclosed', None), (None, None), ('claude opus low [ ]', None)]
        for text, file_text in cases:
            with self.subTest(text=text, file_text=file_text):
                with self.assertRaises(ValueError) as ctx:
                    r.parse_request(text, file_text)
                self.assertTrue(str(ctx.exception).endswith(': ' + r.FORM_HINT), str(ctx.exception))

    def test_flags_outside(self):
        req = r.parse_request('--task task-1 [skill --new --auto] --session session-1 codex')
        self.assertEqual((req.task_selector, req.session_selector, req.new_session),
                         ('task-1', 'session-1', False))
        self.assertEqual(req.assignment, 'skill --new --auto')

    def test_aliases_multiple_and_defaults(self):
        choices = r.resolve_agents(r.parse_request('[review] claude opus low agy argon'), self.config(), 'codex')
        self.assertEqual([c.family for c in choices], ['claude-code', 'antigravity'])
        self.assertEqual(choices[0].model, 'claude-opus-5-5')
        defaults = r.resolve_agents(r.parse_request('[review]'), self.config(), 'codex')
        self.assertEqual([c.family for c in defaults], ['antigravity', 'claude-code'])

    def test_unknown_model_effort_missing_agent(self):
        for text in ('[a] codex nope', '[a] codex sol max'):
            with self.assertRaises(ValueError):
                r.resolve_agents(r.parse_request(text), self.config(), 'claude')
        data = document(); del data['agents']['codex']
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a] codex'), self.config(data), 'claude')

    def test_no_agents_and_pending_capability(self):
        for data in ({'schema_version': 1, 'agents': {}}, document()):
            if data['agents']:
                data['agents']['codex']['capabilities']['read-only'] = 'requires-live-probe'
            with self.assertRaises(r.UnavailableError):
                r.resolve_agents(r.parse_request('[a] codex'), self.config(data), 'claude')

    def test_execution_single_writer(self):
        data = document()
        for agent in data['agents'].values():
            agent['profiles'] = ['execution']; agent['capabilities'] = {'execution': 'supported'}
        cfg = self.config(data, 'execution', 'execution')
        for text in ('[implement]', '[implement] claude codex'):
            with self.assertRaises(ValueError):
                r.resolve_agents(r.parse_request(text), cfg, 'codex')
        self.assertEqual(len(r.resolve_agents(r.parse_request('[implement] codex'), cfg, 'codex')), 1)

    def test_schema_and_conflicts_closed(self):
        for mutate in (lambda d: d.update(extra=True),
                       lambda d: d.update(schema_version=True),
                       lambda d: d['agents']['codex'].update(extra=1),
                       lambda d: d['agents'].update(unknown=d['agents']['codex']),
                       lambda d: d['agents']['codex'].update(default_model='unknown')):
            data = document(); mutate(data)
            with self.assertRaises(ValueError): self.config(data)
        with self.assertRaises(ValueError): self.config(role='execution', profile='read-only')

    def test_restrictive_layers_and_json_duplicates(self):
        user = document(); project = document()
        del project['agents']['codex']
        cfg = r.load_config(user_config=user, project_config=project, role='review', profile='read-only')
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a] codex'), cfg, 'claude')
        project = document(); project['agents']['codex']['models']['gpt-6.1-sol'].append('max')
        with self.assertRaises(ValueError):
            r.load_config(user_config=user, project_config=project, role='review', profile='read-only')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{"schema_version":1,"schema_version":1,"agents":{}}')
            with self.assertRaises(ValueError): self.config(path)

    def test_capabilities_cannot_be_elevated(self):
        user = document(); project = document()
        user['agents']['codex']['capabilities']['read-only'] = 'requires-live-probe'
        with self.assertRaises(ValueError):
            r.load_config(user_config=user, project_config=project, role='review', profile='read-only')
        for family in document()['agents']:
            data = document()
            data['agents'][family]['capabilities']['read-only'] = 'requires-live-probe'
            with self.assertRaises(r.UnavailableError):
                r.resolve_agents(r.parse_request('[a] ' + family), self.config(data), 'codex')

    def test_host_layer_narrows_and_no_config_authorizes_nothing(self):
        cfg = r.load_config(role='consultation', profile='read-only')
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a]'), cfg, 'codex')
        host = document(); host['agents'] = {'codex': host['agents']['codex']}
        cfg = r.load_config(user_config=document(), host_config=host, role='review', profile='read-only')
        self.assertEqual(set(cfg.agents), {'codex'})

    def test_existing_aliases_and_explicit_same_family(self):
        for name, family in r.FAMILIES.items():
            choices = r.resolve_agents(r.parse_request('[a] ' + name), self.config(), family)
            self.assertEqual(choices[0].family, family)
            self.assertTrue(choices[0].same_cli)

    def test_valid_json_path_and_missing_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(document()))
            self.assertEqual(set(self.config(path).agents), set(document()['agents']))
            with self.assertRaises(FileNotFoundError): self.config(path.with_name('absent.json'))

    def test_antigravity_execution_always_unavailable(self):
        data = document(); policy = data['agents']['antigravity']
        policy['profiles'] = ['execution']; policy['capabilities'] = {'execution': 'supported'}
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a] agy'), self.config(data, 'execution', 'execution'), 'codex')


class BundledDefaultTests(unittest.TestCase):
    def test_launcher_falls_back_to_bundled_default(self):
        import cross_agent
        self.assertTrue(cross_agent.DEFAULT_CONFIG.is_file())
        self.assertEqual(cross_agent.DEFAULT_CONFIG.parent.name, 'config')

    def test_default_routes_review_and_execution(self):
        import cross_agent
        review = r.load_config(host_config=cross_agent.DEFAULT_CONFIG, role='review', profile='read-only')
        choices = r.resolve_agents(r.parse_request('[a]'), review, 'codex')
        self.assertEqual([(c.family, c.model, c.effort) for c in choices],
                         [('claude-code', 'claude-opus-5-5', 'medium')])
        sonnet = r.resolve_agents(r.parse_request('[a] claude sonnet high'), review, 'codex')[0]
        self.assertEqual((sonnet.model, sonnet.effort), ('claude-sonnet-5-5', 'high'))
        with self.assertRaises(ValueError):
            r.resolve_agents(r.parse_request('[a] claude fable high'), review, 'codex')
        execution = r.load_config(host_config=cross_agent.DEFAULT_CONFIG, role='execution', profile='execution')
        writer = r.resolve_agents(r.parse_request('[a]'), execution, 'claude-code')
        self.assertEqual([(c.family, c.model, c.effort) for c in writer],
                         [('codex', 'gpt-6.1-sol', 'medium')])
        # Config allows a Claude Code writer; transport still requires an orchestrate policy.
        orchestrator = r.resolve_agents(r.parse_request('[a] claude'), execution, 'codex')[0]
        self.assertEqual((orchestrator.family, orchestrator.profile), ('claude-code', 'no-network'))


def one_agent(family='codex', model='gpt-6.1-sol', **changes):
    base = {"models": {model: ["low"]}, "default_model": model, "default_effort": "low",
            "profiles": ["read-only", "no-network", "full"],
            "capabilities": {"read-only": "supported", "no-network": "supported", "full": "supported"}}
    base.update(changes)
    return {"schema_version": 1, "agents": {family: base}}


class ProfileConfigTests(unittest.TestCase):
    def test_legacy_execution_spelling_reads_as_no_network(self):
        doc = one_agent(profiles=["read-only", "execution"],
                        capabilities={"read-only": "supported", "execution": "supported"})
        policy = r.validate_config(doc)["codex"]
        self.assertEqual(policy.profiles, ("read-only", "no-network"))
        self.assertEqual(policy.capabilities, {"read-only": "supported", "no-network": "supported"})
        config = r.load_config(host_config=doc, role="execution", profile="execution")
        self.assertEqual(config.profile, "no-network")

    def test_both_spellings_together_are_rejected(self):
        both_profiles = one_agent(profiles=["execution", "no-network"],
                                  capabilities={"execution": "supported", "no-network": "supported"})
        both_caps = one_agent(profiles=["no-network"],
                              capabilities={"execution": "supported", "no-network": "supported"})
        for doc in (both_profiles, both_caps):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                r.validate_config(doc)

    def test_full_is_unavailable_when_the_config_lacks_it(self):
        doc = one_agent(profiles=["read-only", "execution"],
                        capabilities={"read-only": "supported", "execution": "supported"})
        config = r.load_config(host_config=doc, role="execution", profile="full")
        with self.assertRaisesRegex(r.UnavailableError, 'profile requires verified adapter support: full'):
            r.resolve_agents(r.parse_request("[a] codex"), config, "claude-code")

    def test_full_resolves_when_supported(self):
        config = r.load_config(host_config=one_agent(), role="execution", profile="full")
        choice = r.resolve_agents(r.parse_request("[a] codex"), config, "claude-code")[0]
        self.assertEqual((choice.family, choice.profile, choice.role), ("codex", "full", "execution"))

    def test_role_profile_conflicts_still_fail(self):
        with self.assertRaises(ValueError):
            r.load_config(host_config=one_agent(), role="execution", profile="read-only")
        with self.assertRaises(ValueError):
            r.load_config(host_config=one_agent(), role="review", profile="full")

    def test_subagent_models_are_optional_validated_and_narrow_only(self):
        self.assertEqual(r.validate_config(one_agent())["codex"].subagent_models, ())
        wide = one_agent(subagent_models=["gpt-6.1-terra", "gpt-6.1-lune"])
        narrow = one_agent(subagent_models=["gpt-6.1-terra"])
        self.assertEqual(r.validate_config(wide)["codex"].subagent_models,
                         ("gpt-6.1-terra", "gpt-6.1-lune"))
        r.load_config(user_config=wide, host_config=narrow, role="review", profile="read-only")
        with self.assertRaisesRegex(ValueError, 'widens subagent models'):
            r.load_config(user_config=narrow, host_config=wide, role="review", profile="read-only")
        for bad in ([], ["a", "a"], "gpt-6.1-terra", [1], ["-x"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.validate_config(one_agent(subagent_models=bad))

    def test_subagent_only_model_cannot_be_launched(self):
        doc = one_agent(subagent_models=["gpt-6.1-lune"])
        config = r.load_config(host_config=doc, role="review", profile="read-only")
        with self.assertRaisesRegex(ValueError, 'allowed only as a subagent'):
            r.resolve_agents(r.parse_request("[a] codex lune"), config, "claude-code")

    def test_default_recipients_skip_families_that_are_not_supported(self):
        doc = one_agent()
        doc["agents"]["antigravity"] = one_agent("antigravity", "gemini-4-argon",
            capabilities={"read-only": "requires-live-probe", "no-network": "requires-live-probe",
                          "full": "requires-live-probe"})["agents"]["antigravity"]
        config = r.load_config(host_config=doc, role="review", profile="read-only")
        self.assertEqual([c.family for c in r.resolve_agents(r.parse_request("[a]"), config, "claude-code")],
                         ["codex"])
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request("[a] gemini"), config, "claude-code")
        self.assertEqual([g.family for g in r.default_groups(config, "codex")], [])

    def test_antigravity_never_runs_a_writer_profile(self):
        doc = one_agent("antigravity", "gemini-4-argon")
        config = r.load_config(host_config=doc, role="execution", profile="full")
        with self.assertRaisesRegex(r.UnavailableError, 'Antigravity full has no verified scoped adapter'):
            r.resolve_agents(r.parse_request("[a] agy"), config, "codex")

    def test_lune_alias(self):
        doc = one_agent(models={"gpt-6.1-lune": ["low"]}, default_model="gpt-6.1-lune")
        config = r.load_config(host_config=doc, role="review", profile="read-only")
        self.assertEqual(r.resolve_agents(r.parse_request("[a] codex lune"), config, "claude-code")[0].model,
                         "gpt-6.1-lune")


class DefaultConfigTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(cross_agent.DEFAULT_CONFIG).read_text(encoding='utf-8')
        self.doc = json.loads(self.text)['agents']

    def test_model_set_is_fixed(self):
        levels = ['low', 'medium', 'high']
        self.assertEqual(self.doc['claude-code']['models'],
                         {'claude-opus-5-5': levels, 'claude-sonnet-5-5': levels})
        self.assertEqual(self.doc['codex']['models'], {'gpt-6.1-sol': levels, 'gpt-6.1-terra': levels})
        self.assertEqual(self.doc['antigravity']['models'],
                         {'gemini-4-argon': levels, 'gemini-3.8-flash': levels})
        for family, model in (('claude-code', 'claude-opus-5-5'), ('codex', 'gpt-6.1-sol'),
                              ('antigravity', 'gemini-4-argon')):
            self.assertEqual((self.doc[family]['default_model'], self.doc[family]['default_effort']),
                             (model, 'medium'))
        for gone in ('fable', 'xhigh', '"max"'):
            self.assertNotIn(gone, self.text)

    def test_subagent_models(self):
        self.assertEqual(self.doc['claude-code']['subagent_models'], ['claude-sonnet-5-5', 'claude-haiku-5-5'])
        self.assertEqual(self.doc['codex']['subagent_models'], ['gpt-6.1-terra', 'gpt-6.1-lune'])
        self.assertEqual(self.doc['antigravity']['subagent_models'], ['gemini-3.8-flash'])
        for family in ('claude-code', 'codex'):
            self.assertFalse(set(self.doc[family]['subagent_models'][1:]) & set(self.doc[family]['models']))

    def test_capabilities_and_profiles(self):
        for family in ('claude-code', 'codex'):
            self.assertEqual(self.doc[family]['profiles'], ['read-only', 'no-network', 'full'])
            self.assertEqual(set(self.doc[family]['capabilities'].values()), {'supported'})
        self.assertEqual(self.doc['antigravity']['profiles'], ['read-only', 'no-network', 'full'])
        self.assertEqual(set(self.doc['antigravity']['capabilities'].values()), {'requires-live-probe'})

    def test_default_is_valid_and_resolves(self):
        full = r.load_config(host_config=cross_agent.DEFAULT_CONFIG, role='execution', profile='full')
        self.assertEqual(r.resolve_agents(r.parse_request('[a] codex'), full, 'claude-code')[0].profile, 'full')
        with self.assertRaisesRegex(ValueError, 'allowed only as a subagent'):
            r.resolve_agents(r.parse_request('[a] claude haiku'), full, 'codex')
        review = r.load_config(host_config=cross_agent.DEFAULT_CONFIG, role='review', profile='read-only')
        self.assertEqual([c.family for c in r.resolve_agents(r.parse_request('[a]'), review, 'claude-code')],
                         ['codex'])
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a] gemini flash'), review, 'codex')


if __name__ == '__main__': unittest.main()
