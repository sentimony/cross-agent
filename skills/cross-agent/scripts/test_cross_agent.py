import unittest

import json

import os

from pathlib import Path

import tempfile

from dataclasses import replace

from cross_agent import HostContext, dispatch

from cross_agent_request import load_config, parse_request

from cross_agent_state import TaskContext

class ProcessContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        import sys
        (self.bin / 'python3').symlink_to(sys.executable)
        fixture = '''#!/usr/bin/env python3
import sys,json,uuid,time,os
from pathlib import Path
brief=sys.stdin.read()
args=sys.argv[1:]
if 'SLEEP' in brief:
 print('partial',flush=True)
 time.sleep(20)
sid=args[-2] if 'resume' in args else str(uuid.uuid4())
print(json.dumps({'type':'thread.started','thread_id':sid}),flush=True)
print(json.dumps({'type':'session_meta','payload':{'model':'gpt-6.1-sol','reasoning_effort':'low'}}),flush=True)
value={'schema_version':1,'status':'COMPLETED','output':'done'}
if 'HANDOFF' in brief and 'Host answer' not in brief:
 value.update(status='CONSULTATION_REQUIRED',handoff={'question':'Which?','options':['A','B'],'evidence':[],'consultants':['claude-code']})
if 'MISMATCH' in brief:
 print(json.dumps({'type':'session_meta','payload':{'model':'wrong','reasoning_effort':'low'}}))
Path(args[args.index('-o')+1]).write_text(json.dumps(value))
'''
        (self.bin / 'codex').write_text(fixture)
        (self.bin / 'codex').chmod(0o700)
        self.policy = {'schema_version': 1, 'agents': {'codex': {
            'models': {'gpt-6.1-sol': ['low']}, 'default_model': 'gpt-6.1-sol',
            'default_effort': 'low', 'profiles': ['read-only'],
            'capabilities': {'read-only': 'supported'}}}}
        self.host = HostContext(load_config(host_config=self.policy, role='consultation', profile='read-only'),
                    TaskContext(str(self.root / 'registry'), str(self.root), 'conversation'),
                    'claude', str(self.root), str(self.root / 'artifacts'), {'tree': 'snapshot'},
                    environment={'PATH': str(self.bin), 'HOME': str(self.root)},
                    timeout_seconds=2, kill_grace_seconds=.1)

    def test_explicit_session_with_many_recipients_prevents_launch(self):
        from unittest.mock import patch
        with patch('cross_agent._process') as process:
            with self.assertRaises(ValueError):
                dispatch(parse_request('codex codex [read] --session one'), self.host)
            process.assert_not_called()
        self.assertFalse((self.root / 'registry').exists())

    def test_unknown_host_task_kind_requests_input(self):
        result = dispatch(parse_request('codex [maybe skill]'), replace(self.host, assignment_kind=None))
        self.assertEqual(result.results[0]['status'], 'USER_INPUT_REQUIRED')
        self.assertFalse((self.root / 'registry').exists())

    def test_missing_integration_blocks_before_state_or_process(self):
        from unittest.mock import patch
        with patch('cross_agent._process') as process:
            result=dispatch(parse_request('codex [domain]'), replace(self.host, integration_required=True))
        self.assertEqual(result.results[0]['status'],'UNAVAILABLE')
        process.assert_not_called()
        self.assertFalse((self.root/'registry').exists())

    def test_owner_adapter_sequence_and_rejection(self):
        from unittest.mock import patch
        binding={'owner_id':'fixture.owner','adapter_id':'fixture', 'contract_version':1,
                 'protocol_version':1, 'capabilities':['prepare','validate','intake']}
        stages=[]
        def call(binding,stage,host,payload,**kwargs):
            stages.append(stage)
            if stage=='prepare':
                self.assertFalse((self.root/'registry').exists())
                return {'accepted':True,'context':'Fixture domain instructions'}
            return {'accepted':True}
        with patch('cross_agent.adapter_call',side_effect=call):
            result=dispatch(parse_request('codex [domain]'),replace(self.host,integration=binding))
        self.assertEqual(stages,['prepare','validate','intake'])
        self.assertEqual(result.results[0]['status'],'COMPLETED')
        fresh=replace(self.host,task_context=replace(self.host.task_context,registry_path=str(self.root/'second')),
                      integration=binding)
        with patch('cross_agent.adapter_call',side_effect=[{'accepted':True,'context':'rules'},{'accepted':False}]):
            result=dispatch(parse_request('codex [domain]'),fresh)
        self.assertEqual(result.results[0]['status'],'FAILED')
        self.assertTrue(Path(result.results[0]['artifacts']['stdout']).is_file())

    def test_installed_resource_in_generic_brief(self):
        installed=self.root/'installed';installed.mkdir()
        (installed/'SKILL.md').write_text('Use facts.txt')
        (installed/'facts.txt').write_text('RESOURCE-FROM-INSTALL')
        host=replace(self.host,assignment_kind='skill',installed_skills={'fixture':{
             'origin':'fixture-package','base':str(installed),'resources':['facts.txt']}})
        result=dispatch(parse_request('codex [fixture --auto]'),host).results[0]
        self.assertEqual(result['status'],'COMPLETED')
        self.assertIn('RESOURCE-FROM-INSTALL',Path(result['artifacts']['brief']).read_text())

    def test_owner_context_and_selectors_are_host_owned(self):
        from unittest.mock import patch
        from copy import deepcopy
        context={'active_task_id':'trusted-task', 'review_records':[{'path':'trusted-review'}]}
        seen=[]
        binding={'owner_id':'fixture.owner','adapter_id':'fixture', 'contract_version':1,
                 'protocol_version':1, 'capabilities':['prepare','validate','intake']}
        def call(binding,stage,host,payload,**kwargs):
            seen.append(deepcopy(host))
            self.assertIn('owner_context',host)
            self.assertNotIn('owner_context',payload)
            host['owner_context']['review_records'].clear()
            return {'accepted':True,'context':'owner context'} if stage=='prepare' else {'accepted':True}
        with patch('cross_agent.adapter_call',side_effect=call):
            result=dispatch(parse_request('codex --task new [domain]'),
                            replace(self.host,integration=binding,owner_context=context))
        self.assertEqual(result.results[0]['status'],'COMPLETED')
        self.assertEqual([host['owner_context'] for host in seen],[context]*3)
        self.assertEqual(seen[0]['task_selection'],
                         {'task_selector':'new','session_selector':None,'new_session':False})
        self.assertEqual(context['review_records'],[{'path':'trusted-review'}])

    def test_typed_owner_prepare_stop_never_mutates_or_launches(self):
        from unittest.mock import patch
        binding={'owner_id':'fixture.owner','adapter_id':'fixture', 'contract_version':1,
                 'protocol_version':1, 'capabilities':['prepare','validate','intake']}
        for status in ('USER_INPUT_REQUIRED','BLOCKED','UNAVAILABLE'):
            with self.subTest(status=status), patch('cross_agent._process') as process, \
                 patch('cross_agent.adapter_call',return_value={'accepted':False,'status':status,
                       'reason':'ambiguous intake','handoff':{'question':'Choose review'},'evidence':['candidate-A','candidate-B']}) as adapter:
                result=dispatch(parse_request('codex [domain]'),replace(self.host,integration=binding,
                    owner_context={'review_records':['A','B']}))
                self.assertEqual(result.results[0]['status'],status)
                self.assertEqual(result.results[0]['evidence'],['candidate-A','candidate-B'])
                self.assertEqual(result.results[0]['handoff']['question'],'Choose review')
                self.assertEqual(adapter.call_count,1)
                process.assert_not_called()
                self.assertFalse((self.root/'registry').exists())

    def test_fresh_resume_and_handoff(self):
        first = dispatch(parse_request('codex [HANDOFF]'), self.host).results[0]
        self.assertEqual(first['status'], 'CONSULTATION_REQUIRED')
        second = dispatch(parse_request('codex [HANDOFF]'), replace(self.host, answer={'choice': 'A'})).results[0]
        self.assertEqual(second['status'], 'COMPLETED')
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['cli_session_id'], second['cli_session_id'])
        self.assertNotEqual(first['run_id'], second['run_id'])

    def test_partial_failure_shared_task_snapshot(self):
        result = dispatch(parse_request('claude codex [read]'), self.host)
        self.assertEqual([r['status'] for r in result.results], ['UNAVAILABLE', 'COMPLETED'])
        result = dispatch(parse_request('codex codex --task new [read]'), self.host)
        self.assertEqual(len({r['task_id'] for r in result.results}), 1)
        self.assertEqual(result.results[0]['target_identity'], result.results[1]['target_identity'])

    def test_task_new_clears_inherited_issue_for_all_recipients(self):
        host = replace(self.host, task_context=replace(self.host.task_context, canonical_issue='42'))
        result = dispatch(parse_request('codex codex --task new [read]'), host)
        self.assertEqual([r['status'] for r in result.results], ['COMPLETED', 'COMPLETED'])
        self.assertEqual(len({r['task_id'] for r in result.results}), 1)
        explicit = dispatch(parse_request('codex --task ' + result.task_id + ' [read]'), host)
        self.assertEqual(explicit.results[0]['reason'], 'ISSUE_MISMATCH')

    def test_invalid_later_group_preflight_has_no_state_or_launch(self):
        from unittest.mock import patch
        for request in ('codex sol low codex invalid low [read]',
                        'claude codex invalid low [read]'):
            with self.subTest(request=request), patch('cross_agent._process') as process:
                with self.assertRaisesRegex(ValueError, 'model/effort'):
                    dispatch(parse_request(request), self.host)
                process.assert_not_called()
                self.assertFalse((self.root / 'registry').exists())
                self.assertFalse((self.root / 'artifacts').exists())

    def test_cleanup_failure_keeps_cancel_latch_and_durable_final_result(self):
        from unittest.mock import patch
        from cross_agent_state import StateError
        for status in ('CANCELLED', 'COMPLETED'):
            with self.subTest(status=status):
                # A fresh registry avoids a deliberately unreleased reservation.
                host = replace(self.host, task_context=replace(self.host.task_context,
                    registry_path=str(self.root / ('registry-' + status))))
                from cross_agent import _process
                def execute(*args):
                    if status == 'COMPLETED':
                        return _process(*args)
                    (args[2] / 'stdout.json').write_text('partial cancellation output')
                    return 'CANCELLED', 'SIGNAL'
                with patch('cross_agent._process', side_effect=execute) as process, \
                     patch('cross_agent.finish_session', side_effect=StateError('REGISTRY_BUSY')):
                    result = dispatch(parse_request('codex codex [read]'), host)
                if status == 'CANCELLED':
                    self.assertEqual(process.call_count, 1)
                    self.assertEqual(len(result.results), 1)
                    self.assertEqual(result.results[0]['status'], 'CANCELLED')
                entry = result.results[0]
                if status == 'COMPLETED':
                    self.assertEqual(entry['actual_model'], 'gpt-6.1-sol')
                    self.assertEqual(entry['status'], 'FAILED')
                else:
                    self.assertIn('partial cancellation', Path(entry['artifacts']['stdout']).read_text())
                self.assertIn('REGISTRY_BUSY', json.dumps(entry.get('cleanup_errors')))
                self.assertEqual(json.loads(Path(entry['artifacts']['result']).read_text()), entry)


    def test_observed_mismatch_fails(self):
        result = dispatch(parse_request('codex [MISMATCH]'), self.host).results[0]
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('mismatch', result['reason'])

    def test_unknown_skill_unavailable_before_spawn(self):
        result = dispatch(parse_request('codex [resolve-issue 1]'), replace(self.host, assignment_kind='skill')).results[0]
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertEqual(result['artifacts'], {})

    def test_child_cannot_overwrite_host_fields(self):
        from cross_agent import _answer
        for key in ('task_id', 'actual_model', 'artifacts', 'usage', 'target_identity'):
            with self.assertRaises(ValueError):
                _answer(json.dumps({'schema_version': 1, 'status': 'COMPLETED', 'output': 'ok', key: 'forged'}))

    def test_ambient_nested_guard_cannot_be_hidden(self):
        from unittest.mock import patch
        import cli_transport as cross_review
        with patch.dict(os.environ, {'CROSS_AGENT_DELEGATED': '1'}):
            with self.assertRaises(cross_review.CrossReviewError):
                dispatch(parse_request('codex [read]'), self.host)

    def test_nonfinite_timeout_rejected(self):
        for value in (float('inf'), float('nan'), 0):
            with self.assertRaises(ValueError):
                dispatch(parse_request('codex [read]'), replace(self.host, timeout_seconds=value))

    def test_missing_provider_identity_blocks_resume(self):
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "print(json.dumps({'type':'session_meta','payload':{'model':'gpt-6.1-sol','reasoning_effort':'low'}}),flush=True)"
        self.assertEqual(text.count(old), 1)
        fixture.write_text(text.replace(old, ''))
        first = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(first['actual_model'], 'unknown')
        second = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(second['status'], 'UNAVAILABLE')
        third = dispatch(parse_request('codex --new [read]'), self.host).results[0]
        self.assertEqual(third['status'], 'COMPLETED')

    def test_codex_identity_from_rollout_allows_resume(self):
        # Real codex exec --json prints no session_meta; the rollout file names the model.
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "print(json.dumps({'type':'session_meta','payload':{'model':'gpt-6.1-sol','reasoning_effort':'low'}}),flush=True)"
        self.assertEqual(text.count(old), 1)
        rollout = ("day=Path(os.environ['HOME'])/'.codex'/'sessions'/'2026'/'11'/'03'\n"
                   "day.mkdir(parents=True,exist_ok=True)\n"
                   "with open(day/('rollout-2026-11-03T00-00-00-'+sid+'.jsonl'),'a') as f:\n"
                   " f.write(json.dumps({'type':'session_meta','payload':{'id':sid}})+'\\n')\n"
                   " f.write(json.dumps({'type':'turn_context','payload':{'model':'gpt-6.1-sol','effort':'low'}})+'\\n')")
        fixture.write_text(text.replace(old, rollout))
        first = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(first['status'], 'COMPLETED')
        self.assertEqual(first['actual_model'], 'gpt-6.1-sol')
        self.assertEqual(first['observed_identity']['model_basis'], 'provider-rollout')
        second = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(second['status'], 'COMPLETED')
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(first['cli_session_id'], second['cli_session_id'])

    def test_invalid_handoff_keeps_child_status_for_resume(self):
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "if 'MISMATCH' in brief:"
        self.assertEqual(text.count(old), 1)
        bad = ("if 'BADHANDOFF' in brief and 'Host answer' not in brief:\n"
               " value.update(status='CONSULTATION_REQUIRED',output='Which option?',handoff={'question':'Which?'})\n")
        fixture.write_text(text.replace(old, bad + old))
        first = dispatch(parse_request('codex [BADHANDOFF]'), self.host).results[0]
        self.assertEqual(first['status'], 'CONSULTATION_REQUIRED')
        self.assertIs(first['handoff_invalid'], True)
        self.assertEqual(first['output'], 'Which option?')
        self.assertNotIn('handoff', first)
        self.assertIn('"consultants": []', Path(first['artifacts']['brief']).read_text())
        second = dispatch(parse_request('codex [BADHANDOFF]'), replace(self.host, answer={'choice': 'A'})).results[0]
        self.assertEqual(second['status'], 'COMPLETED')
        self.assertEqual(first['session_id'], second['session_id'])

    def test_fenced_child_result_is_unwrapped(self):
        from cross_agent import _answer
        raw = '{"schema_version": 1, "status": "COMPLETED", "output": "done"}'
        expected = {'status': 'COMPLETED', 'output': 'done'}
        self.assertEqual(_answer('```json\n' + raw + '\n```\n'), expected)
        self.assertEqual(_answer('  ```\n' + raw + '\n```  '), expected)
        self.assertEqual(_answer('Here is the result:\n```json\n' + raw + '\n```'), expected)
        self.assertEqual(_answer(raw), expected)
        # Raw pretty-printed JSON whose output holds a fence parses as is.
        nested = {'schema_version': 1, 'status': 'COMPLETED', 'output': 'see\n```json\n{}\n```'}
        self.assertEqual(_answer(json.dumps(nested, indent=2)),
                         {'status': 'COMPLETED', 'output': nested['output']})

    def test_unusable_child_result_names_the_reason(self):
        from cross_agent import _answer
        raw = '{"schema_version": 1, "status": "COMPLETED", "output": "done"}'
        with self.assertRaises(ValueError) as two:
            _answer('```json\n' + raw + '\n```\n```json\n' + raw + '\n```')
        self.assertTrue(str(two.exception).startswith('child result is not JSON v1: '))
        with self.assertRaises(ValueError) as other:
            _answer('```json\n{"hello": 1}\n```')
        self.assertEqual(str(other.exception), 'invalid child result v1')

    def test_non_json_child_result_fails_with_clear_reason(self):
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "Path(args[args.index('-o')+1]).write_text(json.dumps(value))"
        self.assertEqual(text.count(old), 1)
        fixture.write_text(text.replace(old, "Path(args[args.index('-o')+1]).write_text('All done, no JSON here')"))
        result = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['reason'], 'child result is not JSON v1: All done, no JSON here')

    def test_fenced_child_result_completes_end_to_end(self):
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "Path(args[args.index('-o')+1]).write_text(json.dumps(value))"
        self.assertEqual(text.count(old), 1)
        fixture.write_text(text.replace(old, "Path(args[args.index('-o')+1]).write_text('Result:\\n```json\\n'+json.dumps(value)+'\\n```')"))
        result = dispatch(parse_request('codex [read]'), self.host).results[0]
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(result['output'], 'done')

    def test_adopted_per_round_session_total_is_never_complete(self):
        from cross_agent_request import resolve_agents
        from cross_agent_state import adopt_session
        fixture = self.bin / 'codex'
        text = fixture.read_text()
        old = "value={'schema_version':1"
        self.assertEqual(text.count(old), 1)
        fixture.write_text(text.replace(old, "print(json.dumps({'type':'turn.completed','usage':"
                           "{'input_tokens':100,'output_tokens':10}}),flush=True)\n" + old))
        agent = resolve_agents(parse_request('codex [x]'), self.host.config, 'claude')[0]
        sid = adopt_session(self.host.task_context, agent, 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        for _ in range(2):
            entry = dispatch(parse_request('codex [read] --session ' + sid), self.host).results[0]
            self.assertEqual(entry['status'], 'COMPLETED', entry)
            self.assertIs(entry['usage']['identity_attested'], True)
            self.assertIsNotNone(entry['usage']['tokens'])
            self.assertIs(entry['usage']['session_total']['complete'], False)
        fresh = dispatch(parse_request('codex --new [read]'), self.host).results[0]
        self.assertIs(fresh['usage']['session_total']['complete'], True)

    def test_cancel_stops_remaining_recipients(self):
        from unittest.mock import patch
        with patch('cross_agent._process', return_value=('CANCELLED', 'SIGNAL')) as process:
            result = dispatch(parse_request('codex codex [read]'), self.host)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(process.call_count, 1)


    def test_two_independent_readers_share_one_snapshot(self):
        fixture = self.bin / 'claude'
        fixture.write_text('''#!/usr/bin/env python3
import json,sys
sys.stdin.read()
args=sys.argv[1:]
sid=args[args.index('--session-id')+1]
print(json.dumps({'session_id':sid,'model':'claude-opus-5-5','effort':'low','result':json.dumps({'schema_version':1,'status':'COMPLETED','output':'second reader'})}))
''')
        fixture.chmod(0o700)
        policy = json.loads(json.dumps(self.policy))
        policy['agents']['claude-code'] = {'models': {'claude-opus-5-5': ['low']},
             'default_model': 'claude-opus-5-5', 'default_effort': 'low',
             'profiles': ['read-only'], 'capabilities': {'read-only': 'supported'}}
        host = replace(self.host, config=load_config(host_config=policy, role='consultation', profile='read-only'))
        result = dispatch(parse_request('codex claude [read snapshot]'), host)
        self.assertEqual([r['status'] for r in result.results], ['COMPLETED', 'COMPLETED'])
        self.assertEqual(len({r['task_id'] for r in result.results}), 1)
        self.assertEqual(len({r['session_id'] for r in result.results}), 2)
        self.assertEqual(result.results[0]['target_identity'], result.results[1]['target_identity'])

    def test_fresh_claude_run_records_its_launch_session_id_while_running(self):
        seen = self.root / 'seen.json'
        fixture = self.bin / 'claude'
        fixture.write_text('''#!/usr/bin/env python3
import json,sys
sys.stdin.read()
args=sys.argv[1:]
sid=args[args.index('--session-id')+1]
registry=json.load(open(%r))
runs=[r for s in registry['sessions'].values() for r in s['runs'] if r['status']=='running']
json.dump({'sid':sid,'runs':runs},open(%r,'w'))
print(json.dumps({'session_id':sid,'model':'claude-opus-5-5','effort':'low','result':json.dumps({'schema_version':1,'status':'COMPLETED','output':'ok'})}))
''' % (str(self.root / 'registry/registry.json'), str(seen)))
        fixture.chmod(0o700)
        policy = json.loads(json.dumps(self.policy))
        policy['agents']['claude-code'] = {'models': {'claude-opus-5-5': ['low']},
             'default_model': 'claude-opus-5-5', 'default_effort': 'low',
             'profiles': ['read-only'], 'capabilities': {'read-only': 'supported'}}
        host = replace(self.host, config=load_config(host_config=policy, role='consultation', profile='read-only'))
        result = dispatch(parse_request('claude [read]'), host).results[0]
        self.assertEqual(result['status'], 'COMPLETED', result)
        observed = json.loads(seen.read_text())
        [run] = observed['runs']
        self.assertEqual(run['launch_cli_session_id'], observed['sid'])
        codex = dispatch(parse_request('codex --new [read]'), self.host).results[0]
        registry = json.loads((self.root / 'registry/registry.json').read_text())
        self.assertNotIn('launch_cli_session_id', registry['sessions'][codex['session_id']]['runs'][-1])

    def test_missing_or_inaccessible_executable_is_unavailable(self):
        from unittest.mock import patch
        for error in (FileNotFoundError('CLI missing'), PermissionError('CLI inaccessible')):
            with self.subTest(error=type(error).__name__):
                with patch('cross_agent.subprocess.Popen', side_effect=error):
                    result = dispatch(parse_request('codex --new [read]'), self.host).results[0]
                self.assertEqual(result['status'], 'UNAVAILABLE')
                self.assertTrue(Path(result['artifacts']['result']).is_file())
                registry = json.loads((self.root / 'registry/registry.json').read_text())
                self.assertEqual(registry['sessions'][result['session_id']]['status'], 'failed')

    def test_direct_process_entry_blocks_ambient_nested_before_spawn(self):
        from unittest.mock import patch
        from cross_agent import _process
        import cli_transport as cross_review
        with patch.dict(os.environ, {'CROSS_AGENT_DELEGATED': '1'}):
            with patch('cross_agent.subprocess.Popen') as spawn:
                with self.assertRaises(cross_review.CrossReviewError):
                    _process(['sentinel'], '', self.root, self.host, {})
                spawn.assert_not_called()


class TerminationContract(unittest.TestCase):
    def test_cleanup_wait_is_bounded(self):
        from unittest.mock import Mock,patch
        import subprocess
        from cross_agent import _terminate
        child=Mock(pid=12345)
        child.wait.side_effect=subprocess.TimeoutExpired('fixture',.1)
        with patch('cross_agent.os.killpg'):
            self.assertFalse(_terminate(child,.1))
        self.assertTrue(all(call.kwargs.get('timeout')==.1 for call in child.wait.call_args_list))
