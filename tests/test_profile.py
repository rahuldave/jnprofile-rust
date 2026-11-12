"""Tests of the files of the profile: profile.json, skill-package.json, the recipes and the language layer."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lab'))
import lint_just  # noqa: E402

PROFILE = json.loads((REPO / 'profile.json').read_text())
PACKAGE = json.loads((REPO / 'skill-package.json').read_text())
STEPS = ['fmt-check', 'lint', 'typecheck', 'test', 'coverage-check', 'docstring-check']


class Profile(unittest.TestCase):
    def test_it_finds_a_rust_project_by_cargo_toml_and_names_the_code(self):
        self.assertEqual(PROFILE['name'], 'rust')
        self.assertEqual(PROFILE['detect'], ['Cargo.toml'])
        self.assertEqual(PROFILE['manifest'], 'Cargo.toml')
        self.assertEqual(PROFILE['code'], ['**/*.rs'])
        # Anchored, so that a module folder such as src/target/ stays in git.
        self.assertIn('/target/', PROFILE['base']['lines']['.gitignore'])
        self.assertIn('/target/', PROFILE['base']['lines']['.worktreedisposable'])
        self.assertNotIn('target/', PROFILE['base']['lines']['.gitignore'])

    def test_the_default_tools_give_every_step_and_no_api_tool(self):
        self.assertNotIn('api', PROFILE['tools'])
        self.assertEqual(set(PROFILE['default_tools']), set(PROFILE['tools']))
        verify = [step for tool in PROFILE['tools'].values() for step in tool['verify']]
        self.assertEqual(sorted(verify), sorted(STEPS))
        recipes = ''.join((REPO / '.agents/just/rust' / tool['recipes']).read_text()
                          for tool in PROFILE['tools'].values())
        for step in STEPS + ['test-paths']:
            self.assertRegex(recipes, rf'(?m)^{re.escape(step)}\b')

    def test_the_package_needs_cargo_cargo_llvm_cov_just_and_uv(self):
        names = [entry['name'] for entry in PACKAGE['executables']['required']]
        self.assertEqual(names, ['cargo', 'cargo-llvm-cov', 'just', 'uv'])
        self.assertEqual(PACKAGE['install_extras']['files'], ['.agents/just/rust', '.agents/targets/rust'])

    def test_the_language_layer_has_a_file_for_each_target(self):
        for target in ('review', 'test-review', 'docs', 'prose'):
            self.assertTrue((REPO / f'.agents/targets/rust/{target}.md').is_file(), target)

    def test_the_jnterfaces_entry_doc_shows_the_entry_of_the_lab(self):
        entry = json.loads((REPO / 'lab/stack-entry.json').read_text())
        doc = (REPO / 'docs/jnterfaces-entry.md').read_text()
        block = re.search(r'```json\n(.*?)```', doc, re.S).group(1)
        self.assertEqual(json.loads(block), entry)
        patch = (REPO / 'lab/jnterfaces-rust.patch').read_text()
        self.assertIn(patch.strip(), doc)


class Lint(unittest.TestCase):
    def test_the_recipes_of_the_profile_pass_the_lint(self):
        self.assertEqual([p for path in sorted((REPO / '.agents/just/rust').glob('*.just'))
                          for p in lint_just.problems(path)], [])

    def test_the_lint_finds_a_shebang_a_dash_line_no_cd_a_working_directory_attribute_and_jlog_run(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.just'
            path.write_text('a:\n    #!/usr/bin/env sh\n    true\n\nb:\n    -false\n\n[no-cd]\nc:\n    true\n\n'
                            "set working-directory := '..'\n\n[working-directory: 'x']\nw:\n    true\n\n[script]\nd:\n    true\n\n"
                            'e:\n    @-false\n\nexport JLOG_RUN := env("JLOG_RUN", uuid())\nJLOG_RUN := uuid()\n')
            found = lint_just.problems(path)
        self.assertEqual([line.split(': ', 1)[1] for line in found], [
            'a shebang line', 'a line with the prefix -, which ignores a failure', 'the [no-cd] attribute',
            'the setting working-directory, which only the module of the installer may have',
            'the [working-directory] attribute', 'the [script] attribute',
            'a line with the prefix -, which ignores a failure',
            'an assignment of JLOG_RUN, which only the root Justfile may have',
            'an assignment of JLOG_RUN, which only the root Justfile may have'])


if __name__ == '__main__':
    unittest.main()
