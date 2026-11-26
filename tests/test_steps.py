"""Tests of the helper script .agents/just/rust/steps.py, without cargo."""
import concurrent.futures
import contextlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / '.agents/just/rust'))
import steps  # noqa: E402

CARGO_TEST = ['cargo', 'test', '--locked', '--all-features', '--package']


# The summary line of a test binary with one passed test.
PASSED = ('     Running tests/api.rs (target/debug/deps/api-1)\nrunning 1 test\n'
          'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n')


class Lcov(unittest.TestCase):
    def test_filter_drops_the_records_of_excluded_files_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(os.path.realpath(temp))
            text = (f'SF:{root}/src/gen.rs\nDA:1,0\nend_of_record\nSF:{root}/src/lib.rs\nDA:1,1\nend_of_record\n'
                    'SF:/elsewhere/dep.rs\nDA:1,0\nend_of_record\n')
            kept = steps.filter_lcov(text, root, lambda path: path == 'src/gen.rs')
        self.assertNotIn('gen.rs', kept)
        self.assertIn('src/lib.rs\nDA:1,1\nend_of_record', kept)
        self.assertIn('/elsewhere/dep.rs', kept)


def message(code, path, line, level='warning', text='missing documentation for a function'):
    """One JSON line of cargo clippy for a diagnostic CODE at PATH:LINE."""
    return json.dumps({'reason': 'compiler-message', 'message': {
        'code': {'code': code} if code else None, 'level': level, 'message': text, 'rendered': f'{level}: {text}',
        'spans': [{'is_primary': True, 'file_name': path, 'line_start': line, 'column_start': 1}]}})


class Docs(unittest.TestCase):
    def test_findings_name_file_and_line_in_order_and_skip_generated_and_other_lints(self):
        lines = '\n'.join([
            message('missing_docs', 'src/lib.rs', 10),
            message('clippy::missing_docs_in_private_items', 'src/lib.rs', 2),
            message('clippy::missing_docs_in_private_items', 'src/lib.rs', 2),
            message('missing_docs', 'src/gen/x.rs', 1),
            message('unused_variables', 'src/lib.rs', 4),
            message('E0308', 'src/lib.rs', 5, level='error', text='mismatched types'),
            '{"reason": "build-finished", "success": false}', 'not json'])
        with tempfile.TemporaryDirectory() as temp:
            findings, errors = steps.doc_findings(lines, Path(os.path.realpath(temp)),
                                                  lambda path: path.startswith('src/gen/'))
        self.assertEqual(findings, [
            'src/lib.rs:2:1: missing documentation for a function (clippy::missing_docs_in_private_items)',
            'src/lib.rs:10:1: missing documentation for a function (missing_docs)'])
        self.assertEqual(errors, ['error: mismatched types'])

    def test_a_path_with_a_colon_and_an_error_level_doc_lint_still_give_findings(self):
        lines = '\n'.join([message('missing_docs', 'src/a:b.rs', 3, level='error'),
                           message('missing_docs', 'src/a.rs', 10), message('missing_docs', 'src/a.rs', 9)])
        with tempfile.TemporaryDirectory() as temp:
            findings, errors = steps.doc_findings(lines, Path(os.path.realpath(temp)), lambda path: False)
        self.assertEqual([f.split(': ')[0] for f in findings], ['src/a.rs:9:1', 'src/a.rs:10:1', 'src/a:b.rs:3:1'])
        self.assertEqual(errors, [])


STAND_IN_CARGO = """#!/bin/sh
if [ "$1" = metadata ]; then cat "$META"; exit "${META_CODE:-0}"; fi
if [ "$1 $2" = "llvm-cov --version" ]; then echo "cargo-llvm-cov 0.9.1"; exit 0; fi
if [ "$1 $2" = "llvm-cov clean" ] || [ "$1" = clean ]; then
  printf '%s\\n' "$*" >> "$CLEAN_LOG"
  exit "${CLEAN_CODE:-${CODE:-0}}"
fi
printf '%s\\n' "$*" >> "$LOG"
out=; name=x; last=
for arg in "$@"; do
  [ "$last" = --output-path ] && out=$arg
  [ "$last" = --package ] && name=$arg
  last=$arg
done
[ -n "$out" ] && printf 'SF:/s/%s.rs\\nDA:1,1\\nend_of_record\\n' "$name" > "$out"
exit "${CODE:-0}"
"""


class Merge(unittest.TestCase):
    def test_a_line_runs_when_one_report_runs_it_and_is_measured_when_one_report_measures_it(self):
        one = 'SF:/p/a.rs\nFN:1,f\nDA:1,1\nDA:2,0\nend_of_record\nSF:/p/b.rs\nDA:5,0\nend_of_record\n'
        two = 'SF:/p/a.rs\nDA:2,4\nDA:9,0\nend_of_record\n'
        self.assertEqual(steps.merge_lcov([one, two]),
                         'SF:/p/a.rs\nDA:1,1\nDA:2,4\nDA:9,0\nend_of_record\nSF:/p/b.rs\nDA:5,0\nend_of_record\n')
        self.assertEqual(steps.merge_lcov(['', '']), '')

    def test_missed_lines_names_each_line_with_a_count_of_0(self):
        root = Path('/p')
        self.assertEqual(steps.missed_lines('SF:/p/b.rs\nDA:5,0\nDA:6,2\nend_of_record\nSF:/p/a.rs\nDA:3,0\n'
                                            'end_of_record\n', root), ['a.rs:3', 'b.rs:5'])


class StandInCargo(unittest.TestCase):
    """A scratch folder with a stand-in cargo that prints the metadata and logs its arguments."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        (self.root / 'bin').mkdir()
        (self.root / 'bin/cargo').write_text(STAND_IN_CARGO)
        (self.root / 'bin/cargo').chmod(0o755)
        self.log = self.root / 'cargo.log'
        env = {k: v for k, v in os.environ.items() if k != 'JLOG_RUN'}
        env.update(PATH=f'{self.root / "bin"}:{os.environ["PATH"]}', META=str(self.root / 'meta.json'),
                   LOG=str(self.log), CLEAN_LOG=str(self.root / 'clean.log'))
        patched = mock.patch.dict(os.environ, env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)


class Targets(StandInCargo):
    """target_flags and run_tests, with a stand-in cargo that prints the metadata and logs its arguments."""

    def packages(self, *groups):
        """Write the metadata of a workspace with one package for each list of targets in GROUPS.

        A target is a kind, such as 'lib', or a tuple of its kind, its name and the value of its key test.
        """
        packages = []
        for n, group in enumerate(groups):
            targets = []
            for i, target in enumerate(group):
                kind, name, tested = target if isinstance(target, tuple) else (target, f't{n}{i}', True)
                targets.append({'name': name, 'kind': [kind], 'test': tested, 'src_path': f'/x/{n}/{i}.rs'})
            packages.append({'name': f'p{n}', 'id': f'path+file:///x/{n}#p{n}@0.1.0', 'targets': targets})
        (self.root / 'meta.json').write_text(json.dumps({'packages': packages}))

    def test_test_takes_the_libraries_and_the_tested_binaries_by_name(self):
        for groups, want in (([['lib', ('bin', 'app', True), 'test']], [['--workspace', '--lib', '--bin', 'app']]),
                             ([[('bin', 'app', True)]], [['--workspace', '--bin', 'app']]),
                             ([['proc-macro']], [['--workspace', '--lib']]),
                             ([['cdylib'], [('bin', 'b', True)]], [['--workspace', '--lib', '--bin', 'b']]),
                             ([[('bin', 'b', False)]], []),
                             ([['test', 'bench', 'example']], [])):
            self.packages(*groups)
            self.assertEqual(steps.selections('test'), want, groups)

    def test_a_library_with_test_false_leaves_the_others_each_in_a_run_of_its_package(self):
        self.packages([('lib', 'a', False)], ['lib'], [('lib', 'c', True), ('bin', 'cb', True)])
        self.assertEqual(steps.selections('test'), [['--package', 'path+file:///x/1#p1@0.1.0', '--lib'],
                                                   ['--package', 'path+file:///x/2#p2@0.1.0', '--lib', '--bin', 'cb']])
        self.packages([('lib', 'a', False)])
        self.assertEqual(steps.selections('test'), [])

    def test_integration_takes_each_tested_test_example_and_bench_by_name(self):
        self.packages(['lib', ('test', 'api', True), ('test', 'slow', False), ('example', 'demo', True),
                       ('example', 'plain', False), ('bench', 'speed', True)], [('test', 'api', True)])
        self.assertEqual(steps.selections('integration'), [['--workspace', '--test', 'api', '--example', 'demo',
                                                            '--bench', 'speed']])
        self.packages(['lib', 'bin', ('test', 'slow', False), ('bench', 'b', False)])
        self.assertEqual(steps.selections('integration'), [])

    def test_a_name_that_one_package_tests_and_another_does_not_gives_a_run_for_each_package(self):
        self.packages(['lib', ('test', 'it', True)], ['lib', ('test', 'it', False), ('test', 'own', True)], ['lib'])
        self.assertEqual(steps.selections('integration'), [['--package', 'path+file:///x/0#p0@0.1.0', '--test', 'it'],
                                                          ['--package', 'path+file:///x/1#p1@0.1.0', '--test', 'own']])
        self.packages([('bin', 'tool', True)], [('bin', 'tool', False)])
        self.assertEqual(steps.selections('test'), [['--package', 'path+file:///x/0#p0@0.1.0', '--bin', 'tool']])

    def test_extra_arguments_that_select_targets_take_the_place_of_the_selections(self):
        for args, want in ((['--test', 'api'], 'targets'), (['--test=api'], 'targets'), (['--doc'], 'targets'),
                           (['-p', 'x'], 'packages'), (['-px'], 'packages'), (['--package=x'], 'packages'),
                           (['--all', '--lib'], 'packages'), (['--lib', '--workspace'], 'packages'),
                           (['--exclude', 'b'], 'targets'), (['--workspace', '--exclude', 'b'], 'packages'),
                           (['parse'], None), (['parse', '--', '--test', '-p'], None), ([], None)):
            self.assertEqual(steps.chosen_by(args), want, args)
        self.packages(['lib', ('test', 'api', True), ('test', 'other', True)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('integration', ['--test', 'api']), 0)
        self.assertEqual(self.log.read_text().splitlines(), [
            'llvm-cov test --all-features --locked --include-build-script --no-clean --workspace --lcov --output-path '
            'target/llvm-cov/integration.part0.lcov --test api'])
        self.log.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', ['-p', 'core', '--lib']), 0)
        self.assertEqual(self.log.read_text().splitlines(), [
            'llvm-cov test --all-features --locked --include-build-script --no-clean --lcov --output-path '
            'target/llvm-cov/test.part0.lcov -p core --lib'])

    def quick(self, *args):
        """Run quick_test with ARGS, and return its exit code and the commands of the stand-in cargo."""
        self.log.unlink(missing_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            code = steps.quick_test(list(args))
        return code, self.log.read_text().splitlines() if self.log.exists() else []

    def test_quick_test_takes_the_targets_of_just_test_with_a_plain_cargo_test(self):
        """A workspace with no library gets no --lib, which cargo test refuses there."""
        self.packages([('bin', 'app', True), ('test', 'it', True)])
        self.assertEqual(self.quick('parse'), (0, ['test --all-features --locked --workspace --bin app parse']))
        self.packages([('lib', 'a', False)], [('lib', 'b', True)])
        self.assertEqual(self.quick(), (0, ['test --all-features --locked --package path+file:///x/1#p1@0.1.0 --lib']))
        self.packages(['test'])
        self.assertEqual(self.quick(), (0, []))

    def test_quick_test_lets_extra_arguments_that_choose_take_the_place_of_the_selections(self):
        self.packages(['lib', 'bin'])
        self.assertEqual(self.quick('-p', 'core'), (0, ['test --all-features --locked -p core']))
        self.assertEqual(self.quick('--test', 'it'), (0, ['test --all-features --locked --workspace --test it']))

    def test_quick_test_stops_at_the_first_cargo_that_fails(self):
        self.packages([('lib', 'a', False)], ['lib'], ['lib'])
        os.environ['CODE'] = '101'
        code, log = self.quick()
        self.assertEqual((code, len(log)), (101, 1))

    def test_integration_runs_with_nextest_only_when_the_project_sets_it(self):
        self.packages(['lib', ('test', 'api', True)])
        for value, runner in (('1', 'nextest'), ('', 'test'), ('0', 'test'), ('yes', 'test')):
            self.log.unlink(missing_ok=True)
            with mock.patch.dict(os.environ, {steps.NEXTEST: value}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(steps.run_tests('integration', []), 0)
            line = self.log.read_text().splitlines()[0]
            self.assertTrue(line.startswith(f'llvm-cov {runner} --all-features'), (value, line))
            self.assertEqual('--no-tests=pass' in line, runner == 'nextest', value)
        self.log.unlink()
        with mock.patch.dict(os.environ, {steps.NEXTEST: '1'}), contextlib.redirect_stdout(io.StringIO()):
            steps.run_tests('test', [])
        self.assertTrue(self.log.read_text().startswith('llvm-cov test '))

    def test_run_gives_cargo_the_targets_the_report_and_the_arguments_last(self):
        self.packages(['lib', ('test', 'api', True)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(steps.run_tests('integration', ['parse', '--', '--nocapture']), 0)
        want = ('llvm-cov test --all-features --locked --include-build-script --no-clean --workspace --test api --lcov '
                '--output-path target/llvm-cov/integration.part0.lcov parse -- --nocapture')
        self.assertEqual(self.log.read_text().splitlines(), [want])
        self.assertIn('cargo ' + want, out.getvalue())
        self.assertEqual(steps.report('integration').read_text(), 'SF:/s/x.rs\nDA:1,1\nend_of_record\n')

    def test_run_merges_the_report_of_each_run_of_cargo(self):
        self.packages([('lib', 'a', False)], ['lib'], ['lib'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', []), 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)
        self.assertEqual(steps.report('test').read_text(),
                         'SF:/s/path+file:///x/1#p1@0.1.0.rs\nDA:1,1\nend_of_record\n'
                         'SF:/s/path+file:///x/2#p2@0.1.0.rs\nDA:1,1\nend_of_record\n')

    def test_workspace_cleanup_preserves_the_locked_dependency_contract(self):
        self.packages(['lib'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', []), 0)
        self.assertEqual((self.root / 'clean.log').read_text(), 'llvm-cov clean --workspace --locked\n')

    def test_run_with_no_target_runs_no_cargo_test_and_writes_an_empty_report(self):
        self.packages(['lib'])
        steps.report('integration').parent.mkdir(parents=True)
        steps.report('integration').write_text('SF:old\nend_of_record\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(steps.run_tests('integration', []), 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(steps.report('integration').read_text(), '')
        self.assertIn('integration: the workspace has no test target, so no test runs', out.getvalue())

    def test_run_gives_the_code_of_a_failed_cargo_and_removes_the_old_report(self):
        self.packages(['lib'])
        steps.report('test').parent.mkdir(parents=True)
        steps.report('test').write_text('SF:old\nend_of_record\n')
        with mock.patch.dict(os.environ, CODE='101'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', []), 101)
        self.assertFalse(steps.report('test').exists())
        self.assertFalse(steps.pending_run('test').exists())

    def test_run_fails_when_cargo_metadata_fails(self):
        self.packages(['lib'])
        with mock.patch.dict(os.environ, META_CODE='101'), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(steps.run_tests('test', []), 1)
        self.assertIn('test: cargo metadata failed', err.getvalue())
        self.assertFalse(self.log.exists())

    def test_main_runs_only_the_two_steps(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(steps.main(['run', 'docs']), 2)
            self.assertEqual(steps.main(['fresh']), 2)
            self.assertEqual(steps.main(['coverage', 'x']), 2)
        self.assertIn('STEP is test or integration', err.getvalue())


class WarmCoverage(StandInCargo):
    """The coverage build stays warm: a full clean only for the first run or a new fingerprint of the build."""

    def setUp(self):
        super().setUp()
        for args in (['init', '-q'], ['config', 'user.name', 'T'], ['config', 'user.email', 't@example.invalid']):
            subprocess.run(['git', *args], cwd=self.root, check=True)
        (self.root / '.gitignore').write_text('/target/\n/bin/\n/*.log\n/meta.json\n')
        for name in ('a', 'b', 'b/inner', 'c'):
            (self.root / name / 'src').mkdir(parents=True)
            (self.root / name / 'Cargo.toml').write_text(f'[package]\nname = "{name.replace("/", "-")}"\n')
            (self.root / name / 'src/lib.rs').write_text('pub fn f() {}\n')
        (self.root / 'Cargo.toml').write_text('[workspace]\nmembers = ["a", "b", "b/inner", "c"]\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'one'], cwd=self.root, check=True)
        uses = {'a': [], 'b': [], 'b/inner': [{'name': 'a', 'kind': None, 'path': str(self.root / 'a')}],
                'c': [{'name': 'b-inner', 'kind': 'dev', 'path': str(self.root / 'b/inner')},
                      {'name': 'serde', 'kind': None}]}
        packages = [{'name': name.replace('/', '-'), 'id': f'path+file://{self.root / name}#0.1.0',
                     'manifest_path': str(self.root / name / 'Cargo.toml'), 'dependencies': uses[name],
                     'targets': [{'name': name.replace('/', '-'), 'kind': ['lib'], 'test': True,
                                  'src_path': str(self.root / name / 'src/lib.rs')}]}
                    for name in ('a', 'b', 'b/inner', 'c')]
        ids = {p['name']: p['id'] for p in packages}
        # The resolved graph, as cargo metadata gives it without --no-deps. serde is not a member.
        nodes = [{'id': p['id'], 'deps': [{'pkg': ids.get(d['name'], 'registry+serde'), 'name': d['name']}
                                          for d in p['dependencies']]} for p in packages]
        meta = {'packages': packages, 'target_directory': str(self.root / 'target'),
                'workspace_members': [p['id'] for p in packages], 'resolve': {'nodes': nodes}}
        (self.root / 'meta.json').write_text(json.dumps(meta))
        self.meta = meta
        self.clean_log = self.root / 'clean.log'

    def check(self, **env):
        """Run the step test, and return its code and the cleans that it asked of cargo."""
        self.clean_log.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            code = steps.run_tests('test', [])
        return code, self.clean_log.read_text().splitlines() if self.clean_log.exists() else []

    def test_the_tests_run_with_no_clean(self):
        self.assertIn('--no-clean', steps.COVERAGE)
        self.check()
        self.assertTrue(all('--no-clean' in line for line in self.log.read_text().splitlines()))

    def test_the_first_run_cleans_the_workspace_and_the_next_cleans_only_the_profraw_files(self):
        self.assertEqual(self.check(), (0, ['llvm-cov clean --workspace --locked']))
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked']))

    def commit(self, message):
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', message], cwd=self.root, check=True)

    def test_a_change_committed_since_the_saved_run_is_cleaned(self):
        """A cache of CI or a seeded worktree brings a build of an older commit: its changes count from that one."""
        self.check()
        (self.root / 'b/inner/src/lib.rs').write_text('pub fn g() {}\n')
        self.commit('two')
        folder = self.root / 'target/llvm-cov-target'
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked',
                                            f'clean --locked --target-dir {folder} -p b-inner',
                                            f'clean --locked --target-dir {folder} -p c']))
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked']))

    def test_a_soft_reset_after_the_saved_run_still_cleans_the_changes_against_head(self):
        self.check()
        (self.root / 'b/inner/src/lib.rs').write_text('pub fn g() {}\n')
        self.commit('two')
        self.check()
        subprocess.run(['git', 'reset', '-q', '--soft', 'HEAD~1'], cwd=self.root, check=True)
        folder = self.root / 'target/llvm-cov-target'
        self.assertIn(f'clean --locked --target-dir {folder} -p b-inner', self.check()[1])

    def test_a_commit_with_no_rust_change_keeps_the_build_warm(self):
        self.check()
        (self.root / 'b/README.md').write_text('words\n')
        self.commit('two')
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked']))

    def test_a_stamp_of_a_commit_that_the_clone_does_not_have_cleans_the_workspace(self):
        self.check()
        stamp = self.root / 'target/llvm-cov-target/jnprofile-rust-build.txt'
        text = stamp.read_text()
        self.assertRegex(text, r'\ncommit [0-9a-f]{40}\n$')
        stamp.write_text(text.rpartition('\ncommit ')[0] + '\ncommit ' + 'ab' * 20 + '\n')
        self.assertEqual(self.check(), (0, ['llvm-cov clean --workspace --locked']))

    def test_a_stamp_with_no_commit_cleans_the_workspace(self):
        self.check()
        stamp = self.root / 'target/llvm-cov-target/jnprofile-rust-build.txt'
        stamp.write_text(stamp.read_text().rpartition('\ncommit ')[0])
        self.assertEqual(self.check(), (0, ['llvm-cov clean --workspace --locked']))

    def test_each_package_that_holds_a_changed_rust_file_is_cleaned_and_the_others_stay(self):
        self.check()
        (self.root / 'b/inner/src/lib.rs').write_text('pub fn g() {}\n')
        (self.root / 'a/src/new.rs').write_text('pub fn h() {}\n')
        (self.root / 'b/README.md').write_text('words\n')
        folder = self.root / 'target/llvm-cov-target'
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked',
                                            f'clean --locked --target-dir {folder} -p a',
                                            f'clean --locked --target-dir {folder} -p b-inner',
                                            f'clean --locked --target-dir {folder} -p c']))

    def test_each_package_that_depends_on_a_changed_one_is_cleaned_too(self):
        """Its binaries hold the old code of the changed package, and llvm-cov may take their counts."""
        self.check()
        (self.root / 'a/src/lib.rs').write_text('pub fn g() {}\n')
        folder = self.root / 'target/llvm-cov-target'
        self.assertEqual(self.check()[1], ['llvm-cov clean --profraw-only --locked',
                                           f'clean --locked --target-dir {folder} -p a',
                                           f'clean --locked --target-dir {folder} -p b-inner',
                                           f'clean --locked --target-dir {folder} -p c'])
        (self.root / 'a/src/lib.rs').write_text('pub fn f() {}\n')
        (self.root / 'c/src/lib.rs').write_text('pub fn g() {}\n')
        self.assertEqual(self.check()[1], ['llvm-cov clean --profraw-only --locked',
                                           f'clean --locked --target-dir {folder} -p c'])

    def test_a_changed_file_in_the_dep_info_of_another_crate_cleans_the_workspace(self):
        """A crate that compiles the file through #[path] or include! does not depend on its package."""
        self.check()
        (self.root / 'a/src/lib.rs').write_text('pub fn g() {}\n')
        deps = self.root / 'target/llvm-cov-target/debug/deps'
        deps.mkdir(parents=True)
        (deps / 'b-0123.d').write_text(f'{deps}/b-0123: {self.root}/b/src/lib.rs {self.root}/a/src/lib.rs\n\n'
                                       f'{self.root}/a/src/lib.rs:\n')
        self.assertEqual(self.check()[1], ['llvm-cov clean --workspace --locked'])
        (deps / 'b-0123.d').write_text(f'{deps}/b-0123: {self.root}/b/src/lib.rs\n')
        self.assertEqual(self.check()[1][0], 'llvm-cov clean --profraw-only --locked')

    def test_a_changed_file_that_is_not_rust_in_any_dep_info_cleans_the_workspace(self):
        """include! can read any file into the code of a crate."""
        (self.root / 'a/src/table.in').write_text('1\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'two'], cwd=self.root, check=True)
        self.check()
        deps = self.root / 'target/llvm-cov-target/debug/deps'
        deps.mkdir(parents=True)
        (deps / 'a-0123.d').write_text(f'{deps}/a-0123: {self.root}/a/src/lib.rs {self.root}/a/src/table.in\n')
        self.assertEqual(self.check()[1], ['llvm-cov clean --profraw-only --locked'])
        (self.root / 'a/src/table.in').write_text('2\n')
        self.assertEqual(self.check()[1], ['llvm-cov clean --workspace --locked'])

    def test_the_dep_info_reader_takes_the_sources_of_the_output_and_paths_with_spaces(self):
        info = self.root / 'x.d'
        info.write_text(f'/t/x: {self.root}/a\\ b.rs {self.root}/c.rs\n\n{self.root}/a\\ b.rs:\n/elsewhere/z.rs:\n')
        self.assertEqual(steps.dep_info(info), {str(self.root / 'a b.rs'), str(self.root / 'c.rs')})

    def test_a_build_folder_of_its_own_cleans_the_workspace_on_each_run(self):
        """cargo clean -p would clean another folder than the build of cargo llvm-cov."""
        full = (0, ['llvm-cov clean --workspace --locked'])
        moved = {**self.meta, 'build_directory': str(self.root / 'bdir')}
        (self.root / 'meta.json').write_text(json.dumps(moved))
        self.assertEqual(self.check(), full)
        self.assertEqual(self.check(), full)
        (self.root / 'meta.json').write_text(json.dumps(self.meta))
        for name in ('CARGO_LLVM_COV_TARGET_DIR', 'CARGO_LLVM_COV_BUILD_DIR'):
            self.check()
            self.assertEqual(self.check(**{name: str(self.root / 'elsewhere')}), full)

    def test_a_run_with_extra_arguments_saves_no_fingerprint(self):
        """An argument such as --config can change the build."""
        full = (0, ['llvm-cov clean --workspace --locked'])
        self.clean_log.unlink(missing_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', ['--config', 'profile.test.debug=0']), 0)
        self.assertEqual(self.check(), full)

    def test_a_new_fingerprint_of_the_build_cleans_the_workspace(self):
        """A new name of a test binary leaves the old binary, whose lines would count as lines that no test runs."""
        full = (0, ['llvm-cov clean --workspace --locked'])
        self.check()
        (self.root / 'a/Cargo.toml').write_text('[package]\nname = "a"\nversion = "0.2.0"\n')
        self.assertEqual(self.check(), full)
        self.assertEqual(self.check(), (0, ['llvm-cov clean --profraw-only --locked']))
        (self.root / 'Cargo.lock').write_text('# new\n')
        self.assertEqual(self.check(), full)
        (self.root / '.cargo').mkdir()
        (self.root / '.cargo/config.toml').write_text('[build]\n')
        self.assertEqual(self.check(), full)
        self.assertEqual(self.check(RUSTFLAGS='-C debuginfo=1'), full)
        self.assertEqual(self.check(RUSTFLAGS='-C debuginfo=1')[1], ['llvm-cov clean --profraw-only --locked'])
        for name, value in (('CARGO_PROFILE_TEST_DEBUG', '0'), ('CARGO_INCREMENTAL', '0'),
                            ('CARGO_BUILD_RUSTFLAGS', '-C opt-level=1')):
            self.assertEqual(self.check(RUSTFLAGS='-C debuginfo=1', **{name: value}), full, name)
        (self.root / '.gitignore').write_text('/target/\n/bin/\n/*.log\n/meta.json\n/.cargo/\n')
        self.check()
        (self.root / '.cargo/config.toml').write_text('[build]\nincremental = false\n')
        self.assertEqual(self.check(), full)

    def test_an_empty_rustc_wrapper_for_coverage_check_costs_two_full_cleans(self):
        """docs/tools.md tells a user of sccache to run RUSTC_WRAPPER= for a commit of declarations only."""
        full = (0, ['llvm-cov clean --workspace --locked'])
        warm = (0, ['llvm-cov clean --profraw-only --locked'])
        self.check(RUSTC_WRAPPER='sccache')
        self.assertEqual(self.check(RUSTC_WRAPPER='sccache'), warm)
        self.assertEqual(self.check(RUSTC_WRAPPER=''), full)
        self.assertEqual(self.check(RUSTC_WRAPPER='sccache'), full)
        self.assertEqual(self.check(RUSTC_WRAPPER='sccache'), warm)

    def test_the_fingerprint_is_saved_only_after_a_run_passes(self):
        """cargo llvm-cov clean exits with 0 even when its cargo clean fails, so only a passed run proves the clean."""
        full = (0, ['llvm-cov clean --workspace --locked'])
        code, cleans = self.check(CODE='101', CLEAN_CODE='0')
        self.assertEqual((code, cleans), (101, ['llvm-cov clean --workspace --locked']))
        self.assertEqual(self.check(), full)

    def test_a_failed_clean_of_a_package_stops_the_run_and_forgets_the_fingerprint(self):
        self.check()
        (self.root / 'a/src/lib.rs').write_text('pub fn g() {}\n')
        code, cleans = self.check(CLEAN_CODE='101')
        self.assertEqual((code, cleans), (101, ['llvm-cov clean --profraw-only --locked']))
        self.assertFalse(self.log.read_text().count('--lcov') > 1)
        self.assertEqual(self.check()[1], ['llvm-cov clean --workspace --locked'])

    def test_with_no_parent_commit_each_run_cleans_the_workspace(self):
        subprocess.run(['git', 'update-ref', '-d', 'HEAD'], cwd=self.root, check=True)
        self.check()
        self.assertEqual(self.check()[1], ['llvm-cov clean --workspace --locked'])


@unittest.skipUnless(shutil.which('cargo-llvm-cov') and shutil.which('cargo'), 'needs cargo llvm-cov')
class WarmCoverageCargo(unittest.TestCase):
    """The real cargo llvm-cov: a warm run compiles nothing, and a new version leaves no line of an old binary."""

    def test_a_new_version_of_a_package_leaves_no_line_of_the_old_test_binary(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'src').mkdir()
        (root / 'Cargo.toml').write_text('[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'src/lib.rs').write_text('pub fn add(a: u32) -> u32 { a + 1 }\npub fn more(a: u32) -> u32 { a * 2 }\n'
                                         '#[cfg(test)]\nmod t { #[test] fn a() { assert_eq!(super::add(1), 2); } }\n')
        (root / '.gitignore').write_text('/target/\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        old = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old)

        def run():
            """Clean as the step does, run the tests with coverage, and give the stderr and the lines of the report."""
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code, save = steps.clean_coverage_build()
                self.assertEqual(code, 0)
                done = subprocess.run([*steps.COVERAGE, '--lcov', '--output-path', 'r.lcov'], capture_output=True,
                                      text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                save()
            return done.stderr, sorted(line for line in Path('r.lcov').read_text().splitlines()
                                       if line.startswith('DA:'))
        _, first = run()
        err, again = run()
        self.assertNotIn('Compiling w', err)
        self.assertEqual(again, first)
        (root / 'Cargo.toml').write_text('[package]\nname = "w"\nversion = "0.2.0"\nedition = "2021"\n')
        (root / 'src/lib.rs').write_text('pub fn add(a: u32) -> u32 { a + 1 }\n'
                                         '#[cfg(test)]\nmod t { #[test] fn a() { assert_eq!(super::add(1), 2); } }\n')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        # Committed, so no changed file cleans the package: only the new fingerprint of the build can.
        for args in (['add', '-A'], ['commit', '-q', '-m', 'two']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        err, after = run()
        self.assertIn('Compiling w', err)
        self.assertNotIn('DA:2,0', after)
        self.assertNotIn('DA:4,0', after)



@unittest.skipUnless(shutil.which('cargo-llvm-cov') and shutil.which('cargo'), 'needs cargo llvm-cov')
class QuickTestCargo(unittest.TestCase):
    """The real cargo test of quick-test, on a package with only a binary, where cargo test --lib fails."""

    def test_a_package_with_only_a_binary_runs_its_tests(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'src').mkdir()
        (root / 'Cargo.toml').write_text('[package]\nname = "single"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'src/main.rs').write_text('fn main() {}\n#[cfg(test)]\nmod t { #[test] fn unit_ran() {} }\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'quick-test'], cwd=root,
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('unit_ran', done.stdout)


class Launcher(unittest.TestCase):
    """The RUSTC_WRAPPER of the coverage steps runs no shell, so each variable of cargo reaches rustc."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.helper = self.root / 'helper.py'
        self.helper.write_text('import json, os, sys\nprint(json.dumps([os.environ.get("CARGO_BIN_EXE_my-tool"), '
                               'sys.argv]))\n')

    def run_launcher(self, launcher):
        env = dict(os.environ, **{'CARGO_BIN_EXE_my-tool': '/x/my-tool'})
        done = subprocess.run([str(launcher), 'rustc', 'a b', '--crate-name', 't'], env=env, capture_output=True,
                              text=True, check=True)
        return json.loads(done.stdout)

    def test_a_variable_whose_name_has_a_hyphen_reaches_the_helper(self):
        """dash, the /bin/sh of Ubuntu, drops such a variable, and env! of a test then fails to compile."""
        launcher = steps.write_launcher(self.root / 'rustc-wrapper', self.helper)
        self.assertEqual(self.run_launcher(launcher), ['/x/my-tool', [str(self.helper), 'rustc', 'a b', '--crate-name',
                                                                      't']])
        interpreter = launcher.read_text().splitlines()[0][2:]
        self.assertEqual(os.path.realpath(interpreter), os.path.realpath(sys.executable))

    def test_the_launcher_through_a_link_keeps_the_modules_of_this_python(self):
        """steps.py runs in a virtual environment of uv with tree-sitter, which a Python started from a link loses."""
        (self.root / 'only').mkdir()
        (self.root / 'only/onlyhere.py').write_text('NAME = "here"\n')
        self.helper.write_text('import onlyhere\nprint(onlyhere.NAME)\n')
        spaced = self.root / 'a python' / 'python'
        spaced.parent.mkdir()
        spaced.symlink_to(os.path.realpath(sys.executable))
        with mock.patch.object(sys, 'executable', str(spaced)), \
                mock.patch.object(sys, 'path', [sys.path[0], str(self.root / 'only'), *sys.path[1:]]):
            launcher = steps.write_launcher(self.root / 'rustc-wrapper', self.helper)
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        done = subprocess.run([str(launcher)], env=env, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'here'), done.stderr)

    def test_the_launcher_is_utf_8_in_any_locale(self):
        """Python reads a script as UTF-8, so a helper path with an accent works also in a Latin-1 locale."""
        helper = self.root / 'josé' / 'helper.py'
        helper.parent.mkdir()
        helper.write_text('print("ran")\n')
        script = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import steps; '
                  'steps.write_launcher(Path(sys.argv[2]), Path(sys.argv[3]))')
        env = dict(os.environ, LC_ALL='en_US.ISO8859-1', PYTHONUTF8='0')
        subprocess.run([sys.executable, '-c', script, str(REPO / '.agents/just/rust'), str(self.root / 'launcher'),
                        str(helper)], env=env, check=True, capture_output=True)
        done = subprocess.run([str(self.root / 'launcher')], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, 'ran\n'), done.stderr)

    def test_a_python_path_with_a_space_goes_through_a_link(self):
        spaced = self.root / 'a python' / 'python'
        spaced.parent.mkdir()
        spaced.symlink_to(os.path.realpath(sys.executable))
        with mock.patch.object(sys, 'executable', str(spaced)):
            launcher = steps.write_launcher(self.root / 'rustc-wrapper', self.helper)
        self.assertNotIn(' ', launcher.read_text().splitlines()[0])
        self.assertEqual(self.run_launcher(launcher)[0], '/x/my-tool')


@unittest.skipUnless(shutil.which('cargo-llvm-cov') and shutil.which('cargo'), 'needs cargo llvm-cov')
class LauncherCargo(unittest.TestCase):
    """The real integration step: a test reads the path of a binary whose name has a hyphen."""

    def test_env_of_a_binary_with_a_hyphen_compiles_through_the_wrapper(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'src/bin').mkdir(parents=True)
        (root / 'tests').mkdir()
        (root / 'Cargo.toml').write_text('[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'src/lib.rs').write_text('pub fn one() -> u32 { 1 }\n')
        (root / 'src/bin/my-tool.rs').write_text('fn main() {}\n')
        (root / 'tests/it.rs').write_text('#[test]\nfn path() {\n'
                                          '    assert!(env!("CARGO_BIN_EXE_my-tool").ends_with("my-tool"));\n'
                                          '    assert_eq!(w::one(), 1);\n}\n')
        (root / '.gitignore').write_text('/target/\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'run', 'integration'],
                              cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])


class IntegrationChangedRules(unittest.TestCase):
    """The rules of integration-changed: the module of a file, the paths a test names, and the names of a library."""

    def test_the_module_path_of_a_source_file(self):
        root = Path(os.path.realpath(tempfile.gettempdir())) / 'p/src'
        for path, want in (('lib.rs', []), ('main.rs', []), ('store.rs', ['store']), ('store/mod.rs', ['store']),
                           ('store/disk.rs', ['store', 'disk'])):
            self.assertEqual(steps.module_path(root / path, root), want, path)
        self.assertIsNone(steps.module_path(root.parent / 'tests/api.rs', root))

    def test_the_first_segments_that_a_test_names(self):
        for text, want in (('use app::store::get;', {'store'}),
                           ('use app::{other::two, store::{get}};', {'other', 'store'}),
                           ('let x = app :: store :: get();', {'store'}),
                           ('use app::r#type::x;', {'type'}),
                           ('use myapp::store::get; use x::app::y;', set()),
                           ('use app;', None),
                           ('use app::{self, store};', None),
                           ('fn f() {}', set())):
            self.assertEqual(steps.first_segments(text, 'app'), want, text)

    def test_a_library_is_named_by_its_crate_or_by_the_rename_of_a_dependency(self):
        packages = [{'id': 'a', 'name': 'my-app', 'dependencies': []},
                    {'id': 'u', 'name': 'user', 'dependencies': [{'name': 'my-app', 'rename': 'a', 'path': '/x'}]},
                    {'id': 'v', 'name': 'plain', 'dependencies': [{'name': 'my-app', 'rename': None, 'path': '/x'}]},
                    {'id': 'w', 'name': 'other', 'dependencies': [{'name': 'serde', 'rename': None}]}]
        self.assertEqual(steps.library_names(packages, ('my-app', 'my_app')),
                         {'a': {'my_app'}, 'u': {'a'}, 'v': {'my_app'}, 'w': set()})


@unittest.skipUnless(shutil.which('cargo'), 'needs cargo')
class IntegrationChangedCargo(unittest.TestCase):
    """The real integration-changed: which test targets run for a change, and that it prints them."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(os.path.realpath(temp.name))
        for folder in ('src/store', 'src/bin', 'tests/support', 'tests/multi', 'itest', 'examples'):
            (root / folder).mkdir(parents=True)
        files = {
            'Cargo.toml': ('[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n'
                           '[[test]]\nname = "custom"\npath = "itest/main.rs"\n\n'
                           '[[example]]\nname = "ex"\ntest = true\n'),
            'src/lib.rs': 'pub mod other;\npub mod store;\npub mod data;\n',
            'src/store.rs': 'mod disk;\npub fn get() -> u32 { disk::one() }\n',
            'src/store/disk.rs': 'pub fn one() -> u32 { 1 }\n',
            'src/other.rs': 'pub fn two() -> u32 { 2 }\n',
            'src/data.rs': 'pub const TEXT: &str = include_str!("data.txt");\n',
            'src/data.txt': 'one\n',
            'src/main.rs': 'fn main() { println!("{}", app::store::get()); }\n',
            'tests/api.rs': '#[test]\nfn api() { assert_eq!(app::store::get(), 1); }\n',
            'tests/group.rs': ('use app::{other::two, store::{get}};\n#[test]\n'
                               'fn g() { assert_eq!(get() + two(), 3); }\n'),
            'tests/alias.rs': 'use app as a;\n#[test]\nfn al() { assert_eq!(a::store::get(), 1); }\n',
            'tests/db.rs': ('#[path = "support/mod.rs"]\n#[allow(\n    dead_code,\n)]\nmod helpers;\n'
                            '#[test]\nfn db() { assert!(helpers::ok()); }\n'),
            'tests/fixture.rs': ('const F: &str = include_str!("support/fixture.txt");\n'
                                 '#[test]\nfn f() { assert_eq!(F, "x\\n"); }\n'),
            'tests/cli.rs': ('#[test]\nfn cli() {\n    let out = std::process::Command::new(env!("CARGO_BIN_EXE_app"))'
                             '.output().unwrap();\n    assert_eq!(out.stdout, b"1\\n");\n}\n'),
            'tests/unrelated.rs': '#[test]\nfn u() { assert_eq!(app::other::two(), 2); }\n',
            'tests/multi/main.rs': 'mod part;\n#[test]\nfn m() { part::p(); }\n',
            'tests/multi/part.rs': 'pub fn p() {}\n',
            'tests/support/mod.rs': 'pub fn ok() -> bool { true }\n',
            'tests/support/fixture.txt': 'x\n',
            'examples/ex.rs': ('fn main() {}\n#[test]\nfn ex() { assert_eq!(app::store::get(), 1); }\n'),
            'tests/cli_extra.rs': ('#[test]\nfn e() {\n    let _ = env!("CARGO_BIN_EXE_app-extra");\n}\n'),
            'src/bin/app-extra.rs': 'fn main() {}\n',
            'itest/main.rs': '#[test]\nfn custom() { assert_eq!(app::other::two(), 2); }\n',
            '.gitignore': '/target/\n'}
        for name, text in files.items():
            (root / name).write_text(text)
        self.env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=self.env, check=True,
                       capture_output=True)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=self.env, check=True, capture_output=True)

    def changed(self, cwd=None):
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'integration-changed'],
                              cwd=cwd or self.root, env=self.env, capture_output=True, text=True)
        runs = sorted(re.findall(r'^integration-changed: runs app --(?:test|example) ([\w-]+)', done.stdout, re.M))
        skips = sorted(re.findall(r'^integration-changed: skips app --(?:test|example) ([\w-]+)', done.stdout, re.M))
        return done.returncode, runs, skips, done.stdout + done.stderr

    def test_a_changed_private_submodule_runs_the_tests_that_name_its_module_and_the_binary(self):
        (self.root / 'src/store/disk.rs').write_text('pub fn one() -> u32 { 2 - 1 }\n')
        code, runs, _, out = self.changed()
        self.assertEqual((code, runs), (0, ['alias', 'api', 'cli', 'cli_extra', 'ex', 'group']), out)
        self.assertIn('test api ... ok', out)
        self.assertIn('test ex ... ok', out)

    def test_a_changed_helper_runs_each_test_that_reads_it_by_path_or_mod(self):
        (self.root / 'tests/support/mod.rs').write_text('pub fn ok() -> bool { 1 == 1 }\n')
        code, runs, _, out = self.changed()
        self.assertEqual((code, runs), (0, ['db']), out)

    def test_a_file_that_include_str_reads_counts(self):
        (self.root / 'tests/support/fixture.txt').write_text('x\n\n')
        code, runs, _, out = self.changed()
        self.assertEqual(runs, ['fixture'], out)
        self.assertEqual(code, 1, out)

    def test_a_changed_binary_runs_the_tests_that_run_it(self):
        (self.root / 'src/main.rs').write_text('fn main() { println!("{}", app::store::get() + 0); }\n')
        code, runs, _, out = self.changed()
        self.assertEqual((code, runs), (0, ['cli', 'cli_extra']), out)
        self.assertRegex(out, r'runs app --test cli_extra, for the binary app-extra\n')

    def test_a_test_with_a_path_of_its_own_and_a_folder_test_run_by_name(self):
        (self.root / 'itest/main.rs').write_text('#[test]\nfn custom() { assert_eq!(app::other::two(), 2 + 0); }\n')
        (self.root / 'tests/multi/part.rs').write_text('pub fn p() { let _ = 1; }\n')
        code, runs, _, out = self.changed()
        self.assertEqual((code, runs), (0, ['custom', 'multi']), out)
        self.assertIn('test custom ... ok', out)
        self.assertNotIn('test api ... ok', out)

    def test_a_changed_manifest_runs_every_target(self):
        with (self.root / 'Cargo.toml').open('a') as manifest:
            manifest.write('\n# a comment\n')
        code, runs, skips, out = self.changed()
        self.assertEqual((code, skips), (0, []), out)
        self.assertEqual(len(runs), 11, out)

    def test_no_change_runs_nothing_and_passes(self):
        code, runs, skips, out = self.changed()
        self.assertEqual((code, runs, len(skips)), (0, [], 11), out)
        self.assertIn('so no test ran', out)

    def test_a_project_in_a_folder_of_its_repo_sees_the_committed_changes(self):
        outer = self.root.parent / (self.root.name + '-outer')
        shutil.copytree(self.root, outer / 'rust', ignore=shutil.ignore_patterns('.git', 'target'))
        self.addCleanup(shutil.rmtree, outer, True)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=outer, env=self.env, check=True, capture_output=True)
        (outer / 'rust/tests/support/mod.rs').write_text('pub fn ok() -> bool { 1 == 1 }\n')
        code, runs, _, out = self.changed(outer / 'rust')
        self.assertEqual((code, runs), (0, ['db']), out)

    def test_a_stale_lock_file_exits_with_the_runner_code(self):
        manifest = self.root / 'Cargo.toml'
        manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
        code, _, _, out = self.changed()
        self.assertEqual(code, 2, out)

    def test_a_build_that_fails_fails(self):
        (self.root / 'src/store.rs').write_text('mod disk;\npub fn get() -> u32 { nope }\n')
        self.assertEqual(self.changed()[0], 1)


@unittest.skipUnless(shutil.which('cargo'), 'needs cargo')
class IntegrationChangedWorkspace(unittest.TestCase):
    """integration-changed in a workspace with a virtual manifest."""

    def test_a_change_to_the_manifest_of_the_workspace_runs_every_target(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'a/src').mkdir(parents=True)
        (root / 'a/tests').mkdir()
        (root / 'Cargo.toml').write_text('[workspace]\nmembers = ["a"]\nresolver = "2"\n')
        (root / 'a/Cargo.toml').write_text('[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'a/src/lib.rs').write_text('pub fn one() -> u32 { 1 }\n')
        (root / 'a/tests/t.rs').write_text('#[test]\nfn t() { assert_eq!(a::one(), 1); }\n')
        (root / '.gitignore').write_text('/target/\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        with (root / 'Cargo.toml').open('a') as manifest:
            manifest.write('\n[profile.dev]\nopt-level = 0\n')
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'integration-changed'],
                              cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('runs a --test t, for Cargo.toml of the workspace', done.stdout)


class NextestFilters(unittest.TestCase):
    """The names of test(...) in .config/nextest.toml, against the tests that nextest saw."""

    def test_each_matcher_of_nextest(self):
        names = {'api::plan_cli_and_evidence', 'slow'}
        for matcher, want in (('plan_cli', True), ('~plan_cli', True), ('=slow', True), ('=plan_cli', False),
                              ('/^api::plan/', True), ('/^plan/', False), ('#api::*', True), ('#plan*', False),
                              ('nope', False), ('/(/', True)):
            self.assertEqual(steps.filter_matches(matcher, names), want, matcher)

    def test_the_names_of_the_lines_of_nextest(self):
        text = ('        SKIP [         ] (───) w::api slow\n        PASS [   0.010s] (1/2) w::api a::b\n'
                '     Summary [   0.013s] 1 test run: 1 passed, 1 skipped\n')
        self.assertEqual(set(steps.NEXTEST_LINE.findall(text)), {'slow', 'a::b'})
        self.assertEqual(steps.NEXTEST_LINE.findall('        PASS [   0.015s] (2/4) w::mimic my spaced test\n'),
                         ['my spaced test'])

    def test_the_matchers_of_a_filter(self):
        self.assertEqual(steps.test_matchers('test(=a) | not test( b ) & test(/a\\)|[)]/) | rdeps(x) | test(#c*)'),
                         ['=a', 'b', '/a\\)|[)]/', '#c*'])

    def test_the_filters_of_every_profile_and_script_are_read(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / '.config').mkdir()
        (root / '.config/nextest.toml').write_text(
            "[profile.ci]\ndefault-filter = 'not test(gone)'\n\n[[profile.ci.overrides]]\n"
            "filter = 'test(a) & package(my-test)'\nslow-timeout = '1m'\n")
        old = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old)
        self.assertEqual(steps.unmatched_filters({'a'}), ['test(gone)'])
        (root / 'lib.rs').write_text('#[test]\nfn gone() {}\n')
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        self.assertEqual(steps.unmatched_filters({'a'}), [], 'a unit test of the project counts')
        (root / '.config/nextest.toml').write_text(
            "[profile.ci]\ndefault-filter = 'test(=gon) | test(=t::gone) | test(on)'\n")
        self.assertEqual(steps.unmatched_filters(set()), ['test(=gon)'])
        (root / '.config/nextest.toml').write_text('not toml [')
        self.assertEqual(steps.unmatched_filters(set()), [])


@unittest.skipUnless(shutil.which('cargo-llvm-cov') and shutil.which('cargo-nextest'),
                     'needs cargo llvm-cov and nextest')
class NextestCargo(unittest.TestCase):
    """The real integration step with nextest: it writes the coverage, and leaves out the ignored tests."""

    def test_integration_with_nextest_writes_the_report(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'src').mkdir()
        (root / 'tests').mkdir()
        (root / 'Cargo.toml').write_text('[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'src/lib.rs').write_text('pub fn one() -> u32 {\n    1\n}\n')
        (root / 'tests/api.rs').write_text('#[test]\nfn a() { assert_eq!(w::one(), 1); }\n'
                                           '#[test]\n#[ignore]\nfn slow() { panic!("ran"); }\n')
        (root / 'tests/empty.rs').write_text('\n')
        (root / '.gitignore').write_text('/target/\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid', JNPROFILE_RUST_NEXTEST='1')
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'run', 'integration'],
                              cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        self.assertIn('llvm-cov nextest', done.stdout)
        self.assertIn('DA:2,1', (root / 'target/llvm-cov/integration.lcov').read_text())

    def project(self, tests):
        """A committed scratch project with a library and the test files TESTS, and its environment."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        (root / 'src').mkdir()
        (root / 'tests').mkdir()
        (root / 'Cargo.toml').write_text('[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'src/lib.rs').write_text('pub fn one() -> u32 {\n    1\n}\n')
        for name, text in tests.items():
            (root / 'tests' / name).write_text(text)
        (root / '.gitignore').write_text('/target/\n')
        env = {k: v for k, v in os.environ.items() if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid', JNPROFILE_RUST_NEXTEST='1')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        return root, env

    def integration(self, root, env, *args):
        return subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'run', 'integration', *args],
                              cwd=root, env=env, capture_output=True, text=True)

    def test_a_failed_test_fails_the_step(self):
        root, env = self.project({'api.rs': '#[test]\nfn a() { assert_eq!(w::one(), 2); }\n'})
        self.assertNotEqual(self.integration(root, env).returncode, 0)

    def test_the_test_group_example_of_the_docs_runs(self):
        """docs/tools.md names a test and a test binary; in a project that has both, its nextest.toml works."""
        text = (REPO / 'docs/tools.md').read_text()
        example = text.split('### integration with nextest', 1)[1].split('```toml\n', 1)[1].split('```', 1)[0]
        root, env = self.project({'conformance.rs': '#[test]\nfn c() { assert_eq!(w::one(), 1); }\n',
                                  'plan.rs': '#[test]\nfn plan_cli_and_evidence() { assert_eq!(w::one(), 1); }\n'})
        (root / '.config').mkdir()
        (root / '.config/nextest.toml').write_text(example)
        done = self.integration(root, env)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])

    def test_a_filter_name_that_matches_no_test_fails_the_step(self):
        root, env = self.project({'api.rs': '#[test]\nfn a() {}\n#[test]\n#[ignore]\nfn slow_one() {}\n'})
        (root / '.config').mkdir()
        (root / '.config/nextest.toml').write_text(
            "[test-groups]\nserial = { max-threads = 1 }\n\n[[profile.default.overrides]]\n"
            "filter = 'test(=a) | test(slow) | test(nope) | binary(api)'\ntest-group = 'serial'\n")
        done = self.integration(root, env)
        self.assertEqual(done.returncode, 1, done.stdout[-3000:])
        self.assertIn('the filter test(nope) in .config/nextest.toml matches no test', done.stderr)
        self.assertNotIn('test(=a)', done.stderr)
        self.assertNotIn('test(slow)', done.stderr)

    def test_a_users_status_level_stays_and_turns_the_check_off(self):
        root, env = self.project({'api.rs': '#[test]\nfn a() {}\n'})
        (root / '.config').mkdir()
        (root / '.config/nextest.toml').write_text("[[profile.default.overrides]]\nfilter = 'test(nope)'\n"
                                                    "slow-timeout = '1m'\n")
        self.assertEqual(self.integration(root, env).returncode, 1)
        done = self.integration(root, dict(env, NEXTEST_STATUS_LEVEL='fail'))
        self.assertEqual(done.returncode, 0, done.stdout[-3000:])
        self.assertNotIn('PASS [', done.stdout)

    def test_a_filter_expression_goes_before_the_double_dash(self):
        root, env = self.project({'api.rs': '#[test]\nfn a() {}\n#[test]\nfn b() { panic!("b ran"); }\n'})
        done = self.integration(root, env, '-E', 'test(a)')
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])


@unittest.skipUnless(shutil.which('cargo'), 'needs cargo')
class InstallBinCargo(unittest.TestCase):
    """The real install-bin: every binary of the package, one old copy kept, and the choice of binaries."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.project = self.root / 'p'
        (self.project / 'src/bin').mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ('RUSTC_WRAPPER', 'CARGO_TARGET_DIR', 'JLOG_RUN')}
        self.env['CARGO_INSTALL_ROOT'] = str(self.root / 'install')
        self.bin = self.root / 'install/bin'

    def version(self, version, broken=False, feature=False):
        (self.project / 'Cargo.toml').write_text(
            f'[package]\nname = "tool-pkg"\nversion = "{version}"\nedition = "2021"\n\n[features]\nfast = []\n')
        for name in ('gossil', 'gos'):
            fast = 'if cfg!(feature = "fast") { " fast" } else { "" }'
            body = 'nope' if broken else f'println!("{name} {version}{{}}", {fast});'
            (self.project / f'src/bin/{name}.rs').write_text(f'fn main() {{ {body} }}\n')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.project, env=self.env, check=True,
                       capture_output=True)

    def install(self, *names):
        return subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'install-bin', *names],
                              cwd=self.project, env=self.env, capture_output=True, text=True)

    def output(self, name):
        return subprocess.run([str(self.bin / name)], capture_output=True, text=True).stdout.strip()

    def files(self):
        return sorted(p.name for p in self.bin.iterdir())

    def test_each_install_keeps_only_the_last_old_binary(self):
        self.version('0.1.0')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.files(), ['gos', 'gossil'])
        self.version('0.2.0')
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('kept the old gossil as', done.stdout)
        self.assertEqual(self.files(), ['gos', 'gos-0.1.0', 'gossil', 'gossil-0.1.0'])
        self.assertEqual((self.output('gossil'), self.output('gossil-0.1.0')), ('gossil 0.2.0', 'gossil 0.1.0'))
        self.version('0.3.0')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.files(), ['gos', 'gos-0.2.0', 'gossil', 'gossil-0.2.0'])
        self.assertEqual(self.output('gossil-0.2.0'), 'gossil 0.2.0')

    def test_a_file_that_install_bin_did_not_make_stays(self):
        self.version('0.1.0')
        self.install()
        for name in ('gos-1.2.3-backup', 'gos-0.0.9', 'gos-tool'):
            (self.bin / name).write_text(name)
        self.version('0.2.0')
        self.install()
        self.version('0.3.0')
        self.install()
        self.assertEqual(self.files(), ['gos', 'gos-0.0.9', 'gos-0.2.0', 'gos-1.2.3-backup', 'gos-tool', 'gossil',
                                        'gossil-0.2.0'])

    def test_a_file_of_the_name_of_the_copy_that_it_did_not_make_stays(self):
        self.version('0.1.0')
        self.install()
        (self.bin / 'gos-0.1.0').write_text('mine')
        self.version('0.2.0')
        done = self.install()
        self.assertIn('is a file that it did not make', done.stdout)
        self.assertEqual((self.bin / 'gos-0.1.0').read_text(), 'mine')
        self.assertEqual(self.output('gossil-0.1.0'), 'gossil 0.1.0')

    def test_an_unknown_old_version_keeps_no_copy(self):
        self.version('0.1.0')
        self.install()
        (self.root / 'install/.crates2.json').unlink()
        self.version('0.2.0')
        done = self.install()
        self.assertIn('recorded no version', done.stdout)
        self.assertEqual(self.files(), ['gos', 'gossil'])

    def test_the_same_version_again_keeps_the_old_copy(self):
        self.version('0.1.0')
        self.install()
        self.version('0.2.0')
        self.install()
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('kept the old', done.stdout)
        self.assertEqual(self.output('gossil-0.1.0'), 'gossil 0.1.0')

    def test_a_failed_build_keeps_the_binary_and_the_older_copy(self):
        self.version('0.1.0')
        self.install()
        self.version('0.2.0')
        self.install()
        self.version('0.3.0', broken=True)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(self.files(), ['gos', 'gos-0.1.0', 'gossil', 'gossil-0.1.0'])
        self.assertEqual(self.output('gossil'), 'gossil 0.2.0')

    def test_options_after_a_double_dash_go_to_cargo_install(self):
        self.version('0.1.0')
        self.assertEqual(self.install('gos', '--', '--features', 'fast').returncode, 0)
        self.assertEqual(self.output('gos'), 'gos 0.1.0 fast')

    def test_an_install_root_of_the_cargo_config_does_not_split_the_folders(self):
        """install-bin names its folder with --root, so cargo installs where the copies are kept."""
        del self.env['CARGO_INSTALL_ROOT']
        self.env['CARGO_HOME'] = str(self.root / 'install')
        (self.project / '.cargo').mkdir()
        (self.project / '.cargo/config.toml').write_text(f'[install]\nroot = "{self.root / "elsewhere"}"\n')
        self.version('0.1.0')
        self.install()
        self.version('0.2.0')
        self.install()
        self.assertEqual(self.files(), ['gos', 'gos-0.1.0', 'gossil', 'gossil-0.1.0'])
        self.assertFalse((self.root / 'elsewhere').exists())

    def test_a_name_installs_only_that_binary_and_an_unknown_name_fails(self):
        self.version('0.1.0')
        self.assertEqual(self.install('gos').returncode, 0)
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ['gos'])
        done = self.install('nope')
        self.assertEqual(done.returncode, 1)
        self.assertIn('no binary named nope', done.stderr)

    def test_a_workspace_with_binaries_in_two_packages_needs_names(self):
        (self.project / 'Cargo.toml').write_text('[workspace]\nmembers = ["a", "b"]\nresolver = "2"\n')
        for name in ('a', 'b'):
            (self.project / name / 'src').mkdir(parents=True)
            (self.project / name / 'Cargo.toml').write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n')
            (self.project / name / 'src/main.rs').write_text('fn main() {}\n')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.project, env=self.env, check=True,
                       capture_output=True)
        done = self.install()
        self.assertEqual(done.returncode, 1)
        self.assertIn('name the binaries to install', done.stderr)
        self.assertEqual(self.install('a', 'b').returncode, 0)
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ['a', 'b'])


class WarmCoverageSteps(unittest.TestCase):
    """run_tests with the real cargo llvm-cov and the real wrapper of the evidence, in a workspace of two packages.

    The package c depends on b, and its integration test calls b. The step test builds no integration binary.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(os.path.realpath(temp.name))
        (root / 'Cargo.toml').write_text('[workspace]\nmembers = ["b", "c"]\nresolver = "2"\n')
        for name in ('b', 'c'):
            (root / name / 'src').mkdir(parents=True)
        (root / 'b/Cargo.toml').write_text('[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n')
        (root / 'b/src/lib.rs').write_text('pub fn h() -> u32 {\n    1\n}\n#[cfg(test)]\nmod t {\n    #[test]\n'
                                           '    fn x() {\n        assert_eq!(super::h(), 1);\n    }\n}\n')
        (root / 'c/Cargo.toml').write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                           '[dependencies]\nb = { path = "../b" }\n')
        (root / 'c/src/lib.rs').write_text('pub fn c() -> u32 { b::h() }\n#[cfg(test)]\n'
                                           'mod t { #[test] fn x() { assert_eq!(super::c(), 1); } }\n')
        (root / 'c/tests').mkdir()
        (root / 'c/tests/api.rs').write_text('#[test]\nfn api() { assert_eq!(b::h(), 1); }\n')
        (root / '.gitignore').write_text('/target/\n')
        self.env = env = {k: v for k, v in os.environ.items()
                          if k not in ('JLOG_RUN', 'RUSTC_WRAPPER', 'CARGO_TARGET_DIR', 'CARGO_BUILD_BUILD_DIR')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=root, env=env, check=True, capture_output=True)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'one']):
            subprocess.run(['git', *args], cwd=root, env=env, check=True, capture_output=True)
        old = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old)

    def step(self, name):
        """Run the step NAME, and return its code, the lines of b/src/lib.rs in its report, and its evidence."""
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = steps.run_tests(name, [])
        lines, inside = {}, False
        for line in steps.report(name).read_text().splitlines():
            if line.startswith('SF:'):
                inside = line.endswith('b/src/lib.rs')
            elif inside and line.startswith('DA:'):
                number, count = line[3:].split(',')[:2]
                lines[int(number)] = max(lines.get(int(number), 0), int(count))
        sources = json.loads((steps.REPORTS / f'{name}.modules.json').read_text())['sources']
        return code, lines, sources

    def test_an_old_binary_of_a_dependent_package_gives_no_count_to_a_new_line(self):
        """Before the fix, the old integration binary of c held h at line 1, and llvm-cov took its count."""
        self.assertEqual(self.step('test')[0], 0)
        self.assertEqual(self.step('integration')[0], 0)
        text = (self.root / 'b/src/lib.rs').read_text()
        (self.root / 'b/src/lib.rs').write_text('pub fn bad(x: u32) -> u32 { if x > 5 { x * 2 } else { x + 3 } }\n'
                                                 + text)
        code, lines, _ = self.step('test')
        self.assertEqual(code, 0)
        self.assertEqual(lines[1], 0)

    def commit(self):
        for args in (['add', '-A'], ['commit', '-q', '-m', 'more']):
            subprocess.run(['git', *args], cwd=self.root, env=self.env, check=True, capture_output=True)

    def lock(self):
        subprocess.run(['cargo', 'generate-lockfile', '--offline'], cwd=self.root, env=self.env, check=True,
                       capture_output=True)

    def break_b(self):
        """Run test and integration, then add an untested line at the top of b, and return line 1 of the step test."""
        self.commit()
        self.assertEqual(self.step('test')[0], 0)
        self.assertEqual(self.step('integration')[0], 0)
        text = (self.root / 'b/src/lib.rs').read_text()
        (self.root / 'b/src/lib.rs').write_text('pub fn bad(x: u32) -> u32 { if x > 5 { x * 2 } else { x + 3 } }\n'
                                                 + text)
        code, lines, _ = self.step('test')
        self.assertEqual(code, 0)
        return lines[1]

    def test_a_dependent_through_a_path_crate_outside_the_workspace_is_cleaned(self):
        """c depends on ext, which is not a member, and ext depends on b. Only the resolved graph shows it."""
        (self.root / 'Cargo.toml').write_text('[workspace]\nmembers = ["b", "c"]\nexclude = ["ext"]\n'
                                              'resolver = "2"\n')
        (self.root / 'ext/src').mkdir(parents=True)
        (self.root / 'ext/Cargo.toml').write_text('[package]\nname = "ext"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                                  '[dependencies]\nb = { path = "../b" }\n')
        (self.root / 'ext/src/lib.rs').write_text('pub fn e() -> u32 { b::h() }\n')
        (self.root / 'c/Cargo.toml').write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                                '[dependencies]\next = { path = "../ext" }\n')
        (self.root / 'c/src/lib.rs').write_text('pub fn c() -> u32 { ext::e() }\n#[cfg(test)]\n'
                                                'mod t { #[test] fn x() { assert_eq!(super::c(), 1); } }\n')
        (self.root / 'c/tests/api.rs').write_text('#[test]\nfn api() { assert_eq!(ext::e(), 1); }\n')
        self.lock()
        self.assertEqual(self.break_b(), 0)

    def test_a_dependent_through_an_optional_dependency_is_cleaned(self):
        """The tests run with every feature, so an optional dependency links b too."""
        (self.root / 'c/Cargo.toml').write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                                '[dependencies]\nb = { path = "../b", optional = true }\n')
        (self.root / 'c/src/lib.rs').write_text('#[cfg(feature = "b")]\npub fn c() -> u32 { b::h() }\n#[cfg(test)]\n'
                                                'mod t {\n    #[cfg(feature = "b")]\n    #[test]\n'
                                                '    fn x() { assert_eq!(super::c(), 1); }\n}\n')
        (self.root / 'c/tests/api.rs').write_text('#![cfg(feature = "b")]\n'
                                                  '#[test]\nfn api() { assert_eq!(b::h(), 1); }\n')
        self.lock()
        self.assertEqual(self.break_b(), 0)

    def test_a_dependent_through_a_patch_is_cleaned(self):
        """c asks for b from the registry, and [patch] gives it the member b."""
        text = (self.root / 'Cargo.toml').read_text()
        (self.root / 'Cargo.toml').write_text(text + '\n[patch.crates-io]\nb = { path = "b" }\n')
        (self.root / 'c/Cargo.toml').write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                                '[dependencies]\nb = "0.1"\n')
        self.lock()
        self.assertEqual(self.break_b(), 0)

    def test_a_file_that_another_crate_takes_in_through_path_cleans_the_workspace(self):
        """c compiles b/src/x.rs as a module of its own, so it does not depend on b for that code."""
        (self.root / 'b/src/x.rs').write_text('pub fn x() -> u32 {\n    7\n}\n')
        # c does not depend on b, so only the dep-info file of c shows that it compiles the file. d depends on c
        # and runs its copy of x, so an old binary of c with the same symbol could give x its counts.
        (self.root / 'Cargo.toml').write_text('[workspace]\nmembers = ["b", "c", "d"]\nresolver = "2"\n')
        (self.root / 'c/Cargo.toml').write_text('[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n')
        (self.root / 'c/src/lib.rs').write_text('#[path = "../../b/src/x.rs"]\npub mod x;\n')
        (self.root / 'd/src').mkdir(parents=True)
        (self.root / 'd/Cargo.toml').write_text('[package]\nname = "d"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                                '[dependencies]\nc = { path = "../c" }\n')
        (self.root / 'd/src/lib.rs').write_text('#[cfg(test)]\n'
                                                'mod t { #[test] fn x() { assert_eq!(c::x::x(), 7); } }\n')
        self.lock()
        (self.root / 'c/tests/api.rs').write_text('#[test]\nfn api() { assert_eq!(c::x::x(), 7); }\n')
        self.commit()
        self.assertEqual(self.step('test')[0], 0)
        self.assertEqual(self.step('integration')[0], 0)
        (self.root / 'b/src/x.rs').write_text('pub fn bad(x: u32) -> u32 { if x > 5 { x * 2 } else { x + 3 } }\n'
                                              'pub fn x() -> u32 {\n    7\n}\n')
        self.assertEqual(self.step('test')[0], 0)
        lines, inside = {}, False
        for line in steps.report('test').read_text().splitlines():
            if line.startswith('SF:'):
                inside = line.endswith('b/src/x.rs')
            elif inside and line.startswith('DA:'):
                number, count = line[3:].split(',')[:2]
                lines[int(number)] = max(lines.get(int(number), 0), int(count))
        self.assertEqual(lines.get(1, 0), 0)

    def test_a_changed_file_that_did_not_change_since_the_last_run_is_compiled_again(self):
        """The evidence needs rustc to run on the file in this run, so its package is cleaned on each run."""
        (self.root / 'b/src/lib.rs').write_text((self.root / 'b/src/lib.rs').read_text() + '// changed\n')
        for _ in range(2):
            code, _, sources = self.step('test')
            self.assertEqual(code, 0)
            self.assertTrue(any(path.endswith('b/src/lib.rs') for path in sources), sources)

    def test_with_a_build_folder_of_its_own_the_changed_file_is_still_compiled_again(self):
        """With build.build-dir, cargo clean -p would clean another folder, so each run cleans the whole workspace."""
        (self.root / '.cargo').mkdir()
        (self.root / '.cargo/config.toml').write_text('[build]\nbuild-dir = "bdir"\n')
        (self.root / '.gitignore').write_text('/target/\n/bdir/\n')
        (self.root / 'b/src/lib.rs').write_text((self.root / 'b/src/lib.rs').read_text() + '// changed\n')
        for _ in range(2):
            code, _, sources = self.step('test')
            self.assertEqual(code, 0)
            self.assertTrue(any(path.endswith('b/src/lib.rs') for path in sources), sources)


class Repository(unittest.TestCase):
    """The lists come from the parent commit, in a temporary git repository."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.git('init', '-q')
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        names = mock.patch.object(steps, 'target_names', side_effect=lambda: {
            os.path.realpath(path): path.stem for path in self.root.glob('**/tests/*.rs')})
        names.start()
        self.addCleanup(names.stop)

    def git(self, *args):
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *args], cwd=self.root,
                       check=True, capture_output=True)

    def files(self, *paths):
        for path in paths:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text('')

    def test_git_matches_the_patterns_as_glob_pathspecs(self):
        self.files('src/gen/deep/x.rs', 'src/a.rs', 'src/b.rs', 'src/c.rs', 'src/inner/lib.rs', 'build.rs',
                   'crates/x/build.rs', 'top.rs', 'with space.rs')
        match = steps.matcher(['src/gen', 'src/[ab].rs', '**/build.rs', '/top.rs', 'with space.rs'])
        for path in ('src/gen/deep/x.rs', 'src/a.rs', 'src/b.rs', 'build.rs', 'crates/x/build.rs', 'top.rs',
                     'with space.rs'):
            self.assertTrue(match(path), path)
        self.assertFalse(match('src/c.rs') or match('src/inner/lib.rs'))
        self.assertFalse(steps.matcher(['src/*.rs'])('src/inner/lib.rs'))
        self.assertFalse(steps.matcher(['src/**.rs'])('src/inner/lib.rs'))
        self.assertFalse(steps.matcher([])('src/a.rs'))

    def test_in_a_subfolder_the_lists_and_the_paths_are_those_of_the_project(self):
        (self.root / 'rs/.agents').mkdir(parents=True)
        (self.root / 'rs/.agents/generated.txt').write_text('src/gen.rs\n')
        self.files('rs/src/gen.rs', 'rs/src/lib.rs')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        os.chdir(self.root / 'rs')
        self.assertEqual(steps.parent_list(steps.GENERATED), ['src/gen.rs'])
        self.assertTrue(steps.matcher(['src/gen.rs'])('src/gen.rs'))
        self.assertEqual(steps.top(), self.root / 'rs')

    def test_a_changed_file_missing_from_the_report_is_named_unless_excluded_or_a_test(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('src/lib.rs')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        self.files('src/orphan.rs', 'examples/demo.rs', 'tests/api.rs', 'benches/b.rs', 'src/tests/mod.rs',
                   'src/skip.rs', 'notes.md')
        for path in ('src/orphan.rs', 'examples/demo.rs', 'src/tests/mod.rs'):
            Path(path).write_text('pub fn runtime() {}\n')
        (self.root / 'src/lib.rs').write_text('// changed\n')
        changed = steps.changed_code()
        self.assertEqual(changed, ['benches/b.rs', 'examples/demo.rs', 'src/lib.rs', 'src/orphan.rs', 'src/skip.rs',
                                   'src/tests/mod.rs', 'tests/api.rs'])
        self.assertEqual(steps.unreported(changed, {'src/lib.rs'}, lambda path: path == 'src/skip.rs'),
                         ['examples/demo.rs', 'src/orphan.rs', 'src/tests/mod.rs'])

    def test_missing_declarations_are_classified_without_waiving_runtime_files(self):
        sources = {
            'src/plain.rs': '/// A type.\npub struct A { pub text: String }\npub enum B { One, Two }',
            'src/modules.rs': 'pub mod child; pub use child::Thing; mod nested { pub struct Item; }',
            'src/args.rs': '\n'.join([
                'use clap::{Args, Subcommand, ValueEnum};',
                '#[derive(Args, Clone, Debug, Default)] pub struct Options {',
                '#[arg(long, value_parser=crate::values::parse, num_args=1..=3)] pub name: String,',
                '#[arg(conflicts_with_all=["one", "two"])] pub other: bool, }',
                '#[derive(Subcommand)] pub enum Commands { #[command(alias="ls")] List(Options) }',
                '#[derive(ValueEnum, Clone)] pub enum Kind { #[value(name="a")] A }']),
            'src/comments.rs': '/* fn bad() { /* nested */ } */ pub struct A; // impl X {}',
            'src/empty.rs': '// empty declarations\n',
        }
        self.files(*sources)
        for path, source in sources.items():
            Path(path).write_text(source)
        with mock.patch.object(steps, 'compiled_modules', return_value=set(sources)):
            self.assertEqual(steps.unreported(list(sources), set(), lambda p: False), [])

    def test_missing_runtime_or_unknown_syntax_stays_reportable(self):
        runtime = [
            'fn hidden() {}', 'impl A { fn hidden() {} }',
            'trait T { fn hidden() {} }', 'const A: u8 = 1;', 'static A: u8 = 1;',
            'enum E { A = call() }', 'struct B { a: [u8; { call() }] }',
            'struct B<const N: usize = { call() }>;', 'mod x { fn hidden() {} }',
            'include!("generated.rs");', 'macro_rules! make { () => {} }', 'make!();',
            '#[custom] struct B;', '#[derive(Custom)] struct B;',
            '#[cfg_attr(feature="x", custom)] struct B;',
            'use other::Args; #[derive(Args)] struct B;',
            'use clap::Args; #[derive(Args)] struct B { #[arg(value_parser=|x| x)] x: String }',
            'use clap::Args; #[derive(Args)] struct B { #[arg(default_value_t=call())] x: u8 }',
            'use clap::Args; #[derive(Args)] struct B { #[arg(long={ call() })] x: u8 }',
            'pub struct Broken {',
            '/// fn not_runtime() {}\nstruct A; fn actual() {}',
            '#[doc=r###"} fn fake() {"###] struct A; fn actual() {}',
        ]
        self.files('src/probe.rs')
        for source in runtime:
            with self.subTest(source=source):
                Path('src/probe.rs').write_text(source)
                self.assertEqual(steps.unreported(['src/probe.rs'], set(), lambda p: False), ['src/probe.rs'])

    def test_ignored_counts_an_ignored_untracked_file_and_a_path_outside_but_not_a_tracked_file(self):
        (self.root / '.gitignore').write_text('/target/\n')
        self.files('target/out/gen.rs', 'src/lib.rs')
        self.assertTrue(steps.ignored('target/out/gen.rs'))
        self.assertTrue(steps.ignored('/elsewhere/gen.rs') and steps.ignored('../gen.rs'))
        self.assertFalse(steps.ignored('src/lib.rs'))
        self.git('add', '-f', 'target/out/gen.rs')
        self.assertFalse(steps.ignored('target/out/gen.rs'))

    def test_package_of_stops_at_the_top_of_the_project(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "outer"\n')
        self.files('inner/src/lib.rs')
        os.chdir(self.root / 'inner')
        with self.assertRaises(SystemExit):
            steps.package_of('src/lib.rs')
        self.assertFalse(steps.test_file('src/lib.rs'))

    def write_reports(self, test, integration):
        """Write the reports of test and integration, each a list of (path, line, count)."""
        for step, rows in (('test', test), ('integration', integration)):
            text = ''.join(f'SF:{self.root / path}\nDA:{line},{count}\nend_of_record\n' for path, line, count in rows)
            steps.report(step).parent.mkdir(parents=True, exist_ok=True)
            steps.report(step).write_text(text)

    @contextlib.contextmanager
    def diff_cover(self):
        """Stand in for diff-cover, which passes, and give the list of its commands. git still runs."""
        calls, real = [], subprocess.run

        def run(command, *args, **kwargs):
            if 'diff-cover' in command:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0)
            return real(command, *args, **kwargs)
        with mock.patch.object(steps.subprocess, 'run', side_effect=run):
            yield calls

    def first_commit(self):
        """Run coverage on the first commit, as the script, and return the finished process."""
        return subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'coverage'],
                              cwd=self.root, capture_output=True, text=True)

    def test_the_first_commit_names_itself_and_takes_a_line_that_either_report_runs(self):
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 0)], [('src/lib.rs', 2, 3)])
        done = self.first_commit()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(),
                         ['coverage-check: the first commit, so every line of code must run in the tests'])

    def test_the_first_commit_names_each_line_that_no_report_runs(self):
        self.write_reports([('src/lib.rs', 2, 0), ('src/a.rs', 7, 0)], [('src/lib.rs', 2, 0)])
        done = self.first_commit()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[1:],
                         ['src/a.rs:7: no test runs this line', 'src/lib.rs:2: no test runs this line'])
        self.assertIn('write tests', done.stderr)

    def test_on_the_first_commit_a_report_of_integration_that_does_not_count_is_not_read(self):
        (self.root / '.agents').mkdir()
        (self.root / '.agents/stack.json').write_text(json.dumps({'steps': ['test', 'coverage-check']}))
        self.write_reports([('src/lib.rs', 2, 0)], [('src/lib.rs', 2, 1)])
        self.assertEqual(self.first_commit().returncode, 1)
        steps.report('integration').unlink()
        self.assertEqual(self.first_commit().returncode, 1)

    def test_the_report_of_integration_counts_only_when_integration_is_a_step_of_just_check(self):
        self.assertTrue(steps.counts('integration'))
        (self.root / '.agents').mkdir()
        stack = self.root / '.agents/stack.json'
        for data, want in (({'steps': ['test', 'integration']}, True),
                           ({'steps': ['test', 'coverage-check']}, False),
                           ({'steps': ['test', 'integration'], 'provided_steps': ['test', 'integration']}, True),
                           ({'steps': ['test', 'integration'], 'provided_steps': ['test']}, False),
                           ({'profiles': ['rust']}, True), (['integration'], True)):
            stack.write_text(json.dumps(data))
            self.assertEqual(steps.counts('integration'), want, data)
            self.assertTrue(steps.counts('test'))
        stack.write_text('{')
        self.assertTrue(steps.counts('integration'))

    def test_a_report_that_does_not_count_is_fresh_and_names_why(self):
        (self.root / '.agents').mkdir()
        (self.root / '.agents/stack.json').write_text(json.dumps({'steps': ['test']}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(steps.fresh('integration'))
        self.assertIn('integration is not in the steps of just check', out.getvalue())

    def test_a_missing_report_of_integration_says_write_tests_and_one_that_does_not_count_is_not_needed(self):
        self.commit_one()
        self.write_reports([], [])
        steps.report('integration').unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.coverage(), 1)
        self.assertIn('integration.lcov, so write tests', err.getvalue())
        (self.root / '.agents').mkdir(exist_ok=True)
        (self.root / '.agents/stack.json').write_text(json.dumps({'steps': ['test']}))
        with self.diff_cover() as calls, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.coverage(), 0)
        diff_cover, = calls
        self.assertEqual(diff_cover[diff_cover.index('diff-cover') + 1], str(steps.WORK / 'lcov.info'))
        self.assertNotIn('integration', ' '.join(diff_cover))

    def gate(self):
        """Run coverage with the real diff-cover, as the script, and return its exit code and its output."""
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'coverage'],
                              cwd=self.root, capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def test_a_line_that_one_report_misses_and_the_other_does_not_measure_still_fails(self):
        """The code of cfg(not(test)) is in the build of integration only, and that of cfg(test) in test only."""
        self.commit_one()
        (self.root / 'src/lib.rs').write_text('a\nb\nc\n')
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 1)], [('src/lib.rs', 3, 0)])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('Missing lines 3', out)
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 0)], [('src/lib.rs', 1, 1)])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('Missing lines 2', out)
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 0)], [('src/lib.rs', 2, 1), ('src/lib.rs', 3, 1)])
        code, out = self.gate()
        self.assertEqual(code, 0, out)

    def untracked_links(self):
        """Add the untracked paths that diff-cover cannot open: links to folders, a broken link, a nested repo."""
        (self.root / '.agents/skills/rust-profile').mkdir(parents=True)
        (self.root / '.agents/skills/rust-profile/SKILL.md').write_text('a\n')
        (self.root / '.claude/skills').mkdir(parents=True)
        (self.root / '.claude/skills/rust-profile').symlink_to('../../.agents/skills/rust-profile')
        (self.root / 'src/[a]*?').symlink_to('../.agents')
        (self.root / 'gone').symlink_to('nowhere')
        (self.root / 'nested').mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.root / 'nested', check=True)
        (self.root / 'nested/x').write_text('x\n')
        subprocess.run(['git', 'add', 'x'], cwd=self.root / 'nested', check=True)

    def test_an_untracked_link_to_a_folder_does_not_stop_diff_cover(self):
        """The jnterfaces installer links .claude/skills/rust-profile to a folder, before anything is staged."""
        self.commit_one()
        self.untracked_links()
        (self.root / 'src/lib.rs').write_text('a\nb\n')
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 1)], [])
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertNotIn('Error', out)
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 0)], [])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('Missing lines 2', out)

    def test_an_untracked_file_still_counts_next_to_an_untracked_link(self):
        self.commit_one()
        self.untracked_links()
        (self.root / 'src/new.rs').write_text('a\nb\n')
        self.write_reports([('src/lib.rs', 1, 1), ('src/new.rs', 1, 1), ('src/new.rs', 2, 0)], [])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('src/new.rs', out)
        self.assertIn('Missing lines 2', out)

    def test_only_the_untracked_paths_that_are_not_files_are_skipped(self):
        self.commit_one()
        self.untracked_links()
        (self.root / 'src/new.rs').write_text('a\n')
        skipped = steps.unreadable_untracked()
        self.assertEqual(skipped, ['.claude/skills/rust-profile', 'gone', 'nested/', 'src/[a]*?'])
        root = os.path.abspath('.')
        self.assertEqual(steps.exclude_patterns(skipped),
                         [f'{root}/.claude/skills/rust-profile', f'{root}/gone', f'{root}/nested',
                          f'{root}/src/[[]a][*][?]'])
        shutil.rmtree(self.root / 'nested')
        self.git('add', '-A')
        self.assertEqual(steps.unreadable_untracked(), [])

    def test_a_link_to_a_folder_with_a_carriage_return_does_not_stop_diff_cover(self):
        """text=True turned a carriage return into a newline, so the pattern named another path."""
        self.commit_one()
        (self.root / 'd').mkdir()
        (self.root / 'src/x\r').symlink_to('../d')
        (self.root / 'src/lib.rs').write_text('a\n')
        self.write_reports([('src/lib.rs', 1, 1)], [])
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertNotIn('Error', out)

    def test_a_link_with_a_backslash_stops_the_check_and_does_not_hide_a_real_file(self):
        """diff-cover reads src\\lib.rs as src/lib.rs, so a pattern that skips the link skips the real file too."""
        self.commit_one()
        (self.root / 'd').mkdir()
        (self.root / 'src/lib.rs').write_text('a\nb\n')
        self.write_reports([('src/lib.rs', 1, 1), ('src/lib.rs', 2, 0)], [])
        for name in ('src\\lib.rs', 'x\\..\\zz'):
            link = self.root / name
            link.symlink_to('d' if name.startswith('src') else 'nowhere')
            code, out = self.gate()
            self.assertEqual(code, 1, out)
            self.assertIn(f'{name}: an untracked path that is not a file, and its name holds a \\', out)
            self.assertIn('Rename them', out)
            self.assertNotIn('Error', out)
            link.unlink()
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('Missing lines 2', out)

    def test_an_untracked_name_that_is_not_utf_8_is_read(self):
        """Linux allows such a name. git gives its bytes, and the pattern keeps them as the file system does."""
        real = subprocess.run

        def run(command, *args, **kwargs):
            if command[:2] == ['git', 'ls-files']:
                self.assertNotIn('text', kwargs)
                return subprocess.CompletedProcess(command, 0, b'notes/caf\xe9\0gone\0', b'')
            return real(command, *args, **kwargs)
        with mock.patch.object(steps.subprocess, 'run', side_effect=run):
            patterns = steps.unreadable_untracked()
        self.assertEqual(patterns, ['gone', os.fsdecode(b'notes/caf\xe9')])
        root = os.path.abspath('.')
        self.assertEqual([os.fsencode(p) for p in steps.exclude_patterns(patterns)],
                         [os.fsencode(root) + b'/gone', os.fsencode(root) + b'/notes/caf\xe9'])

    def test_with_no_untracked_link_diff_cover_gets_no_exclude(self):
        self.commit_one()
        self.write_reports([('src/lib.rs', 1, 1)], [])
        with self.diff_cover() as calls, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.coverage(), 0)
        diff_cover, = calls
        self.assertNotIn('--exclude', diff_cover)

    def test_the_merged_report_leaves_out_the_generated_files_of_the_parent(self):
        self.commit_one()
        (self.root / '.agents').mkdir(exist_ok=True)
        (self.root / '.agents/generated.txt').write_text('src/gen.rs\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'two')
        (self.root / 'src/gen.rs').write_text('a\n')
        (self.root / 'src/lib.rs').write_text('a\n')
        self.write_reports([('src/lib.rs', 1, 1), ('src/gen.rs', 1, 0)], [('src/lib.rs', 1, 0)])
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        kept = (steps.WORK / 'lcov.info').read_text()
        self.assertNotIn('gen.rs', kept)
        self.assertIn('DA:1,1', kept)

    def test_with_no_parent_the_lists_are_empty(self):
        (self.root / '.agents').mkdir()
        (self.root / '.agents/generated.txt').write_text('src/**\n')
        self.assertFalse(steps.has_parent())
        self.assertEqual(steps.parent_list(steps.GENERATED), [])

    def test_the_list_of_the_parent_counts_and_the_worktree_does_not(self):
        (self.root / '.agents').mkdir()
        (self.root / '.agents/generated.txt').write_text('# made by build.rs\n\nsrc/gen.rs\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        (self.root / '.agents/generated.txt').write_text('src/gen.rs\nsrc/**\n')
        self.assertEqual(steps.parent_list(steps.GENERATED), ['src/gen.rs'])
        self.assertEqual(steps.parent_list(steps.COVERAGE_EXCLUDE), [])

    def test_a_missing_report_says_write_tests(self):
        (self.root / 'a').write_text('a\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(steps.coverage(), 1)
        self.assertIn('write tests', err.getvalue())

    def test_test_paths_picks_the_target_of_each_test_file_and_skips_code_files(self):
        (self.root / 'Cargo.toml').write_text('[workspace]\nmembers = ["crates/*"]\n')
        for name in ('one', 'two'):
            (self.root / f'crates/{name}/tests/common').mkdir(parents=True)
            (self.root / f'crates/{name}/src').mkdir()
            (self.root / f'crates/{name}/Cargo.toml').write_text(f'[package]\nname = "{name}"\n')
            (self.root / f'crates/{name}/src/lib.rs').write_text('')
            (self.root / f'crates/{name}/tests/api.rs').write_text('')
            (self.root / f'crates/{name}/tests/more.rs').write_text('')
            (self.root / f'crates/{name}/tests/common/mod.rs').write_text('')
        self.assertEqual(steps.test_commands(['crates/one/tests/api.rs', 'crates/one/tests/more.rs',
                                              'crates/one/tests/api.rs', 'crates/two/src/lib.rs']),
                         ([[*CARGO_TEST, 'one', '--test', 'api', '--test', 'more']], ['crates/two/src/lib.rs']))
        self.assertEqual(steps.test_commands(['crates/one/tests/api.rs', 'crates/two/tests/common/mod.rs']),
                         ([[*CARGO_TEST, 'one', '--test', 'api'], [*CARGO_TEST, 'two', '--test', '*']], []))
        self.assertEqual(steps.test_commands(['crates/one/tests/common/mod.rs', 'crates/one/tests/api.rs']),
                         ([[*CARGO_TEST, 'one', '--test', '*']], []))
        with self.assertRaises(SystemExit):
            steps.test_commands(['crates/one/tests/none.rs'])
        (self.root / 'loose.rs').write_text('')
        with self.assertRaises(SystemExit):
            steps.test_commands(['loose.rs'])

    def run_tests(self, run, args=(), change=None, step='test'):
        """Stand in for test: begin, a test run that writes the report and may change a file, then stamp."""
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'JLOG_RUN': run} if run else {}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            if not run:
                os.environ.pop('JLOG_RUN', None)
            steps.begin(step, list(args))
            steps.report(step).parent.mkdir(parents=True, exist_ok=True)
            steps.report(step).write_text('SF:x\nend_of_record\n')
            if change:
                change()
            steps.stamp(step)
        return out.getvalue()

    def is_fresh(self, run, step='test'):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'JLOG_RUN': run} if run else {}), contextlib.redirect_stdout(out):
            if not run:
                os.environ.pop('JLOG_RUN', None)
            return steps.fresh(step)

    def commit_one(self):
        (self.root / '.gitignore').write_text('/target/\n')
        self.files('src/lib.rs')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')

    def test_the_stamp_ties_the_report_to_the_run_and_the_tree(self):
        self.commit_one()
        self.run_tests('run-a')
        self.assertTrue(self.is_fresh('run-a'))
        self.assertFalse(self.is_fresh('run-b'))
        self.assertFalse(self.is_fresh(None))
        (self.root / 'src/lib.rs').write_text('// changed\n')
        self.assertFalse(self.is_fresh('run-a'))
        self.run_tests('run-a')
        self.assertTrue(self.is_fresh('run-a'))
        self.run_tests('run-a', args=['parse'])
        self.assertFalse(steps.saved_run('test').exists())
        self.assertFalse(self.is_fresh('run-a'))
        self.run_tests(None)
        self.assertFalse(steps.saved_run('test').exists())
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=self.root, capture_output=True,
                                text=True).stdout
        self.assertEqual(staged, '')

    def test_the_run_of_one_step_leaves_that_of_the_other_alone(self):
        self.commit_one()
        self.run_tests('run-a')
        self.run_tests('run-a', step='integration')
        self.assertTrue(self.is_fresh('run-a', 'integration'))
        self.run_tests('run-a', args=['parse'], step='integration')
        self.assertFalse(self.is_fresh('run-a', 'integration'))
        self.assertTrue(self.is_fresh('run-a'))
        out = self.run_tests('run-a', step='integration',
                             change=lambda: (self.root / 'src/lib.rs').write_text('pub fn x() {}\n'))
        self.assertIn('integration: a file changed during the run', out)

    def test_a_file_changed_while_the_tests_run_drops_the_run(self):
        self.commit_one()
        out = self.run_tests('run-a', change=lambda: (self.root / 'src/lib.rs').write_text('pub fn x() {}\n'))
        self.assertIn('a file changed during the run', out)
        self.assertFalse(steps.saved_run('test').exists())
        self.assertFalse(self.is_fresh('run-a'))

    def test_a_missing_report_is_not_fresh(self):
        self.commit_one()
        self.run_tests('run-a')
        steps.report('test').unlink()
        self.assertFalse(self.is_fresh('run-a'))

    def test_the_folder_target_does_not_count_in_the_tree_even_when_git_sees_it(self):
        self.files('src/lib.rs')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        tree = steps.worktree_tree()
        self.files('target/llvm-cov/test.lcov')
        self.assertEqual(steps.worktree_tree(), tree)
        self.run_tests('run-a')
        self.assertTrue(self.is_fresh('run-a'))

    def test_a_file_that_git_cannot_index_gives_no_run_and_no_crash(self):
        self.commit_one()
        subprocess.run(['git', 'init', '-q', 'nested'], cwd=self.root, check=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(steps.worktree_tree())
        self.assertIn('cannot compute the worktree tree', err.getvalue())
        self.run_tests('run-a')
        self.assertFalse(steps.saved_run('test').exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.is_fresh('run-a'))

    def test_trees_at_the_same_time_do_not_share_an_index(self):
        self.commit_one()
        want = steps.worktree_tree()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            got = list(pool.map(lambda _: steps.worktree_tree(), range(12)))
        self.assertEqual(got, [want] * 12)

    def test_test_paths_says_when_no_test_ran_and_quotes_the_star(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('src/lib.rs', 'benches/b.rs', 'tests/common/mod.rs')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(steps.test_paths(['src/lib.rs', 'benches/b.rs']), 5)
        self.assertIn('skips benches/b.rs, which is not in the folder tests of a package', out.getvalue())
        self.assertIn('no file in the folder tests of a package, so no test ran', out.getvalue())
        with mock.patch.object(steps, 'run_streamed', return_value=(0, PASSED)), \
                contextlib.redirect_stdout(out):
            steps.test_paths(['tests/common/mod.rs'])
        self.assertIn("--package app --test '*'", out.getvalue())

    def test_test_paths_gives_1_for_a_failed_test_or_build_and_2_when_cargo_itself_fails(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/api.rs')
        fail = (PASSED.replace('ok. 1 passed; 0', 'FAILED. 0 passed; 1')
                + 'error: test failed, to rerun pass `--test api`\n')
        cases = (
            (0, PASSED, 0),
            (101, fail, 1),
            (101, 'error: could not compile `app` (test "api") due to 1 previous error\n', 1),
            (101, 'error: failed to run custom build command for `app v0.1.0`\n', 1),
            (101, 'error: the lock file Cargo.lock needs to be updated but --locked was passed\n', 2),
            (101, 'error: no test target named `api` in `app` package\n', 2),
            # cargo passes on the code of a test binary that exits by itself, so the output decides.
            (5, fail, 1), (1, fail, 1), (-9, fail, 1), (256, fail, 1),
            (5, '', 2), (1, '', 2), (-9, '', 2), (256, '', 2),
            # A binary that announced its tests and stopped with no result, as on exit(0) in a test: the run ended.
            (0, '     Running tests/api.rs (target/debug/deps/api-1)\nrunning 2 tests\n', 2),
            # libtest named a failed test before the binary stopped, so the failure counts.
            (0, '     Running tests/api.rs (target/debug/deps/api-1)\nrunning 2 tests\ntest a_fails ... FAILED\n', 1),
            # A #[should_panic] test that did not panic, then a stop: the failure counts.
            (0, '     Running tests/api.rs (target/debug/deps/api-1)\nrunning 2 tests\n'
                'test a_no_panic - should panic ... FAILED\n', 1),
            # exit(1) in a test: cargo says the test failed, but the binary gave no result, so the run ended.
            (101, '     Running tests/api.rs (target/debug/deps/api-1)\nrunning 2 tests\ntest a ... ok\n'
                  'error: test failed, to rerun pass `--test api`\n', 2),
            # A binary with harness = false announces no tests, so its exit code of 1 is a failed test.
            (101, '     Running tests/plain.rs (target/debug/deps/plain-1)\n'
                  'error: test failed, to rerun pass `--test plain`\n', 1),
        )
        for got, out, want in cases:
            with self.subTest(got=got, out=out), \
                    mock.patch.object(steps, 'run_streamed', return_value=(got, out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(steps.test_paths(['tests/api.rs']), want)

    def test_test_paths_runs_every_package_and_a_failed_test_wins_in_any_order(self):
        stopped = (0, '     Running tests/q.rs (target/debug/deps/q-1)\nrunning 1 test\n')
        fail = (101, '     Running tests/f.rs (target/debug/deps/f-1)\nrunning 1 test\ntest f ... FAILED\n\n'
                     'test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n'
                     'error: test failed, to rerun pass `--test f`\n')
        broken = (101, 'error: the lock file Cargo.lock needs to be updated but --locked was passed\n')
        two = ([['cargo', 'test', '-p', 'a'], ['cargo', 'test', '-p', 'b']], [])
        for outs, want in (([stopped, fail], 1), ([fail, stopped], 1), ([stopped, (0, PASSED)], 2),
                           ([(0, PASSED), broken], 2), ([broken, fail], 1)):
            with self.subTest(outs=outs), mock.patch.object(steps, 'test_commands', return_value=two), \
                    mock.patch.object(steps, 'run_streamed', side_effect=outs) as ran, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(steps.test_paths(['x']), want)
                self.assertEqual(ran.call_count, 2)

    def test_test_paths_with_no_path_runs_no_test_and_gives_5(self):
        with mock.patch.object(steps, 'run_streamed') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.main(['test-paths']), 5)
        run.assert_not_called()

    def test_test_paths_runs_a_test_file_by_the_name_of_its_target_and_skips_a_file_that_is_no_target(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/bar.rs', 'tests/loose.rs')
        names = {os.path.realpath('tests/bar.rs'): 'foo'}
        out = io.StringIO()
        with mock.patch.object(steps, 'target_names', return_value=names), contextlib.redirect_stdout(out):
            commands, skipped = steps.test_commands(['tests/bar.rs', 'tests/loose.rs'])
        self.assertEqual(commands,
                         [['cargo', 'test', '--locked', '--all-features', '--package', 'app', '--test', 'foo']])
        self.assertEqual(skipped, [])
        self.assertIn('skips tests/loose.rs, which is no test target of its package', out.getvalue())
        with mock.patch.object(steps, 'target_names', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.test_paths(['tests/loose.rs']), 5)

    def test_test_paths_ignores_running_lines_that_a_test_leaks(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/api.rs')
        head, result = PASSED.split('running 1 test\n')
        for leak in ('     Running `target/inner/debug/leak6`\n', '     Running x\n', 'Running tests/a.rs (x)\n'):
            out = head + 'running 2 tests\n' + leak + result.replace('1 passed', '2 passed')
            with self.subTest(leak=leak), mock.patch.object(steps, 'run_streamed', return_value=(0, out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(steps.test_paths(['tests/api.rs']), 0)
        self.assertRegex('     Running unittests src/lib.rs (target/debug/deps/app-1)', steps.RUNNING)
        self.assertRegex('     Running tests\\api.rs (target\\debug\\deps\\api-1.exe)', steps.RUNNING)

    def test_test_paths_reads_the_binaries_of_the_build_dir_layout_of_newer_cargo(self):
        # The cargo of the ubuntu-24.04 image of 2026-11-16 puts each test binary in target/debug/build/<package>/
        # <hash>/out, not in target/debug/deps. Its Running line still counts, so the test that ran is seen.
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/api.rs')
        out = ('   Compiling app v0.1.0 (/tmp/app)\n'
               '     Running tests/api.rs (target/debug/build/app/b71f777615de6373/out/api-b71f777615de6373)\n\n'
               'running 1 test\ntest add_works_from_outside ... ok\n\n'
               'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n\n')
        with mock.patch.object(steps, 'run_streamed', return_value=(0, out)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.test_paths(['tests/api.rs']), 0)
        self.assertRegex('     Running unittests src/lib.rs (target/debug/build/app/1f/out/app-1f)', steps.RUNNING)
        self.assertRegex('     Running tests\\api.rs (target\\debug\\build\\app\\1f\\out\\api-1f.exe)',
                         steps.RUNNING)
        # A cargo run that a test leaks still does not count.
        self.assertNotRegex('     Running `target/debug/build/app/1f/out/leak`', steps.RUNNING)
        # A folder that only ends in out or deps is no binary folder.
        self.assertNotRegex('     Running tests/api.rs (target/layout/api-1)', steps.RUNNING)
        self.assertNotRegex('     Running tests/api.rs (target/mydeps/api-1)', steps.RUNNING)

    def test_test_paths_reads_a_binary_whose_path_holds_a_space(self):
        # A build-dir outside the project, such as one under the home folder /Users/First Last, gives a path with a
        # space in the Running line.
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/api.rs')
        result = ('running 1 test\ntest t ... ok\n\n'
                  'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n')
        for path in ('../bd build/debug/deps/api-1', '/Users/First Last/.cargo/build/ab/debug/build/app/1/out/api-1',
                     'C:\\Users\\First Last\\target\\debug\\deps\\api-1.exe'):
            out = f'     Running tests/api.rs ({path})\n\n' + result
            with self.subTest(path=path), mock.patch.object(steps, 'run_streamed', return_value=(0, out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertRegex(out, steps.RUNNING)
                self.assertEqual(steps.test_paths(['tests/api.rs']), 0)

    def test_test_paths_gives_2_for_a_path_that_it_cannot_run(self):
        (self.root / 'Cargo.toml').write_text('[workspace]\n')
        self.files('tests/api.rs')
        for path in ('tests/nope.rs', 'tests/api.rs'):
            with self.subTest(path=path), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(steps.test_paths([path]), 2)
            self.assertIn('test-paths:', err.getvalue())

    def test_test_paths_gives_no_test_when_cargo_runs_zero_tests_or_finds_no_target(self):
        (self.root / 'Cargo.toml').write_text('[package]\nname = "app"\n')
        self.files('tests/empty.rs', 'tests/common/mod.rs')
        zero = 'test result: ok. 0 passed; 0 failed; 2 ignored; 0 measured; 0 filtered out\n'
        missing = 'error: no test target matches pattern `*`\n'
        running = '     Running tests/empty.rs (target/debug/deps/empty-1)\n'
        failed = PASSED.replace('ok. 1 passed; 0', 'FAILED. 0 passed; 1')
        cases = (
            ((0, running + zero), ['tests/empty.rs'], 5),
            ((101, missing), ['tests/common/mod.rs'], 5),
            ((101, running + failed + 'error: test failed, to rerun pass `--test x`\n' + missing),
             ['tests/common/mod.rs'], 1),
            ((0, zero + PASSED), ['tests/empty.rs'], 0),
            # A target with harness = false prints no test result, but it ran.
            ((0, running), ['tests/empty.rs'], 0),
            # A compile error that quotes the text, or a warning of a build script, is not cargo's own error line.
            ((101, '   |     let x: u8 = "' + missing.strip() + '";\nerror: could not compile `app`\n'),
             ['tests/common/mod.rs'], 1),
            ((101, 'warning: app@0.1.0: ' + missing + 'error: could not compile `app`\n'), ['tests/common/mod.rs'], 1),
            ((101, missing + 'error: could not compile `app`\n'), ['tests/common/mod.rs'], 1),
        )
        for done, paths, want in cases:
            out = io.StringIO()
            with self.subTest(paths=paths, want=want), \
                    mock.patch.object(steps, 'run_streamed', return_value=done), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(steps.test_paths(paths), want)
                if want == 5:
                    self.assertIn('so no test ran', out.getvalue())

    def test_run_streamed_keeps_the_order_and_survives_bytes_that_are_not_utf_8(self):
        script = 'import sys; print("one", flush=True); sys.stderr.write("two\\n"); sys.stderr.flush(); ' \
            'sys.stdout.buffer.write(b"\\xff three\\n"); sys.exit(3)'
        raw = io.BytesIO()
        wrapper = io.TextIOWrapper(raw)
        self.addCleanup(wrapper.detach)
        with mock.patch.object(steps.sys, 'stdout', wrapper):
            code, out = steps.run_streamed([sys.executable, '-c', script])
        self.assertEqual(code, 3)
        self.assertEqual(out, 'one\ntwo\n\ufffd three\n')
        self.assertEqual(raw.getvalue(), b'one\ntwo\n\xff three\n')

    def test_run_streamed_does_not_wait_for_a_process_that_the_command_left_running(self):
        script = 'import subprocess, sys; subprocess.Popen(["sleep", "30"]); print("\\x1b[1mdone\\x1b[0m")'
        start = time.monotonic()
        with contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())):
            code, out = steps.run_streamed([sys.executable, '-c', script])
        self.assertEqual((code, out), (0, 'done\n'))
        self.assertLess(time.monotonic() - start, 10)

    def test_run_streamed_copies_all_the_output_to_a_slow_reader(self):
        class Slow(io.BytesIO):
            """A stdout that takes a while for each line."""

            def write(self, data):
                """Wait, then keep DATA."""
                time.sleep(0.01)
                return super().write(data)

        slow = Slow()
        script = 'for n in range(100): print(n)\nprint("BOOM")'
        with mock.patch.object(steps, 'LINGER', 0.2), \
                mock.patch.object(steps.sys, 'stdout', mock.Mock(buffer=slow)):
            code, out = steps.run_streamed([sys.executable, '-c', script])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith('99\nBOOM\n'))
        self.assertTrue(slow.getvalue().endswith(b'99\nBOOM\n'))

    def test_toml_fails_outside_git_and_ends_the_options_before_the_files(self):
        real, calls = subprocess.run, []

        def fake(command, *args, **kwargs):
            """Run git for real, and record each other command as passed."""
            if command[0] == 'git':
                return real(command, *args, **kwargs)
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        self.files('Cargo.toml', '-dash.toml', 'sub/a.toml')
        with mock.patch.object(steps.subprocess, 'run', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.toml(['--check']), 0)
        self.assertEqual(calls[0][:5], ['cargo', 'sort', '--workspace', '--no-format', '--check'])
        self.assertEqual(calls[1], ['taplo', 'fmt', '--check', '--', '-dash.toml', 'Cargo.toml', 'sub/a.toml'])
        with tempfile.TemporaryDirectory() as plain:
            os.chdir(plain)
            with mock.patch.object(steps.subprocess, 'run', side_effect=fake), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(steps.toml(['--check']), 1)
            self.assertIn('git cannot list the TOML files', err.getvalue())

    def test_main_names_the_usage_for_unknown_commands(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(steps.main(['nothing']), 2)
        self.assertIn('usage', err.getvalue())

class Version(unittest.TestCase):
    """The recipe rust::version, which prints the version for the release tag."""

    def run_on(self, text=None, raw=None):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / 'Cargo.toml'
            if raw is not None:
                manifest.write_bytes(raw)
            elif text is not None:
                manifest.write_text(text)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = steps.version(manifest)
            return code, out.getvalue(), err.getvalue()

    def test_a_package_prints_its_version(self):
        self.assertEqual(self.run_on('[package]\nname = "a"\nversion = "1.2.3"\n'), (0, '1.2.3\n', ''))

    def test_a_workspace_root_with_no_package_prints_the_workspace_version(self):
        text = '[workspace]\nmembers = ["a"]\n[workspace.package]\nversion = "0.4.0"\n'
        self.assertEqual(self.run_on(text), (0, '0.4.0\n', ''))

    def test_a_package_that_inherits_takes_the_workspace_version(self):
        text = '[package]\nname = "a"\nversion.workspace = true\n[workspace]\n[workspace.package]\nversion = "2.0.1"\n'
        self.assertEqual(self.run_on(text), (0, '2.0.1\n', ''))

    def test_no_manifest_or_no_version_prints_nothing(self):
        self.assertEqual(self.run_on(), (0, '', ''))
        self.assertEqual(self.run_on('[package]\nname = "a"\n'), (0, '', ''))
        self.assertEqual(self.run_on('[workspace]\nmembers = []\n'), (0, '', ''))

    def test_a_package_with_no_version_does_not_take_the_workspace_version(self):
        text = '[package]\nname = "a"\n[workspace]\n[workspace.package]\nversion = "9.9.9"\n'
        self.assertEqual(self.run_on(text), (0, '', ''))

    def test_a_bom_and_crlf_and_an_inline_inheritance_are_read(self):
        self.assertEqual(self.run_on(raw=b'\xef\xbb\xbf[package]\r\nname = "a"\r\nversion = "1.0.0"\r\n'),
                         (0, '1.0.0\n', ''))
        text = '[package]\nname = "a"\nversion = {workspace = true}\n[workspace.package]\nversion = "1.1.0"\n'
        self.assertEqual(self.run_on(text), (0, '1.1.0\n', ''))
        self.assertEqual(self.run_on('[package]\nversion = "1.0.0-rc.1+build.5"\n'), (0, '1.0.0-rc.1+build.5\n', ''))

    def test_a_bad_file_exits_2_with_a_message(self):
        bad = [b'[package\n', b'\xff\xfe', b'[package]\nversion = 5\n', b'[package]\nversion = ""\n',
               b'[package]\nversion = "v1.0.0"\n', b'[package]\nversion = " 1.0.0 "\n',
               b'[package]\nversion = "01.0.0"\n', b'[package]\nversion = "1.0"\n',
               b'[package]\nversion = "latest/../x"\n', b'[package]\nversion = "1.0.0\\nevil"\n',
               b'[package]\nversion.workspace = false\n', b'[package]\nversion.workspace = true\n']
        for raw in bad:
            code, out, err = self.run_on(raw=raw)
            self.assertEqual((code, out), (2, ''), raw)
            self.assertIn('version:', err)

    def test_a_folder_a_dangling_link_and_a_locked_file_exit_2(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'folder' / 'Cargo.toml'
            folder.mkdir(parents=True)
            link = Path(temp) / 'link' / 'Cargo.toml'
            link.parent.mkdir()
            link.symlink_to(Path(temp) / 'missing')
            locked = Path(temp) / 'locked' / 'Cargo.toml'
            locked.parent.mkdir()
            locked.write_text('[package]\nversion = "1.0.0"\n')
            locked.chmod(0)
            cases = [folder, link] + ([locked] if os.geteuid() != 0 else [])
            for manifest in cases:
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(steps.version(manifest), 2, manifest)
                self.assertEqual(out.getvalue(), '')
            locked.chmod(0o600)

    def test_main_runs_version_and_refuses_extra_arguments(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            Path('Cargo.toml').write_text('[package]\nversion = "0.3.0"\n')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(steps.main(['version']), 0)
            self.assertEqual(out.getvalue(), '0.3.0\n')
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(steps.main(['version', 'x']), 2)
            self.assertEqual(out.getvalue(), '')

    def test_the_recipe_fails_through_just_with_nothing_on_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'Cargo.toml').write_text('[package]\nversion = "v1.0.0"\n')
            module = REPO / '.agents/just/rust/base.just'
            done = subprocess.run(['just', '--justfile', str(module), '--working-directory', temp, 'version'],
                                  capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (2, ''))
        self.assertIn('semver', done.stderr)

    def test_the_recipe_prints_the_version_through_just(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'Cargo.toml').write_text('[package]\nname = "a"\nversion = "3.1.4"\n')
            module = REPO / '.agents/just/rust/base.just'
            done = subprocess.run(['just', '--justfile', str(module), '--working-directory', temp, 'version'],
                                  capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, '3.1.4\n'), done.stderr)

if __name__ == '__main__':
    unittest.main()
