import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cross_agent_cli as cli
from cross_agent import HostContext
from cross_agent_request import AgentChoice, Request, load_config
from cross_agent_state import TaskContext, finish_session, reserve_session, select_session

SCRIPT = str(Path(__file__).with_name('cross_agent.py'))
GIT = '/usr/bin/git'
FAKE_CODEX = '''#!/usr/bin/env python3
import json, sys, time, uuid
from pathlib import Path
brief = sys.stdin.read()
args = sys.argv[1:]
if 'SLEEP' in brief:
    time.sleep(30)
sid = str(uuid.uuid4())
print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
print(json.dumps({'type': 'session_meta', 'payload': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'low'}}), flush=True)
Path(args[args.index('-o') + 1]).write_text(json.dumps(
    {'schema_version': 1, 'status': 'COMPLETED', 'output': ' '.join(args)}))
'''


class CliFixture(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(GIT):
            self.skipTest('git is required for the repository fixture')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        (self.bin / 'git').symlink_to(GIT)
        (self.bin / 'codex').write_text(FAKE_CODEX)
        (self.bin / 'codex').chmod(0o700)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in (['init'], ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                                'commit', '--allow-empty', '-m', 'initial']):
            subprocess.run([GIT, '-C', str(self.repo), *args], check=True, capture_output=True)
        (self.root / 'brief.txt').write_text('question')
        self.env = {'PATH': str(self.bin), 'HOME': str(self.home), 'CLAUDE_CODE_SESSION_ID': 'conv-1',
                    'LANG': 'C'}

    def run_cli(self, *args, env=None, input=None):
        result = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                                env=self.env if env is None else env, input=input, cwd=str(self.repo))
        try:
            document = json.loads(result.stdout) if result.stdout.strip().startswith(('{', '[')) else None
        except ValueError:
            document = None
        return result, document

    def state(self):
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            return cli.state_dir(self.repo)

    def brief(self):
        return ['--assignment-file', str(self.root / 'brief.txt')]


class RunTests(CliFixture):
    def test_run_defaults_to_full_and_builds_private_state(self):
        result, doc = self.run_cli('run', '--repo', str(self.repo), *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = doc['results'][0]
        self.assertEqual((entry['profile'], entry['role']), ('full', 'execution'))
        self.assertIn('danger-full-access', entry['output'])
        self.assertEqual(entry['granted']['network'], 'allowed')
        self.assertEqual(oct(self.state().stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct((self.state() / 'registry').stat().st_mode & 0o777), '0o700')

    def test_mode_read_only_narrows_the_role(self):
        result, doc = self.run_cli('run', '--mode', 'read-only', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = doc['results'][0]
        self.assertEqual((entry['profile'], entry['role']), ('read-only', 'consultation'))
        self.assertIn('-s read-only', entry['output'])

    def test_mode_no_network_needs_a_host_policy(self):
        result, doc = self.run_cli('run', '--mode', 'no-network', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(doc['results'][0]['status'], 'UNAVAILABLE')
        self.assertIn('execution needs verified scoped host policy', doc['results'][0]['reason'])

    def test_mode_cannot_widen_a_consultation_host(self):
        made, _ = self.run_cli('init-host', '--repo', str(self.repo), '--host-family', 'claude-code',
                               '--conversation-id', 'c', '--role', 'consultation')
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        result, doc = self.run_cli('run', '--mode', 'full', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(doc['status'], 'BLOCKED')
        self.assertIn('widens', doc['reason'])
        result, doc = self.run_cli('run', *self.brief(), 'codex sol low')
        self.assertEqual(doc['results'][0]['profile'], 'read-only')

    def test_unknown_host_family_is_blocked_with_the_fix(self):
        env = {k: v for k, v in self.env.items() if k != 'CLAUDE_CODE_SESSION_ID'}
        result, doc = self.run_cli('run', *self.brief(), 'codex sol low', env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('host family unknown: pass --host-family claude-code|codex or --host-context',
                      doc['reason'])
        result, doc = self.run_cli('run', '--host-family', 'claude-code', '--conversation-id', 'c',
                                   *self.brief(), 'codex sol low', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_outer_flags_as_separate_arguments_and_duplicates(self):
        result, doc = self.run_cli('run', '--task', 'new', '--new', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, doc = self.run_cli('run', '--task', 'other', *self.brief(), 'codex sol low --task new')
        self.assertEqual(result.returncode, 2)
        self.assertIn('duplicate outer flag', doc['reason'])

    def test_assignment_from_stdin(self):
        result, doc = self.run_cli('run', '--assignment-file', '-', 'codex sol low', input='question from stdin')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        brief = Path(doc['results'][0]['artifacts']['brief']).read_text()
        self.assertIn('question from stdin', brief)

    def test_dry_run_writes_nothing_and_reports_availability(self):
        result, doc = self.run_cli('run', '--dry-run', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(doc['dry_run'])
        recipient = doc['recipients'][0]
        self.assertEqual((recipient['family'], recipient['model'], recipient['effort'], recipient['profile'],
                          recipient['available']), ('codex', 'gpt-6.1-sol', 'low', 'full', True))
        self.assertEqual(recipient['granted']['network'], 'allowed')
        self.assertFalse((self.state() / 'registry').exists())
        self.assertFalse((self.state() / 'artifacts').exists())
        result, doc = self.run_cli('run', '--dry-run', *self.brief(), 'gemini flash')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(doc['recipients'][0]['available'])
        self.assertIn('requires verified adapter support', doc['recipients'][0]['reason'])
        result, doc = self.run_cli('run', '--dry-run', '--mode', 'no-network', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 1)
        recipient = doc['recipients'][0]
        self.assertEqual((recipient['available'], recipient['profile']), (False, 'no-network'))
        self.assertIn('execution needs verified scoped host policy', recipient['reason'])
        self.assertIn('codex no-network: sandbox workspace-write, no network, no write access to .git',
                      doc['warnings'])

    def test_legacy_invocation_still_works(self):
        host = self.root / 'host.json'
        host.write_text(json.dumps({'task_context': {'registry_path': str(self.root / 'reg'),
            'repo': str(self.repo), 'conversation_id': 'c'}, 'role': 'consultation', 'profile': 'read-only',
            'host_family': 'claude', 'workspace': str(self.repo), 'artifact_root': str(self.root / 'art'),
            'target_identity': {'tree': 'x'}, 'environment': {'PATH': str(self.bin), 'HOME': str(self.home)}}))
        result, doc = self.run_cli('--host-context', str(host), *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(doc['results'][0]['profile'], 'read-only')
        status, listing = self.run_cli('--host-context', str(host), '--status')
        self.assertEqual(len(listing['sessions']), 1)
        self.assertIn('result_path', listing['sessions'][0])


class InitHostTests(CliFixture):
    def test_init_host_writes_a_private_host_json_once(self):
        args = ('init-host', '--repo', str(self.repo), '--host-family', 'codex', '--conversation-id', 'c1')
        result, doc = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = Path(doc['host_context'])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        written = json.loads(path.read_text())
        self.assertEqual((written['host_family'], written['role'], written['task_context']['conversation_id']),
                         ('codex', 'execution', 'c1'))
        self.assertNotIn('target_identity', written)
        again, doc = self.run_cli(*args)
        self.assertEqual(again.returncode, 2)
        self.assertEqual(doc['status'], 'BLOCKED')
        forced, doc = self.run_cli(*args, '--force')
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)

    def test_init_host_needs_a_family(self):
        result, doc = self.run_cli('init-host', '--repo', str(self.repo), env={
            'PATH': str(self.bin), 'HOME': str(self.home)})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(doc['status'], 'BLOCKED')

    def test_same_named_repositories_do_not_share_state(self):
        other = self.root / 'elsewhere' / 'repo'
        other.mkdir(parents=True)
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            self.assertNotEqual(cli.state_dir(self.repo), cli.state_dir(other))
            self.assertEqual(cli.state_dir(self.repo), cli.state_dir(str(self.repo)))

    def test_host_json_of_another_repository_is_refused(self):
        made, doc = self.run_cli('init-host', '--repo', str(self.repo), '--host-family', 'claude-code',
                                 '--conversation-id', 'c')
        path = Path(doc['host_context'])
        written = json.loads(path.read_text())
        written['workspace'] = str(self.root / 'somewhere-else')
        path.write_text(json.dumps(written))
        result, doc = self.run_cli('run', *self.brief(), 'codex sol low')
        self.assertEqual(result.returncode, 2)
        self.assertIn('host.json belongs to another repository', doc['reason'])


class StatusTests(CliFixture):
    def test_status_text_json_and_filters(self):
        self.run_cli('run', '--label', 'first', *self.brief(), 'codex sol low')
        self.run_cli('run', '--label', 'second', '--new', *self.brief(), 'codex sol low')
        text, _ = self.run_cli('status')
        lines = [line for line in text.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, text.stdout)
        self.assertIn('codex', lines[0])
        as_json, doc = self.run_cli('status', '--json')
        self.assertEqual(len(doc['sessions']), 2)
        sid = doc['sessions'][0]['session_id']
        _, one = self.run_cli('status', '--json', '--session', sid)
        self.assertEqual([r['session_id'] for r in one['sessions']], [sid])
        _, active = self.run_cli('status', '--json', '--active')
        self.assertEqual(active['sessions'], [])
        self.assertTrue(all(row['result_path'].endswith('result.json') for row in doc['sessions']))


class StatusTextTests(CliFixture):
    def test_text_row_shows_the_result_path(self):
        self.run_cli('run', '--label', 'first', *self.brief(), 'codex sol low')
        text, _ = self.run_cli('status')
        self.assertTrue(text.stdout.strip().endswith('result.json'), text.stdout)
        self.assertIn('first', text.stdout)
        row = {'session_id': 's', 'state': 'running', 'family': 'codex', 'model': 'm', 'effort': 'low',
               'elapsed_seconds': None, 'label': None, 'result_path': None}
        self.assertTrue(cli.format_rows([row]).endswith(' - -'))


class StateSafetyTests(CliFixture):
    ARGS = ('init-host', '--host-family', 'codex', '--conversation-id', 'c1')

    def test_symlinked_or_open_state_dir_is_refused(self):
        state = self.state()
        state.parent.mkdir(parents=True, mode=0o700)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        state.symlink_to(elsewhere)
        result, doc = self.run_cli(*self.ARGS, '--repo', str(self.repo))
        self.assertEqual((result.returncode, doc['status']), (2, 'BLOCKED'), result.stdout + result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])
        state.unlink()
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        result, doc = self.run_cli(*self.ARGS, '--repo', str(self.repo))
        self.assertEqual((result.returncode, doc['status']), (2, 'BLOCKED'), result.stdout + result.stderr)
        self.assertFalse((state / 'host.json').exists())

    def test_force_does_not_write_through_a_staging_symlink(self):
        args = cli.build_parser('init-host').parse_args([*self.ARGS[1:], '--repo', str(self.repo), '--force'])
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            state = cli.state_dir(self.repo)
            cli.ensure_private_dirs(state)
            victim = self.root / 'victim.txt'
            victim.write_text('keep')
            (state / '.host.json.4242').symlink_to(victim)
            with patch('cross_agent_cli.os.getpid', return_value=4242):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli._init_host(args), 0)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertEqual(oct((state / 'host.json').stat().st_mode & 0o777), '0o600')
        self.assertEqual(sorted(p.name for p in state.iterdir()), ['.host.json.4242', 'host.json'])


class HelperTests(unittest.TestCase):
    def test_merge_outer_flags(self):
        self.assertEqual(cli.merge_outer_flags('codex [a]', None, None, False), 'codex [a]')
        self.assertEqual(cli.merge_outer_flags('codex [a]', 'new', 's 1', True),
                         "codex [a] --task new --session 's 1' --new")
        self.assertEqual(cli.merge_outer_flags(None, None, None, True), '--new')
        self.assertIsNone(cli.merge_outer_flags(None, None, None, False))

    def test_filter_and_format_rows(self):
        rows = [{'session_id': 'a', 'state': 'running', 'alive': True, 'family': 'codex',
                 'model': 'm', 'effort': 'low', 'elapsed_seconds': 3, 'label': 'x', 'result_path': None},
                {'session_id': 'b', 'state': 'completed', 'alive': None, 'family': 'codex',
                 'model': 'm', 'effort': 'low', 'elapsed_seconds': 5, 'label': None, 'result_path': '/r/result.json'}]
        self.assertEqual([r['session_id'] for r in cli.filter_rows(rows, active=True)], ['a'])
        self.assertEqual([r['session_id'] for r in cli.filter_rows(rows, session='b')], ['b'])
        text = cli.format_rows(rows)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn('running', text.splitlines()[0])
        self.assertIn('x', text.splitlines()[0])

    def test_detect_host(self):
        self.assertEqual(cli.detect_host({'CLAUDE_CODE_SESSION_ID': 's'}), ('claude-code', 's'))
        self.assertEqual(cli.detect_host({'CODEX_THREAD_ID': 't'}), ('codex', 't'))
        self.assertEqual(cli.detect_host({}), (None, None))

    def test_git_target_identity(self):
        if not os.path.exists(GIT):
            self.skipTest('git is required')
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([GIT, '-C', tmp, 'init'], check=True, capture_output=True)
            subprocess.run([GIT, '-C', tmp, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                            'commit', '--allow-empty', '-m', 'i'], check=True, capture_output=True)
            old = cli.GIT
            cli.GIT = GIT
            try:
                before = cli.git_target_identity(tmp)
                (Path(tmp) / 'new.txt').write_text('x')
                after = cli.git_target_identity(tmp)
                self.assertEqual(set(before), {'tree', 'head', 'status_digest'})
                self.assertEqual(before['tree'], after['tree'])
                self.assertNotEqual(before['status_digest'], after['status_digest'])
                with tempfile.TemporaryDirectory() as plain:
                    with self.assertRaises(ValueError):
                        cli.git_target_identity(plain)
            finally:
                cli.GIT = old


class ControlFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        config = {'schema_version': 1, 'agents': {'codex': {
            'models': {'gpt-6.1-sol': ['low']}, 'default_model': 'gpt-6.1-sol', 'default_effort': 'low',
            'profiles': ['read-only', 'no-network', 'full'],
            'capabilities': {'read-only': 'supported', 'no-network': 'supported', 'full': 'supported'}}}}
        self.ctx = TaskContext(str(self.root / 'registry'), str(self.root), 'conversation')
        self.host = HostContext(load_config(host_config=config, role='execution', profile='full'),
                                self.ctx, 'claude', str(self.root), str(self.root / 'artifacts'), {})
        agent = AgentChoice('codex', 'gpt-6.1-sol', 'low', 'execution', 'full', False)
        self.decision = select_session(Request('x'), self.ctx, agent, 'execution', 'full')
        (self.root / 'artifact').mkdir()

    def start_run(self):
        return reserve_session(self.ctx, self.decision, str(self.root / 'artifact'))

    def finish_run(self, run_id):
        finish_session(self.ctx, self.decision.session_id, run_id, 'completed')


class CancelTests(ControlFixture):
    def test_unknown_not_running_and_pid_mismatch(self):
        sent = []
        kill = lambda pid, sig: sent.append((pid, sig))
        self.assertEqual(cli.cancel(self.host, 'nope', kill=kill)['reason'], 'UNKNOWN_SESSION')
        self.assertEqual(cli.cancel(self.host, self.decision.session_id, kill=kill)['reason'], 'NOT_RUNNING')
        run_id = self.start_run()
        for other in ('vim notes', 'cat cross_agent.py.bak', 'python3 /x/not_cross_agent.py', '',
                      'python3 /other/worker.py /x/cross_agent.py', '/usr/bin/python3 /a b/cross_agent.py'):
            with self.subTest(command=other):
                mismatch = cli.cancel(self.host, self.decision.session_id, kill=kill, command=lambda pid: other)
                self.assertEqual((mismatch['status'], mismatch['reason']), ('BLOCKED', 'PID_MISMATCH'))
        self.assertEqual(sent, [])
        self.finish_run(run_id)
        self.assertEqual(cli.cancel(self.host, self.decision.session_id, kill=kill)['reason'], 'NOT_RUNNING')

    def test_cancel_signals_the_launcher_with_sigterm(self):
        self.start_run()
        for command in ('python3 /x/cross_agent.py run codex', '/usr/bin/python3 -B /x/cross_agent.py --status'):
            with self.subTest(command=command):
                sent = []
                answer = cli.cancel(self.host, self.decision.session_id,
                                    kill=lambda pid, sig: sent.append((pid, sig)), command=lambda pid: command)
                self.assertEqual((answer['status'], answer['signalled'], answer['pid']),
                                 ('COMPLETED', True, os.getpid()))
                self.assertEqual(sent, [(os.getpid(), signal.SIGTERM)])

    def test_dead_process_is_not_running(self):
        self.start_run()
        with patch('cross_agent_cli.os.kill', side_effect=ProcessLookupError):
            answer = cli.cancel(self.host, self.decision.session_id, kill=lambda *a: None,
                                command=lambda pid: 'python3 /x/cross_agent.py')
        self.assertEqual(answer['reason'], 'NOT_RUNNING')

    def test_process_vanishing_before_the_signal_is_not_running(self):
        self.start_run()
        def kill(pid, sig):
            raise ProcessLookupError()
        answer = cli.cancel(self.host, self.decision.session_id, kill=kill,
                            command=lambda pid: 'python3 /x/cross_agent.py run')
        self.assertEqual((answer['status'], answer['reason']), ('BLOCKED', 'NOT_RUNNING'))


class CancelStartTimeTests(ControlFixture):
    def test_process_started_after_the_run_is_a_reused_pid(self):
        self.start_run()
        sent = []
        run_started = cli._session_and_run(self.host, self.decision.session_id)[1]['started_at']
        command = lambda pid: 'python3 /x/cross_agent.py run'
        for label, started, expected in (('later', run_started + 3600, 'PID_MISMATCH'),
                                         ('unreadable', None, 'PID_MISMATCH'),
                                         ('earlier', run_started - 60, None),
                                         ('within tolerance', run_started + 1, None)):
            with self.subTest(label=label):
                answer = cli.cancel(self.host, self.decision.session_id,
                                    kill=lambda pid, sig: sent.append(pid), command=command,
                                    started=lambda pid, started=started: started)
                self.assertEqual(answer.get('reason'), expected)
        self.assertEqual(sent, [os.getpid()] * 2)

    def test_elapsed_time_parsing(self):
        self.assertEqual(cli._parse_etime('05'), 5)
        self.assertEqual(cli._parse_etime('01:05'), 65)
        self.assertEqual(cli._parse_etime('2:01:05'), 7265)
        self.assertEqual(cli._parse_etime('3-02:01:05'), 3 * 86400 + 7265)
        for bad in ('', 'x', '1:2:3:4', '-1', '1-x'):
            with self.subTest(bad=bad):
                self.assertIsNone(cli._parse_etime(bad))

    def test_real_process_start_is_close_to_now(self):
        if not os.path.exists(cli.PS):
            self.skipTest('/bin/ps is required')
        started = cli._process_start(os.getpid())
        self.assertIsNotNone(started)
        self.assertLessEqual(started, time.time() + 2)


class WaitTimeoutArgumentTests(CliFixture):
    def test_non_finite_or_negative_timeout_is_a_usage_error(self):
        for value in ('nan', 'inf', '-inf', '-1', 'abc'):
            with self.subTest(value=value):
                result, _ = self.run_cli('wait', 'sid', '--timeout', value)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--timeout', result.stderr)

    def test_zero_and_positive_timeouts_are_accepted_by_the_parser(self):
        for value in ('0', '2.5'):
            self.assertEqual(cli.build_parser('wait').parse_args(['sid', '--timeout', value]).timeout, float(value))


class EarlyCancelTests(ControlFixture):
    def test_sigterm_between_reservation_and_launch_is_recorded(self):
        def interrupted(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        request = Request('x', agent_groups=())
        from cross_agent import dispatch
        from cross_agent_request import parse_request
        with cli.sigterm_as_interrupt(), patch.object(cli.transport, 'build_run_command', interrupted):
            result = dispatch(parse_request('codex sol low --new [work]'), self.host)
        entry = result.results[0]
        self.assertEqual((entry['status'], entry['reason']), ('CANCELLED', 'SIGNAL'))
        self.assertEqual(core_state_last_run(self.ctx, entry['session_id'])['status'], 'failed')
        self.assertTrue(Path(entry['artifacts']['result']).is_file())
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class PublishTests(ControlFixture):
    def test_result_appears_only_complete(self):
        import cross_agent
        final = self.root / 'result.json'
        seen = []
        real = os.replace
        def spy(src, dst):
            seen.append((final.exists(), json.loads(Path(src).read_text())))
            real(src, dst)
        with patch.object(cross_agent.os, 'replace', spy):
            cross_agent._publish(final, {'status': 'COMPLETED'})
        self.assertEqual(seen, [(False, {'status': 'COMPLETED'})])
        self.assertEqual(oct(final.stat().st_mode & 0o777), '0o600')
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.result')], [])


class ReservationCancelTests(ControlFixture):
    def test_sigterm_while_the_reservation_is_published_still_releases_the_run(self):
        import cross_agent
        real = cross_agent.reserve_session
        def late_signal(*args, **kwargs):
            run_id = real(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
            return run_id
        with cli.sigterm_as_interrupt(), patch.object(cross_agent, 'reserve_session', late_signal):
            result = cross_agent.dispatch(cross_agent.parse_request('codex sol low --new [work]'), self.host)
        entry = result.results[0]
        self.assertEqual((entry['status'], entry['reason']), ('CANCELLED', 'SIGNAL'))
        self.assertEqual(core_state_last_run(self.ctx, entry['session_id'])['status'], 'failed')
        self.assertTrue(Path(entry['artifacts']['result']).is_file())


def core_state_last_run(ctx, session_id):
    import cross_agent_state as store
    return store.read_registry(ctx)['sessions'][session_id]['runs'][-1]


class WaitTests(ControlFixture):
    def clock(self):
        state = {'now': 0.0}
        def sleep(seconds):
            state['now'] += seconds
        return (lambda: state['now']), sleep

    def test_finished_run_returns_at_once(self):
        result = self.root / 'result.json'
        result.write_text('{}')
        run_id = self.start_run()
        finish_session(self.ctx, self.decision.session_id, run_id, 'completed', result_path=str(result))
        clock, sleep = self.clock()
        code, row = cli.wait(self.host, self.decision.session_id, 10, sleep=sleep, clock=clock)
        self.assertEqual((code, row['timed_out'], row['state']), (0, False, 'completed'))

    def test_finished_run_without_a_result_path_is_not_success(self):
        self.finish_run(self.start_run())
        clock, sleep = self.clock()
        code, row = cli.wait(self.host, self.decision.session_id, 10, sleep=sleep, clock=clock)
        self.assertEqual((code, row['state'], row['result_missing'], row['timed_out']),
                         (1, 'completed', True, False))

    def test_running_run_times_out_with_exit_3(self):
        self.start_run()
        clock, sleep = self.clock()
        code, row = cli.wait(self.host, self.decision.session_id, 5, interval=2, sleep=sleep, clock=clock)
        self.assertEqual((code, row['timed_out'], row['state']), (3, True, 'running'))
        self.assertGreaterEqual(row['waited_seconds'], 5)

    def test_dead_launcher_ends_the_wait(self):
        self.start_run()
        clock, sleep = self.clock()
        with patch('cross_agent.os.kill', side_effect=ProcessLookupError):
            code, row = cli.wait(self.host, self.decision.session_id, 5, sleep=sleep, clock=clock)
        # No result.json will ever appear: success would be a false claim.
        self.assertEqual((code, row['alive'], row['result_missing'], row['timed_out']), (1, False, True, False))

    def test_finished_run_waits_for_the_result_file(self):
        result = self.root / 'result.json'
        run_id = self.start_run()
        finish_session(self.ctx, self.decision.session_id, run_id, 'completed', result_path=str(result))
        clock, base_sleep = self.clock()
        calls = []
        def publish_then_sleep(seconds):
            calls.append(seconds)
            result.write_text('{}')
            base_sleep(seconds)
        code, row = cli.wait(self.host, self.decision.session_id, 10, interval=2,
                             sleep=publish_then_sleep, clock=clock)
        self.assertEqual((code, row['timed_out'], len(calls)), (0, False, 1))
        result.unlink()
        clock, sleep = self.clock()
        code, row = cli.wait(self.host, self.decision.session_id, 4, interval=2, sleep=sleep, clock=clock)
        self.assertEqual((code, row['timed_out'], row['state']), (3, True, 'completed'))

    def test_unknown_session_is_blocked(self):
        clock, sleep = self.clock()
        code, answer = cli.wait(self.host, 'nope', 5, sleep=sleep, clock=clock)
        self.assertEqual((code, answer['status'], answer['reason']), (2, 'BLOCKED', 'UNKNOWN_SESSION'))


class CancelEndToEnd(CliFixture):
    def test_cancel_stops_a_running_launcher_and_wait_sees_it(self):
        if not os.path.exists(cli.PS):
            self.skipTest('/bin/ps is required')
        (self.root / 'sleep.txt').write_text('SLEEP')
        proc = subprocess.Popen([sys.executable, SCRIPT, 'run', '--repo', str(self.repo),
                                 '--assignment-file', str(self.root / 'sleep.txt'), 'codex sol low'],
                                env=self.env, cwd=str(self.repo), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        row = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and row is None:
            _, doc = self.run_cli('status', '--json', '--active')
            row = doc['sessions'][0] if doc and doc['sessions'] else None
            time.sleep(0.2)
        self.assertIsNotNone(row, 'launcher run never appeared as active')
        cancelled, answer = self.run_cli('cancel', row['session_id'])
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        self.assertTrue(answer['signalled'])
        waited, final = self.run_cli('wait', row['session_id'], '--timeout', '20')
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertFalse(final['timed_out'])
        proc.wait(timeout=20)
        result = json.loads(Path(final['result_path']).read_text())
        self.assertEqual((result['status'], result['reason']), ('CANCELLED', 'SIGNAL'))


if __name__ == '__main__':
    unittest.main()
