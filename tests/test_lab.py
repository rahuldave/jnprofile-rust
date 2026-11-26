"""The lab of the rust profile: install it into a temporary Cargo project and run its steps.

The lab copies jnterfaces at commit BASE, from the clone in the folder that JNTERFACES names, else in
SKILL_PACKAGES_DIR/jnterfaces, else in jnterfaces next to the main clone of this repository, also from a worktree.
BASE has the rust entry of jnterfaces#34, the programs of jnterfaces#39, the order of #52, and the version floors of
#57, and jlog v0.5.0 of jnterfaces v0.5.2. The fixed commit
keeps the lab the same when the clone moves on. It installs this working tree as the package jnprofile-rust, as
tests/test_install_stack.py of jnterfaces makes its profile packages. The roles tasker and prose come from small
stand-in packages. The lab needs git, just, rustup, cargo, cargo-llvm-cov and uv, and each program of profile.json that
it installs. It skips when a check of such a program fails, so that the installer of the lab never installs a program on
the machine. It runs offline after the first run.
"""
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
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lab'))
import ci_programs  # noqa: E402


def main_clone(repo):
    """The folder of the main clone of the git repository at REPO, which is REPO itself unless REPO is a worktree."""
    try:
        done = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                              capture_output=True, text=True)
    except OSError:
        return repo
    common = Path(done.stdout.strip())
    return common.parent if done.returncode == 0 and common.name == '.git' else repo


# The clone of jnterfaces: JNTERFACES, else jnterfaces in the folder of package clones SKILL_PACKAGES_DIR, as in CI,
# else jnterfaces next to the main clone of this repository, so a worktree finds the same clone.
PACKAGES_DIR = os.environ.get('SKILL_PACKAGES_DIR')
JNTERFACES = Path(os.environ.get('JNTERFACES')
                  or (Path(PACKAGES_DIR) / 'jnterfaces' if PACKAGES_DIR else main_clone(REPO).parent / 'jnterfaces'))
TOOLS = ('git', 'just', 'rustup', 'cargo', 'cargo-llvm-cov', 'uv')
MISSING = [tool for tool in TOOLS if not shutil.which(tool)]
# The merge of jnterfaces#88, the release v0.5.2. It installs the programs of a profile (#39), runs integration
# before coverage-check (#52), runs integration once in just verify (#54), reads the version floors of programs
# (#57), pins smalldocs 0.5.13 (#82), writes rust-toolchain.toml when a project has none (#84), and installs jlog
# v0.5.0 from its release binary.
BASE = '9e1f22dbaa423c4990493b883462663c14564858'
READY = not MISSING and subprocess.run(['git', '-C', str(JNTERFACES), 'cat-file', '-e', f'{BASE}^{{commit}}'],
                                       capture_output=True).returncode == 0
WHY = f'needs {", ".join(MISSING)}' if MISSING else f'needs a clone of jnterfaces with commit {BASE} in {JNTERFACES}'

LIB = '''//! The crate of the lab.

/// Adds two numbers.
///
/// # Returns
/// The sum.
pub fn add(
    a: i32, // the first number
    b: i32, // the second number
) -> i32 {
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


# A stand-in jlog: it logs each line that just gives it, then runs the line. The commands of jlog itself, such as
# jlog step test at the end of a step of the index, do nothing.
STAND_IN_JLOG = """#!/bin/sh
case "$1" in step|skip|pass|session|env|tools) exit 0 ;; esac
printf "%s\\n" "$3" >> "{log}"
exec "$@"
"""


def run(args, cwd, env=None, check=False):
    """Run ARGS in CWD with no input, and return the finished process with its output as text."""
    # The project of the lab is a scratch repository, not the pull request of CI, so its steps run as on a machine.
    env = {k: v for k, v in (env or os.environ).items()
           if k not in ('CI', 'GITHUB_BASE_REF', 'GITHUB_REF', 'GITHUB_EVENT_NAME', 'GITHUB_EVENT_PATH')}
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
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


# What the installer prints before it installs a program.
INSTALLING = 'failed, so the installer runs'
# Since jnterfaces#84, the installer pins rust-toolchain.toml to an exact version, as 1.100.0. A machine that has
# that compiler only as the channel stable, as the runners of GitHub, then installs the pin once: not a program.
PIN_INSTALL = 'rustup show active-toolchain failed, so the installer runs for the rust profile, in the project ' \
              'folder: rustup toolchain install'


def installs(out):
    """Whether the output OUT of an install installs a program, besides the toolchain of the pin it just wrote."""
    if 'Wrote rust-toolchain.toml' in out:
        out = '\n'.join(line for line in out.splitlines() if line != PIN_INSTALL)
    return INSTALLING in out
PROFILE = json.loads((REPO / 'profile.json').read_text())


def missing_programs(names, env=None, pin=None):
    """The programs of NAMES whose check fails, or does not print its expect or meet its floor, in an empty folder.
    With PIN, the folder has a rust-toolchain.toml that pins that toolchain. RUSTUP_AUTO_INSTALL=0 stops rustup from
    installing a missing toolchain during a check."""
    programs = {entry['name']: entry for entry in PROFILE['programs']}
    env = dict(env or os.environ, RUSTUP_AUTO_INSTALL='0')
    missing = []
    with tempfile.TemporaryDirectory() as folder:
        if pin:
            (Path(folder) / 'rust-toolchain.toml').write_text(f'[toolchain]\nchannel = "{pin}"\n')
        for name in names:
            entry = programs[name]
            try:
                done = subprocess.run(shlex.split(entry['check']), cwd=folder, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL, timeout=300, env=env)
            except (OSError, subprocess.TimeoutExpired):
                missing.append(name)
                continue
            if done.returncode or not ci_programs.meets(entry, done.stdout, done.stderr):
                missing.append(name)
    return missing


def doc_entry():
    """The entry of stack.json that docs/jnterfaces-entry.md shows."""
    doc = (REPO / 'docs/jnterfaces-entry.md').read_text()
    return json.loads(re.search(r'```json\n(.*?)```', doc, re.S).group(1))


def make_packages(base):
    """Make the folder of package clones: jnterfaces at BASE, this profile, and the stand-in roles."""
    packages = base / 'packages'
    jn = packages / 'jnterfaces'
    jn.mkdir(parents=True)
    archive = subprocess.run(['git', '-C', str(JNTERFACES), 'archive', BASE], capture_output=True, check=True)
    subprocess.run(['tar', '-x', '-C', str(jn)], input=archive.stdout, check=True)
    run(['git', 'init', '-q'], jn, check=True)
    commit(jn)
    profile = packages / 'jnprofile-rust'
    shutil.copytree(REPO, profile, ignore=shutil.ignore_patterns('.git', 'target', '_site', '__pycache__'))
    run(['git', 'init', '-q'], profile, check=True)
    commit(profile)
    for name, skill in {'agent_gest_git_skills': 'gtw', 'nossaifd': 'nossaifd', 'sklanes': 'lanes'}.items():
        stand_in(packages / name, name, skill)
    return packages


def install(packages, target, *args):
    """Run the installer of the lab copy of jnterfaces on TARGET, and return the finished process."""
    env = {k: v for k, v in os.environ.items() if k not in ('SKILL_PACKAGES_DIR', 'VIRTUAL_ENV')}
    return run([sys.executable, str(packages / 'jnterfaces/scripts/install_stack.py'), '--target', str(target),
                *args], target, env=env)


@unittest.skipUnless(shutil.which('git'), 'needs git')
class MainClone(unittest.TestCase):
    """The lab finds jnterfaces next to the main clone, so a worktree in another folder runs the lab and skips none."""

    def test_each_class_keeps_its_own_skip(self):
        """Guard needs rustup, and MainClone needs only git."""
        source = Path(__file__).read_text()
        self.assertIn("@unittest.skipUnless(shutil.which('rustup'), 'needs rustup')\nclass Guard(", source)
        self.assertIn("@unittest.skipUnless(shutil.which('git'), 'needs git')\nclass MainClone(", source)

    def test_no_git_gives_the_folder_itself(self):
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError('git')):
            self.assertEqual(main_clone(Path('/x')), Path('/x'))

    def test_a_worktree_gives_the_folder_of_its_main_clone(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(os.path.realpath(temp.name))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        clone, tree = root / 'projects/repo', root / 'trees/feature'
        clone.mkdir(parents=True)
        for args in (['init', '-q'], ['commit', '-q', '--allow-empty', '-m', 'one'],
                     ['worktree', 'add', '-q', '-b', 'feature', str(tree)]):
            subprocess.run(['git', *args], cwd=clone, env=env, check=True, capture_output=True)
        self.assertEqual(main_clone(tree), clone)
        self.assertEqual(main_clone(clone), clone)
        (root / 'plain').mkdir()
        self.assertEqual(main_clone(root / 'plain'), root / 'plain')


@unittest.skipUnless(shutil.which('rustup'), 'needs rustup')
class Guard(unittest.TestCase):
    def test_the_guard_finds_a_missing_default_toolchain_and_downloads_nothing(self):
        # rustup 1.29 installs a missing toolchain during a check. The guard turns that off, so a default toolchain
        # that is set but not installed counts as missing. The servers point at a closed port in case it did not.
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'settings.toml').write_text('version = "12"\ndefault_toolchain = "stable"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith('RUSTUP_')}
            env.update(RUSTUP_HOME=home, RUSTUP_DIST_SERVER='http://127.0.0.1:9',
                       RUSTUP_UPDATE_ROOT='http://127.0.0.1:9/rustup')
            self.assertEqual(missing_programs(['rustup', 'toolchain', 'rustfmt', 'llvm-tools'], env),
                             ['toolchain', 'rustfmt', 'llvm-tools'])
            self.assertEqual(os.listdir(home), ['settings.toml'])

    def test_no_check_downloads_a_toolchain_that_the_project_pins_and_lacks(self):
        # Each check of the profile runs in a project that pins a toolchain rustup does not have. The checks of
        # rustup and taplo pass, as neither needs a toolchain. Every other check fails and leaves RUSTUP_HOME as it
        # was: no toolchain, no download. The servers point at a closed port in case one tried.
        # This holds for rustup 1.28.0 or later, the floor of rustup: an older one installs during rustup --version.
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'settings.toml').write_text('version = "12"\ndefault_toolchain = "stable"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith('RUSTUP_')}
            env.update(RUSTUP_HOME=home, RUSTUP_DIST_SERVER='http://127.0.0.1:9',
                       RUSTUP_UPDATE_ROOT='http://127.0.0.1:9/rustup')
            names = [entry['name'] for entry in PROFILE['programs']]
            expected = [name for name in names if name not in ('rustup', 'taplo')]
            if not shutil.which('taplo'):
                expected = [name for name in names if name != 'rustup']
            self.assertEqual(missing_programs(names, env, pin='1.70.0'), expected)
            self.assertEqual(os.listdir(home), ['settings.toml'])


@unittest.skipUnless(os.environ.get('CI', '').strip().lower() not in ('', 'false', '0'), 'only in CI')
class Ci(unittest.TestCase):
    """In CI, the lab must run, not skip: each tool, the clone of jnterfaces and each program are there."""

    def test_ci_has_everything_that_the_lab_needs(self):
        self.assertEqual(MISSING, [])
        self.assertTrue(READY, WHY)
        self.assertEqual(missing_programs([entry['name'] for entry in PROFILE['programs']]), [])


class Workflow(unittest.TestCase):
    """The workflow of CI pins what the lab pins."""

    def test_the_workflow_clones_jnterfaces_at_the_commit_of_the_lab_and_runs_just_check(self):
        text = (REPO / '.github/workflows/verify.yml').read_text()
        self.assertIn(BASE, text)
        self.assertIn('SKILL_PACKAGES_DIR', text)
        self.assertIn('python3 lab/ci_programs.py', text)
        self.assertRegex(text, r'(?m)^\s+run: just check$')

    def test_verify_installs_the_pinned_release_binary_of_jlog(self):
        # install.sh comes from the tag that it installs, so main cannot change what CI runs.
        text = (REPO / '.github/workflows/verify.yml').read_text()
        version = re.search(r'jlog/(v\d+\.\d+\.\d+)/scripts/install\.sh', text).group(1)
        line = (f'curl -fsSL https://raw.githubusercontent.com/rahuldave/jlog/{version}/scripts/install.sh'
                f' | sh -s -- --tag {version}')
        step = re.search(r'(?ms)^      - name: Install jlog[^\n]*\n(.*?)(?=^      - )', text).group(1)
        self.assertIn(line, step)
        # The default shell of a run step has no pipefail, so a failed curl would still pass through sh.
        self.assertRegex(step, r'(?m)^        shell: bash$')
        self.assertIn('echo "$HOME/.local/bin" >> "$GITHUB_PATH"', step)
        # setup-uv comes first, so install.sh makes the environments of the plugins of jlog.
        self.assertLess(text.index('astral-sh/setup-uv@'), text.index(line))
        # jlog is on PATH before just check, whose lab runs the installer of jnterfaces through it.
        self.assertLess(text.index(line), text.index('run: just check'))
        # Nothing builds jlog with cargo, and the cache keeps no build of it.
        self.assertNotRegex(text, r'cargo[^\n]*install[^\n]*rahuldave/jlog')
        self.assertNotIn('.cargo/bin/jlog', text)
        self.assertNotRegex(text, r'key: .*jlog')
        # The version is the one that the installer of jnterfaces at BASE asks for.
        if READY:
            script = subprocess.run(['git', '-C', str(JNTERFACES), 'show', f'{BASE}:scripts/install_stack.py'],
                                    capture_output=True, text=True, check=True).stdout
            self.assertIn(f'--tag {version}', script)

    def test_lint_runs_ruff_at_a_fixed_version(self):
        justfile = (REPO / 'Justfile').read_text()
        self.assertRegex(justfile, r'(?m)^ruff := "uvx --quiet ruff@\d+\.\d+\.\d+"$')
        self.assertRegex(justfile, r'(?m)^lint:\n    \{\{ruff\}\} check \.$')


@unittest.skipUnless(READY, WHY)
class Pin(unittest.TestCase):
    """The docs agree with the commit of jnterfaces that the lab pins. This needs only git and the clone."""
    PAGES = ('README.md', 'docs/index.md', 'docs/jnterfaces-entry.md')

    def test_the_docs_name_the_jlog_version_that_the_installer_of_jnterfaces_needs(self):
        script = subprocess.run(['git', '-C', str(JNTERFACES), 'show', f'{BASE}:scripts/install_stack.py'],
                                capture_output=True, text=True, check=True).stdout
        # The text can run over several lines of the script.
        version = re.search(r"^JLOG_INSTALL = .*?git checkout (v\d+\.\d+\.\d+)", script, re.M | re.S).group(1)
        for page in self.PAGES:
            text = (REPO / page).read_text()
            # Each version such as v0.1.1 in these pages is a version of jlog, also across a line break.
            self.assertEqual(set(re.findall(r'\bv\d+\.\d+\.\d+\b', text)), {version}, page)
            self.assertRegex(text, rf'jlog`?\s+{re.escape(version)}', page)

    def test_the_spec_installs_the_same_jlog_and_no_table_cell_holds_a_pipe(self):
        script = subprocess.run(['git', '-C', str(JNTERFACES), 'show', f'{BASE}:scripts/install_stack.py'],
                                capture_output=True, text=True, check=True).stdout
        version = re.search(r"^JLOG_INSTALL = .*?git checkout (v\d+\.\d+\.\d+)", script, re.M | re.S).group(1)
        spec = (REPO / 'docs/spec.md').read_text()
        row = next(line for line in spec.splitlines() if line.startswith('| `jlog` |'))
        self.assertIn(f'git checkout {version}', row)
        self.assertIn(f'jlog/{version}/scripts/install.sh | sh -s -- --tag {version}', spec)
        # A | in a table cell ends the cell on GitHub, even inside backticks, so the command sits under the table.
        for page in ('README.md', 'docs/index.md', 'docs/spec.md'):
            for line in (REPO / page).read_text().splitlines():
                if line.startswith('|'):
                    self.assertNotIn('install.sh |', line, page)

    def test_the_docs_name_the_commit_that_the_lab_pins(self):
        for page in ('README.md', 'docs/jnterfaces-entry.md'):
            text = (REPO / page).read_text()
            self.assertIn(f'`{BASE[:7]}`', text, page)
            # No other commit of the lab: the old pins are gone.
            for old in ('f9e66f7', '7fe6811', '4b9b52d', '338ca39', '8d8885f'):
                self.assertNotIn(old, text, page)
        self.assertIn('`2cfb176`', (REPO / 'docs/jnterfaces-entry.md').read_text())

    def test_the_floor_check_of_the_lab_agrees_with_tools_check_of_jnterfaces(self):
        # ci_programs.meets copies the rules of .agents/tools_check.py at BASE. Run both on the same outputs.
        source = subprocess.run(['git', '-C', str(JNTERFACES), 'show', f'{BASE}:.agents/tools_check.py'],
                                capture_output=True, text=True, check=True).stdout
        tools_check = {}
        exec(compile(source, 'tools_check.py', 'exec'), tools_check)
        cases = [('>=0.10.0', 'taplo 0.10.0\n', ''), ('>=0.10.0', 'taplo 0.9.9\n', ''), ('>=1.2', 'x 1.2\n', ''),
                 ('>=1.102.0', 'rustc 1.102.0-nightly (3ef0c22cd 2026-11-11)\n', ''), ('>=1.0.0', '', 'v 1.2.3\n'),
                 ('>=1.0.0', 'none\n', 'none\n'), ('>=1.0.0', 'x 0.9.0\n', 'x 1.2.3\n'), ('>=2.1.4', '11.2.1.4', '')]
        for floor, output, errors in cases:
            with self.subTest(floor=floor, output=output, errors=errors):
                found = tools_check['found_version'](output) or tools_check['found_version'](errors)
                theirs = found is not None and found >= tools_check['parse_floor'](floor)
                self.assertEqual(ci_programs.meets({'version': floor}, output, errors), theirs)


class Installs(unittest.TestCase):
    """installs: the install of the toolchain of a pin that the installer just wrote is not a program."""

    def test_only_the_pin_that_the_installer_wrote_is_allowed(self):
        wrote = 'Wrote rust-toolchain.toml with the channel 1.100.0.'
        self.assertFalse(installs(f'{wrote}\n{PIN_INSTALL}\ninfo: installed'))
        self.assertTrue(installs(PIN_INSTALL))
        self.assertTrue(installs(f'{wrote}\n{PIN_INSTALL}\ncargo-deny --version failed, so the installer runs'))
        self.assertFalse(installs('nothing to install'))


@unittest.skipUnless(READY, WHY)
class RustLab(unittest.TestCase):
    """Each test gets a copy of one Cargo project that has the profile installed and one commit."""

    @classmethod
    def setUpClass(cls):
        """Make the packages, a Cargo project with tested and documented code, install the profile, and build."""
        needed = PROFILE['base']['programs'] + [name for tool in PROFILE['default_tools']
                                                for name in PROFILE['tools'][tool]['programs']]
        missing = missing_programs(needed)
        if missing:
            raise unittest.SkipTest(f'needs the programs {", ".join(missing)}, which the lab must not install')
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

    def test_installed_parser_accepts_declarations_and_retains_runtime_files(self):
        payload = self.dir / '.agents/just/rust'
        self.assertTrue((payload / 'declarations.py').is_file())
        self.assertTrue((payload / 'compiler_evidence.py').is_file())
        recipe = (payload / 'base.just').read_text().split('rust_py := "', 1)[1].split('"', 1)[0]
        command = shlex.split(recipe)
        (self.dir / 'src/declaration.rs').write_text('use clap::Args; #[derive(Args)] pub struct A;')
        (self.dir / 'src/orphan.rs').write_text('pub fn unwired() {}')
        probe = ('import sys; sys.path.insert(0, ' + repr(str(payload)) + '); import declarations; '
                 'assert declarations.classify("src/declaration.rs")[0] == "known Clap declarations"; '
                 'assert declarations.classify("src/orphan.rs")[0] is None')
        done = run([*command, '-I', '-c', probe], self.dir)
        self.assertEqual(done.returncode, 0, self.out(done))

    def test_current_compiler_module_proof_pairs_declarations_with_absent_source_failures(self):
        cases = {
            'mounted': ('pub struct Declaration;', 'pub mod candidate;', 0),
            'unmounted': ('pub struct Declaration;', '', 1),
            'include_str': ('pub struct Declaration;',
                            'pub const DATA: &str = include_str!("candidate.rs");', 1),
            'include_bytes': ('pub struct Declaration;',
                              'pub const DATA: &[u8] = include_bytes!("candidate.rs");', 1),
            'cfg_disabled': ('pub struct Declaration;', '#[cfg(any())] pub mod candidate;', 1),
            'cfg_disabled_data': ('pub struct Declaration;',
                                  '#[cfg(any())] pub mod candidate;\n'
                                  'pub const DATA: &str = include_str!("candidate.rs");', 1),
            'executable': ('pub fn uncalled() -> u8 { 42 }', 'pub mod candidate;', 1),
            'dead': ('pub fn uncalled() -> u8 { 42 }', '', 1),
            'unknown': ('#[repr(C)] pub struct Declaration;', 'pub mod candidate;', 1),
        }
        for name, (source, edge, expected) in cases.items():
            with self.subTest(name=name):
                self.reset()
                (self.dir / 'src/candidate.rs').write_text(source)
                (self.dir / 'src/lib.rs').write_text(LIB + '\n' + edge + '\n')
                done = self.just('coverage-check')
                self.assertEqual(done.returncode, expected, self.out(done))
                if not expected:
                    self.assertIn('current-run compiled module', done.stdout)
                else:
                    self.assertIn('src/candidate.rs', done.stdout)

    def test_actual_gossil_derive_shapes_and_valid_unknown_helpers(self):
        # Match the exact failing source rather than a declaration-shaped stand-in.
        # Pins match Gossil's recorded failing Cargo.lock; no program is installed.
        manifest = (self.dir / 'Cargo.toml').read_text()
        (self.dir / 'Cargo.toml').write_text(manifest + '\n'
            'serde = { version = "=1.0.229", features = ["derive"] }\n'
            'getset = "=0.1.7"\npulldown-cmark = "=0.13.4"\nserde_json = "=1.0.154"\n')
        done = self.just('setup')
        self.assertEqual(done.returncode, 0, self.out(done))
        commit(self.dir, 'pin actual declaration fixture dependencies')
        fixtures = REPO / 'tests/fixtures/declarations'
        sources = {'src/ui/config.rs': (fixtures / 'gossil_config.rs').read_text(),
                   'src/ui/markdown/block.rs': (fixtures / 'gossil_block.rs').read_text(),
                   'src/ui.rs': 'pub mod config; pub mod markdown;\n',
                   'src/ui/markdown.rs': 'pub mod block;\n',
                   'src/ui/config/colors.rs': '#[derive(Clone, Debug, Default, serde::Deserialize, '
                       'PartialEq, serde::Serialize)] pub struct Settings;\n'}
        for path, source in sources.items():
            target = self.dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        (self.dir / 'src/lib.rs').write_text(LIB + '\npub mod ui;\n')
        (self.dir / 'tests/derived.rs').write_text('''#[test]
fn settings_default_roundtrip_and_getter() {
    let settings: app::ui::config::Settings = serde_json::from_str("{}").unwrap();
    assert_eq!(settings, app::ui::config::Settings::default());
    assert_eq!(settings.colors(), &app::ui::config::colors::Settings::default());
    let encoded = serde_json::to_string(&settings).unwrap();
    assert_eq!(serde_json::from_str::<app::ui::config::Settings>(&encoded).unwrap(), settings);
}
''')
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('src/ui/config.rs: absent from LCOV; known derived declarations', done.stdout)
        self.assertIn('src/ui/markdown/block.rs: absent from LCOV; declarations', done.stdout)
        # Both forms compile and keep the behavior test passing. The missing-file
        # gate must still reject the unsupported source attribute itself.
        for path, original, changed in (
            ('src/ui/config.rs', '#[serde(default)]', '#[serde(default = "Default::default")]'),
            ('src/ui/markdown/block.rs', '#[allow(clippy::enum_variant_names)]',
             '#[allow(clippy::enum_variant_names, dead_code)]'),
        ):
            with self.subTest(path=path):
                (self.dir / path).write_text(sources[path].replace(original, changed))
                done = self.just('coverage-check')
                self.assertNotEqual(done.returncode, 0, self.out(done))
                self.assertIn(path + ': a changed file that is absent from the coverage report', done.stdout)
                self.assertIn('unknown or executable attribute', done.stdout)
                (self.dir / path).write_text(sources[path])

    def test_the_installer_finds_the_profile_by_cargo_toml_and_writes_its_files(self):
        self.assertIn('Profile(s) found from the files of the project: rust', self.installed.stderr)
        saved = json.loads((self.app / '.agents/stack.json').read_text())
        self.assertEqual(saved['profiles'], ['rust'])
        # The installer copies the code and tests globs of the profile into the record (jnterfaces#34 and #46).
        self.assertEqual(saved['code'], {'rust': PROFILE['code']})
        self.assertEqual(saved['tests'], {'rust': PROFILE['tests']})
        # jnterfaces#52 runs integration before coverage-check, so just check reads both reports of one run.
        self.assertLess(saved['steps'].index('integration'), saved['steps'].index('coverage-check'))
        self.assertIn('integration', saved['provided_steps'])
        # The order of the tools in profile.json gives the module verify integration, test, then coverage-check.
        module = (self.app / '.agents/just/rust.just').read_text()
        verify = next(line for line in module.splitlines() if line.startswith('verify:')).split()
        self.assertLess(verify.index('integration'), verify.index('test'))
        self.assertLess(verify.index('test'), verify.index('coverage-check'))
        for path in ('.agents/just/rust/steps.py', '.agents/skills/rust-profile/SKILL.md',
                     *(f'.agents/targets/rust/{name}.md'
                       for name in ('review', 'test-review', 'update-docs', 'prose'))):
            self.assertTrue((self.app / path).is_file(), path)
        self.assertIn('/target/', (self.app / '.gitignore').read_text().splitlines())
        self.assertIn('/target/', (self.app / '.worktreedisposable').read_text().splitlines())
        self.assertIn('lint for rust needs no packages', self.installed.stderr)
        summary = self.just('--summary').stdout.split()
        for step in ('fmt-check', 'lint', 'typecheck', 'test', 'integration', 'coverage-check', 'docstring-check',
                     'test-paths', 'api-check', 'api-prose-check'):
            self.assertIn(step, summary)
        self.assertNotIn('_api-prose-check', summary)

    def test_with_integration_only_in_verify_the_tests_of_integration_run_once_in_verify(self):
        project = self.base / 'late'
        (project / 'src').mkdir(parents=True)
        (project / 'tests').mkdir()
        self.addCleanup(shutil.rmtree, project, True)
        for name in ('Cargo.toml', 'Cargo.lock', 'src/lib.rs', 'tests/api.rs'):
            shutil.copy(self.fresh / name, project / name)
        git(project, 'init', '-q')
        done = install(self.packages, project, '--defaults', '--answer', 'integration=verify')
        self.assertEqual(done.returncode, 0, self.out(done))
        saved = json.loads((project / '.agents/stack.json').read_text())
        self.assertNotIn('integration', saved['steps'])
        index = (project / '.agents/just/index.just').read_text()
        self.assertIn('\nverify:\n', index)
        block = index.split('\nverify:\n', 1)[1].split('\n\n')[0]
        self.assertIn('rust verify', block)
        # jnterfaces#54: the module verify runs integration, so the root verify adds no second run.
        self.assertNotIn('integration', block)
        commit(project, 'start')
        done = run(['just', 'verify'], project)
        self.assertEqual(done.returncode, 0, self.out(done))
        runs = [line for line in done.stdout.splitlines()
                if line.startswith('cargo llvm-cov test') and 'integration.part0.lcov' in line]
        self.assertEqual(len(runs), 1, runs)

    def test_jnterfaces_has_the_entry_of_the_doc_and_the_installer_finds_every_program(self):
        stack = json.loads((self.packages / 'jnterfaces/stack.json').read_text())
        self.assertEqual({'rust': stack['profiles']['rust']}, doc_entry())
        self.assertEqual(self.installed.returncode, 0, self.out(self.installed))
        self.assertFalse(installs(self.out(self.installed)), self.out(self.installed))
        done = install(self.packages, self.app, '--check')
        self.assertNotIn('Missing program', self.out(done))
        self.assertNotIn('are not checked', self.out(done))

    def test_without_rustup_the_installer_names_rustup_and_writes_nothing(self):
        target = self.base / 'norustup'
        (target / 'src').mkdir(parents=True)
        (target / 'Cargo.toml').write_text(CARGO)
        (target / 'src/lib.rs').write_text(LIB)
        git(target, 'init', '-q')
        commit(target)
        # A PATH with the programs of jnterfaces and the system, and with no rustup and no cargo.
        bin_dir = self.base / 'norustup-bin'
        bin_dir.mkdir()
        for name in ('git', 'just', 'uv', 'jlog'):
            if shutil.which(name):
                (bin_dir / name).symlink_to(shutil.which(name))
        env = {k: v for k, v in os.environ.items() if k not in ('SKILL_PACKAGES_DIR', 'VIRTUAL_ENV')}
        env['PATH'] = f'{bin_dir}:/usr/bin:/bin'
        if shutil.which('rustup', path=env['PATH']):
            self.skipTest('rustup is in /usr/bin or /bin')
        done = run([sys.executable, str(self.packages / 'jnterfaces/scripts/install_stack.py'), '--target', str(target),
                    '--profile', 'rust', '--defaults'], target, env=env)
        self.assertNotEqual(done.returncode, 0, self.out(done))
        self.assertIn('https://rustup.rs/', self.out(done))
        self.assertEqual(git(target, 'status', '--porcelain', '--ignored'), '')

    def test_a_new_folder_gets_cargo_init_from_the_profile(self):
        target = self.base / 'empty'
        target.mkdir()
        git(target, 'init', '-q')
        done = install(self.packages, target, '--profile', 'rust', '--defaults')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('cargo init --vcs none', done.stderr)
        self.assertTrue((target / 'Cargo.toml').is_file())

    def test_a_clean_project_passes_every_step_and_leaves_no_file_that_git_does_not_ignore(self):
        for step in ('fmt-check', 'lint', 'typecheck', 'test', 'coverage-check', 'docstring-check', 'api-check',
                     'verify'):
            done = self.just(step)
            self.assertEqual(done.returncode, 0, f'{step}:\n{self.out(done)}')
        self.assertEqual(git(self.dir, 'status', '--porcelain', '--untracked-files=all'), '')

    def test_api_check_fails_on_an_item_with_no_comments_and_the_install_brings_the_rule_of_the_gate(self):
        self.assertTrue((self.dir / '.agents/just/rust/api_rule.py').is_file())
        (self.dir / 'src/lib.rs').write_text(LIB.replace('///\n/// # Returns\n/// The sum.\n', ''))
        done = self.just('api-check')
        self.assertNotEqual(done.returncode, 0, self.out(done))
        self.assertIn('app:add: no return docment', self.out(done))
        self.assertIn('# Returns section', self.out(done))
        done = self.just('verify')
        self.assertNotEqual(done.returncode, 0, self.out(done))
        self.assertRegex(self.out(done), r'(?i)recipe `api-check` failed')
        self.assertIn('app:add: no return docment', self.out(done))

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
            'build.rs': ('fn main() {\n    if std::env::var("NEVER_SET_X").is_ok() {\n'
                         '        println!("x");\n    }\n}\n',
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
            f'{LIB}\n/// Triples a number.\n#[cfg(feature = "extra")]\n'
            'pub fn triple(a: i32) -> i32 {\n    a * 3\n}\n')
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
        # jlog prints the record of the step that passed. Nothing else is on the output: no warning of a missing doc.
        self.assertEqual([line for line in done.stdout.splitlines() if not line.startswith('{"kind":"step"')], [])

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

    def test_test_fails_on_a_failed_unit_test_and_verify_stops(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('5);', '6);'))
        self.assertNotEqual(self.just('test').returncode, 0)
        self.assertEqual(self.just('integration').returncode, 0)
        self.assertNotEqual(self.just('verify').returncode, 0)

    def test_integration_fails_on_a_failed_test_in_the_folder_tests_and_test_does_not_run_it(self):
        (self.dir / 'tests/api.rs').write_text(API_TEST.replace('2);', '3);'))
        done = self.just('test')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertNotIn('add_works_from_outside', self.out(done))
        done = self.just('integration')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('add_works_from_outside', self.out(done))
        self.assertNotIn('add_sums_two_numbers', self.out(done))
        self.assertNotEqual(self.just('verify').returncode, 0)

    def add_outside_only(self):
        """Add the module extra, which only a test in the folder tests runs, and commit nothing."""
        self.add_extra()
        (self.dir / 'tests/extra.rs').write_text('//! The test of extra.\n\n#[test]\nfn double_works() {\n'
                                                 '    assert_eq!(app::extra::double(2), 4);\n}\n')

    def set_steps(self, root, drop=()):
        """Take the steps DROP out of the list of steps of .agents/stack.json in ROOT, as the setup answer does."""
        stack = root / '.agents/stack.json'
        data = json.loads(stack.read_text())
        data['steps'] = [step for step in data['steps'] if step not in drop]
        stack.write_text(json.dumps(data, indent=2) + '\n')

    def test_a_line_that_only_integration_runs_passes_coverage_check_while_integration_is_in_just_check(self):
        self.assertIn('integration', json.loads((self.dir / '.agents/stack.json').read_text())['steps'])
        self.add_outside_only()
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertTrue((self.dir / 'target/llvm-cov/integration.lcov').is_file())
        self.set_steps(self.dir, drop=['integration'])
        done = self.just('coverage-check')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('integration is not in the steps of just check', done.stdout)
        self.assertIn('src/extra.rs', done.stdout)
        self.assertIn('Missing lines 4-6', done.stdout)

    def test_on_the_first_commit_a_line_that_only_integration_runs_passes(self):
        fresh = self.base / 'first-integration'
        shutil.copytree(self.fresh, fresh, symlinks=True)
        self.addCleanup(shutil.rmtree, fresh, True)
        (fresh / 'src/extra.rs').write_text(EXTRA)
        (fresh / 'src/lib.rs').write_text(f'{LIB}\n/// The module with untested code.\npub mod extra;\n')
        (fresh / 'tests/extra.rs').write_text('#[test]\nfn double_works() {\n'
                                              '    assert_eq!(app::extra::double(2), 4);\n}\n')
        done = self.just('coverage-check', cwd=fresh)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.set_steps(fresh, drop=['integration'])
        done = self.just('coverage-check', cwd=fresh)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('src/extra.rs:4: no test runs this line', done.stdout)

    def test_a_test_target_with_test_false_does_not_run_and_extra_arguments_choose_the_targets(self):
        (self.dir / 'tests/slow.rs').write_text('#[test]\nfn slow_fails() {\n    panic!("slow");\n}\n')
        (self.dir / 'Cargo.toml').write_text((self.dir / 'Cargo.toml').read_text()
                                             + '\n[[test]]\nname = "slow"\ntest = false\n')
        done = self.just('integration')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertNotIn('slow_fails', self.out(done))
        done = self.just('integration', '--test', 'slow')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('slow_fails', self.out(done))
        done = self.just('test', '--test', 'api')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('add_works_from_outside', self.out(done))
        self.assertNotIn('add_sums_two_numbers', self.out(done))
        done = self.just('test', '-p', 'app', '--lib')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('add_sums_two_numbers', self.out(done))
        self.assertNotIn('add_works_from_outside', self.out(done))

    def test_a_line_that_only_the_build_without_tests_has_fails_when_no_test_runs_it(self):
        (self.dir / 'src/lib.rs').write_text(LIB.replace('    a + b\n', '    #[cfg(not(test))]\n    if a == -100 {\n'
                                                         '        return 0;\n    }\n    a + b\n'))
        done = self.just('coverage-check')
        self.assertNotEqual(done.returncode, 0, self.out(done))
        self.assertIn('src/lib.rs', done.stdout)
        self.assertIn('Missing lines', done.stdout)

    def test_a_project_with_no_test_target_gets_an_empty_report_of_integration(self):
        (self.dir / 'tests/api.rs').unlink()
        done = self.just('integration')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('the workspace has no test target', done.stdout)
        self.assertEqual((self.dir / 'target/llvm-cov/integration.lcov').read_text(), '')
        done = self.just('coverage-check')
        self.assertEqual(done.returncode, 0, self.out(done))

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
        # An enclosing guarded test run may export JLOG_RUN. These independent
        # just calls deliberately represent separate runs, regardless of it.
        for step in ('test', 'integration'):
            report = self.dir / f'target/llvm-cov/{step}.lcov'
            self.assertEqual(self.just(step, env=dict(os.environ, JLOG_RUN=f'producer-{step}')).returncode, 0)
            report.write_text('SF:old\nend_of_record\n')
        done = self.just('coverage-check', env=dict(os.environ, JLOG_RUN='standalone-consumer'))
        self.assertEqual(done.returncode, 0, self.out(done))
        for step in ('test', 'integration'):
            report = (self.dir / f'target/llvm-cov/{step}.lcov').read_text()
            self.assertIn('SF:', report)
            self.assertNotIn('SF:old', report)
            self.assertIn(f'no report of {step} for this run and this tree, so the tests run now', done.stdout)

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
        self.assertEqual(self.just('test-paths', 'tests/api.rs').returncode, 1)
        (self.dir / 'tests/api.rs').write_text(API_TEST.replace('2);', '"2");'))
        self.assertEqual(self.just('test-paths', 'tests/api.rs').returncode, 1)
        (self.dir / 'tests/api.rs').write_text(API_TEST)
        self.assertEqual(self.just('test-paths', 'tests/nope.rs').returncode, 2)
        done = self.just('test-paths')
        self.assertEqual(done.returncode, 5, self.out(done))
        self.assertNotIn('cargo test', done.stdout)
        manifest = (self.dir / 'Cargo.toml').read_text()
        (self.dir / 'Cargo.toml').write_text(manifest.replace('version = "0.1.0"', 'version = "0.2.0"', 1))
        done = self.just('test-paths', 'tests/api.rs')
        self.assertEqual(done.returncode, 2, self.out(done))
        (self.dir / 'Cargo.toml').write_text(manifest)
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

    def test_test_paths_reads_target_names_and_the_result_of_each_binary_from_cargo(self):
        manifest = (self.dir / 'Cargo.toml').read_text()
        (self.dir / 'tests/bar.rs').write_text(API_TEST)
        (self.dir / 'tests/plain.rs').write_text('//! Fails by its exit code.\n\n'
                                                 'fn main() {\n    std::process::exit(1);\n}\n')
        (self.dir / 'Cargo.toml').write_text(manifest + '\n[[test]]\nname = "foo"\npath = "tests/bar.rs"\n'
                                             '\n[[test]]\nname = "plain"\npath = "tests/plain.rs"\nharness = false\n')
        leaks = API_TEST.replace('#[test]', '#[test]\nfn leaks() {\n'
                                 '    std::process::Command::new("echo").arg("     Running x").status().unwrap();\n'
                                 '}\n\n#[test]', 1)
        (self.dir / 'tests/bar.rs').write_text(leaks)
        done = self.just('test-paths', 'tests/bar.rs')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('--test foo', done.stdout)
        self.assertIn('     Running x', done.stdout)
        done = self.just('test-paths', 'tests/plain.rs')
        self.assertEqual(done.returncode, 1, self.out(done))
        quits = API_TEST.replace('#[test]', '#[test]\nfn quits() {\n    std::process::exit(0);\n}\n\n#[test]', 1)
        (self.dir / 'tests/bar.rs').write_text(quits)
        done = self.just('test-paths', 'tests/bar.rs')
        self.assertEqual(done.returncode, 2, self.out(done))
        self.assertIn('stopped before it gave its result, so the run ended early', done.stdout)
        (self.dir / 'tests/bar.rs').write_text(quits.replace('exit(0)', 'exit(1)'))
        done = self.just('test-paths', 'tests/bar.rs')
        self.assertEqual(done.returncode, 2, self.out(done))
        (self.dir / 'Cargo.toml').write_text(manifest.replace('[package]\n', '[package]\nautotests = false\n', 1)
                                             + '\n[[test]]\nname = "foo"\npath = "tests/bar.rs"\n')
        done = self.just('test-paths', 'tests/api.rs')
        self.assertEqual(done.returncode, 5, self.out(done))
        self.assertIn('skips tests/api.rs, which is no test target of its package', done.stdout)

    def test_the_toml_tool_checks_and_formats_the_toml_files(self):
        missing = missing_programs(PROFILE['tools']['toml']['programs'])
        if missing:
            self.skipTest(f'needs the programs {", ".join(missing)}, which the lab must not install')
        done = install(self.packages, self.dir, 'toml')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(installs(self.out(done)), self.out(done))
        self.assertTrue((self.dir / '.agents/just/rust/toml.just').is_file())
        done = self.just('rust', 'toml-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        manifest = (self.dir / 'Cargo.toml').read_text()
        (self.dir / 'Cargo.toml').write_text(manifest.replace('name = "app"', 'name="app"'))
        self.assertNotEqual(self.just('rust', 'toml-check').returncode, 0)
        # Unsorted features, and an array long enough to wrap, which cargo-sort and taplo would indent each its own way.
        long = ', '.join(f'"feature-number-{n}"' for n in range(7))
        (self.dir / 'Cargo.toml').write_text(manifest + f'\n[features]\nz = []\na = []\nall = [{long}]\n'
                                             + ''.join(f'feature-number-{n} = []\n' for n in range(7)))
        self.assertNotEqual(self.just('rust', 'toml-check').returncode, 0)
        done = self.just('rust', 'toml-fmt')
        self.assertEqual(done.returncode, 0, self.out(done))
        done = self.just('rust', 'toml-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        # A TOML file that git ignores, such as one that cargo package writes into target/, is never read.
        (self.dir / 'target/package').mkdir(parents=True, exist_ok=True)
        (self.dir / 'target/package/bad.toml').write_text('a="b"\n')
        done = self.just('rust', 'toml-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertNotIn('target/package', done.stdout)
        self.assertNotIn('--config', done.stdout)
        (self.dir / '.config').mkdir()
        (self.dir / '.config/tomlfmt.toml').write_text('always_trailing_comma = false\n')
        done = self.just('rust', 'toml-check')
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('--config .config/tomlfmt.toml', done.stdout)

    def test_coverage_check_reads_the_report_of_test_only_in_the_same_run_and_tree(self):
        report = self.dir / 'target/llvm-cov/test.lcov'
        env = dict(os.environ, JLOG_RUN='run-one')
        self.assertEqual(self.just('test', env=env).returncode, 0)
        self.assertEqual(self.just('integration', env=env).returncode, 0)
        before = report.stat().st_mtime_ns
        done = self.just('coverage-check', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('the tests of test in this run, run-one, wrote the report, so they do not run again', done.stdout)
        self.assertIn('the tests of integration in this run, run-one, wrote the report', done.stdout)
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
        (bin_dir / 'jlog').write_text(STAND_IN_JLOG.format(log=log))
        (bin_dir / 'jlog').chmod(0o755)
        # The installer writes the shell into the module, as the spec says. The stand-in comes first on PATH.
        self.assertIn('set shell := ["jlog", "sh", "-cu"]',
                      (self.dir / '.agents/just/rust.just').read_text().splitlines())
        justfile = self.dir / 'Justfile'
        # The installer writes the line of the run ID. A root recipe has the two steps, as just check will have
        # them. The index runs each in an inner just call.
        self.assertIn('export JLOG_RUN := env("JLOG_RUN", uuid())', justfile.read_text().splitlines())
        justfile.write_text(justfile.read_text() + '\ngates: test integration coverage-check\n')
        env = {k: v for k, v in os.environ.items() if k != 'JLOG_RUN'}
        env['PATH'] = f'{bin_dir}:{env["PATH"]}'
        done = self.just('gates', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        self.assertIn('so they do not run again', done.stdout)
        runs = [line for line in done.stdout.splitlines() if line.startswith('cargo llvm-cov test')]
        self.assertEqual(len(runs), 2, runs)
        self.assertTrue(runs[0].endswith('--workspace --lib --lcov --output-path target/llvm-cov/test.part0.lcov'),
                        runs)
        self.assertTrue(runs[1].endswith('--workspace --test api --lcov --output-path '
                                         'target/llvm-cov/integration.part0.lcov'), runs)

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
        (project / 'src/lib.rs').write_text(f'{LIB}\n/// The generated code.\npub mod gen;\n'
                                            '/// More.\npub mod extra;\n')
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
        (bin_dir / 'jlog').write_text(STAND_IN_JLOG.format(log=log))
        (bin_dir / 'jlog').chmod(0o755)
        # The installer writes the shell into the module, as the spec says. The stand-in comes first on PATH.
        self.assertIn('set shell := ["jlog", "sh", "-cu"]',
                      (self.dir / '.agents/just/rust.just').read_text().splitlines())
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')
        done = self.just('rust', 'verify', env=env)
        self.assertEqual(done.returncode, 0, self.out(done))
        lines = log.read_text().splitlines()
        for start in ('cargo fmt --all --check', 'cargo clippy --workspace --all-targets', 'cargo check --workspace'):
            self.assertTrue(any(line.startswith(start) for line in lines), f'{start} not in {lines}')
        for end in ("steps.py' run test \"$@\"", "steps.py' run integration \"$@\"", "steps.py' coverage",
                    "steps.py' fresh integration || " "uv run"):
            self.assertTrue(any(line.endswith(end) or end in line for line in lines), f'{end} not in {lines}')
        self.assertTrue(any(line.endswith('steps.py\' docs') for line in lines), lines)


if __name__ == '__main__':
    unittest.main()
