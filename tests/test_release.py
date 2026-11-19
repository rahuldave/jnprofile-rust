"""The release tags: just check runs release-check, CI tags each release, and the script makes the next tag.

scripts/release_tag.py is a copy of the script of the gpa skill of agent_gest_git_skills. These tests check that the
copy works here, and that only the job release of CI can write to the repository.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'release_tag.py'
WORKFLOW = ROOT / '.github' / 'workflows' / 'verify.yml'
# The Git blob of release_tag.py at the commit e03015c of agent_gest_git_skills. A change of the copy comes from there.
UPSTREAM_BLOB = '5830085f986ce05fc2c7dddb680e3e34263a2b1f'
# A git hook sets these for the repository of the hook, and the scratch repository must not use them.
HOOK_VARIABLES = ('GIT_DIR', 'GIT_INDEX_FILE', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY')
# The commit-msg hook of the commit gate sets these for release-check, and the scratch repository must not see them.
RELEASE_VARIABLES = ('JN_RELEASE_MESSAGE', 'JN_RELEASE_AMEND')
# Git with no config of the user, such as a signing key, and a fixed author.
GIT_ENV = {key: value for key, value in os.environ.items() if key not in HOOK_VARIABLES + RELEASE_VARIABLES}
GIT_ENV |= {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}


def run(args, cwd, check=True):
    result = subprocess.run(args, cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f'{args} failed: {result.stdout}{result.stderr}')
    return result


def workflow():
    """The workflow verify.yml as data, read with PyYAML through uv."""
    read = 'import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))'
    out = run(['uv', 'run', '--no-project', '--quiet', '--with', 'pyyaml==6.0.2', 'python', '-c', read, str(WORKFLOW)],
              ROOT).stdout
    return json.loads(out)


def checkout_depth(job):
    return next(step.get('with', {}).get('fetch-depth') for step in job['steps']
                if step.get('uses', '').startswith('actions/checkout@'))


class CheckTest(unittest.TestCase):
    def test_check_runs_release_check(self):
        dump = json.loads(run(['just', '--dump', '--dump-format', 'json'], ROOT).stdout)
        check = dump['recipes']['check']
        dependencies = [dependency['recipe'] for dependency in check['dependencies']]
        lines = [''.join(part if isinstance(part, str) else '' for part in line) for line in check['body']]
        self.assertTrue('release-check' in dependencies or 'just release-check' in lines,
                        'just check does not run release-check')


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.flow = workflow()
        self.jobs = self.flow['jobs']

    def test_only_the_job_release_can_write(self):
        self.assertEqual(self.flow['permissions'], {'contents': 'read'})
        for name, job in self.jobs.items():
            if name != 'release':
                self.assertNotEqual(job.get('permissions', {}).get('contents'), 'write', name)
        self.assertEqual(self.jobs['release']['permissions'], {'contents': 'write'})

    def test_release_runs_after_verify_on_main(self):
        job = self.jobs['release']
        self.assertEqual(job['needs'], 'verify')
        self.assertIn("github.event_name == 'push'", job['if'])
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertEqual(job['steps'][-1]['run'], 'just release')
        self.assertEqual(checkout_depth(job), 0)

    def test_verify_checks_the_release_with_the_whole_history(self):
        job = self.jobs['verify']
        self.assertEqual(checkout_depth(job), 0)
        runs = [step.get('run') for step in job['steps']]
        self.assertTrue('just release-check' in runs or 'just check' in runs, 'CI does not run release-check')


class CopyTest(unittest.TestCase):
    def test_the_script_is_the_copy_of_upstream(self):
        blob = run(['git', 'hash-object', str(SCRIPT)], ROOT).stdout.strip()
        self.assertEqual(blob, UPSTREAM_BLOB, 'scripts/release_tag.py differs from agent_gest_git_skills e03015c')


class HookTest(unittest.TestCase):
    def test_git_env_has_no_variable_of_a_hook(self):
        self.assertFalse(set(HOOK_VARIABLES) & set(GIT_ENV))

    def test_the_scripts_tests_pass_with_the_variables_of_release_check_exported(self):
        """A developer may have exported them. Then every check of the scratch repository would read their file."""
        env = {**os.environ, 'JN_RELEASE_MESSAGE': os.path.join(ROOT, 'no-such-message'), 'JN_RELEASE_AMEND': '1'}
        done = subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_release.ScriptTest'], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class ScriptTest(unittest.TestCase):
    """The copy of release_tag.py tags a scratch repository and pushes the tag to a bare remote."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote, self.repo = base / 'remote.git', base / 'repo'
        run(['git', 'init', '-q', '--bare', str(self.remote)], base)
        run(['git', 'init', '-q', '-b', 'main', str(self.repo)], base)
        run(['git', 'remote', 'add', 'origin', str(self.remote)], self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, message):
        run(['git', 'commit', '-q', '--allow-empty', '-m', message], self.repo)

    def script(self, *args, check=True):
        return run([sys.executable, str(SCRIPT), *args], self.repo, check=check)

    def remote_tags(self):
        out = run(['git', 'ls-remote', '--tags', 'origin'], self.repo).stdout
        return sorted(line.split('refs/tags/')[1] for line in out.splitlines() if not line.endswith('^{}'))

    def test_release_tags_and_pushes_the_next_version(self):
        self.commit('feat: start')
        self.assertIn('tagged v0.1.0', self.script().stdout)
        self.commit('fix: a bug')
        self.assertIn('the next tag is v0.1.1', self.script('--check').stdout)
        self.assertIn('next tag: v0.1.1', self.script('--dry-run').stdout)
        self.assertIn('tagged v0.1.1', self.script().stdout)
        self.assertEqual(self.remote_tags(), ['v0.1.0', 'v0.1.1'])
        self.assertIn('no release', self.script().stdout)
        self.commit('docs: a page')
        self.assertIn('no release', self.script().stdout)
        self.commit('feat: a feature')
        self.assertIn('tagged v0.2.0', self.script().stdout)
        self.assertEqual(self.remote_tags(), ['v0.1.0', 'v0.1.1', 'v0.2.0'])

    def test_a_labelled_bullet_in_the_body_asks_for_no_release(self):
        """A bullet such as "- Removed: x" is not a commit. Only a bullet of a known lower-case type counts."""
        self.commit('feat: start')
        self.script()
        self.commit('chore: tidy\n\n- Removed: the old copies\n- Fixed: a word\n* Feat: not a type')
        self.assertIn('no release', self.script('--check').stdout)
        self.commit('chore: squash\n\n* fix(api): a bug')
        self.assertIn('the next tag is v0.1.1', self.script('--check').stdout)


    def test_check_counts_the_commit_being_made_from_the_message_file(self):
        """The commit-msg hook of the commit gate names the message file in JN_RELEASE_MESSAGE."""
        self.commit('feat: start')
        self.script()
        message = Path(self.tmp.name) / 'COMMIT_EDITMSG'

        def check(text, **extra):
            message.write_text(text)
            env = {**GIT_ENV, 'JN_RELEASE_MESSAGE': str(message), **extra}
            return subprocess.run([sys.executable, str(SCRIPT), '--check'], cwd=self.repo, env=env,
                                  capture_output=True, text=True, check=True).stdout
        self.assertIn('no release', self.script('--check').stdout)
        self.assertIn('the next tag is v0.1.1', check('fix: a bug\n'))
        self.assertIn('and the commit being made', check('fix: a bug\n'))
        self.assertIn('no release', check('chore: tidy\n\n# Please enter the commit message\n# fix: not a line\n'))
        self.assertIn('the next tag is v0.2.0', check('docs: a page\n\n- feat: a bullet\n'))
        self.commit('fix: a bug')
        self.assertIn('no release', check('chore: reword\n', JN_RELEASE_AMEND='1'))


if __name__ == '__main__':
    unittest.main()
