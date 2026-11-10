from pathlib import Path
import ast, json, re, unittest
ROOT=Path(__file__).parents[1]
HIDDEN_PATHS = ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json', '.antigravity-plugin/plugin.json')
class PackageTests(unittest.TestCase):
    def test_one_root_skill_and_manifest_identity(self):
        self.assertFalse((ROOT/'plugin.json').exists())
        docs={}
        for name in HIDDEN_PATHS:
            docs[name]=json.loads((ROOT/name).read_text())
            self.assertEqual((docs[name]['name'],docs[name]['version']),('cross-agent','0.0.7'),name)
        codex=docs['.codex-plugin/plugin.json']
        self.assertEqual(codex['skills'],'./skills/')
        self.assertTrue(codex['interface'])
        self.assertNotIn('extensions',codex)
        self.assertNotIn('skills',docs['.claude-plugin/plugin.json'])
        anti=docs['.antigravity-plugin/plugin.json']
        self.assertNotIn('skills',anti)
        self.assertEqual(anti['$schema'],'https://antigravity.google/schemas/v1/plugin.json')
        for key in ('displayName','suggestedPrompts','category'):
            self.assertTrue(anti.get(key),key)
        self.assertEqual(sorted(p.name for p in (ROOT/'skills').iterdir()),['cross-agent', 'transfer-context'])
        groups=json.loads((ROOT/'skills.sh.json').read_text())['groupings']
        self.assertEqual([g['title'] for g in groups].count('Renamed skills'),1)
        self.assertEqual(groups[-1]['title'],'Renamed skills')
        renamed=groups[-1]['skills']
        self.assertEqual(renamed,['context-transfer'])
        listed=[s for g in groups[:-1] for s in g['skills']]
        for name in renamed:
            self.assertNotIn(name,listed)
            self.assertFalse((ROOT/'skills'/name).exists(),name)
    def test_frontmatter_license_changelog_and_reference_links(self):
        for name, version in (('cross-agent', '0.0.6'), ('transfer-context', '0.0.6')):
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
