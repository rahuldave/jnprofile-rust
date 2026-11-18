"""Tests of the helper script .agents/just/rust/steps.py, without cargo."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
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


class Targets(unittest.TestCase):
    """target_flags and run_tests, with a stand-in cargo that prints the metadata and logs its arguments."""

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
                   LOG=str(self.log))
        patched = mock.patch.dict(os.environ, env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)

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
            'llvm-cov test --all-features --locked --include-build-script --workspace --lcov --output-path '
            'target/llvm-cov/integration.part0.lcov --test api'])
        self.log.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(steps.run_tests('test', ['-p', 'core', '--lib']), 0)
        self.assertEqual(self.log.read_text().splitlines(), [
            'llvm-cov test --all-features --locked --include-build-script --lcov --output-path '
            'target/llvm-cov/test.part0.lcov -p core --lib'])

    def test_run_gives_cargo_the_targets_the_report_and_the_arguments_last(self):
        self.packages(['lib', ('test', 'api', True)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(steps.run_tests('integration', ['parse', '--', '--nocapture']), 0)
        want = ('llvm-cov test --all-features --locked --include-build-script --workspace --test api --lcov '
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
        (self.root / 'src/lib.rs').write_text('// changed\n')
        changed = steps.changed_code()
        self.assertEqual(changed, ['benches/b.rs', 'examples/demo.rs', 'src/lib.rs', 'src/orphan.rs', 'src/skip.rs',
                                   'src/tests/mod.rs', 'tests/api.rs'])
        self.assertEqual(steps.unreported(changed, {'src/lib.rs'}, lambda path: path == 'src/skip.rs'),
                         ['examples/demo.rs', 'src/orphan.rs', 'src/tests/mod.rs'])

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

    def test_unreported_vendor_diagnostic_keeps_failure_without_claiming_no_execution(self):
        self.commit_one()
        self.files('vendor/dep/build.rs')
        self.write_reports([('src/lib.rs', 1, 1)], [])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('vendor/dep/build.rs: a changed file that is absent from the coverage report', out)
        self.assertIn('reporting scope or copied source paths', out)
        self.assertNotIn('so no test runs it', out)
        self.assertIn('independent review', out)

    def test_exact_vendor_exclusion_requires_prior_commit_and_does_not_cover_neighbors(self):
        self.commit_one()
        (self.root / '.agents').mkdir(exist_ok=True)
        listing = self.root / '.agents/coverage-exclude.txt'
        listing.write_text('# Independently reviewed pinned upstream source; project verifies integrity.\n'
                           'vendor/dep/build.rs\n')
        self.files('vendor/dep/build.rs')
        self.write_reports([('src/lib.rs', 1, 1)], [])
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn('vendor/dep/build.rs:', out)
        self.git('add', '.agents/coverage-exclude.txt')
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.git('commit', '-q', '-m', 'reviewed exclusion prerequisite')
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.files('vendor/dep/src/lib.rs', 'vendor/neighbor/build.rs', 'src/adapted.rs')
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        for path in ('vendor/dep/src/lib.rs', 'vendor/neighbor/build.rs', 'src/adapted.rs'):
            self.assertIn(path + ':', out)
        self.assertNotIn('vendor/dep/build.rs:', out)

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
