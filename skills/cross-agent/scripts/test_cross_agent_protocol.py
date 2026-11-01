import unittest
from cross_agent_protocol import validate_binding, envelope, ProtocolError

class ProtocolTests(unittest.TestCase):
    def binding(self):
        return {'owner_id':'sentimony.agentic-workflow', 'adapter_id':'review',
                'contract_version':1, 'protocol_version':1,
                'capabilities':['prepare','validate','intake']}
    def test_version_pair_rejects_before_dispatch(self):
        for key in ('protocol_version','contract_version'):
            value=self.binding();value[key]=2
            with self.assertRaises(ProtocolError):validate_binding(value)
    def test_missing_adapter_and_capability(self):
        for value in (None, {}, dict(self.binding(), capabilities=['prepare'])):
            with self.assertRaises(ProtocolError):validate_binding(value, ('prepare','validate'))
    def test_payload_cannot_forge_host_fields(self):
        with self.assertRaises(ProtocolError):envelope({'task_id':'trusted','task_kind':'skill'}, {'task_id':'forged'})
    def test_valid_pair(self):
        self.assertEqual(validate_binding(self.binding(), ('prepare',))['owner_id'],'sentimony.agentic-workflow')
        self.assertEqual(envelope({'task_id':'trusted'}, {'question':'text'})['payload'], {'question':'text'})

if __name__=='__main__':unittest.main()

class AdapterIOTests(unittest.TestCase):
    def test_forged_owner_response_rejected(self):
        import json,sys,tempfile
        from pathlib import Path
        from unittest.mock import patch,Mock
        from cross_agent_protocol import adapter_call
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);entry=root/'adapter.py';entry.write_text('fixture')
            binding={'owner_id':'fixture.owner','adapter_id':'fixture','contract_version':1,
                     'protocol_version':1,'capabilities':['prepare','validate','intake'],
                     'installed_root':str(root),'entrypoint':str(entry),'interpreter':sys.executable}
            response={k:binding[k] for k in ('owner_id','adapter_id','contract_version','protocol_version')}
            response.update(schema_version=1,stage='prepare',payload={'accepted':True,'context':'fixture'})
            with patch('cross_agent_protocol.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps(response))):
                self.assertTrue(adapter_call(binding,'prepare',{'task_kind':'skill'},{'assignment':'fixture'})['accepted'])
            response['owner_id']='forged'
            with patch('cross_agent_protocol.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps(response))):
                with self.assertRaises(ProtocolError):adapter_call(binding,'prepare',{}, {})
    def test_nested_direct_adapter_call_cannot_spawn(self):
        from unittest.mock import patch
        from cross_agent_protocol import adapter_call
        from cli_transport import CrossReviewError
        with patch.dict('os.environ',{'CROSS_AGENT_DELEGATED':'1'}), patch('cross_agent_protocol.subprocess.run') as child:
            with self.assertRaises(CrossReviewError):adapter_call({},'prepare',{}, {})
            child.assert_not_called()

class OwnerContextEnvelopeTests(unittest.TestCase):
    def test_payload_cannot_replace_owner_context_or_task_selection(self):
        for field in ('owner_context','task_selection'):
            with self.subTest(field=field),self.assertRaises(ProtocolError):
                envelope({}, {field:{'review_records':['forged']}})

class PrepareOutcomeTests(unittest.TestCase):
    def test_stop_cannot_overwrite_host_fields(self):
        from cross_agent_protocol import prepare_stop
        good={'accepted':False,'status':'USER_INPUT_REQUIRED','reason':'Choose current review'}
        self.assertEqual(prepare_stop(good)['status'],'USER_INPUT_REQUIRED')
        for change in ({'task_id':'forged'},{'owner_context':{}},{'status':'COMPLETED'},
                       {'reason':''},{'handoff':[]},{'accepted':True}):
            with self.subTest(change=change),self.assertRaises(ProtocolError):
                prepare_stop(dict(good,**change))
