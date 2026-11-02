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
        self.assertNotIn('--restricted', argv)
        self.assertNotIn('--safe-mode', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[-2:], ['--session-id', SESSION])

    def test_orchestrator_resume_keeps_the_same_policy(self):
        argv = self.build(policy(self.repo), transport.build_resume_command)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertEqual(argv[-2:], ['--resume', SESSION])

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

    def test_without_pass_env_the_variable_stays_out(self):
        host = replace(self.host, pass_env=())
        entry = dispatch(parse_request('claude opus high [orchestrate]'), host).results[0]
        self.assertEqual(json.loads(entry['output'])['ssh'], None)


if __name__ == '__main__':
    unittest.main()
