from pathlib import Path
import ast, json, re, unittest
ROOT=Path(__file__).parents[1]
# Hidden manifests kept because their host probe did not pass; see README.md.
KEPT_HIDDEN = ()
HIDDEN_PATHS = ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', '.antigravity-plugin/plugin.json')
class PackageTests(unittest.TestCase):
    def test_one_root_skill_and_manifest_identity(self):
        root=json.loads((ROOT/'plugin.json').read_text())
        self.assertEqual((root['name'],root['version']),('cross-agent','0.0.6'))
        self.assertEqual(root['skills'],'./skills/')
        self.assertEqual(root['$schema'],'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json')
        self.assertTrue(root['extensions']['com.openai']['interface'])
        for name in HIDDEN_PATHS:
            if name in KEPT_HIDDEN:
                doc=json.loads((ROOT/name).read_text())
                self.assertEqual((doc['name'],doc['version']),('cross-agent','0.0.6'))
            else:
                self.assertFalse((ROOT/name).exists(),name)
        self.assertEqual(sorted(p.name for p in (ROOT/'skills').iterdir()),['cross-agent', 'transfer-context'])
        groups=json.loads((ROOT/'skills.sh.json').read_text())['groupings']
        renamed=[s for g in groups if g.get('title')=='Renamed skills' for s in g['skills']]
        self.assertEqual(renamed,['context-transfer'])
        for name in renamed:
            self.assertFalse((ROOT/'skills'/name).exists(),name)
    def test_frontmatter_license_changelog_and_reference_links(self):
        for name, version in (('cross-agent', '0.0.6'), ('transfer-context', '0.0.5')):
            skill=ROOT/'skills'/name; text=(skill/'SKILL.md').read_text()
            self.assertIn('name: '+name,text.split('---',2)[1])
            self.assertIn('version: "%s"' % version,text.split('---',2)[1])
            self.assertEqual((ROOT/'LICENSE').read_bytes(),(skill/'LICENSE').read_bytes())
            self.assertIn('## 0.0.1',(skill/'CHANGELOG.md').read_text())
            for link in re.findall(r'\]\((references/[^)]+)\)',text):
                self.assertTrue((skill/link).is_file(),link)
    def test_runtime_imports_are_local_or_stdlib(self):
        allowed = set('argparse ast base64 collections contextlib dataclasses datetime enum fcntl functools hashlib json math os pathlib pwd re shlex shutil signal stat subprocess sys tempfile threading time typing urllib uuid'.split())
        for name in ('cross-agent', 'transfer-context'):
            scripts = ROOT/'skills'/name/'scripts'
            local = {p.stem for p in scripts.glob('*.py') if not p.name.startswith('test_')}
            for path in scripts.glob('*.py'):
                if path.name.startswith('test_'):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                               else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                    for module in modules:
                        self.assertIn(module.split('.')[0], allowed | local, (path, module))
    def test_no_workflow_sibling_dependency(self):
        scripts=ROOT/'skills/cross-agent/scripts'
        text='\n'.join(p.read_text() for p in scripts.glob('*.py') if not p.name.startswith('test_'))
        for forbidden in ('CR_REQUEST_DIR','standard_sections','select_review_intake','import cross_review'):
            self.assertNotIn(forbidden,text)
if __name__=='__main__':unittest.main()
