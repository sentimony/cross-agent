import json
import tempfile
import unittest
from pathlib import Path
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
        fable = r.resolve_agents(r.parse_request('[a] claude fable high'), review, 'codex')[0]
        self.assertEqual((fable.model, fable.effort), ('claude-fable-5-1', 'high'))
        execution = r.load_config(host_config=cross_agent.DEFAULT_CONFIG, role='execution', profile='execution')
        writer = r.resolve_agents(r.parse_request('[a]'), execution, 'claude-code')
        self.assertEqual([(c.family, c.model, c.effort) for c in writer],
                         [('codex', 'gpt-6.1-sol', 'medium')])
        with self.assertRaises(r.UnavailableError):
            r.resolve_agents(r.parse_request('[a] claude'), execution, 'codex')


if __name__ == '__main__': unittest.main()
