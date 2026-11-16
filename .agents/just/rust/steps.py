"""The helper of the steps of the rust profile: test, integration, coverage-check, docstring-check and test-paths.

The recipes in this folder run it with uv: python steps.py run STEP ARG..., fresh STEP, coverage, docs, test-paths
PATH... and toml. STEP is test or integration. test runs the unit tests, those of the library and the binaries, and
integration runs the test targets, such as the files of the folder tests. Each writes its own coverage report. The
run ID JLOG_RUN of the root Justfile and the worktree tree tie each report to its run, so that coverage-check reads a
report only in the same run. It needs Python 3.11 or later and no package. The step coverage-check also needs
diff-cover, which it runs with uv.

The lists .agents/generated.txt and .agents/coverage-exclude.txt come from the parent commit, through
git show HEAD:./PATH, so a commit cannot exempt its own code. With no parent, or a parent without the file, the list is empty. git matches
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
# The other place of the config of cargo-sort, which reads tomlfmt.toml from the current folder.
SORT_CONFIG = '.config/tomlfmt.toml'
# The exit codes of test-paths, the same for each profile: the tests passed, a test failed or did not build,
# the runner itself failed, and no test ran, as pytest gives when it collects none.
PASSED, FAILED, RUNNER, NO_TEST = 0, 1, 2, 5
# What cargo prints when a test fails or a test does not build.
BROKE = re.compile(r'^error: (?:test failed|could not compile|failed to run custom build command)', re.M)
# What cargo prints when no test target matches the name or the pattern.
NO_TARGET = re.compile(r'^error: no test target matches pattern', re.M)
# The line of cargo for each test binary that it starts, in its exact form, so a line that a test leaks, such as
# that of a cargo run inside a test, does not match. A binary with harness = false prints no test result.
RUNNING = re.compile(r'^     Running (?:unittests )?[^`\s].* \(\S*deps[/\\]\S+\)$', re.M)
# The line of libtest that announces how many tests a binary will run.
ANNOUNCED = re.compile(r'^running (\d+) tests?$', re.M)
# The escape codes of terminal colors, which a test may still print.
ANSI = re.compile(r'\x1b\[[0-9;]*m')
# The seconds with no new line, once the command ends, after which the copy of its output stops.
LINGER = 2
# The line of libtest for a test that failed, also a #[should_panic] test, whose name ends in - should panic.
FAILED_TEST = re.compile(r'^test .+ \.\.\. FAILED$', re.M)
# The summary line of each test binary: the numbers of the passed and of the failed tests.
TEST_RESULT = re.compile(r'^test result: \w+\. (\d+) passed; (\d+) failed;', re.M)
GENERATED = '.agents/generated.txt'
COVERAGE_EXCLUDE = '.agents/coverage-exclude.txt'
REPORTS = Path('target/llvm-cov')
# The steps that write a coverage report.
COVERED = ('test', 'integration')
# The kinds of target of cargo metadata that --lib selects.
LIBRARY = {'lib', 'rlib', 'dylib', 'cdylib', 'staticlib', 'proc-macro'}
# The tests with coverage, with every feature. run adds the targets of the step and the path of its report.
COVERAGE = ['cargo', 'llvm-cov', 'test', '--all-features', '--locked', '--include-build-script']
# The file of jnterfaces whose steps list says whether integration runs in just check.
STACK = Path('.agents/stack.json')
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

    It starts from HEAD in a temporary index of its own, so neither the real index nor a second run at the same
    time changes. The tree only ties a report to its run: steps.py compares it with its own trees, never with
    the tree of jlog or jagt, which start from a copy of the index. The folder target of the project never counts. A file that git cannot
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


def report(step):
    """The coverage report of STEP, test or integration: target/llvm-cov/STEP.lcov."""
    return REPORTS / f'{step}.lcov'


def saved_run(step):
    """The file that ties the report of STEP to its run: target/llvm-cov/STEP.run.json."""
    return REPORTS / f'{step}.run.json'


def pending_run(step):
    """The file that holds the run of STEP while its tests run: target/llvm-cov/STEP.run.pending."""
    return REPORTS / f'{step}.run.pending'


def begin(step, args):
    """Save the run ID and the worktree tree before a run of the tests of STEP, in target/llvm-cov/STEP.run.pending.

    Only a run of all the tests, with no ARGS, inside a run of just that has JLOG_RUN saves them. Any other run
    saves nothing, so coverage-check runs the tests again. Returns the exit code, always 0.
    """
    saved_run(step).unlink(missing_ok=True)
    pending_run(step).unlink(missing_ok=True)
    run = os.environ.get('JLOG_RUN', '')
    tree = worktree_tree() if not args and run else None
    if tree:
        REPORTS.mkdir(parents=True, exist_ok=True)
        pending_run(step).write_text(json.dumps({'run': run, 'tree': tree}) + '\n')
    return 0


def stamp(step):
    """After the tests of STEP passed, keep the saved run as target/llvm-cov/STEP.run.json when the tree did not change.

    A change to a file while the tests ran means that the report may come from the old code, so the run is dropped.
    Returns the exit code, always 0.
    """
    try:
        saved = json.loads(pending_run(step).read_text())
    except (OSError, ValueError):
        return 0
    finally:
        pending_run(step).unlink(missing_ok=True)
    if isinstance(saved, dict) and saved.get('tree') and saved['tree'] == worktree_tree():
        saved_run(step).write_text(json.dumps(saved) + '\n')
    else:
        print(f'{step}: a file changed while the tests ran, so coverage-check runs them again', file=sys.stderr)
    return 0


def counts(step):
    """True when the report of STEP counts for coverage-check.

    The report of test always counts. That of integration counts when integration is in the steps of just check,
    the key steps of .agents/stack.json, and an installed tool provides it, the key provided_steps. The setup
    answer of jnterfaces that runs integration only in just verify takes it out of steps. With no stack.json or no
    list of steps, the default of the spec holds: integration runs in just check, so it counts.
    """
    if step == 'test':
        return True
    try:
        stack = json.loads(STACK.read_text())
    except (OSError, ValueError):
        return True
    listed = stack.get('steps') if isinstance(stack, dict) else None
    if not isinstance(listed, list):
        return True
    provided = stack.get('provided_steps')
    return step in listed and (not isinstance(provided, list) or step in provided)


def fresh(step):
    """True when the report of STEP comes from this run of just, JLOG_RUN, and from the current worktree tree.

    A person can set JLOG_RUN by hand, so the tree must match too. An old run of the same tree is a valid run.
    A report that does not count for coverage-check needs no run, so it is fresh too.
    """
    if not counts(step):
        print(f'coverage-check: {step} is not in the steps of just check, so its report does not count')
        return True
    run = os.environ.get('JLOG_RUN', '')
    try:
        saved = json.loads(saved_run(step).read_text())
    except (OSError, ValueError):
        saved = None
    if (run and isinstance(saved, dict) and saved.get('run') == run and report(step).is_file()
            and saved.get('tree') and saved['tree'] == worktree_tree()):
        print(f'coverage-check: the tests of {step} in this run, {run}, wrote the report, so they do not run again')
        return True
    print(f'coverage-check: no report of {step} for this run and this tree, so the tests run now', flush=True)
    return False


def selections(step):
    """The selections of targets of STEP, from cargo metadata: one list of cargo options for each run of cargo.

    Only a target whose key test is true counts, as for a plain cargo test, so a [[test]] with test = false does
    not run. test takes the libraries and the binaries. integration takes the test targets, such as the files of
    the folder tests, and the examples and the benches that set test = true. Each such target is named, with --bin,
    --test, --example or --bench. cargo looks for a name in each selected package, and a library has no name of
    its own, only --lib. So one run with --workspace takes them all only when each library is tested and no
    package has an untested target of a tested name. Otherwise each package gets a run of its own, with
    --package and the id of the package, since a dependency can have the same name. A step with no target gives
    no selection.
    """
    done = subprocess.run(['cargo', 'metadata', '--no-deps', '--format-version', '1'], capture_output=True, text=True)
    if done.returncode:
        sys.stderr.write(done.stderr)
        raise SystemExit(f'{step}: cargo metadata failed')
    kinds = (('bin', '--bin'),) if step == 'test' else (('test', '--test'), ('example', '--example'),
                                                         ('bench', '--bench'))
    chosen, named, skipped, untested_library = [], [], set(), False
    for package in json.loads(done.stdout)['packages']:
        library, pairs = False, []
        for target in package['targets']:
            tested = target.get('test', True)
            if step == 'test' and set(target['kind']) & LIBRARY:
                library = library or tested
                untested_library = untested_library or not tested
            for kind, option in kinds:
                if kind in target['kind']:
                    if tested:
                        pairs.append((option, target['name']))
                    else:
                        skipped.add((option, target['name']))
        named += pairs
        if library or pairs:
            chosen.append((package['id'], library, pairs))
    if untested_library or skipped & set(named):
        return [['--package', name, *(['--lib'] if library else []), *[arg for pair in pairs for arg in pair]]
                for name, library, pairs in chosen]
    if not chosen:
        return []
    library = any(library for _, library, _ in chosen)
    return [['--workspace', *(['--lib'] if library else []), *[arg for pair in dict.fromkeys(named) for arg in pair]]]


# The options of cargo that select targets, and those that select packages. With one of them in the extra
# arguments, the arguments choose. The step adds --workspace only when they choose no package.
TARGETS = ('--lib', '--bin', '--bins', '--test', '--tests', '--example', '--examples', '--bench', '--benches',
           '--all-targets', '--doc')
PACKAGES = ('-p', '--package', '--workspace', '--all')


def chosen_by(args):
    """What ARGS, before a --, choose: 'packages' when they name packages, 'targets' for targets only, else None.

    --exclude alone chooses targets: cargo takes it only with --workspace, which the step then adds.
    """
    found = None
    for arg in args:
        if arg == '--':
            break
        name = arg.split('=', 1)[0]
        if name in PACKAGES or (arg.startswith('-p') and not arg.startswith('--')):
            return 'packages'
        if name in TARGETS or name == '--exclude':
            found = 'targets'
    return found


def merge_lcov(texts):
    """One LCOV report from the reports TEXTS: a line runs when one report runs it.

    For each source file, a line is in the report when one of TEXTS measures it, and its count is the highest of
    their counts. diff-cover takes only the lines that each report fails, so it cannot merge reports itself.
    """
    counts, path = {}, None
    for text in texts:
        for line in text.splitlines():
            if line.startswith('SF:'):
                path = line[3:].strip()
                counts.setdefault(path, {})
            elif line.startswith('DA:') and path is not None:
                number, count = line[3:].split(',')[:2]
                lines = counts[path]
                lines[int(number)] = max(lines.get(int(number), 0), int(count))
            elif line.strip() == 'end_of_record':
                path = None
    return ''.join(f'SF:{path}\n' + ''.join(f'DA:{number},{count}\n' for number, count in sorted(lines.items()))
                   + 'end_of_record\n' for path, lines in sorted(counts.items()))


def run_tests(step, args):
    """Run the tests of STEP with coverage into target/llvm-cov/STEP.lcov, with ARGS for cargo llvm-cov test.

    Each selection of targets runs in its own cargo llvm-cov, with a report of its own, and the step merges them.
    When ARGS select targets or packages, such as --test api or -p core, they take the place of the selections. With no target for
    STEP, no test runs and the report is empty. Returns the exit code of the first cargo that fails.
    """
    out = report(step)
    out.unlink(missing_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    for old in REPORTS.glob(f'{step}.part*.lcov'):
        old.unlink()
    begin(step, args)
    try:
        chosen = chosen_by(args)
        runs = [[]] if chosen == 'packages' else [['--workspace']] if chosen else selections(step)
    except SystemExit as error:
        pending_run(step).unlink(missing_ok=True)
        print(error, file=sys.stderr, flush=True)
        return 1
    if not runs:
        what = 'test target' if step == 'integration' else 'library or binary'
        print(f'{step}: the workspace has no {what}, so no test runs and the report is empty', flush=True)
        out.write_text('')
        return stamp(step)
    parts = []
    for number, selection in enumerate(runs):
        parts.append(REPORTS / f'{step}.part{number}.lcov')
        command = [*COVERAGE, *selection, '--lcov', '--output-path', parts[-1].as_posix(), *args]
        print(shlex.join(command), flush=True)
        code = subprocess.run(command).returncode
        if code:
            pending_run(step).unlink(missing_ok=True)
            return code
    out.write_text(merge_lcov([part.read_text() for part in parts]))
    return stamp(step)


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


def missed_lines(text, root):
    """Each line that the LCOV report TEXT gives a count of 0, as PATH:LINE relative to ROOT, in order."""
    missed, path = [], None
    for line in text.splitlines():
        if line.startswith('SF:'):
            path = relative(line[3:].strip(), root) or line[3:].strip()
        elif line.startswith('DA:') and path is not None:
            number, count = line[3:].split(',')[:2]
            if int(count) == 0:
                missed.append((path, int(number)))
    return [f'{path}:{number}' for path, number in sorted(missed)]


def coverage():
    """Fail on each changed line of code that the tests do not run, as diff-cover against HEAD finds it.

    It reads the report of test, and that of integration when it counts. A line that one of them runs passes.
    The files of generated.txt and coverage-exclude.txt of the parent commit do not count. On the first commit,
    every line of the reports must run instead. Returns the exit code.
    """
    reports = [report(step) for step in COVERED if counts(step)]
    for path in reports:
        if not path.is_file():
            print(f'coverage-check: there is no coverage report {path}, so write tests and run just test',
                  file=sys.stderr)
            return 1
    texts = [path.read_text() for path in reports]
    root = top()
    if not has_parent():
        print('coverage-check: the first commit, so every line of code must run in the tests', flush=True)
        missed = missed_lines(merge_lcov(texts), root)
        for line in missed:
            print(f'{line}: no test runs this line')
        if missed:
            print('coverage-check: write tests for the lines that no test runs', file=sys.stderr)
            return 1
        return 0
    excluded = matcher(parent_list(GENERATED) + parent_list(COVERAGE_EXCLUDE))
    WORK.mkdir(parents=True, exist_ok=True)
    kept = WORK / 'lcov.info'
    kept.write_text(filter_lcov(merge_lcov(texts), root, excluded))
    code = subprocess.run(['uv', 'run', '--no-project', '--quiet', '--with', DIFF_COVER, 'diff-cover', str(kept),
                           '--compare-branch', 'HEAD', '--include-untracked', '--fail-under', '100']).returncode
    lines = set().union(*(reported(text, root) for text in texts))
    missing = unreported(changed_code(), lines, excluded)
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


def target_names():
    """Map the real path of the file of each test target of the workspace to the name of the target.

    A [[test]] in Cargo.toml can give a target a name that is not the name of its file, so the names come from
    cargo metadata.
    """
    done = subprocess.run(['cargo', 'metadata', '--no-deps', '--format-version', '1'], capture_output=True, text=True)
    if done.returncode:
        sys.stderr.write(done.stderr)
        raise SystemExit('test-paths: cargo metadata failed')
    return {os.path.realpath(target['src_path']): target['name']
            for package in json.loads(done.stdout)['packages'] for target in package['targets']
            if 'test' in target['kind']}


def test_commands(paths):
    """The cargo test commands for PATHS, one for each package, in the order of the paths, and the skipped paths.

    Only a file in the folder tests of a package counts. A file tests/NAME.rs runs as its test target, which
    cargo metadata names, and a file that is no test target, as with autotests = false, is skipped. Any
    other file there, for example tests/common/mod.rs, runs every test target of that folder, with --test '*'. A
    unit test lives in the file of its code, such as src/lib.rs. A copy of that file in the parent commit brings
    the new code too, so the test proves nothing there, and test-paths skips the file.
    """
    targets, skipped, names = {}, [], None
    for path in paths:
        if not Path(path).is_file():
            raise SystemExit(f'test-paths: {path} is not a file')
        name, folder = package_of(path)
        parts = Path(os.path.realpath(path)).relative_to(folder).parts
        if len(parts) < 2 or parts[0] != 'tests':
            skipped.append(path)
            continue
        if len(parts) == 2 and parts[1].endswith('.rs'):
            if names is None:
                names = target_names()
            target = names.get(os.path.realpath(path))
            if target is None:
                print(f'test-paths: skips {path}, which is no test target of its package', flush=True)
                continue
            targets.setdefault(name, []).append(['--test', target])
        else:
            targets.setdefault(name, []).append(['--test', '*'])
    commands = []
    for name, chosen in targets.items():
        every = ['--test', '*'] in chosen
        args = ['--test', '*'] if every else [arg for pair in dict.fromkeys(map(tuple, chosen)) for arg in pair]
        commands.append(['cargo', 'test', '--locked', '--all-features', '--package', name, *args])
    return commands, skipped


def test_paths(paths):
    """Run the tests of PATHS without coverage, and return the exit code that the spec gives every profile.

    PASSED (0) when the tests ran and passed, FAILED (1) when a test failed or did not build, NO_TEST (5) when no
    test ran, and RUNNER (2) when cargo itself failed, for example on a stale Cargo.lock or a path it cannot run.
    A test binary that announced its tests and stopped with no result, as on std::process::exit in a test, ended
    the run, and gives RUNNER too. Each command runs, one for each package, and a failed test in any of them wins
    over a run that ended or a cargo that failed.
    """
    try:
        commands, skipped = test_commands(paths)
    except SystemExit as error:
        print(error, file=sys.stderr, flush=True)
        return RUNNER
    for path in skipped:
        print(f'test-paths: skips {path}, which is not in the folder tests of a package', flush=True)
    if not commands:
        print('test-paths: no file in the folder tests of a package, so no test ran', flush=True)
        return NO_TEST
    ran, codes = 0, set()
    for command in commands:
        print(shlex.join(command), flush=True)
        code, out = run_streamed(command)
        if code and NO_TARGET.search(out) and not RUNNING.search(out) and not BROKE.search(out):
            print('test-paths: no test target in the folder tests, so no test ran', flush=True)
            continue
        stopped = False
        for binary in RUNNING.split(out)[1:]:
            results = TEST_RESULT.findall(binary)
            if results:
                ran += sum(int(a) + int(b) for a, b in results)
            elif FAILED_TEST.search(binary):
                # libtest named a failed test, and then the binary stopped: the failure counts.
                codes.add(FAILED)
            elif any(int(n) for n in ANNOUNCED.findall(binary)):
                # The run ended, as on std::process::exit in a test, and no test result says why. The spec gives
                # that the code of a runner that failed, so test-review does not read it as a failed test.
                print('test-paths: a test binary stopped before it gave its result, so the run ended early',
                      flush=True)
                stopped = True
                codes.add(RUNNER)
            else:
                ran += 1
        if code and not stopped:
            codes.add(outcome(code, out))
    for result in (FAILED, RUNNER):
        if result in codes:
            return result
    if not ran:
        print('test-paths: the test targets hold no test that is not ignored, so no test ran', flush=True)
        return NO_TEST
    return PASSED


def outcome(code, out):
    """The exit code of test-paths for a cargo command that exited with CODE, not 0, and printed OUT.

    cargo exits with 101 both when a test fails or does not build and when it fails itself, and it passes on the
    code of a test binary that exits by itself. Its output tells them apart: a failed test or a failed build is
    FAILED, and anything else is RUNNER.
    """
    if BROKE.search(out):
        return FAILED
    print(f'test-paths: cargo failed with {code} before a test could run', flush=True)
    return RUNNER


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
    if not reader.is_alive():
        child.stdout.close()
    return code, ANSI.sub('', ''.join(lines))


def toml(args):
    """Sort each Cargo.toml with cargo-sort, then format the TOML files with taplo, with ARGS such as --check.

    cargo-sort only sorts, with --no-format, because it would indent arrays other than taplo does, and the two
    would undo each other. taplo gets the TOML files that git tracks or does not ignore, so it never reads target/.
    cargo-sort reads tomlfmt.toml from the current folder. A project may keep the file in .config/ instead, and then
    it gets the option --config. Both run, so one run shows every problem; the exit code is 1 when either fails.
    """
    command = ['cargo', 'sort', '--workspace', '--no-format', *args]
    if not Path('tomlfmt.toml').is_file() and Path(SORT_CONFIG).is_file():
        command += ['--config', SORT_CONFIG]
    print(shlex.join(command), flush=True)
    failed = subprocess.run(command).returncode != 0
    listed = git('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.toml')
    if listed.returncode:
        print(f'git cannot list the TOML files: {listed.stderr.strip()}', file=sys.stderr, flush=True)
        return 1
    files = sorted({name for name in listed.stdout.split('\0') if name and Path(name).is_file()})
    if files:
        command = ['taplo', 'fmt', *args, '--', *files]
        print(shlex.join(command), flush=True)
        failed = subprocess.run(command).returncode != 0 or failed
    return 1 if failed else 0


def main(argv):
    """Run the command of ARGV and return its exit code: run STEP [ARG...], fresh STEP, coverage, docs,
    test-paths [PATH...] or toml [--check]. STEP is test or integration."""
    if len(argv) >= 2 and argv[0] == 'run' and argv[1] in COVERED:
        return run_tests(argv[1], argv[2:])
    if len(argv) == 2 and argv[0] == 'fresh' and argv[1] in COVERED:
        return 0 if fresh(argv[1]) else 1
    if argv == ['coverage']:
        return coverage()
    if argv == ['docs']:
        return docs()
    if argv[:1] == ['test-paths']:
        return test_paths(argv[1:])
    if argv in (['toml'], ['toml', '--check']):
        return toml(argv[1:])
    print('usage: steps.py run STEP [ARG...] | fresh STEP | coverage | docs | test-paths [PATH...] | toml [--check], '
          'where STEP is test or integration', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
