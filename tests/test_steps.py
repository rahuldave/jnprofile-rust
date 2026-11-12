"""Tests of the helper script .agents/just/rust/steps.py, without cargo."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / '.agents/just/rust'))
import steps  # noqa: E402

CARGO_TEST = ['cargo', 'test', '--locked', '--all-features', '--package']


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

    def test_test_paths_picks_the_target_of_each_test_file(self):
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
                         [[*CARGO_TEST, 'one', '--test', 'api', '--test', 'more'], [*CARGO_TEST, 'two', '--tests']])
        self.assertEqual(steps.test_commands(['crates/one/tests/api.rs', 'crates/one/tests/common/mod.rs']),
                         [[*CARGO_TEST, 'one', '--tests']])
        with self.assertRaises(SystemExit):
            steps.test_commands(['crates/one/tests/none.rs'])
        (self.root / 'loose.rs').write_text('')
        with self.assertRaises(SystemExit):
            steps.test_commands(['loose.rs'])

    def test_main_names_the_usage_for_unknown_commands(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(steps.main(['nothing']), 2)
        self.assertIn('usage', err.getvalue())


if __name__ == '__main__':
    unittest.main()
