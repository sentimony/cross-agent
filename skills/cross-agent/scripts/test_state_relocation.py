"""Installation-root changes retain the host-owned private registry unchanged."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from cross_agent_request import AgentChoice, Request


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def state_at(self, name):
        install = self.root / name
        install.mkdir()
        path = install / 'cross_agent_state.py'
        path.write_bytes(Path(__file__).with_name('cross_agent_state.py').read_bytes())
        module_name = 'relocated_state_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(module)
        return module

    def test_provider_task_and_artifact_identity_survive_installation_move(self):
        old, new = self.state_at('old-install'), self.state_at('new-install')
        registry = str(self.root / 'registry')
        agent = AgentChoice('codex', 'gpt-6.1-sol', 'low', 'consultation', 'read-only', False)
        old_context = old.TaskContext(registry, 'fixture/repo', 'same-conversation')
        decision = old.select_session(Request('consult'), old_context, agent, 'consultation', 'read-only')
        artifacts = str(self.root / 'existing-artifacts')
        run_id = old.reserve_session(old_context, decision, artifacts)
        provider_id = str(uuid.uuid4())
        old.update_session(old_context, decision.session_id, run_id,
                           actual_model=agent.model, actual_effort=agent.effort,
                           cli_session_id=provider_id)
        old.finish_session(old_context, decision.session_id, run_id)
        resumed = new.select_session(Request('follow up'),
            new.TaskContext(registry, 'fixture/repo', 'same-conversation'),
            agent, 'consultation', 'read-only')
        self.assertEqual((resumed.task_id, resumed.session_id, resumed.cli_session_id),
                         (decision.task_id, decision.session_id, provider_id))
        self.assertTrue(resumed.resume)
        data = json.loads((Path(registry) / 'registry.json').read_text())
        self.assertEqual(data['sessions'][resumed.session_id]['runs'][0]['artifact_path'], artifacts)

    def test_unknown_legacy_schema_is_preserved_without_reset(self):
        state = self.state_at('new-install')
        registry = self.root / 'private'
        registry.mkdir(mode=0o700)
        path = registry / 'registry.json'
        original = '{"schema_version":99,"opaque":"retain legacy data"}\n'
        path.write_text(original)
        path.chmod(0o600)
        agent = AgentChoice('codex', 'gpt-6.1-sol', 'low', 'consultation', 'read-only', False)
        with self.assertRaisesRegex(state.StateError, 'CORRUPT_REGISTRY'):
            state.select_session(Request('follow up'),
                state.TaskContext(str(registry), 'fixture/repo', 'conversation'),
                agent, 'consultation', 'read-only')
        self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
