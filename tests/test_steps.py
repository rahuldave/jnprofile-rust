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

    def test_the_first_commit_names_itself_before_the_report(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'cargo').write_text('#!/bin/sh\necho REPORT "$@"\n')
        (bin_dir / 'cargo').chmod(0o755)
        done = subprocess.run([sys.executable, str(REPO / '.agents/just/rust/steps.py'), 'coverage', 'x'],
                              cwd=self.root, capture_output=True, text=True,
                              env=dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            'coverage-check: the first commit, so every line of code must run in the tests',
            'REPORT llvm-cov report --include-build-script --fail-under-lines 100'])

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
            self.assertEqual(steps.coverage('target/llvm-cov/lcov.info'), 1)
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

    def run_tests(self, run, args=(), change=None):
        """Stand in for test: begin, a test run that writes the report and may change a file, then stamp."""
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'JLOG_RUN': run} if run else {}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            if not run:
                os.environ.pop('JLOG_RUN', None)
            steps.begin(list(args))
            steps.REPORT.parent.mkdir(parents=True, exist_ok=True)
            steps.REPORT.write_text('SF:x\nend_of_record\n')
            if change:
                change()
            steps.stamp()
        return out.getvalue()

    def is_fresh(self, run):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'JLOG_RUN': run} if run else {}), contextlib.redirect_stdout(out):
            if not run:
                os.environ.pop('JLOG_RUN', None)
            return steps.fresh()

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
        self.assertFalse(steps.RUN.exists())
        self.assertFalse(self.is_fresh('run-a'))
        self.run_tests(None)
        self.assertFalse(steps.RUN.exists())
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=self.root, capture_output=True,
                                text=True).stdout
        self.assertEqual(staged, '')

    def test_a_file_changed_while_the_tests_run_drops_the_run(self):
        self.commit_one()
        out = self.run_tests('run-a', change=lambda: (self.root / 'src/lib.rs').write_text('pub fn x() {}\n'))
        self.assertIn('a file changed while the tests ran', out)
        self.assertFalse(steps.RUN.exists())
        self.assertFalse(self.is_fresh('run-a'))

    def test_a_missing_report_is_not_fresh(self):
        self.commit_one()
        self.run_tests('run-a')
        steps.REPORT.unlink()
        self.assertFalse(self.is_fresh('run-a'))

    def test_the_folder_target_does_not_count_in_the_tree_even_when_git_sees_it(self):
        self.files('src/lib.rs')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'one')
        tree = steps.worktree_tree()
        self.files('target/llvm-cov/lcov.info')
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
        self.assertFalse(steps.RUN.exists())
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
        fail = PASSED.replace('ok. 1 passed; 0', 'FAILED. 0 passed; 1') + 'error: test failed, to rerun pass `--test api`\n'
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
            # A binary that announced its tests and stopped with no result, as on exit(0) in a test.
            (0, '     Running tests/api.rs (target/debug/deps/api-1)\nrunning 2 tests\n', 1),
        )
        for got, out, want in cases:
            with self.subTest(got=got, out=out), \
                    mock.patch.object(steps, 'run_streamed', return_value=(got, out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(steps.test_paths(['tests/api.rs']), want)

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
        self.assertEqual(commands, [['cargo', 'test', '--locked', '--all-features', '--package', 'app', '--test', 'foo']])
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

    def test_main_names_the_usage_for_unknown_commands(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(steps.main(['nothing']), 2)
        self.assertIn('usage', err.getvalue())


if __name__ == '__main__':
    unittest.main()
