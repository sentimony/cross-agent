from pathlib import Path
import json, re, unittest
ROOT=Path(__file__).parents[1]
class PackageTests(unittest.TestCase):
    def test_one_root_skill_and_manifest_identity(self):
        paths=['plugin.json','.claude-plugin/plugin.json','.antigravity-plugin/plugin.json']
        for name in paths:
            doc=json.loads((ROOT/name).read_text())
            self.assertEqual((doc['name'],doc['version']),('cross-agent','0.0.1'))
        self.assertEqual([p.name for p in (ROOT/'skills').iterdir()],['cross-agent'])
        self.assertFalse((ROOT/'.codex-plugin').exists())
    def test_frontmatter_license_changelog_and_reference_links(self):
        skill=ROOT/'skills/cross-agent';text=(skill/'SKILL.md').read_text()
        self.assertIn('name: cross-agent',text.split('---',2)[1])
        self.assertIn('version: "0.0.1"',text.split('---',2)[1])
        self.assertEqual((ROOT/'LICENSE').read_bytes(),(skill/'LICENSE').read_bytes())
        self.assertIn('## 0.0.1',(skill/'CHANGELOG.md').read_text())
        for link in re.findall(r'\]\((references/[^)]+)\)',text):
            self.assertTrue((skill/link).is_file(),link)
    def test_no_workflow_sibling_dependency(self):
        scripts=ROOT/'skills/cross-agent/scripts'
        text='\n'.join(p.read_text() for p in scripts.glob('*.py') if not p.name.startswith('test_'))
        for forbidden in ('CR_REQUEST_DIR','standard_sections','select_review_intake','import cross_review'):
            self.assertNotIn(forbidden,text)
if __name__=='__main__':unittest.main()
