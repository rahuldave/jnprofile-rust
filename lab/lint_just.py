"""Find the recipes of the profile that the run log cannot see, and check that the recipe files parse.

A recipe with a shebang line or the [script] attribute does not run through the shell of the Justfile, which is
jlog. A line with the prefix - ignores a failure. [no-cd] and working-directory change the folder in which jlog
looks for the log folder. The spec of the commit gates forbids all four in a step.

    python lab/lint_just.py .agents/just/rust
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

RULES = [
    (re.compile(r'^\s+#!'), 'a shebang line'),
    (re.compile(r'^\s*\[\s*script\b'), 'the [script] attribute'),
    (re.compile(r'^\s+@?-'), 'a line with the prefix -, which ignores a failure'),
    (re.compile(r'^\s*\[[^\]]*\bno-cd\b'), 'the [no-cd] attribute'),
    (re.compile(r'^\s*(set\s+working-directory|\[[^\]]*\bworking-directory\b)'), 'the setting working-directory'),
]


def problems(path):
    """Each line of the recipe file PATH that breaks a rule, as FILE:LINE: RULE."""
    found = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        for regex, rule in RULES:
            if regex.search(line):
                found.append(f'{path}:{number}: {rule}')
                break
    return found


def parses(folder):
    """The error of just for a module that imports every recipe file of FOLDER, or None when it parses."""
    with tempfile.TemporaryDirectory() as temp:
        module = Path(temp) / 'module.just'
        lines = ['set positional-arguments'] + [f"import '{p.resolve()}'" for p in sorted(Path(folder).glob('*.just'))]
        module.write_text('\n'.join(lines) + '\n')
        run = subprocess.run(['just', '--justfile', str(module), '--summary'], capture_output=True, text=True)
        return None if run.returncode == 0 else run.stderr.strip()


def main(argv):
    """Lint the recipe files of each folder in ARGV, and return 1 when one breaks a rule or does not parse."""
    found = []
    for folder in argv:
        for path in sorted(Path(folder).glob('*.just')):
            found += problems(path)
        if shutil.which('just'):
            error = parses(folder)
            if error:
                found.append(f'{folder}: the recipe files do not parse: {error}')
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
