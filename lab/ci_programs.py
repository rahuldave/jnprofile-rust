"""Install the programs of profile.json for CI, with the install lines that the installer of jnterfaces runs.

    python lab/ci_programs.py

For each program in its order, it runs the check, in an empty folder with RUSTUP_AUTO_INSTALL=0 as the lab does.
When the check fails or does not print its expect, it runs the install line in the home folder or the empty folder,
as install_in says, and checks again. So CI has the same pins as the profile. A program with no install line, rustup,
must already be there. It returns 1 when a program is still missing.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

PROFILE = json.loads((Path(__file__).resolve().parents[1] / 'profile.json').read_text())


def works(entry, folder, env):
    """True when the check of ENTRY passes in FOLDER and prints its expect."""
    try:
        done = subprocess.run(shlex.split(entry['check']), cwd=folder, capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and entry.get('expect', '') in done.stdout


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
