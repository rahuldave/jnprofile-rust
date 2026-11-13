"""The lab of the rust profile: install it into a temporary Cargo project and run its steps.

The lab copies the committed jnterfaces from the folder that JNTERFACES names, else from ../jnterfaces next to this
repository. It applies lab/jnterfaces-rust.patch to that copy and adds the entry of lab/stack-entry.json to its
stack.json, because jnterfaces has no rust profile yet. It installs this working tree as the package jnprofile-rust,
as tests/test_install_stack.py of jnterfaces makes its profile packages. The roles tasker and prose come from small
stand-in packages. The lab needs git, just, cargo, cargo-llvm-cov and uv, and it runs offline after the first run.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
JNTERFACES = Path(os.environ.get('JNTERFACES', REPO.parent / 'jnterfaces'))
TOOLS = ('git', 'just', 'cargo', 'cargo-llvm-cov', 'uv')
MISSING = [tool for tool in TOOLS if not shutil.which(tool)]
READY = not MISSING and (JNTERFACES / 'scripts/install_stack.py').is_file()
WHY = f'needs {", ".join(MISSING)}' if MISSING else f'needs jnterfaces in {JNTERFACES}'

LIB = '''//! The crate of the lab.

/// Adds two numbers.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_sums_two_numbers() {
        assert_eq!(add(2, 3), 5);
    }
}
'''
API_TEST = '''//! The tests of the public functions.

#[test]
fn add_works_from_outside() {
    assert_eq!(app::add(1, 1), 2);
}
'''
EXTRA = '''//! A module with code that no test runs.

/// Doubles a number.
pub fn double(a: i32) -> i32 {
    a * 2
}
'''
CARGO = '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'


def run(args, cwd, env=None, check=False):
    """Run ARGS in CWD with no input, and return the finished process with its output as text."""
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env=env or os.environ.copy())
    if check and done.returncode:
        raise AssertionError(f'{args} failed with {done.returncode}:\n{done.stdout}\n{done.stderr}')
    return done


def git(path, *args):
    """Run git in PATH with a fixed author, fail on an error, and return its output."""
    return run(['git', '-c', 'user.name=Lab', '-c', 'user.email=lab@example.invalid', *args], path, check=True).stdout


def commit(path, message='change'):
    """Stage every file in PATH and commit it."""
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', message)


def stand_in(root, name, skill):
    """Make a small package NAME with one skill SKILL, as the role packages of the jnterfaces tests are."""
    (root / '.agents/skills' / skill).mkdir(parents=True)
    (root / '.agents/skills' / skill / 'SKILL.md').write_text(f'---\nname: {skill}\ndescription: d\n---\n')
    (root / 'skill-package.json').write_text(json.dumps(
        {'repository': {'name': name}, 'skills': [{'name': skill, 'path': f'.agents/skills/{skill}'}]}))
    git(root, 'init', '-q')
    commit(root)


def make_packages(base):
    """Make the folder of package clones: jnterfaces with the patch, this profile, and the stand-in roles."""
    packages = base / 'packages'
    jn = packages / 'jnterfaces'
    jn.mkdir(parents=True)
    archive = subprocess.run(['git', '-C', str(JNTERFACES), 'archive', 'HEAD'], capture_output=True, check=True)
    subprocess.run(['tar', '-x', '-C', str(jn)], input=archive.stdout, check=True)
    run(['git', 'init', '-q'], jn, check=True)
    run(['git', 'apply', str(REPO / 'lab/jnterfaces-rust.patch')], jn, check=True)
    stack = json.loads((jn / 'stack.json').read_text())
    stack['profiles'].update(json.loads((REPO / 'lab/stack-entry.json').read_text()))
    (jn / 'stack.json').write_text(json.dumps(stack, indent=2) + '\n')
    commit(jn)
    profile = packages / 'jnprofile-rust'
    shutil.copytree(REPO, profile, ignore=shutil.ignore_patterns('.git', 'target', '_site', '__pycache__'))
    run(['git', 'init', '-q'], profile, check=True)
    commit(profile)
    for name, skill in {'agent_gest_git_skills': 'gtw', 'nossaifd': 'nossaifd', 'sklanes': 'lanes'}.items():
        stand_in(packages / name, name, skill)
    return packages


def install(packages, target, *args):
    """Run the patched installer of the lab copy of jnterfaces on TARGET, and return the finished process."""
    env = {k: v for k, v in os.environ.items() if k not in ('SKILL_PACKAGES_DIR', 'VIRTUAL_ENV')}
    return run([sys.executable, str(packages / 'jnterfaces/scripts/install_stack.py'), '--target', str(target),
                *args], target, env=env)


@unittest.skipUnless(READY, WHY)
class RustLab(unittest.TestCase):
    """Each test gets a copy of one Cargo project that has the profile installed and one commit."""

    @classmethod
    def setUpClass(cls):
        """Make the packages, a Cargo project with tested and documented code, install the profile, and build."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(os.path.realpath(cls.temp.name))
        cls.packages = make_packages(cls.base)
        app = cls.base / 'app'
        (app / 'src').mkdir(parents=True)
        (app / 'tests').mkdir()
        (app / 'Cargo.toml').write_text(CARGO)
        (app / 'src/lib.rs').write_text(LIB)
        (app / 'tests/api.rs').write_text(API_TEST)
        git(app, 'init', '-q')
        cls.installed = install(cls.packages, app, '--defaults')
        if cls.installed.returncode == 0:
            # The steps run with --locked, so the project makes and commits Cargo.lock before it runs them.
            run(['just', 'setup'], app, check=True)
        cls.fresh = cls.base / 'fresh'
        shutil.copytree(app, cls.fresh, symlinks=True)
        commit(app, 'start')
        if cls.installed.returncode == 0:
            run(['just', 'verify'], app)
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        """Remove the folder of the lab."""
        cls.temp.cleanup()

    def setUp(self):
        """Copy the project, with its build folder, so that each test starts from the same commit."""
        self.assertEqual(self.installed.returncode, 0, self.installed.stderr)
        self.dir = self.base / f'case-{self.id().rsplit(".", 1)[-1]}'
        shutil.copytree(self.app, self.dir, symlinks=True)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def just(self, *args, cwd=None, env=None):
        """Run a recipe in the project of the test."""
        return run(['just', *args], cwd or self.dir, env=env)

    def out(self, done):
        """The output and the error output of a finished process, as one text."""
        return done.stdout + done.stderr

    def test_the_installer_finds_the_profile_by_cargo_toml_and_writes_its_files(self):
        self.assertIn('Profile(s) found from the files of the project: rust', self.installed.stderr)
        saved = json.loads((self.app / '.agents/stack.json').read_text())
        self.assertEqual(saved['profiles'], ['rust'])
        self.assertEqual([pair.split(':')[1] for pair in saved['tools']], ['lint', 'typecheck', 'test', 'docstring'])
        for path in ('.agents/just/rust/steps.py', '.agents/skills/rust-profile/SKILL.md',
                     *(f'.agents/targets/rust/{name}.md' for name in ('review', 'test-review', 'docs', 'prose'))):
            self.assertTrue((self.app / path).is_file(), path)
        self.assertIn('/target/', (self.app / '.gitignore').read_text().splitlines())
        self.assertIn('/target/', (self.app / '.worktreedisposable').read_text().splitlines())
        self.assertIn('lint for rust needs no packages', self.installed.stderr)
        summary = self.just('--summary').stdout.split()
        for step in ('fmt-check', 'lint', 'typecheck', 'test', 'coverage-check', 'docstring-check', 'test-paths'):
            self.assertIn(step, summary)
        self.assertNotIn('api', ' '.join(summary))

    def test_a_new_folder_gets_cargo_init_from_the_profile(self):
        target = self.base / 'empty'
        target.mkdir()
        git(target, 'init', '-q')
        done = install(self.packages, target, '--profile', 'rust', '--defaults')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('cargo init --vcs none', done.stderr)
        self.assertTrue((target / 'Cargo.toml').is_file())

    def test_a_clean_project_passes_every_step_and_leaves_no_file_that_git_does_not_ignore(self):
        for step in ('fmt-check', 'lint', 'typecheck', 'test', 'coverage-check', 'docstring-check', 'verify'):
            done = self.just(step)
            self.assertEqual(done.returncode, 0, f'{step}:\n{self.out(done)}')
        self.assertEqual(git(self.dir, 'status', '--porcelain', '--untracked-files=all'), '')

    def test_a_project_with_no_cargo_lock_fails_the_steps_and_they_write_no_lock_file(self):
        git(self.dir, 'rm', '-q', 'Cargo.lock')
        commit(self.dir)
        for step in ('lint', 'typecheck', 'test', 'docstring-check'):
            done = self.just(step)
            self.assertNotEqual(done.returncode, 0, step)
            self.assertIn('--locked', self.out(done), step)
        self.assertEqual(git(self.dir, 'status', '--porcelain', '--untracked-files=all'), '')

    def test_a_module_folder_named_target_stays_in_git_and_counts(self):
        (self.dir / 'src/target').mkdir()
        (self.dir / 'src/target/mod.rs').write_text(EXTRA)
        (self.dir / 'src/lib.rs').write_text(f'{LIB}\n/// A module named target.\npub mod target;\n')
        self.assertIn('?? src/target/', git(self.dir, 'status', '--porcelain'))
        done = self.just('coverage-check')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('src/target/mod.rs', done.stdout)

    def reset(self):
        """Take back each change of the worktree, and keep the ignored build folder."""
        git(self.dir, 'checkout', '-q', '--', '.')
        git(self.dir, 'clean', '-q', '-fd')

    def test_a_changed_file_that_the_report_leaves_out_fails_coverage_check(self):
        cases = {
            'build.rs': ('fn main() {\n    if std::env::var("NEVER_SET_X").is_ok() {\n        println!("x");\n    }\n}\n',
                         None),
            'examples/demo.rs': ('fn main() {\n    println!("{}", app::add(1, 2));\n}\n', None),
            'src/orphan.rs': (EXTRA, None),
            'src/tests/mod.rs': (EXTRA, '#[path = "tests/mod.rs"]\npub mod util;\n'),
        }
        for path, (text, mod) in cases.items():
            with self.subTest(path=path):
                self.reset()
                (self.dir / path).parent.mkdir(parents=True, exist_ok=True)
                (self.dir / path).write_text(text)
                if mod:
                    (self.dir / 'src/lib.rs').write_text(f'{LIB}\n/// The module.\n{mod}')
                done = self.just('coverage-check')
                self.assertNotEqual(done.returncode, 0, self.out(done))
                self.assertIn(path, done.stdout)
        self.reset()
        (self.dir / 'tests/more.rs').write_text(API_TEST.replace('add_works', 'more_works'))
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))

    def test_code_behind_a_feature_counts_for_coverage_and_docs(self):
        (self.dir / 'Cargo.toml').write_text(CARGO + '\n[features]\nextra = []\n')
        (self.dir / 'src/lib.rs').write_text(
            f'{LIB}\n/// Triples a number.\n#[cfg(feature = "extra")]\npub fn triple(a: i32) -> i32 {{\n    a * 3\n}}\n')
        done = self.just('coverage-check')
        self.assertNotEqual(done.returncode, 0, self.out(done))
        self.assertIn('src/lib.rs', done.stdout)
        (self.dir / 'src/lib.rs').write_text(
            f'{LIB}\n#[cfg(feature = "extra")]\npub fn triple(a: i32) -> i32 {{\n    a * 3\n}}\n')
        done = self.just('docstring-check')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('missing documentation for a function', done.stdout)

    def test_a_crate_that_denies_missing_docs_still_gets_file_and_line(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('//! The crate of the lab.\n',
                                                         '//! The crate of the lab.\n#![deny(missing_docs)]\n')
                                             + '\npub fn nodoc() {}\n')
        done = self.just('docstring-check')
        self.assertEqual(done.returncode, 1, self.out(done))
        self.assertIn(f'src/lib.rs:{LIB.count(chr(10)) + 3}:1: missing documentation for a function', done.stdout)

    def test_a_generated_file_in_a_crate_that_denies_missing_docs_passes_docstring_check(self):
        (self.dir / '.agents/generated.txt').write_text('src/gen.rs\n')
        commit(self.dir)
        (self.dir / 'src/gen.rs').write_text('pub fn made() -> i32 {\n    7\n}\n')
        (self.dir / 'src/lib.rs').write_text(LIB.replace('//! The crate of the lab.\n',
                                                         '//! The crate of the lab.\n#![deny(missing_docs)]\n')
                                             + '\n/// Generated.\npub mod gen;\n')
        done = self.just('docstring-check')
        self.assertEqual(done.returncode, 0, self.out(done))

    def test_code_that_build_rs_writes_into_target_counts_as_generated(self):
        (self.dir / 'build.rs').write_text(
            '//! Writes the generated code.\n\nfn main() {\n    let out = std::env::var("OUT_DIR").unwrap();\n'
            '    std::fs::write(format!("{out}/gen.rs"), "pub fn made() -> i32 { 7 }\\n").unwrap();\n}\n')
        (self.dir / 'src/lib.rs').write_text(
            LIB + '\n/// Generated.\npub mod gen {\n    include!(concat!(env!("OUT_DIR"), "/gen.rs"));\n}\n')
        done = self.just('docstring-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertEqual(done.stdout, '')

    def test_test_with_a_name_runs_only_the_tests_that_match(self):
        done = self.just('test', 'add_sums')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('add_sums_two_numbers', self.out(done))
        self.assertNotIn('add_works_from_outside ... ok', self.out(done))

    def test_fmt_check_fails_on_bad_format_and_changes_no_file(self):
        bad = LIB.replace('a + b', 'a+b')
        (self.dir / 'src/lib.rs').write_text(bad)
        self.assertNotEqual(self.just('fmt-check').returncode, 0)
        self.assertEqual((self.dir / 'src/lib.rs').read_text(), bad)

    def test_lint_fails_on_a_clippy_warning(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('    a + b', '    let unused = 1;\n    a + b'))
        done = self.just('lint')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('unused', self.out(done))

    def test_typecheck_fails_on_a_type_error(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('    a + b', '    a + "b"'))
        self.assertNotEqual(self.just('typecheck').returncode, 0)

    def test_test_fails_on_a_failed_test_and_verify_stops(self):
        (self.dir / 'tests/api.rs').write_text(API_TEST.replace('2);', '3);'))
        self.assertNotEqual(self.just('test').returncode, 0)
        self.assertNotEqual(self.just('verify').returncode, 0)

    def add_extra(self, name='extra'):
        """Add the module NAME with a function that no test runs, and name it in src/lib.rs."""
        (self.dir / f'src/{name}.rs').write_text(EXTRA)
        (self.dir / 'src/lib.rs').write_text(f'{LIB}\n/// The module with untested code.\npub mod {name};\n')

    def test_coverage_check_fails_on_a_changed_line_that_no_test_runs_and_names_it(self):
        self.add_extra()
        done = self.just('coverage-check')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('src/extra.rs', done.stdout)
        self.assertIn('Missing lines 4-6', done.stdout)
        self.assertIn('write tests', done.stderr)

    def test_an_exclude_entry_of_the_parent_commit_lets_the_file_pass(self):
        (self.dir / '.agents/coverage-exclude.txt').write_text('# no test can run this file\nsrc/extra.rs\n')
        commit(self.dir)
        self.add_extra()
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))

    def test_a_commit_that_adds_its_own_exclude_entry_still_fails(self):
        (self.dir / '.agents/coverage-exclude.txt').write_text('src/extra.rs\n')
        (self.dir / '.agents/generated.txt').write_text('src/**\n')
        self.add_extra()
        self.assertNotEqual(self.just('coverage-check').returncode, 0)
        (self.dir / 'src/extra.rs').write_text(EXTRA.replace('/// Doubles a number.\n', ''))
        self.assertNotEqual(self.just('docstring-check').returncode, 0)

    def test_a_generated_file_of_the_parent_fails_neither_coverage_check_nor_docstring_check(self):
        (self.dir / '.agents/generated.txt').write_text('src/gen/\n')
        commit(self.dir)
        (self.dir / 'src/gen').mkdir()
        (self.dir / 'src/gen/mod.rs').write_text('pub fn made() -> i32 {\n    7\n}\n')
        (self.dir / 'src/lib.rs').write_text(f'{LIB}\n/// The generated code.\npub mod gen;\n')
        for step in ('coverage-check', 'docstring-check'):
            done = self.just(step)
            self.assertEqual(done.returncode, 0, f'{step}:\n{self.out(done)}')

    def test_coverage_check_alone_runs_the_tests_again(self):
        report = self.dir / 'target/llvm-cov/lcov.info'
        self.assertEqual(self.just('test').returncode, 0)
        report.write_text('SF:old\nend_of_record\n')
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('SF:', report.read_text())
        self.assertNotIn('SF:old', report.read_text())

    def test_on_the_first_commit_coverage_check_needs_every_line(self):
        fresh = self.base / 'first'
        shutil.copytree(self.fresh, fresh, symlinks=True)
        self.addCleanup(shutil.rmtree, fresh, True)
        done = self.just('coverage-check', cwd=fresh)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('the first commit', done.stdout)
        (fresh / 'src/extra.rs').write_text(EXTRA)
        (fresh / 'src/lib.rs').write_text(f'{LIB}\n/// The module with untested code.\npub mod extra;\n')
        done = self.just('coverage-check', cwd=fresh)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('write tests', done.stderr)

    def test_docstring_check_fails_on_a_private_function_and_a_module_and_names_file_and_line(self):
        (self.dir / 'src/lib.rs').write_text(f'{LIB}\nfn helper() -> i32 {{\n    1\n}}\n\nmod inner;\n')
        (self.dir / 'src/inner.rs').write_text('/// Used.\npub fn used() -> i32 {\n    super::helper()\n}\n')
        done = self.just('docstring-check')
        self.assertNotEqual(done.returncode, 0)
        line = LIB.count('\n') + 2
        self.assertIn(f'src/lib.rs:{line}:4: missing documentation for a function', done.stdout)
        self.assertIn(f'src/lib.rs:{line + 4}:5: missing documentation for a module', done.stdout)

    def test_docstring_check_reports_a_compile_error_and_fails(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('    a + b', '    a + "b"'))
        done = self.just('docstring-check')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('error', done.stderr)

    def test_test_paths_runs_the_target_of_one_test_file(self):
        done = self.just('test-paths', 'tests/api.rs')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('cargo test --locked --all-features --package app --test api', done.stdout)
        self.assertIn('add_works_from_outside', self.out(done))
        self.assertNotIn('add_sums_two_numbers', self.out(done))
        (self.dir / 'tests/api.rs').write_text(API_TEST.replace('2);', '3);'))
        self.assertNotEqual(self.just('test-paths', 'tests/api.rs').returncode, 0)
        done = self.just('test-paths', 'src/lib.rs')
        self.assertEqual(done.returncode, 5, self.out(done))
        self.assertIn('test-paths: skips src/lib.rs', done.stdout)
        self.assertNotIn('cargo test', done.stdout)

    def test_test_paths_exits_with_5_when_cargo_runs_no_test(self):
        (self.dir / 'tests/empty.rs').write_text('//! Helpers and no test.\n')
        done = self.just('test-paths', 'tests/empty.rs')
        self.assertEqual(done.returncode, 5, self.out(done))
        self.assertIn('the test targets hold no test', done.stdout)
        manifest = (self.dir / 'Cargo.toml').read_text()
        (self.dir / 'tests/plain.rs').write_text('//! A test with its own main.\n\nfn main() {}\n')
        (self.dir / 'Cargo.toml').write_text(manifest + '\n[[test]]\nname = "plain"\nharness = false\n')
        done = self.just('test-paths', 'tests/plain.rs')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('Running tests/plain.rs', done.stdout)
        loud = dict(os.environ, CARGO_TERM_COLOR='always', CARGO_TERM_QUIET='true')
        self.assertEqual(self.just('test-paths', 'tests/plain.rs', env=loud).returncode, 0)
        self.assertEqual(self.just('test-paths', 'tests/plain.rs', env=dict(loud, CARGO_TERM_QUIET='false',
                                                                             CARGO_TERM_VERBOSE='true')).returncode, 0)
        (self.dir / 'tests/plain.rs').unlink()
        (self.dir / 'Cargo.toml').write_text(manifest)
        (self.dir / 'tests/empty.rs').unlink()
        (self.dir / 'tests/api.rs').unlink()
        (self.dir / 'tests/common').mkdir()
        (self.dir / 'tests/common/mod.rs').write_text('//! A helper.\n')
        done = self.just('test-paths', 'tests/common/mod.rs')
        self.assertEqual(done.returncode, 5, self.out(done))
        self.assertIn('no test target in the folder tests', done.stdout)
        done = self.just('test-paths', 'tests/common/mod.rs', env=dict(os.environ, CARGO_TERM_COLOR='always'))
        self.assertEqual(done.returncode, 5, self.out(done))

    def test_coverage_check_reads_the_report_of_test_only_in_the_same_run_and_tree(self):
        report = self.dir / 'target/llvm-cov/lcov.info'
        env = dict(os.environ, JLOG_RUN='run-one')
        self.assertEqual(self.just('test', env=env).returncode, 0)
        before = report.stat().st_mtime_ns
        done = self.just('coverage-check', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('so they do not run again', done.stdout)
        self.assertEqual(report.stat().st_mtime_ns, before)
        for other in (dict(os.environ, JLOG_RUN='run-two'), {k: v for k, v in os.environ.items() if k != 'JLOG_RUN'}):
            done = self.just('coverage-check', env=other)
            self.assertEqual(done.returncode, 0, self.out(done))
            self.assertIn('so the tests run now', done.stdout)
        self.assertEqual(self.just('test', env=env).returncode, 0)
        self.add_extra()
        done = self.just('coverage-check', env=env)
        self.assertIn('so the tests run now', done.stdout)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('src/extra.rs', done.stdout)
        self.assertEqual(self.just('test', 'add_sums', env=env).returncode, 0)
        self.assertIn('so the tests run now', self.just('coverage-check', env=env).stdout)

    def test_one_run_id_of_the_root_justfile_runs_the_tests_once(self):
        bin_dir = self.dir / 'stand-in-bin'
        bin_dir.mkdir()
        log = self.base / 'once.lines'
        log.unlink(missing_ok=True)
        (bin_dir / 'jlog').write_text(f'#!/bin/sh\nprintf "%s\\n" "$3" >> "{log}"\nexec "$@"\n')
        (bin_dir / 'jlog').chmod(0o755)
        module = self.dir / '.agents/just/rust.just'
        module.write_text(module.read_text().replace('set positional-arguments',
                                                     'set positional-arguments\nset shell := ["jlog", "sh", "-cu"]'))
        justfile = self.dir / 'Justfile'
        # A root recipe with the two steps, as just check will have them. The index runs each in an inner just call.
        justfile.write_text('export JLOG_RUN := env("JLOG_RUN", uuid())\n' + justfile.read_text()
                            + '\ngates: test coverage-check\n')
        env = {k: v for k, v in os.environ.items() if k != 'JLOG_RUN'}
        env['PATH'] = f'{bin_dir}:{env["PATH"]}'
        done = self.just('gates', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('so they do not run again', done.stdout)
        runs = [line for line in log.read_text().splitlines() if line.startswith('cargo llvm-cov test')]
        self.assertEqual(len(runs), 1, runs)

    def test_a_project_in_a_subfolder_reads_its_own_lists(self):
        mono = self.base / 'mono'
        project = mono / 'rs'
        shutil.copytree(self.fresh, project, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        self.addCleanup(shutil.rmtree, mono, True)
        git(mono, 'init', '-q')
        (project / '.agents/generated.txt').write_text('src/gen.rs\n')
        commit(mono)
        (project / 'src/gen.rs').write_text('pub fn made() -> i32 {\n    7\n}\n')
        (project / 'src/lib.rs').write_text(f'{LIB}\n/// The generated code.\npub mod gen;\n')
        (project / 'src/extra.rs').write_text(EXTRA.replace('/// Doubles a number.\n', ''))
        (project / 'src/lib.rs').write_text(f'{LIB}\n/// The generated code.\npub mod gen;\n/// More.\npub mod extra;\n')
        done = self.just('docstring-check', cwd=project)
        self.assertEqual(done.stdout.splitlines(), ['src/extra.rs:3:1: missing documentation for a function '
                                                    '(missing_docs)'], self.out(done))
        done = self.just('coverage-check', cwd=project)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('extra.rs', done.stdout)
        self.assertNotIn('gen.rs', done.stdout)

    def test_every_line_of_every_step_runs_through_the_shell_of_the_justfile(self):
        """With set shell as jlog, a stand-in jlog logs each line that just gives it, then runs the line."""
        bin_dir = self.dir / 'stand-in-bin'
        bin_dir.mkdir()
        log = self.base / 'jlog.lines'
        log.unlink(missing_ok=True)
        (bin_dir / 'jlog').write_text(f'#!/bin/sh\nprintf "%s\\n" "$3" >> "{log}"\nexec "$@"\n')
        (bin_dir / 'jlog').chmod(0o755)
        module = self.dir / '.agents/just/rust.just'
        module.write_text(module.read_text().replace('set positional-arguments',
                                                     'set positional-arguments\nset shell := ["jlog", "sh", "-cu"]'))
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')
        done = self.just('rust', 'verify', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        lines = log.read_text().splitlines()
        for start in ('cargo fmt --all --check', 'cargo clippy --workspace --all-targets', 'cargo check --workspace',
                      'rm -f target/llvm-cov/lcov.info', 'mkdir -p target/llvm-cov', 'cargo llvm-cov test --workspace'):
            self.assertTrue(any(line.startswith(start) for line in lines), f'{start} not in {lines}')
        self.assertTrue(any(line.endswith('coverage target/llvm-cov/lcov.info') for line in lines), lines)
        self.assertTrue(any(line.endswith('steps.py\' docs') for line in lines), lines)


if __name__ == '__main__':
    unittest.main()
