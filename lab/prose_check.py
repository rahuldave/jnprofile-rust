"""Run nossaifd check.py on Markdown pages without their front matter, and write nothing into the repository.

    python lab/prose_check.py CHECKER PAGE...
"""
from pathlib import Path
import subprocess
import sys
import tempfile


def body(text):
    """TEXT without a front matter block between two --- lines at its start."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == '---':
        for number, line in enumerate(lines[1:], 1):
            if line.strip() == '---':
                return ''.join(lines[number + 1:])
    return text


def main(argv):
    """Check each page of ARGV[1:] with the checker ARGV[0], and return 1 when a page fails or the checker is missing."""
    checker, pages = Path(argv[0]).expanduser(), argv[1:]
    if not checker.is_file():
        print(f'prose-check: no checker at {checker}. Clone https://github.com/rahuldave/nossaifd, or set '
              'NOSSAIFD_CHECK to its check.py', file=sys.stderr)
        return 1
    status = 0
    with tempfile.TemporaryDirectory() as temp:
        for page in pages:
            copy = Path(temp) / page.replace('/', '_')
            copy.write_text(body(Path(page).read_text()))
            run = subprocess.run([sys.executable, str(checker), str(copy)], capture_output=True, text=True)
            output = (run.stdout + run.stderr).replace(str(copy), page)
            if output.strip():
                print(output.rstrip())
            status = status or run.returncode
    return 1 if status else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
