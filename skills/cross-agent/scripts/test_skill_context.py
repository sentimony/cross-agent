from pathlib import Path
import tempfile, unittest
from cross_agent_protocol import load_skill_context, ProtocolError

class SkillContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'references').mkdir()
        (self.root/'SKILL.md').write_text('Read references/facts.txt')
        (self.root/'references/facts.txt').write_text('installed resource')
        self.installed={'fixture':{'origin':'fixture-owner','base':str(self.root),
             'instructions':'SKILL.md','resources':['references/facts.txt']}}
    def test_relative_resource_from_installation_base(self):
        result=load_skill_context('fixture', self.installed)
        self.assertEqual(result['resources']['references/facts.txt'],'installed resource')
        self.assertEqual(result['origin'],'fixture-owner')
    def test_missing_skill_or_resource(self):
        with self.assertRaises(ProtocolError):load_skill_context('missing',self.installed)
        (self.root/'references/facts.txt').unlink()
        with self.assertRaises(ProtocolError):load_skill_context('fixture',self.installed)
    def test_escape_and_symlink_denied(self):
        for path in ('../outside','/etc/passwd'):
            self.installed['fixture']['resources']=[path]
            with self.assertRaises(ProtocolError):load_skill_context('fixture',self.installed)
        (self.root/'outside-link').symlink_to('/etc/passwd')
        self.installed['fixture']['resources']=['outside-link']
        with self.assertRaises(ProtocolError):load_skill_context('fixture',self.installed)

if __name__=='__main__':unittest.main()
