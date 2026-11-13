"""The helper of the steps of the rust profile: coverage-check, docstring-check and test-paths.

The recipes in this folder run it with uv: python steps.py coverage LCOV, docs, fresh, begin ARG..., stamp, and
test-paths PATH.... begin, stamp and fresh tie the coverage report to the run ID JLOG_RUN of the root Justfile and to the
worktree tree, so that coverage-check reads the report of test only in the same run. It needs Python 3.11 or later and no package. The step coverage-check also
needs diff-cover, which it runs with uv.

The lists .agents/generated.txt and .agents/coverage-exclude.txt come from the parent commit, through
git show HEAD:./PATH, so a commit cannot exempt its own code. With no parent, both lists are empty. git matches
their patterns, as pathspecs with the magic word glob. Every path is relative to the current folder, the top of
the project, which can be a subfolder of the git repository.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import tomllib

DIFF_COVER = 'diff-cover==11.0.1'
# The exit code of test-paths when it runs no test, as pytest gives when it collects none.
NO_TEST = 5
# What cargo prints when no test target matches the name or the pattern.
NO_TARGET = re.compile(r'^error: no test target matches pattern', re.M)
# The line of cargo for each test binary that it starts; a binary with harness = false prints no test result.
RUNNING = re.compile(r'^\s*Running \S', re.M)
# The escape codes of terminal colors, which a test may still print.
ANSI = re.compile(r'\x1b\[[0-9;]*m')
# The seconds with no new line, once the command ends, after which the copy of its output stops.
LINGER = 2
# The summary line of each test binary: the numbers of the passed and of the failed tests.
TEST_RESULT = re.compile(r'^test result: \w+\. (\d+) passed; (\d+) failed;', re.M)
GENERATED = '.agents/generated.txt'
COVERAGE_EXCLUDE = '.agents/coverage-exclude.txt'
REPORT = Path('target/llvm-cov/lcov.info')
RUN = Path('target/llvm-cov/run.json')
PENDING = Path('target/llvm-cov/run.pending')
DOC_LINTS = ('missing_docs', 'clippy::missing_docs_in_private_items')
WORK = Path('target/jnprofile-rust')


def git(*args):
    """Run git with ARGS in the current folder, and return the finished process, with its output as text."""
    return subprocess.run(['git', *args], capture_output=True, text=True)


def has_parent():
    """True when HEAD names a commit, so that the next commit has a parent."""
    return git('rev-parse', '--verify', '--quiet', 'HEAD^{commit}').returncode == 0


def top():
    """The real path of the current folder, the top of the project. Every path of the steps is relative to it."""
    return Path(os.path.realpath('.'))


def parent_list(path):
    """The patterns of the list file PATH in the parent commit: one for each line that is not blank or a comment.

    A missing parent or a file that the parent does not have gives no patterns.
    """
    if not has_parent():
        return []
    run = git('show', f'HEAD:./{path}')
    if run.returncode:
        return []
    return [line.strip() for line in run.stdout.splitlines() if line.strip() and not line.strip().startswith('#')]


def matcher(patterns):
    """A function that is true for a path, relative to the current folder, that one of PATTERNS matches.

    git matches the patterns, as pathspecs with the magic word glob, against the tracked and the untracked files
    that it does not ignore. So the steps and the gate read a list in the same way. A * does not match a /, ** matches
    any number of folders, and a folder names all that it holds. A leading / is the top of the project.
    """
    if not patterns:
        return lambda path: False
    specs = [':(glob)' + pattern.lstrip('/') for pattern in patterns]
    run = git('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', *specs)
    if run.returncode:
        raise SystemExit(f'steps.py: git cannot read the patterns {patterns}: {run.stderr.strip()}')
    found = set(filter(None, run.stdout.split('\0')))
    return lambda path: path in found


def relative(path, root):
    """PATH relative to ROOT, with / between its parts, or None for a path outside ROOT."""
    full = Path(os.path.realpath(root / path))
    try:
        return full.relative_to(root).as_posix()
    except ValueError:
        return None


def filter_lcov(text, root, excluded):
    """The LCOV report TEXT without the record of each source file that EXCLUDED is true for.

    A record runs from its SF: line to its end_of_record line. A path outside ROOT stays.
    """
    out, keep = [], True
    for line in text.splitlines(keepends=True):
        if line.startswith('SF:'):
            path = relative(line[3:].strip(), root)
            keep = path is None or not excluded(path)
        if keep:
            out.append(line)
        if line.strip() == 'end_of_record':
            keep = True
    return ''.join(out)


def ignored(path):
    """True for a path outside the project, or one that git ignores, such as a file that build.rs writes in target/."""
    if path.startswith('/') or path.startswith('../'):
        return True
    return git('check-ignore', '--quiet', '--', path).returncode == 0


def worktree_tree():
    """The tree hash of every file of the worktree, staged or not, that git does not ignore, or None on an error.

    It uses a temporary index of its own, as the spec of the commit gates says, so neither the real index nor a
    second run at the same time changes. The folder target of the project never counts. A file that git cannot
    index, such as a nested repository with no commit, gives None, and git's message goes to the error output.
    """
    WORK.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=WORK, suffix='.index')
    os.close(handle)
    index = Path(name).resolve()
    index.unlink()
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    try:
        if has_parent():
            subprocess.run(['git', 'read-tree', 'HEAD'], env=env, check=True, capture_output=True)
        # git refuses to name an ignored path, even to exclude it, so the exclude is only for a target that git sees.
        specs = [':/'] if ignored('target') else [':/', ':(exclude)target']
        subprocess.run(['git', 'add', '-A', '--', *specs], env=env, check=True, capture_output=True)
        return subprocess.run(['git', 'write-tree'], env=env, check=True, capture_output=True,
                              text=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f'steps.py: git cannot compute the worktree tree: {(exc.stderr or b"").decode(errors="replace").strip()}',
              file=sys.stderr)
        return None
    finally:
        index.unlink(missing_ok=True)
        Path(str(index) + '.lock').unlink(missing_ok=True)


def begin(args):
    """Save the run ID and the worktree tree before a run of the tests, in target/llvm-cov/run.pending.

    Only a run of all the tests, with no ARGS, inside a run of just that has JLOG_RUN saves them. Any other run
    saves nothing, so coverage-check runs the tests again. Returns the exit code, always 0.
    """
    RUN.unlink(missing_ok=True)
    PENDING.unlink(missing_ok=True)
    run = os.environ.get('JLOG_RUN', '')
    tree = worktree_tree() if not args and run else None
    if tree:
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps({'run': run, 'tree': tree}) + '\n')
    return 0


def stamp():
    """After the tests passed, keep the saved run as target/llvm-cov/run.json when the tree did not change.

    A change to a file while the tests ran means that the report may come from the old code, so the run is dropped.
    Returns the exit code, always 0.
    """
    try:
        saved = json.loads(PENDING.read_text())
    except (OSError, ValueError):
        return 0
    finally:
        PENDING.unlink(missing_ok=True)
    if isinstance(saved, dict) and saved.get('tree') and saved['tree'] == worktree_tree():
        RUN.write_text(json.dumps(saved) + '\n')
    else:
        print('test: a file changed while the tests ran, so coverage-check runs them again', file=sys.stderr)
    return 0


def fresh():
    """True when the report of the tests comes from this run of just, JLOG_RUN, and from the current worktree tree.

    A person can set JLOG_RUN by hand, so the tree must match too. An old run of the same tree is a valid run.
    """
    run = os.environ.get('JLOG_RUN', '')
    try:
        saved = json.loads(RUN.read_text())
    except (OSError, ValueError):
        saved = None
    if (run and isinstance(saved, dict) and saved.get('run') == run and REPORT.is_file()
            and saved.get('tree') and saved['tree'] == worktree_tree()):
        print(f'coverage-check: the tests of this run, {run}, wrote the report, so they do not run again')
        return True
    print('coverage-check: no report of this run and this tree, so the tests run now', flush=True)
    return False


def changed_code():
    """The changed Rust files against HEAD, staged, unstaged or untracked, without the deleted ones."""
    tracked = git('diff', '-z', '--name-only', '--relative', '--diff-filter=d', 'HEAD', '--', '*.rs')
    untracked = git('ls-files', '-z', '--others', '--exclude-standard', '--', '*.rs')
    return sorted(set(filter(None, (tracked.stdout + '\0' + untracked.stdout).split('\0'))))


def reported(text, root):
    """The paths of the source files of the LCOV report TEXT, relative to ROOT."""
    return {relative(line[3:].strip(), root) for line in text.splitlines() if line.startswith('SF:')}


def test_file(path):
    """True for a file in the folder tests or benches of a Cargo package, which holds tests and no code to cover."""
    try:
        _, folder = package_of(path)
    except SystemExit:
        return False
    parts = Path(os.path.realpath(path)).relative_to(folder).parts
    return len(parts) > 1 and parts[0] in ('tests', 'benches')


def unreported(changed, lines, excluded):
    """The files of CHANGED that are not in the report, not excluded and not test files: no test can cover them.

    cargo llvm-cov leaves out a file that no test target compiles, such as a file that no mod names or a file in
    examples. It also leaves out each file in a folder named tests, examples or benches, at any depth.
    """
    return [path for path in changed if path not in lines and not excluded(path) and not test_file(path)]


def coverage(lcov):
    """Fail on each changed line of code that the tests do not run, as diff-cover against HEAD finds it.

    The files of generated.txt and coverage-exclude.txt of the parent commit do not count. On the first commit,
    cargo llvm-cov report checks every line instead, with a limit of 100%. Returns the exit code.
    """
    if not has_parent():
        print('coverage-check: the first commit, so every line of code must run in the tests', flush=True)
        code = subprocess.run(['cargo', 'llvm-cov', 'report', '--include-build-script',
                               '--fail-under-lines', '100']).returncode
        if code:
            print('coverage-check: write tests for the lines that the report shows as missed', file=sys.stderr)
        return code
    if not Path(lcov).is_file():
        print(f'coverage-check: there is no coverage report {lcov}, so write tests and run just test',
              file=sys.stderr)
        return 1
    root = top()
    excluded = matcher(parent_list(GENERATED) + parent_list(COVERAGE_EXCLUDE))
    WORK.mkdir(parents=True, exist_ok=True)
    kept = WORK / 'lcov.info'
    text = Path(lcov).read_text()
    kept.write_text(filter_lcov(text, root, excluded))
    code = subprocess.run(['uv', 'run', '--no-project', '--quiet', '--with', DIFF_COVER, 'diff-cover', str(kept),
                           '--compare-branch', 'HEAD', '--include-untracked', '--fail-under', '100']).returncode
    missing = unreported(changed_code(), reported(text, root), excluded)
    for path in missing:
        print(f'{path}: a changed file that is not in the coverage report, so no test runs it')
    if missing:
        print('coverage-check: no test target compiles these files, or cargo llvm-cov leaves out their folder. Call '
              'the code from a test, move it out of a folder named tests or examples, or exclude the file',
              file=sys.stderr)
        code = code or 1
    if code:
        print('coverage-check: write tests for the missing lines. A file that no test can run goes in '
              f'{COVERAGE_EXCLUDE}, in a commit of its own before this one', file=sys.stderr)
    return code


def doc_findings(messages, root, excluded):
    """The items with no doc in the JSON MESSAGES of cargo clippy, as lines FILE:LINE:COLUMN: TEXT.

    Each message of a doc lint counts, warning or error, except one in a file that EXCLUDED is true for. Also
    returns the rendered errors that are not doc lints, for example a type error, which stop the check.
    """
    findings, errors = set(), []
    for raw in messages.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get('reason') != 'compiler-message':
            continue
        message = entry.get('message', {})
        code = (message.get('code') or {}).get('code')
        if code in DOC_LINTS:
            span = next((s for s in message.get('spans', []) if s.get('is_primary')), None)
            if span is None:
                continue
            path = relative(span['file_name'], root) or span['file_name']
            if not excluded(path):
                findings.add((path, span['line_start'], span['column_start'], f'{message["message"]} ({code})'))
        elif message.get('level') == 'error':
            errors.append(message.get('rendered') or message.get('message', ''))
    return [f'{path}:{line}:{column}: {text}' for path, line, column, text in sorted(findings)], errors


def docs():
    """Fail on each item with no doc comment, public or private, outside the files of generated.txt.

    It runs cargo clippy on the code of every package, without the tests, with the two doc lints as warnings, and
    reads its JSON messages. It turns on every feature, so code behind a feature counts. --cap-lints warn keeps
    each lint a warning, also under #![deny(missing_docs)], so clippy checks every crate. An item in a file that
    git ignores, such as code that build.rs writes into target/, counts as generated. Returns the exit code.
    """
    run = subprocess.run(['cargo', 'clippy', '--workspace', '--all-features', '--locked', '--quiet',
                          '--message-format=json', '--', '--cap-lints', 'warn',
                          '-W', 'missing_docs', '-W', 'clippy::missing_docs_in_private_items'],
                         stdout=subprocess.PIPE, text=True)
    generated = matcher(parent_list(GENERATED))
    findings, errors = doc_findings(run.stdout, top(), lambda path: generated(path) or ignored(path))
    for error in errors:
        print(error, file=sys.stderr)
    for finding in findings:
        print(finding)
    if findings:
        print(f'docstring-check: {len(findings)} items have no doc comment. Write a doc comment for each one',
              file=sys.stderr)
    # --cap-lints warn keeps a #![deny(missing_docs)] of the crate a warning, so clippy fails only on a real error.
    if run.returncode:
        print(f'docstring-check: cargo clippy exited with {run.returncode}', file=sys.stderr)
        return run.returncode
    return 1 if findings else 0


def package_of(path):
    """The name of the Cargo package that holds PATH, and its folder: the nearest Cargo.toml with a [package].

    The search stops at the top of the project, so a Cargo.toml outside the project never counts.
    """
    folder, root = Path(os.path.realpath(path)).parent, top()
    for candidate in [folder, *folder.parents]:
        if candidate != root and root not in candidate.parents:
            break
        manifest = candidate / 'Cargo.toml'
        if manifest.is_file():
            data = tomllib.loads(manifest.read_text())
            if isinstance(data.get('package'), dict) and isinstance(data['package'].get('name'), str):
                return data['package']['name'], candidate
    raise SystemExit(f'test-paths: no Cargo package holds {path}')


def test_commands(paths):
    """The cargo test commands for PATHS, one for each package, in the order of the paths, and the skipped paths.

    Only a file in the folder tests of a package counts. A file tests/NAME.rs runs as the test target NAME. Any
    other file there, for example tests/common/mod.rs, runs every test target of that folder, with --test '*'. A
    unit test lives in the file of its code, such as src/lib.rs. A copy of that file in the parent commit brings
    the new code too, so the test proves nothing there, and test-paths skips the file.
    """
    targets, skipped = {}, []
    for path in paths:
        if not Path(path).is_file():
            raise SystemExit(f'test-paths: {path} is not a file')
        name, folder = package_of(path)
        parts = Path(os.path.realpath(path)).relative_to(folder).parts
        if len(parts) < 2 or parts[0] != 'tests':
            skipped.append(path)
            continue
        chosen = targets.setdefault(name, [])
        if len(parts) == 2 and parts[1].endswith('.rs'):
            chosen.append(['--test', parts[1][:-3]])
        else:
            chosen.append(['--test', '*'])
    commands = []
    for name, chosen in targets.items():
        every = ['--test', '*'] in chosen
        args = ['--test', '*'] if every else [arg for pair in dict.fromkeys(map(tuple, chosen)) for arg in pair]
        commands.append(['cargo', 'test', '--locked', '--all-features', '--package', name, *args])
    return commands, skipped


def test_paths(paths):
    """Run the tests of PATHS without coverage: 0 when they pass, NO_TEST when none ran, else the failure code.

    A failure never returns 0 or NO_TEST, so the test-parent recipe can tell the three apart.
    """
    commands, skipped = test_commands(paths)
    for path in skipped:
        print(f'test-paths: skips {path}, which is not in the folder tests of a package', flush=True)
    if not commands:
        print('test-paths: no file in the folder tests of a package, so no test ran', flush=True)
        return NO_TEST
    ran = 0
    for command in commands:
        print(shlex.join(command), flush=True)
        code, out = run_streamed(command)
        if code and NO_TARGET.search(out) and not RUNNING.search(out) and 'could not compile' not in out:
            print('test-paths: no test target in the folder tests, so no test ran', flush=True)
            continue
        if code:
            return code if 0 < code < 256 and code != NO_TEST else 1
        results = TEST_RESULT.findall(out)
        ran += sum(int(a) + int(b) for a, b in results)
        ran += max(0, len(RUNNING.findall(out)) - len(results))
    if not ran:
        print('test-paths: the test targets hold no test that is not ignored, so no test ran', flush=True)
        return NO_TEST
    return 0


def run_streamed(command):
    """Run COMMAND with its stderr in its stdout, copy each line out as it comes, and return the code and the text.

    The output of cargo is plain, with no colors, no quiet and no verbose lines, so the patterns match it. Once
    COMMAND ends, the copy goes on while lines still come, so a slow reader of stdout loses none. A process that
    COMMAND left running and that holds the pipe open, but prints nothing, does not keep this one waiting.
    """
    env = dict(os.environ, CARGO_TERM_COLOR='never', CARGO_TERM_QUIET='false', CARGO_TERM_VERBOSE='false')
    lines = []

    def copy(pipe):
        """Copy each line of PIPE to stdout and keep its text."""
        for line in pipe:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            lines.append(line.decode('utf-8', 'replace'))

    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    reader = threading.Thread(target=copy, args=(child.stdout,), daemon=True)
    reader.start()
    code = child.wait()
    seen = -1
    while reader.is_alive() and len(lines) != seen:
        seen = len(lines)
        reader.join(LINGER)
    return code, ANSI.sub('', ''.join(lines))


def main(argv):
    """Run the command of ARGV: coverage LCOV, docs, fresh, begin ARG..., stamp, or test-paths PATH...: its exit code."""
    if len(argv) == 2 and argv[0] == 'coverage':
        return coverage(argv[1])
    if argv == ['docs']:
        return docs()
    if argv == ['fresh']:
        return 0 if fresh() else 1
    if argv[:1] == ['begin']:
        return begin(argv[1:])
    if argv == ['stamp']:
        return stamp()
    if len(argv) >= 2 and argv[0] == 'test-paths':
        return test_paths(argv[1:])
    print('usage: steps.py coverage LCOV | docs | fresh | begin [ARG...] | stamp | test-paths PATH...', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
