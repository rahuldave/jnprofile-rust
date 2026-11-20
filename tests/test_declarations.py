"""Parser failures and the installed Python runtime contract, without Cargo."""
import contextlib
import io
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_steps import REPO, steps
import declarations


class DeclarationParser(unittest.TestCase):
    def test_failures_and_invalid_utf8_fail_closed(self):
        import tree_sitter
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.rs'
            path.write_bytes(b'\xff')
            self.assertIsNone(declarations.classify(path)[0])
            path.write_text('pub struct A;')
            with mock.patch.object(tree_sitter, 'Parser', side_effect=RuntimeError('parser broke')):
                category, reason = declarations.classify(path)
            self.assertIsNone(category)
            self.assertIn('parser broke', reason)
            with mock.patch.dict(sys.modules, {'tree_sitter_rust': None}):
                category, reason = declarations.classify(path)
            self.assertIsNone(category)
            self.assertIn('unavailable', reason)

    def test_shadowing_and_nested_initializers_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.rs'
            for source in (
                'use clap::Args; use other::*; #[derive(Args)] struct A;',
                'mod clap; #[derive(clap::Args)] struct A;',
                'use other::clap; #[derive(clap::Args)] struct A;',
                'use other::clap; use clap::Args; #[derive(Args)] struct A;',
                '#[derive(clap::Args)] struct A { #[arg(value_parser=parser!())] x: String }',
                '#[derive(clap::Args)] struct A { #[arg(unknown=true)] x: bool }',
                '#[derive(clap::Args)] struct A { #[arg(long)] x: [u8; call()] }',
                'mod inner { #[doc="safe"] enum E { A = 1 } }',
                'trait T { const N: usize = { call() }; }',
                'struct A { x: [u8; (|| 2)()] }',
                '#[cfg(any())] fn hidden() {}',
            ):
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertIsNone(declarations.classify(path)[0])

    def test_installed_folder_resolves_pinned_parser_without_source_or_ambient_packages(self):
        # install_extras ships this whole folder. Copy only that payload, then use
        # the actual installed recipe command in an isolated uv environment.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = root / 'installed'
            shutil.copytree(REPO / '.agents/just/rust', payload)
            (root / 'input.rs').write_text('use clap::Args; #[derive(Args)] pub struct A;')
            recipe = (payload / 'base.just').read_text().split('rust_py := "', 1)[1].split('"', 1)[0]
            command = shlex.split(recipe)
            self.assertIn(declarations.RUNTIME, command)
            self.assertIn(declarations.GRAMMAR, command)
            probe = payload / 'probe.py'
            probe.write_text('import sys; sys.path.insert(0, ' + repr(str(payload)) + '); '
                             'import declarations; c, r = declarations.classify("input.rs"); '
                             'print(c, r); assert c == "known Clap declarations"')
            done = subprocess.run([*command, '-I', str(probe)], cwd=root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_coverage_mixed_files_and_reported_zero_hits_keep_the_diff_cover_result(self):
        from tests.test_steps import Repository
        fixture = Repository()
        fixture.setUp()
        try:
            fixture.commit_one()
            fixture.files('src/decl.rs', 'src/orphan.rs')
            Path('src/decl.rs').write_text('pub struct A;')
            Path('src/orphan.rs').write_text('pub fn orphan() {}')
            fixture.write_reports([], [])
            with (fixture.diff_cover(), contextlib.redirect_stdout(io.StringIO()),
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(steps.coverage(), 1)
                Path('src/orphan.rs').unlink()
                self.assertEqual(steps.coverage(), 0)
            # Even a declaration-shaped file with an actual LCOV record keeps
            # diff-cover's zero-hit failure; classification cannot override it.
            fixture.write_reports([('src/decl.rs', 1, 0)], [])
            real = subprocess.run

            def run(command, *args, **kwargs):
                if 'diff-cover' in command:
                    return subprocess.CompletedProcess(command, 1)
                return real(command, *args, **kwargs)
            with (mock.patch.object(steps.subprocess, 'run', side_effect=run),
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(steps.coverage(), 1)
        finally:
            fixture.doCleanups()
