import unittest

import json

import sys

import tempfile

from dataclasses import replace

from pathlib import Path

import cli_transport as transport

from cross_agent import HostContext, dispatch

from cross_agent_request import load_config, parse_request

from cross_agent_state import TaskContext

SESSION = '11111111-2222-3333-4444-555555555555'


def policy(repo, family='claude-code', actions=('read', 'write-workspace', 'orchestrate'),
           delegation_blocked=True, config_isolated=False):
    return transport.VerifiedProfilePolicy(family, str(Path(repo).resolve()), 'owner attested',
                                           'owner attested', tuple(actions),
                                           delegation_blocked, config_isolated)


def context(pol):
    return transport.ProfileContext('task', 'parent', 'execution', 'execution', pol)


class ClaudeOrchestratorProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()

    def build(self, pol, builder=transport.build_run_command):
        return builder('claude-code', self.repo, self.repo, 'claude-opus-5-5', 'high', SESSION,
                       profile='execution', context=context(pol))

    def test_orchestrator_run_loads_project_settings_and_workspace_tools(self):
        argv = self.build(policy(self.repo))
        self.assertEqual(argv[:2], ['claude', '-p'])
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'user,project,local')
        allowed = argv[argv.index('--allowedTools') + 1].split(',')
        for tool in ('Bash', 'Edit', 'Write', 'Agent', 'Skill'):
            self.assertIn(tool, allowed)
        # --tools limits availability; --allowedTools alone only grants permission.
        self.assertEqual(argv[argv.index('--tools') + 1], argv[argv.index('--allowedTools') + 1])
        self.assertEqual(argv[argv.index('--tools') + 1], transport._CLAUDE_EXEC_TOOLS)
        self.assertNotIn('--restricted', argv)
        self.assertNotIn('--safe-mode', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[-2:], ['--session-id', SESSION])

    def test_orchestrator_resume_keeps_the_same_policy(self):
        argv = self.build(policy(self.repo), transport.build_resume_command)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(argv[-2:], ['--resume', SESSION])

    def test_incomplete_evidence_names_missing_fields(self):
        def with_evidence(fresh, resume):
            return transport.VerifiedProfilePolicy('claude-code', str(self.repo), fresh, resume,
                                                   ('read', 'write-workspace', 'orchestrate'), True, False)
        with self.assertRaises(transport.CrossReviewError) as caught:
            self.build(with_evidence('', 'owner attested'))
        self.assertIn('missing_fields: ["fresh_evidence"]', caught.exception.message)
        with self.assertRaises(transport.CrossReviewError) as caught:
            self.build(with_evidence('', ''), transport.build_resume_command)
        self.assertIn('missing_fields: ["fresh_evidence", "resume_evidence"]', caught.exception.message)
        with self.assertRaises(transport.CrossReviewError) as caught:
            self.build(with_evidence('owner attested', ''), transport.build_resume_command)
        self.assertIn('missing_fields: ["resume_evidence"]', caught.exception.message)
        self.build(with_evidence('owner attested', 'owner attested'), transport.build_resume_command)
        self.build(with_evidence('owner attested', ''))

    def test_claude_execution_needs_explicit_orchestrate_action(self):
        for pol in (policy(self.repo, actions=('read', 'write-workspace')),
                    policy(self.repo, delegation_blocked=False),
                    policy(self.repo, family='codex'),
                    policy(self.repo / 'other')):
            with self.subTest(pol=pol):
                with self.assertRaises(transport.CrossReviewError) as caught:
                    self.build(pol)
                self.assertIn('UNAVAILABLE', caught.exception.message)

    def test_codex_execution_still_requires_config_isolation(self):
        pol = policy(self.repo, family='codex', config_isolated=False)
        with self.assertRaises(transport.CrossReviewError):
            transport.build_run_command('codex', self.repo, self.repo, 'gpt-6.1-sol', 'high',
                                        SESSION, untrusted=[], profile='execution',
                                        context=context(pol))
        with self.assertRaises(transport.CrossReviewError):
            transport.build_run_command('codex', self.repo, self.repo, 'gpt-6.1-sol', 'high',
                                        SESSION, untrusted=[], profile='execution',
                                        context=context(policy(self.repo, family='codex',
                                                               config_isolated=True)))

    def test_claude_read_only_profile_is_unchanged(self):
        argv = transport.build_run_command('claude-code', self.repo, self.repo, 'claude-opus-5-5',
                                           'low', SESSION)
        self.assertIn('--restricted', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')

    def test_skills_note_follows_the_claude_profile(self):
        tokens = dict.fromkeys(transport.TOKEN_KEYS, 1)
        execution = transport.build_usage('claude-code', 'claude-opus-5-5', 'high', tokens, 0.5,
                                          [], profile='execution')
        read_only = transport.build_usage('claude-code', 'claude-opus-5-5', 'high', tokens, 0.5, [])
        self.assertEqual(execution['skills_note'], 'not detected')
        self.assertEqual(read_only['skills_note'], 'disabled by policy')

    def test_pass_env_reaches_the_child_and_nested_launch_stays_blocked(self):
        env = transport.reviewer_environment({'PATH': '/bin', 'SSH_AUTH_SOCK': '/s', 'SECRET': 'x'},
                                             'claude-code', pass_env=['SSH_AUTH_SOCK'],
                                             context=context(policy(self.repo)))
        self.assertEqual(env['SSH_AUTH_SOCK'], '/s')
        self.assertNotIn('SECRET', env)
        with self.assertRaises(transport.CrossReviewError):
            transport.ensure_not_nested(env)


class ClaudeOrchestratorDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        fixture = self.bin / 'claude'
        fixture.write_text('''#!/usr/bin/env python3
import json,os,sys
brief=sys.stdin.read()
args=sys.argv[1:]
sid=args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]
if 'BLOCKME' in brief:
 print(json.dumps({'type':'result','is_error':False,'session_id':sid,'modelUsage':{'claude-opus-5-5':{}},'result':json.dumps({'schema_version':1,'status':'BLOCKED','output':'Blocker: lockfile owned by #109\\nDetails follow.'})}))
 sys.exit(0)
out={'mode':args[args.index('--permission-mode')+1],'ssh':os.environ.get('SSH_AUTH_SOCK'),'orchestrator':'delegated orchestrator' in brief}
# Real claude -p envelope: no model/effort fields; models appear only in modelUsage.
print(json.dumps({'type':'result','is_error':False,'session_id':sid,'modelUsage':{'claude-opus-5-5':{},'claude-sonnet-5-5':{}},'result':json.dumps({'schema_version':1,'status':'COMPLETED','output':json.dumps(out)})}))
''')
        fixture.chmod(0o700)
        config = {'schema_version': 1, 'agents': {'claude-code': {
            'models': {'claude-opus-5-5': ['high']}, 'default_model': 'claude-opus-5-5',
            'default_effort': 'high', 'profiles': ['execution'],
            'capabilities': {'execution': 'supported'}}}}
        self.host = HostContext(load_config(host_config=config, role='execution', profile='execution'),
                                TaskContext(str(self.root / 'registry'), str(self.root), 'conversation'),
                                'claude', str(self.root), str(self.root / 'artifacts'), {'tree': 'x'},
                                environment={'PATH': str(self.bin), 'HOME': str(self.root),
                                             'SSH_AUTH_SOCK': '/agent'},
                                timeout_seconds=5, kill_grace_seconds=.1,
                                policy=policy(self.root), pass_env=('SSH_AUTH_SOCK',))

    def test_orchestrator_dispatch_runs_with_execution_policy_and_pass_env(self):
        result = dispatch(parse_request('claude opus high [orchestrate]'), self.host)
        entry = result.results[0]
        self.assertEqual(entry['status'], 'COMPLETED', entry)
        self.assertEqual(json.loads(entry['output']),
                         {'mode': 'acceptEdits', 'ssh': '/agent', 'orchestrator': True})

    def test_orchestrator_session_resumes_with_the_real_claude_envelope(self):
        first = dispatch(parse_request('claude opus high [orchestrate]'), self.host).results[0]
        self.assertEqual(first['status'], 'COMPLETED', first)
        self.assertEqual(first['actual_model'], 'claude-opus-5-5')
        follow = dispatch(parse_request('claude opus high [answer] --session ' + first['session_id']),
                          self.host).results[0]
        self.assertEqual(follow['status'], 'COMPLETED', follow)
        self.assertEqual(follow['cli_session_id'], first['cli_session_id'])

    def host_with_subagents(self, allowed, profile='execution'):
        config = {'schema_version': 1, 'agents': {'claude-code': {
            'models': {'claude-opus-5-5': ['high']}, 'default_model': 'claude-opus-5-5',
            'default_effort': 'high', 'profiles': ['read-only', 'no-network', 'full'],
            'capabilities': {'read-only': 'supported', 'no-network': 'supported', 'full': 'supported'},
            **({'subagent_models': allowed} if allowed else {})}}}
        return replace(self.host, config=load_config(host_config=config, role='execution', profile=profile))

    def test_result_records_profile_granted_and_observed_subagents(self):
        host = self.host_with_subagents(['claude-sonnet-5-5', 'claude-haiku-5-5'])
        entry = dispatch(parse_request('claude opus high [orchestrate]'), host).results[0]
        self.assertEqual(entry['status'], 'COMPLETED', entry)
        self.assertEqual(entry['profile'], 'no-network')
        self.assertEqual(entry['granted'], {'write': 'workspace', 'network': 'denied',
                                            'git_write': 'allowed', 'mcp': 'denied',
                                            'basis': 'launcher flags, not observed'})
        self.assertIsNone(entry['actions_observed'])
        self.assertEqual(entry['subagents'], {'observed': ['claude-sonnet-5-5'], 'unexpected': []})

    def test_subagent_outside_the_allowed_set_is_reported_not_blocked(self):
        host = self.host_with_subagents(['claude-haiku-5-5'])
        entry = dispatch(parse_request('claude opus high [orchestrate]'), host).results[0]
        self.assertEqual(entry['status'], 'COMPLETED', entry)
        self.assertEqual(entry['subagents'], {'observed': ['claude-sonnet-5-5'],
                                              'unexpected': ['claude-sonnet-5-5']})

    def test_unconfigured_subagent_set_reports_no_verdict(self):
        entry = dispatch(parse_request('claude opus high [orchestrate]'), self.host).results[0]
        self.assertEqual(entry['subagents'], {'observed': ['claude-sonnet-5-5'], 'unexpected': None})

    def test_full_brief_is_an_orchestrator_brief_with_the_subagent_limit(self):
        from cross_agent import _brief
        host = self.host_with_subagents(['claude-sonnet-5-5', 'claude-haiku-5-5'], profile='full')
        brief = _brief(parse_request('claude opus high [x]'), replace(host, policy=None),
                       'execution', 'claude-code')
        head = brief.split('Assignment (data)')[0]
        self.assertIn('delegated orchestrator', head)
        self.assertIn('only these models: claude-haiku-5-5, claude-sonnet-5-5', head)
        self.assertIn('inherit the full profile', head)

    def test_read_only_brief_and_unconfigured_brief_are_unchanged(self):
        from cross_agent import _brief
        read_only_config = {'schema_version': 1, 'agents': {'claude-code': {
            'models': {'claude-opus-5-5': ['high']}, 'default_model': 'claude-opus-5-5',
            'default_effort': 'high', 'profiles': ['read-only'],
            'capabilities': {'read-only': 'supported'},
            'subagent_models': ['claude-sonnet-5-5']}}}
        read_only = replace(self.host, config=load_config(host_config=read_only_config,
                                                          role='review', profile='read-only'))
        brief = _brief(parse_request('codex [x]'), read_only, 'review', 'codex')
        self.assertIn('You are a delegated agent. Never dispatch cross-agent or another agent.', brief)
        self.assertNotIn('subagents', brief.split('Assignment (data)')[0])
        plain = _brief(parse_request('codex [x]'), self.host, 'execution', 'codex')
        self.assertIn('You are a delegated agent. Never dispatch cross-agent or another agent.', plain)

    def test_writer_briefs_confine_writes_to_the_workspace(self):
        from cross_agent import _brief
        sentence = 'Work only inside the workspace ' + str(self.root) + '; do not write outside it.'
        full = self.host_with_subagents(['claude-sonnet-5-5'], profile='full')
        for family in ('claude-code', 'codex'):
            with self.subTest(family=family):
                brief = _brief(parse_request(family + ' [x]'), replace(full, policy=None),
                               'execution', family, profile='full')
                self.assertIn(sentence, brief.split('Assignment (data)')[0])
        no_network = _brief(parse_request('claude [x]'), self.host, 'execution', 'claude-code')
        self.assertIn(sentence, no_network.split('Assignment (data)')[0])
        read_only = _brief(parse_request('claude [x]'), self.host, 'review', 'claude-code', profile='read-only')
        self.assertNotIn('Work only inside the workspace', read_only)

    def test_model_absent_from_model_usage_cannot_resume(self):
        fixture = self.bin / 'claude'
        fixture.write_text(fixture.read_text().replace("'claude-opus-5-5':{},", ''))
        first = dispatch(parse_request('claude opus high [orchestrate]'), self.host).results[0]
        self.assertEqual(first['actual_model'], 'unknown')
        follow = dispatch(parse_request('claude opus high [answer] --session ' + first['session_id']),
                          self.host).results[0]
        self.assertEqual(follow['status'], 'UNAVAILABLE')

    def test_adopted_cli_session_resumes_and_cannot_be_adopted_twice(self):
        from cross_agent_request import resolve_agents
        from cross_agent_state import StateError, adopt_session
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        sid = adopt_session(self.host.task_context, agent, cli)
        self.assertEqual(adopt_session(self.host.task_context, agent, cli), sid)
        other = replace(agent, effort='low')
        with self.assertRaises(StateError):
            adopt_session(self.host.task_context, other, cli)
        entry = dispatch(parse_request('claude opus high [answer] --session ' + sid), self.host).results[0]
        self.assertEqual(entry['status'], 'COMPLETED', entry)
        self.assertEqual(entry['cli_session_id'], cli)

    def test_adopt_rejects_a_non_uuid_provider_id(self):
        from cross_agent_request import resolve_agents
        from cross_agent_state import StateError, adopt_session
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        with self.assertRaises(StateError):
            adopt_session(self.host.task_context, agent, 'not-a-uuid')

    def test_blocked_result_takes_its_reason_from_the_first_output_line(self):
        entry = dispatch(parse_request('claude opus high [BLOCKME]'), self.host).results[0]
        self.assertEqual(entry['status'], 'BLOCKED')
        self.assertEqual(entry['reason'], 'Blocker: lockfile owned by #109')

    def test_orchestrator_brief_routes_owner_decisions_through_a_handoff(self):
        from cross_agent import _brief
        brief = _brief(parse_request('claude opus high [x]'), self.host, 'execution', 'claude-code')
        self.assertIn('USER_INPUT_REQUIRED', brief.split('Assignment (data)')[0])
        self.assertIn('Blocker:', brief)

    def write_transcript(self, cli, rows):
        from cross_agent import claude_transcript
        path = claude_transcript(self.root, cli, self.root / '.claude')
        path.parent.mkdir(parents=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def test_adopt_requires_the_transcript_in_the_workspace_project(self):
        from cross_agent import adopt
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        with self.assertRaises(ValueError):
            adopt(self.host, parse_request('claude opus high [x]'), cli, self.root / '.claude')
        self.write_transcript(cli, [])
        sid = adopt(self.host, parse_request('claude opus high [x]'), cli, self.root / '.claude')
        self.assertTrue(sid)

    def test_status_reports_running_runs_with_their_last_message_and_tool(self):
        import os
        from cross_agent import adopt, status
        from cross_agent_state import SessionDecision, reserve_session
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        self.write_transcript(cli, [
            {'type': 'assistant', 'timestamp': '2026-11-03T00:00:00Z', 'message': {'content': [
                {'type': 'text', 'text': 'Starting W1'},
                {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'git status', 'description': 'Show status'}}]}},
            {'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'subagent'}]}}])
        sid = adopt(self.host, parse_request('claude opus high [x]'), cli, self.root / '.claude')
        decision = SessionDecision('READY', 'RESUME', None, sid, True, cli, 'claude-code',
                                   'claude-opus-5-5', 'high', 'execution', 'execution')
        from cross_agent_state import _registry
        with _registry(self.host.task_context) as data:
            decision = SessionDecision('READY', 'RESUME', data['sessions'][sid]['task_id'], sid, True,
                                       cli, 'claude-code', 'claude-opus-5-5', 'high', 'execution', 'execution')
        reserve_session(self.host.task_context, decision, str(self.root / 'run'))
        report = status(self.host, self.root / '.claude')
        [row] = report['sessions']
        self.assertEqual(row['session_id'], sid)
        self.assertEqual(row['cli_session_id'], cli)
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['pid'], os.getpid())
        self.assertEqual(row['last_text'], 'Starting W1')
        self.assertEqual(row['last_tool'], 'Bash: Show status')

    def test_status_reads_a_fresh_running_claude_session_through_its_launch_id(self):
        from cross_agent import status
        from cross_agent_request import resolve_agents
        from cross_agent_state import finish_session, reserve_session, select_session
        launch = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff'
        self.write_transcript(launch, [
            {'type': 'assistant', 'timestamp': '2026-11-03T00:00:00Z', 'message': {
                'id': 'm1', 'model': 'claude-opus-5-5',
                'usage': {'input_tokens': 10, 'output_tokens': 5},
                'content': [{'type': 'text', 'text': 'Working on W1'}]}}])
        request = parse_request('claude opus high [x]')
        agent = resolve_agents(request, self.host.config, 'claude')[0]
        decision = select_session(request, self.host.task_context, agent, 'execution', 'execution')
        self.assertIsNone(decision.cli_session_id)
        run_id = reserve_session(self.host.task_context, decision, str(self.root / 'run'), launch)
        [row] = status(self.host, self.root / '.claude')['sessions']
        self.assertIsNone(row['cli_session_id'])
        self.assertEqual(row['launch_cli_session_id'], launch)
        self.assertEqual(row['last_text'], 'Working on W1')
        self.assertEqual(row['estimate']['tokens']['output_tokens'], 5)
        finish_session(self.host.task_context, decision.session_id, run_id, 'failed')
        [row] = status(self.host, self.root / '.claude')['sessions']
        self.assertIsNone(row['launch_cli_session_id'])
        self.assertIsNone(row['last_text'])
        self.assertIsNone(row['estimate'])

    def usage_fixture(self, calls_before=0):
        """claude -p fake reporting cumulative session usage: 100 input, 10 output, 0.25 USD per call."""
        counter = self.root / 'calls'
        counter.write_text(str(calls_before))
        (self.bin / 'claude').write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
sys.stdin.read()
args=sys.argv[1:]
sid=args[args.index('--session-id')+1] if '--session-id' in args else args[args.index('--resume')+1]
counter=Path(os.environ['HOME'])/'calls'
n=int(counter.read_text())+1
counter.write_text(str(n))
print(json.dumps({'type':'result','is_error':False,'session_id':sid,'total_cost_usd':0.25*n,
 'modelUsage':{'claude-opus-5-5':{'inputTokens':100*n,'outputTokens':10*n}},
 'result':json.dumps({'schema_version':1,'status':'COMPLETED','output':'ok'})}))
''')

    def test_claude_resume_measures_the_round_without_reported_effort(self):
        self.usage_fixture()
        first = dispatch(parse_request('claude opus high [one]'), self.host).results[0]
        self.assertEqual(first['status'], 'COMPLETED', first)
        self.assertIs(first['usage']['identity_attested'], True)
        self.assertEqual(first['usage']['skills_note'], 'not detected')
        second = dispatch(parse_request('claude opus high [two] --session ' + first['session_id']),
                          self.host).results[0]
        self.assertEqual(second['status'], 'COMPLETED', second)
        usage = second['usage']
        self.assertIsNone(usage['delta_reason'])
        self.assertEqual(usage['tokens'], {'input': 100, 'cached_input': 0, 'cache_write': 0,
                                           'output': 10, 'reasoning': 0})
        self.assertEqual(usage['cost_usd'], 0.25)
        self.assertEqual(usage['session_total']['cost_usd'], 0.5)

    def test_first_run_of_an_adopted_session_has_no_round_baseline(self):
        from cross_agent_request import resolve_agents
        from cross_agent_state import _registry, adopt_session
        self.usage_fixture(calls_before=3)
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        sid = adopt_session(self.host.task_context, agent, 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        with _registry(self.host.task_context) as data:
            self.assertIs(data['sessions'][sid]['adopted'], True)
        first = dispatch(parse_request('claude opus high [one] --session ' + sid), self.host).results[0]
        self.assertEqual(first['status'], 'COMPLETED', first)
        usage = first['usage']
        self.assertIsNone(usage['tokens'])
        self.assertIsNone(usage['cost_usd'])
        self.assertEqual(usage['delta_reason'], 'adopted session: no baseline')
        self.assertFalse(usage['target_total']['complete'])
        self.assertEqual(usage['session_total']['tokens']['input'], 400)
        self.assertEqual(usage['session_total']['cost_usd'], 1.0)
        self.assertTrue(usage['session_total']['complete'])
        second = dispatch(parse_request('claude opus high [two] --session ' + sid), self.host).results[0]
        self.assertEqual(second['usage']['tokens']['input'], 100)
        self.assertEqual(second['usage']['cost_usd'], 0.25)
        self.assertIsNone(second['usage']['delta_reason'])

    def test_model_basis_records_how_the_model_was_observed(self):
        from cross_agent import _provider
        run = self.root / 'basis'
        run.mkdir()
        result = json.dumps({'schema_version': 1, 'status': 'COMPLETED', 'output': 'ok'})
        for envelope, basis in (
                ({'session_id': SESSION, 'modelUsage': {'claude-opus-5-5': {}}}, 'launch-flag+modelUsage'),
                ({'session_id': SESSION, 'model': 'claude-opus-5-5'}, 'provider'),
                ({'session_id': SESSION, 'modelUsage': {'claude-sonnet-5-5': {}}}, None)):
            with self.subTest(basis=basis):
                (run / 'stdout.json').write_text(json.dumps(dict(envelope, result=result)))
                _, observed, _ = _provider('claude-code', run, 'claude-opus-5-5')
                self.assertEqual(observed['model_basis'], basis)

    def test_adopt_under_another_issue_is_owned_and_same_issue_is_idempotent(self):
        from cross_agent_request import resolve_agents
        from cross_agent_state import StateError, adopt_session
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        issue = replace(self.host.task_context, canonical_issue='42')
        sid = adopt_session(issue, agent, cli)
        self.assertEqual(adopt_session(issue, agent, cli), sid)
        for other in (replace(issue, canonical_issue='43'), self.host.task_context):
            with self.subTest(issue=other.canonical_issue):
                with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_OWNED'):
                    adopt_session(other, agent, cli)

    def test_transcript_lookup_uses_the_child_environment(self):
        import os
        from unittest.mock import patch
        from cross_agent import adopt, claude_transcript, status
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        child_home = self.root / 'child-home'
        config_dir = self.root / 'child-config'
        row = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'child'}]}}
        for env, root in (({'HOME': str(child_home)}, child_home / '.claude'),
                          ({'HOME': str(child_home), 'CLAUDE_CONFIG_DIR': str(config_dir)}, config_dir)):
            with self.subTest(root=root):
                registry = self.root / ('registry-' + root.name)
                host = replace(self.host, environment=dict(self.host.environment, **env),
                               task_context=replace(self.host.task_context, registry_path=str(registry)))
                path = claude_transcript(self.root, cli, root)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(row) + '\n')
                ambient = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
                ambient['HOME'] = str(self.root / 'launcher-home')
                with patch.dict(os.environ, ambient, clear=True):
                    sid = adopt(host, parse_request('claude opus high [x]'), cli)
                    [entry] = status(host)['sessions']
                self.assertEqual(entry['session_id'], sid)
                self.assertEqual(entry['last_text'], 'child')

    def adopted_finished_run(self, started_at, finished_at):
        from cross_agent_request import resolve_agents
        from cross_agent_state import _registry, adopt_session, finish_session, reserve_session, SessionDecision
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        sid = adopt_session(self.host.task_context, agent, cli)
        with _registry(self.host.task_context) as data:
            task_id = data['sessions'][sid]['task_id']
        decision = SessionDecision('READY', 'RESUME', task_id, sid, True, cli, 'claude-code',
                                   'claude-opus-5-5', 'high', 'execution', 'execution')
        run_id = reserve_session(self.host.task_context, decision, str(self.root / 'run'))
        finish_session(self.host.task_context, sid, run_id, 'completed')
        with _registry(self.host.task_context) as data:
            data['sessions'][sid]['runs'][-1].update(started_at=started_at, finished_at=finished_at)
        return sid

    def test_status_elapsed_of_a_finished_run_stops_at_finished_at(self):
        import time
        from cross_agent import status
        started = time.time() - 86400
        self.adopted_finished_run(started, started + 10)
        [row] = status(self.host)['sessions']
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['elapsed_seconds'], 10)

    def reserved_run(self, label):
        from cross_agent_request import resolve_agents
        from cross_agent_state import SessionDecision, _registry, adopt_session, reserve_session
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        sid = adopt_session(self.host.task_context, agent, cli)
        with _registry(self.host.task_context) as data:
            task_id = data['sessions'][sid]['task_id']
        decision = SessionDecision('READY', 'RESUME', task_id, sid, True, cli, 'claude-code',
                                   'claude-opus-5-5', 'high', 'execution', 'execution')
        return sid, reserve_session(self.host.task_context, decision, str(self.root / 'run'), label=label)

    def test_status_rows_carry_the_run_label_or_none(self):
        from cross_agent import status
        self.reserved_run('wd')
        [row] = status(self.host)['sessions']
        self.assertEqual(row['label'], 'wd')

    def test_status_row_without_label_is_none(self):
        from cross_agent import status
        self.reserved_run(None)
        [row] = status(self.host)['sessions']
        self.assertIsNone(row['label'])

    def test_status_marks_a_dead_pid_as_not_alive(self):
        import subprocess
        from cross_agent import status
        from cross_agent_state import _registry
        sid, _ = self.reserved_run('wd')
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        with _registry(self.host.task_context) as data:
            data['sessions'][sid]['runs'][-1]['pid'] = child.pid
        [row] = status(self.host)['sessions']
        self.assertIs(row['alive'], False)
        self.assertEqual(row['label'], 'wd')

    def test_invalid_labels_are_rejected_by_the_state_layer(self):
        from cross_agent_state import StateError
        for bad in ('a\nb', 'x' * 41, '', 5, 'a\u0085b', 'a\u009bb'):
            with self.subTest(bad=bad), self.assertRaises(StateError):
                self.reserved_run(bad)

    def test_stored_bad_label_is_rejected_when_the_registry_is_read(self):
        from cross_agent import status
        from cross_agent_state import StateError
        self.reserved_run('wd')
        path = Path(self.host.task_context.registry_path) / 'registry.json'
        original = path.read_text()
        for bad in ('a\u0085b', 'a\nb', 'x' * 41, ''):
            with self.subTest(bad=bad):
                path.write_text(original.replace('"label": "wd"', '"label": %s' % json.dumps(bad)))
                with self.assertRaisesRegex(StateError, 'CORRUPT_REGISTRY'):
                    status(self.host)

    def test_status_never_writes_the_registry(self):
        import os
        import time
        from cross_agent import status
        started = time.time() - 60
        self.adopted_finished_run(started, started + 1)
        registry = Path(self.host.task_context.registry_path)
        before = (sorted(p.name for p in registry.iterdir()), (registry / 'registry.json').read_bytes(),
                  os.stat(registry / 'registry.json').st_mtime_ns, os.stat(registry / 'registry.json').st_ino)
        self.assertEqual(len(status(self.host)['sessions']), 1)
        after = (sorted(p.name for p in registry.iterdir()), (registry / 'registry.json').read_bytes(),
                 os.stat(registry / 'registry.json').st_mtime_ns, os.stat(registry / 'registry.json').st_ino)
        self.assertEqual(after, before)
        missing = replace(self.host, task_context=replace(self.host.task_context,
                                                          registry_path=str(self.root / 'absent')))
        self.assertEqual(status(missing)['sessions'], [])
        self.assertFalse((self.root / 'absent').exists())

    def estimate_for(self, main_rows, subagent_files=()):
        """--status estimate of an adopted session with fake transcripts under a private config dir."""
        from cross_agent import status
        from cross_agent_request import resolve_agents
        from cross_agent_state import adopt_session
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        if main_rows is not None:
            path = self.write_transcript(cli, main_rows)
            for number, rows in enumerate(subagent_files):
                folder = path.with_suffix('') / 'subagents'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / ('agent-%d.jsonl' % number)).write_text(
                    ''.join(json.dumps(row) + '\n' for row in rows))
        agent = resolve_agents(parse_request('claude opus high [x]'), self.host.config, 'claude')[0]
        adopt_session(self.host.task_context, agent, cli)
        [row] = status(self.host, self.root / '.claude')['sessions']
        return row['estimate']

    @staticmethod
    def usage_row(message_id, model='claude-opus-5-5', sidechain=False, **usage):
        return {'type': 'assistant', 'isSidechain': sidechain, 'requestId': 'req-' + message_id,
                'message': {'id': message_id, 'model': model, 'content': [], 'usage': usage}}

    def test_estimate_counts_each_message_once_and_prices_cache_tokens(self):
        usage = dict(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=2000,
                     cache_read_input_tokens=10000)
        estimate = self.estimate_for([self.usage_row('m1', **usage), self.usage_row('m1', **usage),
                                      'not json', {'type': 'user'}])
        self.assertEqual(estimate['tokens'], usage)
        # Opus 5.5 per million: 4.00 input, 5.00 cache write, 0.20 cache hit, 20.00 output.
        self.assertEqual(estimate['cost_usd'], 0.026)
        self.assertEqual(estimate['basis'], 'transcript-estimate')
        self.assertEqual(estimate['prices_as_of'], transport.PRICES_AS_OF)
        self.assertEqual(estimate['subagent_files'], 0)
        self.assertEqual(estimate['unpriced_models'], [])
        self.assertIs(estimate['complete'], True)
        self.assertEqual(estimate['unread_files'], [])

    def test_estimate_includes_subagent_transcripts_once(self):
        main = [self.usage_row('m1', input_tokens=10, output_tokens=1),
                # A subagent row repeated in the main file must not count twice.
                self.usage_row('s1', 'claude-sonnet-5-5', True, input_tokens=100, output_tokens=10)]
        sub = [self.usage_row('s1', 'claude-sonnet-5-5', True, input_tokens=100, output_tokens=10),
               self.usage_row('s2', 'claude-sonnet-5-5', True, input_tokens=200, output_tokens=20)]
        estimate = self.estimate_for(main, [sub])
        self.assertEqual(estimate['subagent_files'], 1)
        self.assertEqual(estimate['tokens']['input_tokens'], 310)
        self.assertEqual(estimate['by_model']['claude-sonnet-5-5']['tokens']['output_tokens'], 30)
        self.assertEqual(estimate['by_model']['claude-opus-5-5']['tokens']['input_tokens'], 10)

    def test_estimate_with_an_unpriced_model_has_no_cost(self):
        estimate = self.estimate_for([
            self.usage_row('m1', input_tokens=10, output_tokens=1),
            self.usage_row('m2', 'claude-mystery-1', input_tokens=10, output_tokens=1),
            self.usage_row('m3', '<synthetic>', input_tokens=0, output_tokens=0)])
        self.assertIsNone(estimate['cost_usd'])
        self.assertEqual(estimate['unpriced_models'], ['claude-mystery-1'])
        self.assertIsNotNone(estimate['by_model']['claude-opus-5-5']['cost_usd'])

    def test_unreadable_transcripts_never_look_complete(self):
        import os
        if os.geteuid() == 0:
            self.skipTest('root reads mode 000 files')
        main = [self.usage_row('m1', input_tokens=10, output_tokens=1)]
        sub = [self.usage_row('s1', input_tokens=100, output_tokens=10)]
        cli = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        from cross_agent import claude_transcript, transcript_estimate
        path = claude_transcript(self.root, cli, self.root / '.claude')
        self.estimate_for(main, [sub])
        agent_file = path.with_suffix('') / 'subagents' / 'agent-0.jsonl'
        agent_file.chmod(0)
        self.addCleanup(agent_file.chmod, 0o600)
        partial = transcript_estimate(path)
        self.assertIs(partial['complete'], False)
        self.assertEqual(partial['unread_files'], [str(agent_file)])
        self.assertEqual(partial['tokens']['input_tokens'], 10)
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        self.assertIsNone(transcript_estimate(path))

    def test_row_without_required_counts_is_unknown_and_does_not_hide_a_full_copy(self):
        partial = self.usage_row('m1', input_tokens=5)
        partial['message']['usage']['output_tokens'] = None
        estimate = self.estimate_for([partial, self.usage_row('m1', input_tokens=10, output_tokens=3),
                                      self.usage_row('m2', output_tokens=7)])
        self.assertIs(estimate['complete'], False)
        self.assertEqual(estimate['tokens']['input_tokens'], 10)
        self.assertEqual(estimate['tokens']['output_tokens'], 3)

    def test_non_object_rows_and_non_string_ids_are_skipped_safely(self):
        odd_id = self.usage_row('m1', input_tokens=10, output_tokens=1)
        odd_id['message']['id'] = ['not', 'a', 'string']
        odd_message = {'type': 'assistant', 'message': ['usage']}
        estimate = self.estimate_for([['usage'], 'usage', odd_message, odd_id, odd_id])
        self.assertIs(estimate['complete'], True)
        # The list id falls back to requestId, so both copies count once.
        self.assertEqual(estimate['tokens']['input_tokens'], 10)

    def test_largest_output_copy_of_a_message_wins(self):
        estimate = self.estimate_for([self.usage_row('m1', input_tokens=10, output_tokens=1),
                                      self.usage_row('m1', input_tokens=10, output_tokens=50),
                                      self.usage_row('m1', input_tokens=10, output_tokens=20)])
        self.assertEqual(estimate['tokens']['output_tokens'], 50)

    def test_estimate_is_null_without_a_transcript(self):
        self.assertIsNone(self.estimate_for(None))

    def test_without_pass_env_the_variable_stays_out(self):
        host = replace(self.host, pass_env=())
        entry = dispatch(parse_request('claude opus high [orchestrate]'), host).results[0]
        self.assertEqual(json.loads(entry['output'])['ssh'], None)


def full_context(role='execution', profile='full', pol=None):
    return transport.ProfileContext('task', 'parent', role, profile, pol)


class FullAccessProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()

    def codex(self, builder, profile, context):
        return builder('codex', self.repo, self.repo, 'gpt-6.1-sol', 'high', SESSION,
                       untrusted=[str(self.repo)], profile=profile, context=context)

    def claude(self, builder, profile, context):
        return builder('claude-code', self.repo, self.repo, 'claude-opus-5-5', 'high', SESSION,
                       profile=profile, context=context)

    def test_codex_sandbox_follows_the_profile(self):
        pol = policy(self.repo, family='codex', config_isolated=True,
                     actions=('read', 'write-workspace'))
        run = self.codex(transport.build_run_command, 'full', full_context())
        self.assertEqual(run[run.index('-s') + 1], 'danger-full-access')
        resume = self.codex(transport.build_resume_command, 'full', full_context())
        self.assertIn('sandbox_mode="danger-full-access"', resume)
        run = self.codex(transport.build_run_command, 'no-network', full_context(profile='no-network', pol=pol))
        self.assertEqual(run[run.index('-s') + 1], 'workspace-write')
        run = self.codex(transport.build_run_command, 'execution', full_context(profile='execution', pol=pol))
        self.assertEqual(run[run.index('-s') + 1], 'workspace-write')
        run = self.codex(transport.build_run_command, 'read-only', None)
        self.assertEqual(run[run.index('-s') + 1], 'read-only')
        resume = self.codex(transport.build_resume_command, 'read-only', None)
        self.assertIn('sandbox_mode="read-only"', resume)

    def test_claude_full_adds_web_tools_and_keeps_mcp_denied(self):
        argv = self.claude(transport.build_run_command, 'full', full_context())
        allowed = argv[argv.index('--allowedTools') + 1].split(',')
        for tool in ('Bash', 'Edit', 'Write', 'Agent', 'Skill', 'WebFetch', 'WebSearch'):
            self.assertIn(tool, allowed)
        self.assertEqual(argv[argv.index('--tools') + 1], argv[argv.index('--allowedTools') + 1])
        self.assertEqual(argv[argv.index('--disallowedTools') + 1], 'mcp__*')
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'user,project,local')
        resume = self.claude(transport.build_resume_command, 'full', full_context())
        self.assertEqual(resume[-2:], ['--resume', SESSION])
        no_network = self.claude(transport.build_run_command, 'no-network',
                                 full_context(profile='no-network', pol=policy(self.repo)))
        self.assertNotIn('WebFetch', no_network[no_network.index('--allowedTools') + 1])
        self.assertIn('WebFetch', no_network[no_network.index('--disallowedTools') + 1])

    def test_full_needs_a_trusted_execution_context(self):
        with self.assertRaisesRegex(transport.CrossReviewError, 'full needs trusted dispatch context'):
            self.codex(transport.build_run_command, 'full', None)
        with self.assertRaisesRegex(transport.CrossReviewError, 'role/profile mismatch'):
            self.codex(transport.build_run_command, 'full', full_context(role='consultation', profile='read-only'))

    def test_antigravity_refuses_every_writer_profile(self):
        for profile in ('no-network', 'full', 'execution'):
            with self.subTest(profile=profile), self.assertRaisesRegex(transport.CrossReviewError, 'UNAVAILABLE'):
                transport.build_run_command('antigravity', self.repo, self.repo, 'gemini-4-argon', 'low',
                                            SESSION, profile=profile,
                                            context=full_context(profile=profile, pol=policy(self.repo)))

    def test_unknown_profile_is_unavailable(self):
        with self.assertRaisesRegex(transport.CrossReviewError, 'unsupported profile'):
            self.codex(transport.build_run_command, 'bogus', None)

    def test_environment_carries_the_canonical_profile(self):
        env = transport.reviewer_environment({'PATH': '/bin'}, 'codex', context=full_context())
        self.assertEqual(env['CROSS_AGENT_PROFILE'], 'full')
        env = transport.reviewer_environment({'PATH': '/bin'}, 'claude-code',
                                             context=full_context(profile='execution', pol=policy(self.repo)))
        self.assertEqual(env['CROSS_AGENT_PROFILE'], 'no-network')

    def test_skills_note_for_claude_writer_profiles(self):
        tokens = dict.fromkeys(transport.TOKEN_KEYS, 1)
        for profile, note in (('full', 'not detected'), ('no-network', 'not detected'),
                              ('execution', 'not detected'), ('read-only', 'disabled by policy')):
            with self.subTest(profile=profile):
                usage = transport.build_usage('claude-code', 'claude-opus-5-5', 'high', tokens, 0.5,
                                              [], profile=profile)
                self.assertEqual(usage['skills_note'], note)


if __name__ == '__main__':
    unittest.main()
