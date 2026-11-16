"""The tests of the api tool: api_rule.py, and the api commands of steps.py."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
RULE = REPO / '.agents/just/rust/api_rule.py'
sys.path.insert(0, str(REPO / '.agents/just/rust'))
import steps  # noqa: E402

PROFILE = json.loads((REPO / 'profile.json').read_text())
CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
NEEDS_NIGHTLY = shutil.which('rustup') is None or subprocess.run(
    ['rustup', 'run', steps.NIGHTLY, 'rustc', '--version'], capture_output=True).returncode != 0
WHY = f'needs rustup with the toolchain {steps.NIGHTLY}'


def git(root, *args):
    """Run git with ARGS in ROOT, and fail on an error."""
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *args], cwd=root,
                          check=True, capture_output=True, text=True)


class Folder(unittest.TestCase):
    """A temporary git repository as the current folder."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        git(self.root, 'init', '-q')
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)

    def write(self, path, text=''):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def crate(self, folder='.', name='app', lib='', main=None):
        self.write(f'{folder}/Cargo.toml', CARGO.format(name=name))
        if lib is not None:
            self.write(f'{folder}/src/lib.rs', lib)
        if main is not None:
            self.write(f'{folder}/src/main.rs', main)


class Rule(Folder):
    """is_api of api_rule.py, as the commit gate runs it: python -I -B at the top, from the parent commit."""

    def setUp(self):
        super().setUp()
        self.stack()

    def stack(self, tools=('rust:lint', 'rust:api'), docs=True):
        self.write('.agents/stack.json', json.dumps({'tools': list(tools), 'choices': {'docs': docs}}))

    def is_api(self, *paths):
        script = (f'import importlib.util, json, sys\nspec = importlib.util.spec_from_file_location("r", {str(RULE)!r})\n'
                  'r = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(r)\n'
                  'print(json.dumps([r.is_api(p) for p in sys.argv[1:]]))')
        done = subprocess.run([sys.executable, '-I', '-B', '-c', script, *paths], cwd=self.root, capture_output=True,
                              text=True, check=True)
        return dict(zip(paths, json.loads(done.stdout)))

    def test_the_files_of_a_library_are_api_files(self):
        self.crate()
        self.write('src/a/b.rs')
        self.write('src/main.rs')
        self.write('src/bin/tool.rs')
        found = self.is_api('src/lib.rs', 'src/a/b.rs', 'src/main.rs', 'src/bin/tool.rs', 'tests/api.rs',
                            'examples/e.rs', 'benches/b.rs', 'build.rs', 'src/notes.md', 'Cargo.toml')
        self.assertEqual(found, {'src/lib.rs': True, 'src/a/b.rs': True, 'src/main.rs': False,
                                 'src/bin/tool.rs': False, 'tests/api.rs': False, 'examples/e.rs': False,
                                 'benches/b.rs': False, 'build.rs': False, 'src/notes.md': False,
                                 'Cargo.toml': False})

    def test_a_crate_with_only_a_binary_documents_the_binary(self):
        self.crate(lib=None, main='fn main() {}\n')
        self.write('src/cli.rs')
        self.assertEqual(self.is_api('src/main.rs', 'src/cli.rs', 'src/bin/x.rs'),
                         {'src/main.rs': True, 'src/cli.rs': True, 'src/bin/x.rs': False})

    def test_settings_with_no_rust_section_leave_the_crates_as_api_check_does(self):
        self.crate()
        self.write('docs/_smalldocs.yml', 'api:\n  packages: [helper]\n')
        self.assertTrue(self.is_api('src/lib.rs')['src/lib.rs'])
        self.write('docs/_smalldocs.yml', 'api:\n  - language: typescript\n    entry: ../web/index.ts\n')
        self.assertTrue(self.is_api('src/lib.rs')['src/lib.rs'])

    def test_a_crate_with_only_binaries_in_src_bin_counts_each_file_under_src(self):
        self.crate(lib=None)
        self.write('src/bin/tool.rs', 'fn main() {}\n')
        self.write('src/shared.rs')
        self.assertEqual(self.is_api('src/bin/tool.rs', 'src/shared.rs', 'tests/t.rs'),
                         {'src/bin/tool.rs': True, 'src/shared.rs': True, 'tests/t.rs': False})

    def test_each_crate_of_a_workspace_counts_by_its_nearest_cargo_toml(self):
        self.write('Cargo.toml', '[workspace]\nmembers = ["crates/*"]\n')
        self.crate('crates/one', 'one')
        self.crate('crates/two', 'two', lib=None, main='fn main() {}\n')
        self.write('crates/none/src/lib.rs')
        found = self.is_api('crates/one/src/lib.rs', 'crates/two/src/main.rs', 'crates/one/tests/t.rs',
                            'crates/none/src/lib.rs', 'src/lib.rs')
        self.assertEqual(found, {'crates/one/src/lib.rs': True, 'crates/two/src/main.rs': True,
                                 'crates/one/tests/t.rs': False, 'crates/none/src/lib.rs': False,
                                 'src/lib.rs': False})

    def test_a_deleted_file_counts_by_the_crate_that_held_it(self):
        self.crate()
        self.assertTrue(self.is_api('src/gone.rs')['src/gone.rs'])

    def test_a_path_outside_the_repository_or_absolute_is_no_api_file(self):
        self.crate()
        self.assertEqual(self.is_api('../src/lib.rs', str(self.root / 'src/lib.rs'), 'src/../src/lib.rs'),
                         {'../src/lib.rs': False, str(self.root / 'src/lib.rs'): False, 'src/../src/lib.rs': False})

    def test_no_crate_means_no_api_file(self):
        self.write('src/lib.rs')
        self.assertFalse(self.is_api('src/lib.rs')['src/lib.rs'])

    def test_the_rule_writes_nothing_and_needs_only_the_standard_library(self):
        self.crate()
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'one')
        self.is_api('src/lib.rs')
        self.assertEqual(git(self.root, 'status', '--porcelain', '--ignored').stdout, '')
        imports = [line for line in RULE.read_text().splitlines() if line.startswith(('import ', 'from '))]
        self.assertEqual(imports, ['import json', 'import os', 'import re'])

    def test_without_the_api_tool_or_the_docs_role_no_file_is_an_api_file(self):
        self.crate()
        for tools, docs in ((('rust:lint',), True), (('rust:api',), False), (('python:api',), True)):
            self.stack(tools, docs)
            self.assertFalse(self.is_api('src/lib.rs')['src/lib.rs'], (tools, docs))
        self.write('.agents/stack.json', '{not json')
        self.assertFalse(self.is_api('src/lib.rs')['src/lib.rs'])
        self.stack()
        self.assertTrue(self.is_api('src/lib.rs')['src/lib.rs'])

    def test_the_rust_sections_of_the_docs_settings_name_the_crates(self):
        self.write('Cargo.toml', '[workspace]\nmembers = ["crates/*"]\n')
        self.crate('crates/one', 'one')
        self.crate('crates/two', 'two')
        self.crate('crates/three', 'three')
        self.write('docs/_smalldocs.yml', 'project:\n  title: x\napi:\n  - language: python\n    packages: [x]\n'
                                          '  - language: rust\n    manifest: ../crates/one/Cargo.toml\n'
                                          '  - language: rust   # the second crate\n'
                                          '    manifest: "../crates/two/Cargo.toml"\n')
        self.assertEqual(self.is_api('crates/one/src/lib.rs', 'crates/two/src/a.rs', 'crates/three/src/lib.rs'),
                         {'crates/one/src/lib.rs': True, 'crates/two/src/a.rs': True, 'crates/three/src/lib.rs': False})

    def test_a_rust_section_with_no_manifest_takes_the_crate_of_its_root(self):
        self.crate()
        self.write('docs/_smalldocs.yml', 'api:\n  language: rust\n')
        self.assertTrue(self.is_api('src/lib.rs')['src/lib.rs'])
        self.write('docs/_smalldocs.yml', 'api:\n  language: python\n  packages: [x]\n')
        self.assertTrue(self.is_api('src/lib.rs')['src/lib.rs'])
        self.write('docs/_smalldocs.yml', 'api:\n  language: rust\n')
        self.crate('docs', 'site')
        self.write('docs/_smalldocs.yml', 'api:\n  language: rust\n')
        self.assertEqual(self.is_api('src/lib.rs', 'docs/src/lib.rs'), {'src/lib.rs': False, 'docs/src/lib.rs': True})

    def test_settings_that_cannot_be_read_or_point_out_of_the_repository_give_false(self):
        self.crate()
        for text in ('api: {language: rust}\n', 'api:\n  language: "rust\n', 'api:\n  - language: rust\n'
                     '    manifest: ../../Cargo.toml\n', 'api:\n  language: rust\n  root: ../..\n'):
            self.write('docs/_smalldocs.yml', text)
            self.assertFalse(self.is_api('src/lib.rs')['src/lib.rs'], text)

    def test_a_crate_inside_another_crate_keeps_its_own_files(self):
        self.crate()
        self.crate('src/inner', 'inner')
        self.write('docs/_smalldocs.yml', 'api:\n  language: rust\n  manifest: ../Cargo.toml\n')
        self.assertEqual(self.is_api('src/a.rs', 'src/inner/src/lib.rs'),
                         {'src/a.rs': True, 'src/inner/src/lib.rs': False})

    def test_generated_files_and_files_of_the_tools_are_no_api_files(self):
        self.crate()
        self.write('.agents/generated.txt', '# written by build.rs\nsrc/gen/\nsrc/out.rs\n')
        self.assertEqual(self.is_api('src/gen/x.rs', 'src/out.rs', 'src/a.rs'),
                         {'src/gen/x.rs': False, 'src/out.rs': False, 'src/a.rs': True})

    def test_the_rule_runs_on_python_3_9(self):
        found = shutil.which('uv')
        if not found:
            self.skipTest('needs uv')
        self.crate()
        done = subprocess.run(['uv', 'run', '--no-project', '--quiet', '--python', '3.9', 'python', '-I', '-B', '-c',
                               f'exec(open({str(RULE)!r}).read()); print(is_api("src/lib.rs"))'], cwd=self.root,
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'True'), done.stderr)


class Prose(Folder):
    """The run prose of api-prose-check runs once in a run of just, for one tree, one base and one HEAD."""

    def setUp(self):
        super().setUp()
        self.write('src/lib.rs')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'one')

    def ran(self, run, args=(), env=None, finish=True):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'JLOG_RUN': run, **(env or {})}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            for key in steps.BASE_VARIABLES:
                if key not in (env or {}):
                    os.environ.pop(key, None)
            if finish:
                steps.begin('prose', list(args))
                steps.stamp('prose')
                return out.getvalue()
            return steps.ran('prose', list(args))

    def test_a_passed_run_is_not_run_again_in_the_same_run_of_just(self):
        self.ran('run-a')
        self.assertTrue(self.ran('run-a', finish=False))
        self.assertTrue(steps.saved_run('prose').is_file())
        self.assertEqual(steps.saved_run('prose').parent, Path('.jnprofile-rust'))

    def test_another_run_another_base_another_ci_or_another_head_runs_again(self):
        self.ran('run-a', ['--base', 'main'])
        self.assertTrue(self.ran('run-a', ['--base', 'main'], finish=False))
        self.assertFalse(self.ran('run-b', ['--base', 'main'], finish=False))
        self.assertFalse(self.ran('run-a', finish=False))
        self.assertFalse(self.ran('run-a', ['--base', 'main'], {'GITHUB_BASE_REF': 'dev'}, finish=False))
        git(self.root, 'commit', '-q', '--allow-empty', '-m', 'two')
        self.assertFalse(self.ran('run-a', ['--base', 'main'], finish=False))

    def test_a_change_of_the_tree_runs_again(self):
        self.ran('run-a')
        self.write('src/lib.rs', '//! x\n')
        self.assertFalse(self.ran('run-a', finish=False))

    def test_a_file_changed_during_the_run_drops_it(self):
        with mock.patch.dict(os.environ, {'JLOG_RUN': 'run-a'}):
            steps.begin('prose', [])
            self.write('src/lib.rs', '//! y\n')
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                steps.stamp('prose')
        self.assertIn('api-prose-check: a file changed during the run', out.getvalue())
        self.assertFalse(self.ran('run-a', finish=False))

    def test_the_run_of_prose_leaves_the_runs_of_the_tests_alone(self):
        self.assertEqual(steps.saved_run('test'), steps.REPORTS / 'test.run.json')
        self.assertEqual(steps.saved_run('prose'), Path('.jnprofile-rust/prose.run.json'))


class Parts(Folder):
    """The pieces of api check and api prose that need no smalldocs."""

    def test_the_base_option_takes_base_and_v_and_nothing_else(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(steps.base_option([]), '')
            self.assertEqual(steps.base_option(['-v', '--base', 'main']), 'main')
            self.assertEqual(steps.base_option(['--base=a', '--base', 'b']), 'b')
            self.assertEqual(steps.base_option(['--base']), 2)
            self.assertEqual(steps.base_option(['--base=']), 2)
            self.assertEqual(steps.base_option(['--other']), 2)

    def test_the_module_comment_is_the_inner_doc_comment_at_the_top(self):
        cases = {
            '//! The crate.\n//!\n//! More.\n\nuse x;\n': 'The crate.\n\nMore.',
            '﻿\n//! First.\n/// Item.\npub fn f() {}\n': 'First.',
            '/*! Block\n * comment. */\npub fn f() {}\n': 'Block\ncomment.',
            '/// Item.\npub fn f() {}\n//! Late.\n': None,
            '#![deny(missing_docs)]\n//! After.\n': 'After.',
            '// Copyright 2026 X\n//! After a plain comment.\n': 'After a plain comment.',
            '/* licence */\n#![forbid(unsafe_code)]\n\n//! After both.\npub fn f() {}\n': 'After both.',
            '//! A\n\n//! B\nuse x;\n//! Not this.\n': 'A\nB',
            '//// Four slashes are a plain comment.\n//! Then this.\n': 'Then this.',
            '/**/\n//! After an empty comment.\n': 'After an empty comment.',
            '#![doc = "[x]"]\n#![cfg_attr(a, b[c])]\n//! After brackets.\n': 'After brackets.',
            '/* a /* b */ c */\n//! After a nested comment.\n': 'After a nested comment.',
            '/** Outer. */\npub fn f() {}\n': None,
            '/* not closed\n//! No.\n': None,
            '': None,
        }
        for number, (text, want) in enumerate(cases.items()):
            self.write(f'f{number}.rs', text)
            self.assertEqual(steps.module_comment(f'f{number}.rs'), want, repr(text))
        self.assertIsNone(steps.module_comment('missing.rs'))

    def test_the_links_name_the_files_with_percent_encoding_undone(self):
        text = (f'[source]({steps.MARK}src/lib.rs#L7) [source]({steps.MARK}src/a%20b.rs#L1) '
                '[other](https://example.invalid/src/x.rs#L1)')
        self.assertEqual(steps.linked_files(text), {'src/lib.rs', 'src/a b.rs'})

    def test_the_default_settings_give_a_section_for_each_crate_with_the_pinned_nightly(self):
        self.write('Cargo.toml', '[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n')
        self.crate('crates/one', 'one')
        self.crate('crates/two', 'two', lib=None, main='fn main() {}\n')
        self.write('crates/three/Cargo.toml', CARGO.format(name='three'))
        self.write('crates/three/tests/t.rs')
        with tempfile.TemporaryDirectory() as temp:
            folder, about = steps.api_settings(Path(temp))
            sections = json.loads((folder / '_smalldocs.yml').read_text())['api']
        self.assertEqual(about, 'the 2 crates of the workspace')
        self.assertEqual([(s['language'], Path(s['manifest']).parent.name, s['toolchain'], s['out'])
                          for s in sections],
                         [('rust', 'one', steps.NIGHTLY, 'api/one'), ('rust', 'two', steps.NIGHTLY, 'api/two')])

    def test_a_crate_with_no_library_and_no_binary_has_no_api(self):
        self.write('Cargo.toml', CARGO.format(name='app'))
        self.write('tests/t.rs')
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(steps.api_settings(Path(temp))[0], None)

    def test_the_pin_of_steps_is_the_nightly_of_the_profile(self):
        nightly = {entry['name']: entry for entry in PROFILE['programs']}['nightly']
        self.assertEqual(nightly['check'], f'rustup run {steps.NIGHTLY} rustc --version')
        self.assertEqual(nightly['install'], f'rustup toolchain install {steps.NIGHTLY} --profile minimal')
        self.assertEqual(nightly['install_in'], 'home')
        self.assertIn(steps.NIGHTLY, (REPO / '.agents/just/rust/api.just').read_text())

    def test_the_api_files_follow_the_rule_and_leave_out_generated_files(self):
        self.crate()
        self.write('.agents/generated.txt', 'src/gen.rs\n')
        self.write('src/gen.rs')
        self.write('src/a.rs')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'one')
        self.assertEqual(steps.api_files(['src/lib.rs', 'src/gen.rs', 'tests/t.rs', 'src/a.rs']),
                         ['src/lib.rs', 'src/a.rs'])

    def test_the_api_files_of_prose_need_neither_the_api_tool_nor_the_docs_role(self):
        self.crate()
        self.assertFalse((self.root / '.agents/stack.json').exists())
        self.assertEqual(steps.api_files(['src/lib.rs']), ['src/lib.rs'])

    def test_the_api_files_of_a_project_in_a_subfolder_use_the_path_from_the_top(self):
        os.chdir(self.root)
        (self.root / 'sub').mkdir()
        self.crate('sub')
        os.chdir(self.root / 'sub')
        self.assertEqual(steps.api_files(['src/lib.rs', 'tests/t.rs']), ['src/lib.rs'])

    def test_the_api_files_of_a_project_in_a_subfolder_follow_its_own_docs_settings(self):
        self.write('sub/Cargo.toml', '[workspace]\nmembers = ["crates/*"]\n')
        self.crate('sub/crates/one', 'one')
        self.crate('sub/crates/two sp', 'two')
        self.write('sub/docs/_smalldocs.yml', 'api:\n  language: rust\n  manifest: ../crates/one/Cargo.toml\n')
        os.chdir(self.root / 'sub')
        self.assertEqual(steps.api_files(['crates/one/src/lib.rs', 'crates/two sp/src/lib.rs']),
                         ['crates/one/src/lib.rs'])

    def test_settings_that_the_rule_cannot_read_leave_the_changed_files_of_each_crate(self):
        self.crate()
        self.write('docs/_smalldocs.yml', 'api: {language: rust, manifest: ../Cargo.toml}\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(steps.api_files(['src/lib.rs', 'tests/t.rs', 'src/bin/x.rs']), ['src/lib.rs'])
        self.assertIn('api_rule.py cannot read the key api', err.getvalue())

    def test_the_api_files_write_no_bytecode_into_the_folder_of_the_profile(self):
        folder = self.root / '.agents/just/rust'
        folder.mkdir(parents=True)
        for name in ('steps.py', 'api_rule.py'):
            shutil.copy(REPO / '.agents/just/rust' / name, folder / name)
        self.crate()
        script = ('import importlib.util, sys\nsys.dont_write_bytecode = True\n'
                  'spec = importlib.util.spec_from_file_location("s", ".agents/just/rust/steps.py")\n'
                  's = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(s)\n'
                  'sys.dont_write_bytecode = False\nprint(s.api_files(["src/lib.rs"]))')
        done = subprocess.run([sys.executable, '-c', script], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "['src/lib.rs']", done.stderr)
        self.assertEqual(list(folder.rglob('__pycache__')), [])


@unittest.skipIf(NEEDS_NIGHTLY, WHY)
class Smalldocs(Folder):
    """api check and api prose with smalldocs and rustdoc of the pinned nightly."""

    DOCUMENTED = ('//! The crate.\n//!\n//! It adds.\n\n/// Adds two numbers.\n///\n/// # Returns\n/// The sum.\n'
                  'pub fn add(a: i32, // the first\n           b: i32, // the second\n) -> i32 {\n    a + b\n}\n')

    def setUp(self):
        super().setUp()
        self.write('.gitignore', '/target/\n')
        self.crate(lib=self.DOCUMENTED)
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.root, capture_output=True)
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-q', '-m', 'one')

    def api(self, *argv, env=None):
        out = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), *argv], cwd=self.root,
                             capture_output=True, text=True, env={**os.environ, 'CI': '', **(env or {})})
        return out.returncode, out.stdout + out.stderr

    def checker(self):
        """A stand-in checker of nossaifd: it copies each page it gets into checked/ and fails on the word BAD."""
        self.write(str(steps.PROSE_CHECKER), 'import shutil, sys\nfrom pathlib import Path\nbad = 0\n'
                   'for page in sys.argv[1:]:\n    Path("checked").mkdir(exist_ok=True)\n'
                   '    shutil.copy(page, Path("checked") / Path(page).name)\n'
                   '    bad |= "BAD" in Path(page).read_text()\nsys.exit(1 if bad else 0)\n')
        self.write('.git/info/exclude', 'checked/\n.agents/\n')

    def test_a_documented_crate_passes_and_an_undocumented_item_fails(self):
        code, out = self.api('api', 'check')
        self.assertEqual(code, 0, out)
        self.assertIn('api-check: smalldocs api check, with the crate app', out)
        self.write('src/lib.rs', self.DOCUMENTED + '\n/// Bare.\npub fn bare(x: i32) -> i32 {\n    x\n}\n')
        code, out = self.api('api', 'check')
        self.assertEqual(code, 1, out)
        self.assertIn('no return docment', out)
        self.assertIn('# Returns section', out)

    def test_the_rust_sections_of_the_docs_config_win_and_take_the_pin(self):
        self.write('src/lib.rs', self.DOCUMENTED + '\n/// Bare.\npub fn bare(x: i32) -> i32 {\n    x\n}\n')
        self.write('docs/_smalldocs.yml', 'api:\n  - language: python\n    package: x\n'
                                          '  - language: rust\n    manifest: ../Cargo.toml\n    private: false\n')
        code, out = self.api('api', 'check')
        self.assertEqual(code, 1, out)
        self.assertIn('the Rust sections of the api settings of docs/_smalldocs.yml', out)
        with tempfile.TemporaryDirectory() as temp:
            done = subprocess.run(steps.smalldocs('python', str(REPO / '.agents/just/rust/steps.py'), 'config', 'docs',
                                                  temp), cwd=self.root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            config = (Path(temp) / '_smalldocs.yml').read_text()
        self.assertIn(f'toolchain: {steps.NIGHTLY}', config)
        self.assertIn(f'manifest: {self.root / "Cargo.toml"}', config)
        self.assertNotIn('python', config)

    def test_a_docs_config_with_no_rust_section_falls_back_to_the_crates(self):
        self.write('docs/_smalldocs.yml', 'api:\n  language: python\n  package: x\n')
        code, out = self.api('api', 'check')
        self.assertEqual(code, 0, out)
        self.assertIn('with the crate app', out)

    def test_prose_without_the_checker_passes_with_a_word(self):
        code, out = self.api('api', 'prose')
        self.assertEqual(code, 0, out)
        self.assertIn('no checker of nossaifd', out)

    def test_prose_checks_the_pages_and_module_comments_of_the_changed_api_files_only(self):
        self.checker()
        code, out = self.api('api', 'prose')
        self.assertEqual((code, 'no code of the API changed since HEAD' in out), (0, True), out)
        self.write('src/inner.rs', '//! The inner module.\n//!\n//! It is BAD.\n\n/// Doubles.\n///\n/// # Returns\n'
                                   '/// Twice x.\npub fn double(x: i32, // the number\n) -> i32 {\n    x * 2\n}\n')
        self.write('src/lib.rs', self.DOCUMENTED + '\n/// The inner module.\npub mod inner;\n')
        self.write('tests/t.rs', '//! A test that is BAD, but no API file.\n')
        code, out = self.api('api', 'prose')
        self.assertEqual(code, 1, out)
        self.assertIn('the API pages app, inner, the module comment of src/inner.rs, the module comment of src/lib.rs',
                      out)
        self.assertIn('fix each hard problem in the doc comment', out)
        checked = sorted(path.name for path in (self.root / 'checked').iterdir())
        self.assertEqual(checked, ['app.md', 'inner.md', 'inner.rs.md', 'lib.rs.md'])
        self.assertIn('Doubles.', (self.root / 'checked/inner.md').read_text())

    def test_prose_with_a_base_needs_the_gate_of_nossaifd(self):
        self.checker()
        code, out = self.api('api', 'prose', '--base', 'HEAD')
        self.assertEqual(code, 2, out)
        self.assertIn('needs base_commit', out)

    def test_prose_with_a_base_checks_the_changes_since_the_merge_base(self):
        self.checker()
        self.write(str(steps.PROSE_GATE), 'import subprocess\n'
                   'def ci_set():\n    return False\n'
                   'def base_commit(top, ref):\n'
                   '    out = subprocess.run(["git", "merge-base", ref, "HEAD"], cwd=top, capture_output=True, text=True)\n'
                   '    return out.stdout.strip(), ref\n')
        git(self.root, 'branch', 'main')
        self.write('src/lib.rs', self.DOCUMENTED.replace('It adds.', 'It adds, BAD.'))
        git(self.root, 'commit', '-q', '-am', 'two')
        code, out = self.api('api', 'prose', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('the module comment of src/lib.rs, of the files changed since main', out)
        code, out = self.api('api', 'prose')
        self.assertIn('no code of the API changed since HEAD', out)

    def test_the_check_leaves_no_file_in_the_project(self):
        self.checker()
        self.write('src/lib.rs', self.DOCUMENTED + '\n')
        self.api('api', 'check')
        self.api('api', 'prose')
        status = git(self.root, 'status', '--porcelain', '--ignored', '--untracked-files=all').stdout.splitlines()
        self.assertEqual([line for line in status if 'checked/' not in line and '.agents/' not in line],
                         [' M src/lib.rs'])


if __name__ == '__main__':
    unittest.main()
