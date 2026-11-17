"""Tests of the files of the profile: profile.json, skill-package.json, the recipes and the language layer."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lab'))
import ci_programs  # noqa: E402
import lint_just  # noqa: E402

PROFILE = json.loads((REPO / 'profile.json').read_text())
PACKAGE = json.loads((REPO / 'skill-package.json').read_text())


def doc_entry():
    """The entry of stack.json that docs/jnterfaces-entry.md shows."""
    doc = (REPO / 'docs/jnterfaces-entry.md').read_text()
    return json.loads(re.search(r'```json\n(.*?)```', doc, re.S).group(1))


STEPS = ['fmt-check', 'lint', 'typecheck', 'test', 'integration', 'coverage-check', 'docstring-check', 'api-check']


class Floor(unittest.TestCase):
    """ci_programs.meets reads a floor as .agents/tools_check.py of jnterfaces does."""

    def test_a_newer_version_passes_and_an_older_one_fails(self):
        entry = {'version': '>=0.10.0'}
        self.assertTrue(ci_programs.meets(entry, 'taplo 0.10.0\n'))
        self.assertTrue(ci_programs.meets(entry, 'taplo 0.11.2\n'))
        self.assertTrue(ci_programs.meets(entry, 'taplo 1.0\n'))
        self.assertFalse(ci_programs.meets(entry, 'taplo 0.9.9\n'))

    def test_the_first_version_counts_and_a_nightly_suffix_is_ignored(self):
        entry = {'version': '>=1.102.0'}
        self.assertTrue(ci_programs.meets(entry, 'rustc 1.102.0-nightly (3ef0c22cd 2026-11-11)\n'))
        self.assertFalse(ci_programs.meets(entry, 'rustc 1.101.0-nightly (3ef0c22cd 2026-11-11)\n'))
        # The date comes after the version, so its numbers do not count.
        self.assertFalse(ci_programs.meets({'version': '>=2026.0.0'}, 'rustc 1.102.0 (x 2026-11-11)\n'))

    def test_output_with_no_version_fails_a_floor_and_the_expect_still_counts(self):
        self.assertFalse(ci_programs.meets({'version': '>=1.0.0'}, 'no version here\n'))
        self.assertTrue(ci_programs.meets({}, 'anything\n'))
        self.assertFalse(ci_programs.meets({'expect': 'llvm-tools', 'version': '>=1.0.0'}, 'tool 2.0.0\n'))

    def test_a_version_on_stderr_counts_when_stdout_has_none(self):
        self.assertTrue(ci_programs.meets({'version': '>=1.0.0'}, '', 'tool 1.2.3\n'))
        self.assertFalse(ci_programs.meets({'version': '>=1.0.0'}, '', 'tool 0.2.3\n'))
        # stdout comes first.
        self.assertFalse(ci_programs.meets({'version': '>=1.0.0'}, 'tool 0.9.0\n', 'tool 1.2.3\n'))

    def test_a_floor_or_a_version_with_two_numbers_counts_with_a_zero(self):
        self.assertTrue(ci_programs.meets({'version': '>=1.2'}, 'tool 1.2.0\n'))
        self.assertFalse(ci_programs.meets({'version': '>=1.2'}, 'tool 1.1.9\n'))
        self.assertTrue(ci_programs.meets({'version': '>=1.2.0'}, 'tool 1.2\n'))
        self.assertFalse(ci_programs.meets({'version': '>=1.2.1'}, 'tool 1.2\n'))
        self.assertTrue(ci_programs.meets({'version': ' >=1.2.0 '}, 'tool 1.2.0\n'))

    def test_a_malformed_floor_raises_an_error_that_names_it(self):
        for floor in ('1.0', '>1.0', '>=x', '', 1):
            with self.subTest(floor=floor), self.assertRaisesRegex(ValueError, 'not a version floor'):
                ci_programs.meets({'name': 'tool', 'version': floor}, 'tool 1.0.0\n')

    def test_ci_finds_a_program_below_its_floor_missing_so_it_installs_it(self):
        # A fake taplo of 0.9.0 on PATH fails the floor of taplo, so ci_programs.works says to install it.
        taplo = next(entry for entry in PROFILE['programs'] if entry['name'] == 'taplo')
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'taplo'
            fake.write_text('#!/bin/sh\necho "taplo 0.9.0"\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ.get('PATH', ''))
            self.assertFalse(ci_programs.works(taplo, folder, env))
            fake.write_text('#!/bin/sh\necho "taplo 0.11.0"\n')
            self.assertTrue(ci_programs.works(taplo, folder, env))


class Profile(unittest.TestCase):
    def test_it_finds_a_rust_project_by_cargo_toml_and_names_the_code(self):
        self.assertEqual(PROFILE['name'], 'rust')
        self.assertEqual(PROFILE['detect'], ['Cargo.toml'])
        self.assertEqual(PROFILE['manifest'], 'Cargo.toml')
        self.assertEqual(PROFILE['code'], ['**/*.rs'])
        self.assertEqual(PROFILE['tests'], ['tests/**'])
        # Anchored, so that a module folder such as src/target/ stays in git.
        self.assertIn('/target/', PROFILE['base']['lines']['.gitignore'])
        self.assertIn('/target/', PROFILE['base']['lines']['.worktreedisposable'])
        self.assertNotIn('target/', PROFILE['base']['lines']['.gitignore'])

    def test_the_default_tools_give_every_step_and_the_api_tool_of_python_and_typescript(self):
        # The entry of the python and typescript profiles, with the work folder of this profile, and the nightly that
        # smalldocs needs for rustdoc.
        self.assertEqual(PROFILE['tools']['api'], {
            'packages': [], 'recipes': 'api.just', 'verify': ['api-check'],
            'lines': {'.gitignore': ['.jnprofile-rust/'], '.worktreedisposable': ['.jnprofile-rust/']},
            'programs': ['nightly']})
        self.assertEqual(set(PROFILE['default_tools']), set(PROFILE['tools']) - {'toml'})
        verify = [step for name in PROFILE['default_tools'] for step in PROFILE['tools'][name]['verify']]
        self.assertEqual(sorted(verify), sorted(STEPS))
        recipes = ''.join((REPO / '.agents/just/rust' / tool['recipes']).read_text()
                          for tool in PROFILE['tools'].values())
        for step in STEPS + ['test-paths']:
            self.assertRegex(recipes, rf'(?m)^{re.escape(step)}\b')

    def test_the_package_needs_cargo_just_and_uv_and_the_profile_installs_the_rest(self):
        names = [entry['name'] for entry in PACKAGE['executables']['required']]
        self.assertEqual(names, ['cargo', 'just', 'uv'])
        self.assertEqual(PACKAGE['install_extras']['files'], ['.agents/just/rust', '.agents/targets/rust'])

    def test_the_programs_follow_the_schema_agreed_with_jnterfaces(self):
        programs = {entry['name']: entry for entry in PROFILE['programs']}
        self.assertEqual(len(programs), len(PROFILE['programs']))
        listed = PROFILE['base']['programs'] + [name for tool in PROFILE['tools'].values() for name in tool['programs']]
        self.assertEqual(set(listed), set(programs))
        for name, entry in programs.items():
            self.assertLessEqual(set(entry), {'name', 'check', 'expect', 'version', 'install', 'install_in',
                                              'description'}, name)
            for key in ('check', 'install'):
                if key in entry:
                    self.assertNotRegex(entry[key], r'[|&;<>$`*?(){}\\]', f'{name} {key} needs a shell')
                    self.assertEqual(shlex.join(shlex.split(entry[key])), entry[key], name)
            if 'install' in entry:
                self.assertRegex(entry['install'], r'^(cargo \+stable install --locked \S+@\d+\.\d+\.\d+'
                                                   r'|rustup component add \S+'
                                                   r'|rustup toolchain install( (stable|nightly-\d{4}-\d{2}-\d{2})'
                                                   r' --profile minimal)?)$')
            else:
                self.assertIn('https://rustup.rs/', entry['description'], name)
        # The programs that cannot be installed are checked first, and cargo comes before each cargo install.
        # Only rustup must be on the machine. cargo --version can fail when the pinned toolchain is missing, so
        # cargo comes with the toolchain, the first program that the profile installs.
        self.assertEqual(PROFILE['base']['programs'], ['rustup', 'toolchain'])
        self.assertEqual([name for name in programs if 'install' not in programs[name]], ['rustup'])
        self.assertEqual(list(programs)[:2], ['rustup', 'toolchain'])
        self.assertNotIn('cargo', programs)
        # cargo install builds with the toolchain of the folder, so it runs in the home folder, not under the pin.
        for name, entry in programs.items():
            home = entry.get('install', '').startswith(('cargo +stable install', 'rustup toolchain install stable',
                                                        'rustup toolchain install nightly-'))
            self.assertEqual(entry.get('install_in', 'project'), 'home' if home else 'project', name)
        # cargo install builds with stable, so it needs neither a default toolchain nor the pin of the project.
        order = list(programs)
        for name, entry in programs.items():
            if entry.get('install', '').startswith('cargo +stable install'):
                self.assertLess(order.index('stable'), order.index(name), name)
        self.assertEqual(PROFILE['tools']['toml']['programs'], ['stable', 'taplo', 'cargo-sort'])
        # A program that cargo installs at a fixed version has that version as its floor, so an old one is replaced,
        # and a newer one passes: no expect pins the minor line.
        for name, entry in programs.items():
            if entry.get('install', '').startswith('cargo +stable install'):
                self.assertEqual(entry['version'], '>=' + entry['install'].rsplit('@', 1)[1], name)
                self.assertNotIn('expect', entry, name)
        # Each floor is >=X.Y.Z, as jnterfaces reads it.
        for name, entry in programs.items():
            if 'version' in entry:
                self.assertRegex(entry['version'], r'^>=\d+\.\d+\.\d+$', name)
        self.assertEqual({name for name, entry in programs.items() if 'version' in entry},
                         {'rustup', 'cargo-llvm-cov', 'taplo', 'cargo-sort', 'nightly'})
        self.assertNotIn('toml', PROFILE['default_tools'])

    def test_each_check_passes_here_where_the_lab_has_every_program(self):
        for entry in PROFILE['programs']:
            command = shlex.split(entry['check'])
            if not shutil.which(command[0]):
                continue
            # RUSTUP_AUTO_INSTALL=0: a check must not install a missing toolchain on the machine.
            done = subprocess.run(command, capture_output=True, text=True, cwd=REPO,
                                  env=dict(os.environ, RUSTUP_AUTO_INSTALL='0'))
            with self.subTest(program=entry['name']):
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(ci_programs.meets(entry, done.stdout, done.stderr), done.stdout)

    def test_the_language_layer_has_a_file_for_each_target(self):
        # The agentic targets of agent_gest_git_skills read .agents/targets/LANGUAGE/TARGET.md, and the target that
        # writes docs is update-docs.
        self.assertFalse((REPO / '.agents/targets/rust/docs.md').exists())
        for target in ('review', 'test-review', 'update-docs', 'prose'):
            self.assertTrue((REPO / f'.agents/targets/rust/{target}.md').is_file(), target)

    def test_the_jnterfaces_entry_doc_shows_an_entry_that_finds_a_cargo_project(self):
        # The lab checks that the stack.json of jnterfaces has this entry.
        entry = doc_entry()
        self.assertEqual(entry['rust']['package'], 'jnprofile-rust')
        self.assertEqual(entry['rust']['detect'], PROFILE['detect'])

    def test_without_a_default_toolchain_the_install_of_toolchain_fails_and_its_description_says_what_to_do(self):
        # In an empty rustup home, with no pin, the check and the install of toolchain fail. The installer then
        # prints the description. The profile does not set a default itself. The servers point at a closed port, so
        # that a rustup that tried to download would fail at once instead.
        if not shutil.which('rustup'):
            self.skipTest('needs rustup')
        programs = {entry['name']: entry for entry in PROFILE['programs']}
        self.assertNotIn('rustup default', ' '.join(entry.get('install', '') for entry in PROFILE['programs']))
        self.assertIn('rustup default stable', programs['toolchain']['description'])
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as project:
            env = {k: v for k, v in os.environ.items() if not k.startswith('RUSTUP_')}
            env.update(RUSTUP_HOME=home, RUSTUP_DIST_SERVER='http://127.0.0.1:9',
                       RUSTUP_UPDATE_ROOT='http://127.0.0.1:9/rustup')
            for command in (programs['toolchain']['check'], programs['toolchain']['install']):
                done = subprocess.run(shlex.split(command), cwd=project, env=env, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL, timeout=60)
                with self.subTest(command=command):
                    self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(os.listdir(home), [], 'the commands wrote into the empty rustup home')

class Spec(unittest.TestCase):
    def test_the_spec_and_the_docs_agree_on_w_for_docstring_check(self):
        spec = (REPO / 'docs/spec.md').read_text()
        self.assertIn('`cargo clippy` with `-W missing_docs`', spec)
        self.assertIn('--cap-lints warn', spec)
        for page in ['README.md', 'docs/index.md', 'docs/tools.md', 'docs/jnterfaces-entry.md',
                     '.agents/skills/rust-profile/SKILL.md']:
            self.assertNotIn('spec gives the two lints with `-D`', (REPO / page).read_text(), page)
        self.assertIn('--cap-lints', (REPO / '.agents/just/rust/steps.py').read_text())


    def test_the_docs_follow_the_spec_on_test_files_and_the_code_key(self):
        spec = (REPO / 'docs/spec.md').read_text()
        self.assertIn('`test-parent` does not call\n  `test-paths`', spec)
        self.assertIn('The installer copies them into the key `code`', spec)
        self.assertIn('does not call `test-paths`', (REPO / 'docs/tools.md').read_text())
        self.assertIn('gives 1, not 2', (REPO / 'docs/tools.md').read_text())
        self.assertIn('`test-parent` runs no test', (REPO / '.agents/targets/rust/test-review.md').read_text())
        self.assertIn('matches it, `test-parent`', (REPO / '.agents/targets/rust/test-review.md').read_text())
        for page in ['docs/jnterfaces-entry.md', 'docs/tools.md', '.agents/skills/rust-profile/SKILL.md', 'README.md',
                     'docs/index.md']:
            text = (REPO / page).read_text()
            self.assertNotIn('No code of jnterfaces reads the key', text, page)
            self.assertNotIn('does not do that yet', text, page)
            self.assertNotIn('has no entry for the rust profile', text, page)
            self.assertNotIn('The change may add only unit tests', text, page)
        self.assertEqual(PROFILE['tests'], ['tests/**'])

class Lint(unittest.TestCase):
    def test_the_recipes_of_the_profile_pass_the_lint(self):
        self.assertEqual([p for path in sorted((REPO / '.agents/just/rust').glob('*.just'))
                          for p in lint_just.problems(path)], [])

    def test_the_lint_finds_a_shebang_a_dash_line_no_cd_a_working_directory_attribute_jlog_run_and_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.just'
            path.write_text('a:\n    #!/usr/bin/env sh\n    true\n\nb:\n    -false\n\n[no-cd]\nc:\n    true\n\n'
                            "set working-directory := '..'\n\n[working-directory: 'x']\nw:\n    true\n\n"
                            '[script]\nd:\n    true\n\n'
                            'e:\n    @-false\n\nexport JLOG_RUN := env("JLOG_RUN", uuid())\nJLOG_RUN := uuid()\n'
                            'set shell := ["sh", "-cu"]\nset windows-shell := ["cmd"]\nset windows-powershell\n')
            found = lint_just.problems(path)
        self.assertEqual([line.split(': ', 1)[1] for line in found], [
            'a shebang line', 'a line with the prefix -, which ignores a failure', 'the [no-cd] attribute',
            'the setting working-directory, which only the module of the installer may have',
            'the [working-directory] attribute', 'the [script] attribute',
            'a line with the prefix -, which ignores a failure',
            'an assignment of JLOG_RUN, which only the root Justfile may have',
            'an assignment of JLOG_RUN, which only the root Justfile may have',
            'the setting shell, which only the installer may set',
            'the setting shell, which only the installer may set',
            'the setting shell, which only the installer may set'])


if __name__ == '__main__':
    unittest.main()
