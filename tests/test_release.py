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
# The Git blob of release_tag.py at the commit 26ef730 of agent_gest_git_skills. A change of the copy comes from there.
UPSTREAM_BLOB = '7869b962ca04da0efb9d6756c79a15da587fcaa8'
# A git hook sets these for the repository of the hook, and the scratch repository must not use them.
HOOK_VARIABLES = ('GIT_DIR', 'GIT_INDEX_FILE', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY')
# The commit-msg hook of the commit gate sets these for release-check, and the scratch repository must not see them.
RELEASE_VARIABLES = ('JN_RELEASE_MESSAGE', 'JN_RELEASE_AMEND', 'JN_RELEASE_CLEANUP', 'JN_RELEASE_EDITOR',
                     'GIT_EDITOR')
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

    def test_only_the_jobs_release_and_docs_can_write(self):
        self.assertEqual(self.flow['permissions'], {'contents': 'read'})
        for name, job in self.jobs.items():
            if name not in ('release', 'docs'):
                self.assertNotEqual(job.get('permissions', {}).get('contents'), 'write', name)
        self.assertEqual(self.jobs['release']['permissions'], {'contents': 'write'})
        self.assertEqual(self.jobs['docs']['permissions'], {'contents': 'write'})

    def test_docs_publishes_the_site_after_verify_on_main(self):
        """The site is built from each push to main, so gh-pages does not fall behind the docs."""
        job = self.jobs['docs']
        self.assertEqual(job['needs'], 'verify')
        self.assertIn("github.event_name == 'push'", job['if'])
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        last = job['steps'][-1]
        self.assertEqual(last['run'].splitlines()[-1], 'just docs-publish')
        self.assertEqual(last['shell'], 'bash')
        names = ' '.join(step.get('run', '') for step in job['steps'])
        self.assertIn('git config user.name', names)
        self.assertIn('git config user.email', names)
        self.assertEqual(job['concurrency']['group'], 'docs')

    def test_docs_publishes_the_tip_of_main_from_an_older_commit(self):
        """An older run that ends last, or that cancels a newer one, still publishes the newest site."""
        script = self.jobs['docs']['steps'][-1]['run']
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, repo, bin_dir = base / 'remote.git', base / 'repo', base / 'bin'
            run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], base)
            run(['git', 'clone', '-q', str(remote), str(repo)], base)
            run(['git', 'commit', '-q', '--allow-empty', '-m', 'a'], repo)
            run(['git', 'push', '-q', 'origin', 'HEAD:main'], repo)
            bin_dir.mkdir()
            (bin_dir / 'just').write_text('#!/bin/sh\necho "just $* at $(git rev-parse HEAD)"\n')
            (bin_dir / 'just').chmod(0o755)
            env = {**GIT_ENV, 'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}
            old = run(['git', 'rev-parse', 'HEAD'], repo).stdout.strip()
            run(['git', 'commit', '-q', '--allow-empty', '-m', 'b'], repo)
            run(['git', 'push', '-q', 'origin', 'HEAD:main'], repo)
            tip = run(['git', 'rev-parse', 'HEAD'], repo).stdout.strip()
            run(['git', 'checkout', '-q', '--detach', old], repo)
            out = subprocess.run(['bash', '-e', '-c', script], cwd=repo, env=env, capture_output=True, text=True,
                                 check=True).stdout
            self.assertIn(f'just docs-publish at {tip}', out)

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
        self.assertEqual(blob, UPSTREAM_BLOB, 'scripts/release_tag.py differs from agent_gest_git_skills 26ef730')


class HookTest(unittest.TestCase):
    def test_git_env_has_no_variable_of_a_hook(self):
        self.assertFalse(set(HOOK_VARIABLES) & set(GIT_ENV))

    def test_the_scripts_tests_pass_with_the_variables_of_release_check_exported(self):
        """A developer may have exported them. Then every check of the scratch repository would read their file."""
        env = {**os.environ, 'JN_RELEASE_MESSAGE': os.path.join(ROOT, 'no-such-message'), 'JN_RELEASE_AMEND': '1',
               'JN_RELEASE_CLEANUP': 'verbatim', 'JN_RELEASE_EDITOR': 'unknown', 'GIT_EDITOR': ':'}
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

    def test_check_stops_an_amend_of_the_release_and_skips_a_merge(self):
        """The tag stays on the old commit after an amend, and the release skips merge commits."""
        self.commit('feat: start')
        self.script()
        message = Path(self.tmp.name) / 'COMMIT_EDITMSG'
        message.write_text('fix: a bug\n')
        env = {**GIT_ENV, 'JN_RELEASE_MESSAGE': str(message)}

        def check(**extra):
            return subprocess.run([sys.executable, str(SCRIPT), '--check'], cwd=self.repo, env={**env, **extra},
                                  capture_output=True, text=True)
        stopped = check(JN_RELEASE_AMEND='1')
        self.assertEqual(stopped.returncode, 1, stopped.stdout)
        self.assertIn('HEAD is the release v0.1.0', stopped.stderr + stopped.stdout)
        maybe = check(JN_RELEASE_AMEND='maybe')
        self.assertEqual(maybe.returncode, 1, maybe.stdout)
        self.assertIn('it may be an amend', maybe.stderr + maybe.stdout)
        head = run(['git', 'rev-parse', 'HEAD'], self.repo).stdout.strip()
        (self.repo / '.git/MERGE_HEAD').write_text(head + '\n')
        self.assertIn('no release', check().stdout)
        (self.repo / '.git/MERGE_HEAD').unlink()
        self.assertIn('the next tag is v0.1.1', check().stdout)

    def test_without_an_editor_the_comment_lines_count(self):
        """In a hook, git sets GIT_EDITOR to : for git commit -m, and then keeps the comment lines."""
        self.commit('feat: start')
        self.script()
        message = Path(self.tmp.name) / 'COMMIT_EDITMSG'
        env = {**GIT_ENV, 'JN_RELEASE_MESSAGE': str(message)}

        def check(**extra):
            return subprocess.run([sys.executable, str(SCRIPT), '--check'], cwd=self.repo, env={**env, **extra},
                                  capture_output=True, text=True, check=True).stdout
        # A subject that is not conventional asks for a patch. After an editor, git drops it as a comment line.
        message.write_text('#12 fix login\n')
        self.assertIn('no release', check())
        self.assertIn('no release', check(GIT_EDITOR=':', JN_RELEASE_CLEANUP='strip'))
        self.assertIn('the next tag is v0.1.1', check(GIT_EDITOR=':'))
        self.assertIn('the next tag is v0.1.1', check(JN_RELEASE_CLEANUP='verbatim'))
        self.assertIn('the next tag is v0.1.1', check(GIT_EDITOR=':', JN_RELEASE_CLEANUP='unknown'))


if __name__ == '__main__':
    unittest.main()
