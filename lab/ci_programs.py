"""Install the programs of profile.json for CI, with the install lines that the installer of jnterfaces runs.

    python lab/ci_programs.py

For each program in its order, it runs the check, in an empty folder with RUSTUP_AUTO_INSTALL=0 as the lab does. When
the check fails, does not print its expect, or prints a version below the floor in version, it runs the install line in
the home folder or the empty folder, as install_in says, and checks again. So CI has the same pins as the profile. A
program with no install line, rustup, must already be there. It returns 1 when a program is still missing.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

PROFILE = json.loads((Path(__file__).resolve().parents[1] / 'profile.json').read_text())
# A version floor, >=X.Y or >=X.Y.Z, and a version in the output of a check: the first X.Y.Z or X.Y, not inside a
# longer number. They are the rules of .agents/tools_check.py of jnterfaces.
FLOOR = re.compile(r'>=\s*(\d+)\.(\d+)(?:\.(\d+))?')
FOUND = re.compile(r'(?<![\d.])(\d+)\.(\d+)(?:\.(\d+))?')


def numbers(match):
    """The three numbers of a match of FLOOR or FOUND, with 0 for a missing third."""
    return tuple(int(part or 0) for part in match.groups())


def meets(entry, output, errors=''):
    """True when OUTPUT, the stdout of the check of ENTRY, prints its expect, and the first version in OUTPUT, else in
    ERRORS, its stderr, is at its floor or above. A floor that is not >=X.Y or >=X.Y.Z raises ValueError."""
    if entry.get('expect', '') not in output:
        return False
    if 'version' not in entry:
        return True
    floor = FLOOR.fullmatch(entry['version'].strip()) if isinstance(entry['version'], str) else None
    if floor is None:
        raise ValueError(f'{entry.get("name")}: {entry["version"]!r} is not a version floor: write >=X.Y or >=X.Y.Z')
    found = FOUND.search(output) or FOUND.search(errors)
    return found is not None and numbers(found) >= numbers(floor)


def works(entry, folder, env):
    """True when the check of ENTRY passes in FOLDER, prints its expect and meets its floor."""
    try:
        done = subprocess.run(shlex.split(entry['check']), cwd=folder, capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and meets(entry, done.stdout, done.stderr)


def main():
    """Check each program and install the missing ones. Returns the exit code."""
    env = dict(os.environ, RUSTUP_AUTO_INSTALL='0')
    status = 0
    with tempfile.TemporaryDirectory() as folder:
        for entry in PROFILE['programs']:
            if works(entry, folder, env):
                print(f'ci_programs: {entry["name"]} is there', flush=True)
                continue
            if 'install' not in entry:
                print(f'ci_programs: {entry["name"]} is missing and has no install line: {entry["description"]}',
                      file=sys.stderr)
                status = 1
                continue
            where = Path.home() if entry.get('install_in') == 'home' else folder
            print(f'ci_programs: {entry["install"]}', flush=True)
            subprocess.run(shlex.split(entry['install']), cwd=where, stdin=subprocess.DEVNULL)
            if not works(entry, folder, env):
                print(f'ci_programs: {entry["name"]} is still missing after its install', file=sys.stderr)
                status = 1
    return status


if __name__ == '__main__':
    sys.exit(main())
