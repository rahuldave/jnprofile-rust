"""Compiler/module proof controls without invoking Rust."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest import mock

from tests import test_steps

import compiler_evidence as evidence

steps = test_steps.steps


class ModuleGraph(unittest.TestCase):
    def test_plain_modules_and_inline_nesting_but_not_data_or_conditional_edges(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'nested').mkdir()
            (root / 'lib.rs').write_text('mod yes; mod nested { mod child; }\n'
                                        '#[cfg(any())] mod hidden;\n'
                                        'const DATA: &str = include_str!("data.rs");\n'
                                        'const BYTES: &[u8] = include_bytes!("bytes.rs");')
            for path in ('yes.rs', 'nested/child.rs', 'hidden.rs', 'data.rs', 'bytes.rs', 'orphan.rs'):
                (root / path).write_text('pub struct Declaration;')
            found = evidence.module_sources(['rustc', str(root / 'lib.rs')])
            self.assertEqual(set(found), {root / 'lib.rs', root / 'yes.rs', root / 'nested/child.rs'})

    def test_unknown_attributes_path_overrides_inner_cfg_and_ambiguous_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'x.rs').write_text('pub struct X;')
            for source in ('#[path="x.rs"] mod x;', '#[unknown] mod x;',
                           '#![cfg(any())]\nmod x;', '#[cfg_attr(test, path="x.rs")] mod x;'):
                (root / 'lib.rs').write_text(source)
                with self.subTest(source=source):
                    self.assertNotIn(root / 'x.rs', evidence.module_sources(['rustc', str(root / 'lib.rs')]))
            (root / 'x').mkdir()
            (root / 'x/mod.rs').write_text('pub struct X;')
            (root / 'lib.rs').write_text('mod x;')
            self.assertNotIn(root / 'x.rs', evidence.module_sources(['rustc', str(root / 'lib.rs')]))

    def test_exact_gossil_gest_values_registration(self):
        # lib.rs:823-824 at c2a4776e57bb1030aa5ac7cef9648a6cb37f6b90.
        fixture = test_steps.REPO / 'tests/fixtures/declarations/cfg_test/gest_values_mod.rs'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'gest_values').mkdir()
            path = root / 'gest_values/mod.rs'
            path.write_bytes(fixture.read_bytes())
            (root / 'lib.rs').write_text('#[cfg(test)]\nmod gest_values;\n')
            for flags, admitted in (([], False), (['--test'], True)):
                with self.subTest(flags=flags):
                    found = evidence.module_sources(['rustc', str(root / 'lib.rs'), *flags])
                    self.assertEqual(path in found, admitted)
                    if admitted:
                        self.assertEqual(found[path], evidence.digest(path))

    def test_cfg_test_edges_require_actual_test_flag_and_only_exact_attributes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'nested').mkdir()
            (root / 'x.rs').write_text('pub struct X;')
            (root / 'nested/child.rs').write_text('pub struct Child;')
            (root / 'lib.rs').write_text('#[cfg(test)] mod x; '
                                        '#[cfg(test)] mod nested { #[cfg(test)] mod child; }')
            for flags, admitted in (([], False), (['--cfg', 'test'], False),
                                    (['--test=false'], False), (['--test'], True)):
                with self.subTest(flags=flags):
                    found = evidence.module_sources(['rustc', str(root / 'lib.rs'), *flags])
                    self.assertEqual(root / 'x.rs' in found, admitted)
                    self.assertEqual(root / 'nested/child.rs' in found, admitted)
            for source in ('#[cfg(test)] #[unknown] mod x;', '#[cfg(any(test))] mod x;',
                           '#[cfg(test)] #[path="x.rs"] mod x;',
                           '#[allow(unused_imports)] mod x;', '#[cfg(test, other)] mod x;',
                           '#[cfg_attr(test, cfg(test))] mod x;',
                           '#[cfg(test)] custom!(); mod other;',
                           '#[cfg(test)] use other::X; #[unknown] mod x;'):
                with self.subTest(source=source):
                    (root / 'lib.rs').write_text(source)
                    self.assertNotIn(root / 'x.rs', evidence.module_sources(
                        ['rustc', str(root / 'lib.rs'), '--test']))

    def test_dependency_paths_with_spaces_and_data_inputs_remain_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'a b.rs').write_text('pub struct A;')
            self.assertEqual(evidence.dependencies('out: a\\ b.rs\n\na\\ b.rs:\n', root, root),
                             {'a b.rs': evidence.digest(root / 'a b.rs')})

    def test_unambiguous_dep_info_destination_only(self):
        self.assertEqual(evidence.depfile(['rustc', '--emit=dep-info,link', '--crate-name', 'abc',
                                          '--out-dir', '/tmp/out', '-C', 'extra-filename=-hash']),
                         Path('/tmp/out/abc-hash.d'))
        self.assertEqual(evidence.depfile(['rustc', '--emit=dep-info=/tmp/exact.d,metadata']),
                         Path('/tmp/exact.d'))
        self.assertIsNone(evidence.depfile(['rustc', '--emit=dep-info', '-o', '/tmp/out']))
        with self.assertRaises(ValueError):
            evidence.depfile(['rustc', '--emit=dep-info', '--emit=link'])

    def test_wrapper_requires_new_successful_output_and_unchanged_module_source(self):
        for test_flag in (False, True):
            with self.subTest(test_flag=test_flag):
                self.wrapper_controls(test_flag)

    def wrapper_controls(self, test_flag):
        source = ('#[cfg(test)] ' if test_flag else '') + 'mod yes; const X: &str = include_str!("data.rs");'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'lib.rs').write_text(source)
            (root / 'yes.rs').write_text('pub struct Yes;')
            (root / 'data.rs').write_text('pub struct Data;')
            receipt_dir = root / 'receipts'
            receipt_dir.mkdir()
            destination = root / 'fresh.d'
            args = ['wrapper', 'rustc', str(root / 'lib.rs'), '--emit=dep-info=' + str(destination)]
            if test_flag:
                args.append('--test')
            env = {'JNPROFILE_SOURCE_ROOT': str(root), 'JNPROFILE_COMPILER_EVIDENCE': str(receipt_dir),
                   'JNPROFILE_PRIOR_RUSTC_WRAPPER': '', 'RUSTC_WORKSPACE_WRAPPER': ''}
            for mode in ('success', 'failed', 'no-output', 'source-stale', 'ancestor-stale',
                         'cache-wrapper', 'workspace-wrapper'):
                (root / 'lib.rs').write_text(source)
                (root / 'yes.rs').write_text('pub struct Yes;')
                env['JNPROFILE_PRIOR_RUSTC_WRAPPER'] = 'existing-wrapper' if mode == 'cache-wrapper' else ''
                env['RUSTC_WORKSPACE_WRAPPER'] = 'existing-workspace-wrapper' if mode == 'workspace-wrapper' else ''
                destination.write_text('stale: yes.rs\n')
                for receipt in receipt_dir.glob('*.json'):
                    receipt.unlink()

                def compiler(command):
                    self.assertFalse(destination.exists())
                    self.assertEqual(command[0], 'existing-wrapper' if mode == 'cache-wrapper' else 'rustc')
                    if mode != 'no-output':
                        destination.write_text(f'out: {root}/lib.rs {root}/yes.rs {root}/data.rs\n')
                    if mode == 'source-stale':
                        (root / 'yes.rs').write_text('pub struct Changed;')
                    if mode == 'ancestor-stale':
                        (root / 'lib.rs').write_text('const X: &str = include_str!("yes.rs");')
                    return subprocess.CompletedProcess(command, 1 if mode == 'failed' else 0)

                with (self.subTest(mode=mode), mock.patch.dict(os.environ, env),
                      mock.patch.object(evidence.sys, 'argv', args),
                      mock.patch.object(evidence.subprocess, 'run', side_effect=compiler)):
                    evidence.main()
                    receipts = list(receipt_dir.glob('*.json'))
                    sources = json.loads(receipts[0].read_text())['sources'] if receipts else {}
                    self.assertEqual(set(sources), {'lib.rs', 'yes.rs'} if mode == 'success' else set())


class RunProof(unittest.TestCase):
    setUp = test_steps.Repository.setUp
    git = test_steps.Repository.git
    files = test_steps.Repository.files
    commit_one = test_steps.Repository.commit_one
    write_reports = test_steps.Repository.write_reports

    def test_declarations_require_matching_run_tree_digest_and_successful_report(self):
        self.commit_one()
        self.files('src/decl.rs')
        Path('src/decl.rs').write_text('pub struct A;')
        self.write_reports([], [])
        with mock.patch.dict(os.environ, JLOG_RUN='proof'):
            # This is a receipt-reader unit control; real compiler provenance
            # belongs to the RustLab paired fixture.
            tree = steps.worktree_tree()
            data = {'schema': 1, 'run': 'proof', 'tree': tree,
                    'sources': {'src/decl.rs': evidence.digest('src/decl.rs')}}
            steps.REPORTS.mkdir(parents=True, exist_ok=True)
            for step in steps.COVERED:
                steps.saved_run(step).write_text(json.dumps({'run': 'proof', 'tree': tree}))
            receipt = steps.REPORTS / 'test.modules.json'
            receipt.write_text(json.dumps(data))
            self.assertEqual(steps.unreported(['src/decl.rs'], set(), lambda _: False), [])
            for key, value in (('run', 'old'), ('tree', 'old'), ('schema', 0)):
                with self.subTest(key=key):
                    receipt.write_text(json.dumps(dict(data, **{key: value})))
                    self.assertEqual(steps.unreported(['src/decl.rs'], set(), lambda _: False), ['src/decl.rs'])
            receipt.write_text(json.dumps(data))
            Path('src/decl.rs').write_text('pub struct Changed;')
            self.assertEqual(steps.unreported(['src/decl.rs'], set(), lambda _: False), ['src/decl.rs'])
