#!/usr/bin/env python3
"""Record only dependency output produced by successful current compiler calls.

This wrapper preserves compiler arguments and any pre-existing wrapper. It never
uses a cached dependency file as evidence that a module was compiled.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import uuid


def digest(path):
    """Hash source bytes, failing closed when the source cannot be read."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def option(args, name):
    """Read a compiler option in either of its ordinary spellings."""
    found = []
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            found.append(args[index + 1])
        if value.startswith(name + '='):
            found.append(value[len(name) + 1:])
    if len(found) > 1:
        raise ValueError('duplicate compiler option ' + name)
    return found[0] if found else None


def depfile(args):
    """Locate the compiler's requested dep-info output, without guessing a glob."""
    if option(args, '-o') is not None:
        return None
    emit = option(args, '--emit')
    if not emit:
        return None
    entries = [entry for entry in emit.split(',') if entry.split('=')[0] == 'dep-info']
    if len(entries) != 1:
        return None
    if '=' in entries[0]:
        return Path(entries[0].split('=', 1)[1])
    crate, folder = option(args, '--crate-name'), option(args, '--out-dir')
    if not crate or not folder:
        return None
    suffixes = [value.split('=', 1)[1] for index, value in enumerate(args)
                if value.startswith('extra-filename=') and index and args[index - 1] == '-C']
    suffixes += [value.split('=', 1)[1] for value in args if value.startswith('-Cextra-filename=')]
    if len(suffixes) > 1:
        return None
    return Path(folder) / (crate + (suffixes[0] if suffixes else '') + '.d')


def module_sources(args, project=None):
    """Follow plain module edges and exact cfg(test) edges in test compiler calls.

    Compiler dependencies alone include include_str!/include_bytes! inputs. This
    independent positive module graph prevents those inputs from becoming proof.
    Unknown module attributes and nonstandard paths deliberately prove nothing.
    """
    from tree_sitter import Language, Parser
    import tree_sitter_rust
    parser = Parser(Language(tree_sitter_rust.language()))
    roots = [Path(arg).resolve() for arg in args[1:] if arg.endswith('.rs') and Path(arg).is_file()]
    if len(roots) != 1:
        return {}
    if project is not None and not roots[0].is_relative_to(project):
        return {}
    found, visited = {}, set()

    def visit(path, directory):
        if path in visited:
            return
        visited.add(path)
        source = path.read_bytes()
        root = parser.parse(source).root_node
        if root.has_error or any(child.type == 'inner_attribute_item' for child in root.named_children):
            return
        found[path] = hashlib.sha256(source).hexdigest()
        scope(root, directory)

    def scope(node, directory):
        if any(child.type == 'inner_attribute_item' for child in node.named_children):
            return
        attributes = []
        for child in node.named_children:
            if child.type in {'attribute_item', 'inner_attribute_item'}:
                attributes.append(child)
                continue
            if child.type in {'line_comment', 'block_comment'}:
                continue
            test_edge = ('--test' in args and all(
                re.fullmatch(rb'#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]', attr.text)
                for attr in attributes))
            if child.type == 'mod_item' and (not attributes or test_edge):
                name = child.child_by_field_name('name').text.decode()
                if not name.isidentifier():
                    continue
                body = child.child_by_field_name('body')
                if body is not None:
                    scope(body, directory / name)
                else:
                    candidates = [directory / (name + '.rs'), directory / name / 'mod.rs']
                    existing = [candidate for candidate in candidates if candidate.is_file()]
                    if len(existing) == 1:
                        visit(existing[0].resolve(), directory / name)
            attributes = []

    visit(roots[0], roots[0].parent)
    return found


def dependencies(source, cwd, root):
    """Read Rust dep-info prerequisites; reject malformed or ambiguous output."""
    found = {}
    for line in source.replace('\\\n', '').splitlines():
        if not line or line.startswith('#'):
            continue
        # rustc escapes spaces in paths. A rule separator is colon plus space;
        # source path colons alone do not delimit a rule.
        if ': ' not in line:
            if line.endswith(':'):
                continue  # rustc emits empty phony rules for prerequisites
            raise ValueError('unknown dep-info rule')
        _, right = line.split(': ', 1)
        for spelling in shlex.split(right, comments=False):
            path = (cwd / spelling).resolve()
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if path.suffix == '.rs':
                found[relative] = digest(path)
    return found


def main():
    """Delegate rustc, then publish one isolated receipt for fresh dep-info."""
    args = sys.argv[1:]
    try:
        destination = depfile(args)
        project = Path(os.environ['JNPROFILE_SOURCE_ROOT']).resolve()
        before = module_sources(args, project) if destination is not None else {}
    except Exception as error:
        print(f'Rust compiler module evidence unavailable: {error}', file=sys.stderr)
        destination, before = None, {}
    if destination is not None:
        destination.unlink(missing_ok=True)
    prior = os.environ.get('JNPROFILE_PRIOR_RUSTC_WRAPPER', '')
    command = ([prior] if prior else []) + args
    code = subprocess.run(command).returncode
    # A cache wrapper can recreate fresh dep-info without running rustc. Preserve
    # the user's wrapper chain, but make no declaration claim through that chain.
    trusted = not prior and not os.environ.get('RUSTC_WORKSPACE_WRAPPER', '')
    if code == 0 and destination is not None and trusted:
        try:
            root = Path(os.environ['JNPROFILE_SOURCE_ROOT']).resolve()
            sources = dependencies(destination.read_text(), Path.cwd(), root)
            sources = {path: value for path, value in sources.items()
                       if before.get((root / path).resolve()) == value}
            if any(digest(path) != value for path, value in before.items()):
                sources = {}
            folder = Path(os.environ['JNPROFILE_COMPILER_EVIDENCE'])
            receipt = {'schema': 1, 'sources': sources, 'compiler': args[0]}
            (folder / (uuid.uuid4().hex + '.json')).write_text(json.dumps(receipt) + '\n')
        except (OSError, ValueError, KeyError) as error:
            print(f'Rust compiler module evidence unavailable: {error}', file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
