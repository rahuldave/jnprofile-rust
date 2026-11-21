"""Parser failures and the installed Python runtime contract, without Cargo."""
import contextlib
import io
import hashlib
import json
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
    def test_actual_gossil_settings_and_markdown_declarations(self):
        fixtures = REPO / 'tests/fixtures/declarations'
        # Exact untracked working-tree snapshots at Gossil HEAD 146789901c95;
        # sources.json records the failing guard, paths and digests.
        for filename, expected in (('gossil_config.rs', 'known derived declarations'),
                                   ('gossil_block.rs', 'declarations')):
            with self.subTest(filename=filename):
                self.assertEqual(declarations.classify(fixtures / filename), (expected, ''))

    def test_exact_gossil_cfg_test_declarations(self):
        fixtures = REPO / 'tests/fixtures/declarations/cfg_test'
        sources = json.loads((fixtures / 'sources.json').read_text())
        self.assertEqual(len(sources), 5)
        for source in sources:
            path = fixtures / source['fixture']
            with self.subTest(path=path.name):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source['sha256'])
                self.assertEqual(declarations.classify(path), ('declarations', ''))

    def test_cfg_and_import_lint_syntax_remains_bounded(self):
        refused = (
            '#[cfg(other)] mod x;', '#[cfg(any(test))] mod x;',
            '#[cfg_attr(test, allow(unused_imports))] use x::X;',
            '#[cfg(test)] #[unknown] mod x;', '#[cfg(test)] fn f() {}',
            '#[cfg(test)] mod x { fn f() {} }', '#[cfg(test)] custom!();',
            '#[cfg(test)] mod x { custom!(); }', '#![cfg(test)] mod x;',
            '#[allow(unused_imports)] mod x;', '#[allow(unused_imports, dead_code)] use x::X;',
            '#[allow(unused_imports = callback())] use x::X;',
            '#[cfg(test = callback())] mod x;',
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.rs'
            for source in refused:
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertIsNone(declarations.classify(path)[0])
            for source in ('#[cfg(test)] pub use x::X;', '#[cfg(test)] mod x;',
                           '#[cfg(test)] mod x { pub use super::X; }',
                           '#[allow(unused_imports)] pub use x::X;'):
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertEqual(declarations.classify(path), ('declarations', ''))

    def test_known_derives_and_helpers_resolve_explicit_unambiguous_imports(self):
        sources = [
            'use serde::{Deserialize as D, Serialize as S}; use getset::Getters as G; '
            '#[derive(D, S, G)] #[serde(default)] struct A { #[get="pub"] x: u8 }',
            '#[derive(serde::Deserialize, getset::Getters)] #[serde(default)] '
            'struct A { #[get="pub"] x: u8 }',
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.rs'
            for source in sources:
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertEqual(declarations.classify(path)[0], 'known derived declarations')

    def test_serde_getset_and_lint_extensions_keep_adversarial_forms_closed(self):
        sources = [
            'mod serde; #[derive(serde::Deserialize)] struct A;',
            'use other::serde; #[derive(serde::Serialize)] struct A;',
            'use other::getset; #[derive(getset::Getters)] struct A;',
            'use serde::Deserialize as D; use other::D; #[derive(D)] struct A;',
            'use getset::Getters as G; use other::G; #[derive(G)] struct A;',
            'use other::*; #[derive(serde::Deserialize)] struct A;',
            '#[derive(serde::Deserialize)] #[serde(default="callback")] struct A;',
            '#[derive(serde::Deserialize)] #[serde(deserialize_with="callback")] struct A;',
            '#[derive(serde::Deserialize)] #[serde(default=callback())] struct A;',
            '#[derive(serde::Serialize)] #[serde(default)] struct A;',
            '#[derive(serde::Deserialize)] struct A; #[serde(default)] struct B;',
            '#[derive(getset::Getters)] struct A; struct B { #[get="pub"] x: u8 }',
            '#[derive(getset::Getters)] struct A { #[get="pub(crate)"] x: u8 }',
            '#[derive(getset::Getters)] struct A { #[get=callback()] x: u8 }',
            'use other::get; #[derive(getset::Getters)] struct A { #[get="pub"] x: u8 }',
            '#[allow(clippy::enum_variant_names, dead_code)] enum A { A }',
            '#[allow(dead_code)] enum A { A }',
            '#[allow(clippy::enum_variant_names = callback())] enum A { A }',
            'use other::clippy; #[allow(clippy::enum_variant_names)] enum A { A }',
            'use other::allow; #[allow(clippy::enum_variant_names)] enum A { A }',
            '#[allow(clippy::enum_variant_names)] struct A;',
            '#[derive(serde::Deserialize)] struct A; fn executable() {}',
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.rs'
            for source in sources:
                with self.subTest(source=source):
                    path.write_text(source)
                    self.assertIsNone(declarations.classify(path)[0])

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
            with (fixture.diff_cover(), mock.patch.object(steps, 'compiled_modules', return_value={'src/decl.rs'}),
                  contextlib.redirect_stdout(io.StringIO()),
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
