import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from cross_agent_request import Request, AgentChoice
from cross_agent_state import (TaskContext, StateError, select_session, reserve_session,
                               update_session, finish_session)


class SnapshotReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve() / 'private'
        self.dir.mkdir(mode=0o700)
        self.ctx = TaskContext(str(self.dir), 'repo:canonical', 'conversation')
        self.write({})

    def write(self, tasks):
        """Publish like the locked writer: a complete private file swapped in with os.replace."""
        data = {'schema_version': 1, 'tasks': tasks, 'sessions': {}, 'conversations': {}}
        staging = self.dir / '.staging'
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.replace(staging, self.dir / 'registry.json')

    def replacing_fstat(self, times):
        """fstat that lets a writer replace registry.json right after the reader's open."""
        import stat
        real = os.fstat
        calls = []
        def fstat(fd):
            if stat.S_ISREG(real(fd).st_mode) and len(calls) < times:
                calls.append(fd)
                self.write({'t': {'task_id': 't', 'repo': 'repo:canonical', 'canonical_issue': None}})
            return real(fd)
        return fstat, calls

    def test_concurrent_replace_between_open_and_fstat_reads_the_new_registry(self):
        from unittest.mock import patch
        from cross_agent_state import read_registry
        fstat, calls = self.replacing_fstat(1)
        with patch('cross_agent_state.os.fstat', side_effect=fstat):
            data = read_registry(self.ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(data['tasks']), ['t'])

    def test_snapshot_retries_are_bounded(self):
        from unittest.mock import patch
        from cross_agent_state import read_registry
        fstat, calls = self.replacing_fstat(10)
        with patch('cross_agent_state.os.fstat', side_effect=fstat):
            with self.assertRaisesRegex(StateError, 'UNSAFE_REGISTRY_PATH'):
                read_registry(self.ctx)
        self.assertEqual(len(calls), 3)

    def test_snapshot_still_rejects_extra_hard_links(self):
        from cross_agent_state import read_registry
        os.link(self.dir / 'registry.json', self.dir / 'alias.json')
        with self.assertRaisesRegex(StateError, 'UNSAFE_REGISTRY_PATH'):
            read_registry(self.ctx)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ctx = TaskContext(str(self.root / 'private'), 'repo:canonical', 'conversation')
        self.agent = AgentChoice('codex', 'gpt-6.1-sol', 'low', 'review', 'read-only', False)

    def choose(self, request=None, ctx=None, agent=None, role='review', profile='read-only'):
        return select_session(request or Request('review'), ctx or self.ctx,
                              agent or self.agent, role, profile)

    def test_task_new_and_new_session(self):
        first = self.choose()
        import uuid
        uuid.UUID(first.task_id)
        again = self.choose()
        self.assertEqual(first.session_id, again.session_id)
        fresh = self.choose(Request('review', new_session=True))
        self.assertEqual(first.task_id, fresh.task_id)
        self.assertNotEqual(first.session_id, fresh.session_id)
        other = self.choose(Request('review', task_selector='new'))
        self.assertNotEqual(first.task_id, other.task_id)

    def test_issue_task_selector_roundtrip(self):
        from cross_agent_request import parse_request
        ctx = replace(self.ctx, repo='owner/repo [source]', canonical_issue='owner/repo#1')
        first = self.choose(parse_request('codex [review]'), ctx=ctx)
        request = parse_request('codex [review] --task ' + first.task_id)
        selected = self.choose(request, ctx=ctx)
        self.assertEqual(selected.status, 'READY')
        self.assertEqual(selected.task_id, first.task_id)
        self.assertEqual(selected.session_id, first.session_id)
        self.assertTrue(selected.resume)

    def test_unknown_issue_and_ambiguity(self):
        self.assertEqual(self.choose(Request('x', task_selector='unknown')).reason, 'UNKNOWN_TASK')
        first = self.choose()
        conflict = self.choose(Request('x', task_selector=first.task_id),
                               ctx=replace(self.ctx, canonical_issue='https://host/repo/issues/1',
                                           active_task_ids=(first.task_id,)))
        self.assertEqual(conflict.reason, 'ISSUE_MISMATCH')
        second = self.choose(Request('x', task_selector='new'))
        self.assertEqual(self.choose(ctx=replace(self.ctx, active_task_ids=(first.task_id, second.task_id))).reason,
                         'AMBIGUOUS_TASK')

    def test_role_model_and_profile_isolation(self):
        first = self.choose()
        changed = self.choose(agent=replace(self.agent, model='other'))
        self.assertFalse(changed.resume)
        self.assertIn('MODEL', changed.reason)
        consulted = self.choose(ctx=replace(self.ctx, preserve_review_role=True), role='consultation')
        self.assertEqual(first.session_id, consulted.session_id)
        self.assertTrue(consulted.resume)
        self.assertEqual(consulted.role, "review")
        self.assertEqual(consulted.profile, "read-only")
        profile_change = self.choose(role='execution', profile='execution')
        self.assertIn('PROFILE', profile_change.reason)
        explicit = self.choose(Request('x', session_selector=first.session_id), role='execution', profile='execution')
        self.assertEqual(explicit.reason, 'INCOMPATIBLE_SESSION')
        writer = self.choose(Request('x', new_session=True), role='execution', profile='execution')
        denied = self.choose(Request('x', session_selector=writer.session_id))
        self.assertEqual(denied.reason, 'INCOMPATIBLE_SESSION')

    def test_ambiguous_and_explicit_sessions(self):
        first = self.choose()
        self.choose(Request('x', new_session=True))
        self.assertEqual(self.choose().reason, 'AMBIGUOUS_SESSION')
        self.assertTrue(self.choose(Request('x', session_selector=first.session_id)).resume)

    def test_private_corruption_symlink(self):
        self.choose()
        registry = Path(self.ctx.registry_path) / 'registry.json'
        self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
        registry.write_text('{}')
        with self.assertRaises(StateError):
            self.choose()
        registry.unlink()
        registry.symlink_to(self.root / 'outside')
        with self.assertRaises(StateError):
            self.choose()

    def test_directory_symlink_and_permissions(self):
        target = self.root / 'target'
        target.mkdir(mode=0o700)
        Path(self.ctx.registry_path).symlink_to(target)
        with self.assertRaises(StateError):
            self.choose()
        Path(self.ctx.registry_path).unlink()
        target.rename(self.ctx.registry_path)
        Path(self.ctx.registry_path).chmod(0o755)
        with self.assertRaises(StateError):
            self.choose()

    def test_concurrent_process_busy_and_completed_artifacts(self):
        decision = self.choose()
        run = reserve_session(self.ctx, decision, '/private/artifact')
        script = """import sys
from cross_agent_state import *
from cross_agent_request import *
c=TaskContext(sys.argv[1], 'repo:canonical', 'conversation')
a=AgentChoice('codex','gpt-6.1-sol','low','review','read-only',False)
d=select_session(Request('x',session_selector=sys.argv[2]),c,a,'review','read-only')
try:
 reserve_session(c,d,'/other')
except StateError as e:
 print(str(e)); sys.exit(7)
sys.exit(1)
"""
        result = subprocess.run([sys.executable, '-c', script, self.ctx.registry_path, decision.session_id],
                                cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('SESSION_BUSY', result.stdout)
        update_session(self.ctx, decision.session_id, run, cli_session_id='cli-1', actual_model=self.agent.model)
        finish_session(self.ctx, decision.session_id, run)
        state = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
        self.assertEqual(state['sessions'][decision.session_id]['runs'][0]['artifact_path'], '/private/artifact')
        self.assertEqual(self.choose().cli_session_id, 'cli-1')

    def test_dead_process_recovery_preserves_artifacts(self):
        decision = self.choose()
        script = """import sys
from cross_agent_state import *
c=TaskContext(sys.argv[1],'repo:canonical','conversation')
from cross_agent_request import Request, AgentChoice
a=AgentChoice('codex','gpt-6.1-sol','low','review','read-only',False)
d=select_session(Request('x',session_selector=sys.argv[3]),c,a,'review','read-only')
print(reserve_session(c,d,'/crashed/artifacts'))
"""
        result = subprocess.run([sys.executable, '-c', script, self.ctx.registry_path, decision.task_id,
                                 decision.session_id], cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        run = reserve_session(self.ctx, decision, '/new/artifacts')
        finish_session(self.ctx, decision.session_id, run, 'failed')
        state = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
        old = state['sessions'][decision.session_id]['runs'][0]
        self.assertEqual(old['status'], 'failed')
        self.assertEqual(old['reason'], 'PROCESS_EXITED')
        self.assertEqual(old['artifact_path'], '/crashed/artifacts')



    def test_issue_identity_and_implicit_issue_switch(self):
        one_ctx = replace(self.ctx, canonical_issue='1')
        first = self.choose(ctx=one_ctx)
        self.assertEqual(first.task_id, 'issue:%5B%22repo%3Acanonical%22%2C%221%22%5D')
        second_ctx = replace(self.ctx, canonical_issue='2', active_task_ids=(first.task_id,))
        second = self.choose(ctx=second_ctx)
        self.assertEqual(second.task_id, 'issue:%5B%22repo%3Acanonical%22%2C%222%22%5D')
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(self.choose(ctx=one_ctx).task_id, first.task_id)
        standalone = self.choose(Request('x', task_selector='new'), ctx=one_ctx)
        import uuid
        uuid.UUID(standalone.task_id)
        data = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
        self.assertIsNone(data['tasks'][standalone.task_id]['canonical_issue'])


    def test_foreign_owner_and_file_permissions(self):
        from unittest.mock import patch
        self.choose()
        path = Path(self.ctx.registry_path) / 'registry.json'
        path.chmod(0o644)
        with self.assertRaisesRegex(StateError, 'UNSAFE_REGISTRY_PATH'):
            self.choose()
        path.chmod(0o600)
        with patch('cross_agent_state.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(StateError, 'UNSAFE_REGISTRY_PATH'):
                self.choose()

    def test_atomic_write_failure_preserves_registry(self):
        from unittest.mock import patch
        self.choose()
        path = Path(self.ctx.registry_path) / 'registry.json'
        original = path.read_bytes()
        with patch('cross_agent_state.os.replace', side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError):
                self.choose(Request('x', new_session=True))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob('.registry-*')))
        self.assertTrue(self.choose().resume)

    def test_lock_symlink_is_rejected(self):
        self.choose()
        lock = Path(self.ctx.registry_path) / 'registry.lock'
        lock.unlink()
        lock.symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(StateError, 'UNSAFE_REGISTRY_PATH'):
            self.choose()

    def test_forged_decision_identity_is_rejected(self):
        decision = self.choose()
        with self.assertRaisesRegex(StateError, 'INCOMPATIBLE_SESSION'):
            reserve_session(self.ctx, replace(decision, role='execution'), '/artifacts')

    def test_inconsistent_registry_metadata_is_corrupt(self):
        self.choose()
        path = Path(self.ctx.registry_path) / 'registry.json'
        original = path.read_text()
        for field, value in [('repo', 'another-repo'), ('profile', 'execution'),
                             ('role', 'invalid'), ('cli_session_id', 3),
                             ('runs', [{'run_id': 'x', 'pid': -1}])]:
            with self.subTest(field=field):
                data = json.loads(original)
                next(iter(data['sessions'].values()))[field] = value
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(StateError, 'CORRUPT_REGISTRY'):
                    self.choose()
                path.write_text(original)

    def test_failed_identity_update_preserves_run(self):
        decision = self.choose()
        run = reserve_session(self.ctx, decision, '/artifacts')
        with self.assertRaisesRegex(StateError, 'IDENTITY_MISMATCH'):
            update_session(self.ctx, decision.session_id, run, actual_model='wrong')
        finish_session(self.ctx, decision.session_id, run, 'failed', reason='IDENTITY_MISMATCH')



    def test_launch_session_id_is_a_run_field_and_never_a_binding(self):
        launch = 'cccccccc-dddd-4eee-8fff-000000000000'
        decision = self.choose()
        for bad in ('not-a-uuid', launch.upper(), 7):
            with self.subTest(bad=bad), self.assertRaisesRegex(StateError, 'INVALID_UPDATE'):
                reserve_session(self.ctx, decision, '/artifacts', bad)
        run = reserve_session(self.ctx, decision, '/artifacts', launch)
        registry = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
        session = registry['sessions'][decision.session_id]
        self.assertEqual(session['runs'][-1]['launch_cli_session_id'], launch)
        self.assertIsNone(session['cli_session_id'])
        self.assertNotIn('observed_cli_session_id', session)
        finish_session(self.ctx, decision.session_id, run, 'failed')
        resumed = self.choose(Request('x', session_selector=decision.session_id))
        self.assertIsNone(resumed.cli_session_id)
        other = self.choose(Request('x', new_session=True))
        run = reserve_session(self.ctx, other, '/other', launch)
        update_session(self.ctx, other.session_id, run, cli_session_id='opaque-id')
        finish_session(self.ctx, other.session_id, run)
        bound = self.choose(Request('x', session_selector=other.session_id))
        with self.assertRaisesRegex(StateError, 'INVALID_UPDATE'):
            reserve_session(self.ctx, bound, '/bound', launch)

    def test_provider_binding_is_unique_across_all_host_session_keys(self):
        original = self.choose()
        run = reserve_session(self.ctx, original, '/original')
        update_session(self.ctx, original.session_id, run, cli_session_id='opaque-id')
        finish_session(self.ctx, original.session_id, run)
        resumed = self.choose(Request('x', session_selector=original.session_id))
        run = reserve_session(self.ctx, resumed, '/resume')
        update_session(self.ctx, resumed.session_id, run, cli_session_id='opaque-id')
        finish_session(self.ctx, resumed.session_id, run)
        consulted = self.choose(Request('x', session_selector=original.session_id),
                                ctx=replace(self.ctx, preserve_review_role=True), role='consultation')
        self.assertEqual(consulted.session_id, original.session_id)
        self.assertEqual(consulted.role, 'review')
        run = reserve_session(self.ctx, consulted, '/reviewer-consultation')
        finish_session(self.ctx, consulted.session_id, run, cli_session_id='opaque-id')
        choices = [self.choose(Request('x', task_selector='new')),
                   self.choose(Request('x', task_selector=original.task_id, new_session=True)),
                   self.choose(Request('x', task_selector=original.task_id),
                               role='execution', profile='execution'),
                   self.choose(Request('x', task_selector=original.task_id),
                               agent=replace(self.agent, model='other'))]
        for decision in choices:
            with self.subTest(session=decision.session_id):
                run = reserve_session(self.ctx, decision, '/rejected')
                with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
                    update_session(self.ctx, decision.session_id, run, cli_session_id='opaque-id')
                with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
                    finish_session(self.ctx, decision.session_id, run, cli_session_id='opaque-id')
                finish_session(self.ctx, decision.session_id, run, 'blocked')
        self.assertEqual(self.choose(Request('x', session_selector=original.session_id)).reason,
                         'PROVIDER_SESSION_QUARANTINED')
        other_family = self.choose(Request('x', task_selector=original.task_id),
                                   agent=replace(self.agent, family='claude-code'))
        run = reserve_session(self.ctx, other_family, '/other-family')
        finish_session(self.ctx, other_family.session_id, run, cli_session_id='opaque-id')

    def test_preexisting_duplicate_provider_bindings_are_corrupt(self):
        first = self.choose()
        second = self.choose(Request('x', new_session=True))
        path = Path(self.ctx.registry_path) / 'registry.json'
        data = json.loads(path.read_text())
        for session_id in (first.session_id, second.session_id):
            data['sessions'][session_id]['cli_session_id'] = 'duplicate-id'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(StateError, 'CORRUPT_REGISTRY'):
            self.choose()



    def test_processes_compete_for_one_provider_binding(self):
        self._compete_for_provider_binding(False)

    def test_processes_compete_for_failed_observation_binding(self):
        self._compete_for_provider_binding(True)

    def _compete_for_provider_binding(self, observe):
        from concurrent.futures import ThreadPoolExecutor
        first = self.choose()
        second = self.choose(Request('x', new_session=True))
        script = """import sys
from cross_agent_state import *
from cross_agent_request import *
c=TaskContext(sys.argv[1],'repo:canonical','conversation')
a=AgentChoice('codex','gpt-6.1-sol','low','review','read-only',False)
d=select_session(Request('x',session_selector=sys.argv[2]),c,a,'review','read-only')
r=reserve_session(c,d,'/concurrent-artifacts')
print('reserved',flush=True)
sys.stdin.readline()
try:
 finish_session(c,d.session_id,r,cli_session_id='shared-provider')
 print('BOUND',flush=True)
except StateError as e:
 finish_session(c,d.session_id,r,'blocked',reason=str(e))
 print(str(e),flush=True)
"""
        if observe:
            script = script.replace("finish_session(c,d.session_id,r,cli_session_id='shared-provider')",
                "observe_provider_sessions(c,d.session_id,r,['shared-provider']); finish_session(c,d.session_id,r,'failed')")
        processes = []
        try:
            for decision in (first, second):
                processes.append(subprocess.Popen(
                    [sys.executable, '-c', script, self.ctx.registry_path, decision.session_id],
                    cwd=Path(__file__).parent, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), 'reserved')
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(process.communicate, 'go\n', timeout=10)
                           for process in processes]
                outputs = [future.result() for future in futures]
            self.assertEqual(sorted(out.strip() for out, _ in outputs),
                             ['BOUND', 'PROVIDER_SESSION_COLLISION'])
            for process, (_, error) in zip(processes, outputs):
                self.assertEqual(process.returncode, 0, error)
            state = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
            records = [state['sessions'][d.session_id] for d in (first, second)]
            self.assertTrue(all(record['provider_conflicts'] for record in records))
            for decision in (first, second):
                self.assertEqual(self.choose(Request('x', session_selector=decision.session_id)).reason,
                                 'PROVIDER_SESSION_QUARANTINED')
            if observe:
                self.assertEqual(sum('shared-provider' in s['runs'][0].get('observed_cli_session_ids', [])
                                     for s in records), 1)
                self.assertTrue(all(s['cli_session_id'] is None for s in records))
                self.assertEqual(sorted(s['status'] for s in records), ['blocked', 'failed'])
            else:
                self.assertEqual(sum(s['cli_session_id'] == 'shared-provider' for s in records), 1)
                self.assertEqual(sorted(s['status'] for s in records), ['blocked', 'completed'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)



    def test_collision_quarantine_blocks_stale_reservations_and_running_updates(self):
        from cross_agent_state import observe_provider_sessions
        owner = self.choose()
        first_run = reserve_session(self.ctx, owner, '/owner')
        update_session(self.ctx, owner.session_id, first_run, cli_session_id='provider')
        finish_session(self.ctx, owner.session_id, first_run)
        stale = self.choose(Request('x', session_selector=owner.session_id))
        claimant = self.choose(Request('x', task_selector='new'))
        claimant_run = reserve_session(self.ctx, claimant, '/claimant')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            observe_provider_sessions(self.ctx, claimant.session_id, claimant_run, ['provider'])
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_QUARANTINED'):
            reserve_session(self.ctx, stale, '/stale')
        finish_session(self.ctx, claimant.session_id, claimant_run, 'blocked')
        self.assertEqual(self.choose(Request('x', session_selector=owner.session_id)).reason,
                         'PROVIDER_SESSION_QUARANTINED')
        # An already-running original owner must also lose completion authority.
        other = self.choose(Request('x', task_selector='new'))
        run = reserve_session(self.ctx, other, '/running-owner')
        update_session(self.ctx, other.session_id, run, cli_session_id='running-provider')
        collision = self.choose(Request('x', task_selector='new'))
        collision_run = reserve_session(self.ctx, collision, '/collision')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            update_session(self.ctx, collision.session_id, collision_run, cli_session_id='running-provider')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_QUARANTINED'):
            update_session(self.ctx, other.session_id, run, actual_model=self.agent.model)
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_QUARANTINED'):
            finish_session(self.ctx, other.session_id, run, 'completed')
        finish_session(self.ctx, other.session_id, run, 'blocked')
        finish_session(self.ctx, collision.session_id, collision_run, 'blocked')
        saved = json.loads((Path(self.ctx.registry_path) / 'registry.json').read_text())
        self.assertTrue(saved['sessions'][other.session_id]['provider_conflicts'])
        self.assertTrue(saved['sessions'][collision.session_id]['provider_conflicts'])

    def test_provider_uuid_spelling_cannot_bypass_failed_observation_ownership(self):
        from cross_agent_state import observe_provider_sessions
        provider_id = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        first = self.choose()
        run = reserve_session(self.ctx, first, '/first')
        observe_provider_sessions(self.ctx, first.session_id, run, [provider_id.upper()])
        finish_session(self.ctx, first.session_id, run, 'failed')
        second = self.choose(Request('x', task_selector='new'))
        run = reserve_session(self.ctx, second, '/second')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            update_session(self.ctx, second.session_id, run, cli_session_id=provider_id)
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            observe_provider_sessions(self.ctx, second.session_id, run, [provider_id])
        finish_session(self.ctx, second.session_id, run, 'blocked')

    def test_failure_observations_are_atomic_unique_and_keep_legacy_resume_id(self):
        from cross_agent_state import observe_provider_sessions
        first = self.choose()
        run = reserve_session(self.ctx, first, '/first')
        update_session(self.ctx, first.session_id, run, cli_session_id='verified-id')
        observe_provider_sessions(self.ctx, first.session_id, run, ['unexpected-id'])
        finish_session(self.ctx, first.session_id, run, 'failed')
        second = self.choose(Request('x', task_selector='new'))
        run = reserve_session(self.ctx, second, '/second')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            observe_provider_sessions(self.ctx, second.session_id, run, ['unexpected-id', 'new-id'])
        finish_session(self.ctx, second.session_id, run, 'blocked')
        # A collision cannot discard a new ID used in the same failed process.
        third = self.choose(Request('x', task_selector='new'))
        run = reserve_session(self.ctx, third, '/third')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            update_session(self.ctx, third.session_id, run, cli_session_id='new-id')
        finish_session(self.ctx, third.session_id, run, 'blocked')
        resumed = self.choose(Request('x', session_selector=first.session_id))
        self.assertEqual(resumed.reason, 'PROVIDER_SESSION_QUARANTINED')
        path = Path(self.ctx.registry_path) / 'registry.json'
        data = json.loads(path.read_text())
        self.assertEqual(data['sessions'][first.session_id]['cli_session_id'], 'verified-id')
        data['sessions'][third.session_id]['runs'][0]['observed_cli_session_ids'] = ['unexpected-id']
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(StateError, 'CORRUPT_REGISTRY'):
            self.choose()

    def test_observed_only_identity_binds_without_authorizing_resume(self):
        first = self.choose()
        run = reserve_session(self.ctx, first, '/incomplete')
        update_session(self.ctx, first.session_id, run, observed_cli_session_id='observed-only')
        finish_session(self.ctx, first.session_id, run)
        selected = self.choose(Request('x', session_selector=first.session_id))
        self.assertIsNone(selected.cli_session_id)
        run = reserve_session(self.ctx, selected, '/verified')
        with self.assertRaisesRegex(StateError, 'IDENTITY_MISMATCH'):
            update_session(self.ctx, selected.session_id, run, cli_session_id='different')
        finish_session(self.ctx, selected.session_id, run, cli_session_id='observed-only')
        self.assertEqual(self.choose(Request('x', session_selector=first.session_id)).cli_session_id,
                         'observed-only')
        different = self.choose(Request('x', new_session=True))
        run = reserve_session(self.ctx, different, '/other')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_COLLISION'):
            update_session(self.ctx, different.session_id, run, cli_session_id='observed-only')
        finish_session(self.ctx, different.session_id, run, 'blocked')
        with self.assertRaisesRegex(StateError, 'PROVIDER_SESSION_QUARANTINED'):
            reserve_session(self.ctx, selected, '/stale-after-collision')




    def test_observed_binding_corruption_and_legacy_collision_fail_closed(self):
        first = self.choose()
        second = self.choose(Request('x', new_session=True))
        path = Path(self.ctx.registry_path) / 'registry.json'
        original = path.read_text()
        for second_fields in ({'observed_cli_session_id': 'known-id'},
                              {'cli_session_id': 'known-id', 'observed_cli_session_id': 'other-id'},
                              {'observed_cli_session_id': 7},
                              {'observed_cli_session_id': 'unknown'}):
            with self.subTest(fields=second_fields):
                data = json.loads(original)
                data['sessions'][first.session_id]['cli_session_id'] = 'known-id'
                data['sessions'][second.session_id].update(second_fields)
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(StateError, 'CORRUPT_REGISTRY'):
                    self.choose()
                path.write_text(original)


if __name__ == '__main__':
    unittest.main()
