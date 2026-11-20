"""Conservative Rust declaration classification for absent LCOV files.

The pinned tree-sitter runtime and Rust grammar parse the whole source, including
attribute values parsed as Rust expressions. Unknown syntax fails closed. This
category includes known Clap generated parsers; it does not measure their coverage.
"""
from pathlib import Path

# Keep these pins in base.just and the package Justfile as well.
RUNTIME = 'tree-sitter==0.25.2'
GRAMMAR = 'tree-sitter-rust==0.24.0'
CLAP = {'Args', 'Subcommand', 'ValueEnum', 'Parser'}
BUILTIN = {'Clone', 'Copy', 'Debug', 'Default', 'Eq', 'PartialEq', 'Ord', 'PartialOrd', 'Hash'}
# These helpers accept only literals, paths, literal lists and literal ranges.
HELPERS = {
    'arg': set('long short global value_name value_names value_parser num_args action required '
               'default_value default_values default_value_t default_values_t env help long_help '
               'hide hide_default_value hide_env hide_env_values conflicts_with conflicts_with_all '
               'requires requires_all group id index last trailing_var_arg allow_hyphen_values '
               'allow_negative_numbers value_delimiter value_terminator alias aliases visible_alias '
               'visible_aliases exclusive flatten skip from_global verbatim_doc_comment next_line_help '
               'require_equals require_delimiter required_unless_present required_unless_present_any '
               'required_unless_present_all display_order'.split()),
    'command': set('name version author about long_about alias aliases visible_alias visible_aliases '
                   'subcommand flatten next_help_heading next_display_order disable_help_flag '
                   'disable_version_flag disable_help_subcommand arg_required_else_help '
                   'subcommand_required infer_subcommands propagate_version rename_all '
                   'rename_all_env verbatim_doc_comment help_template after_help before_help '
                   'after_long_help before_long_help hide'.split()),
    'value': set('name alias aliases help hide skip rename_all verbatim_doc_comment'.split()),
}
# Positive syntax list: deliberately excludes every expression, initializer, macro,
# function/trait/impl item and const parameter. Comments and string contents are inert.
STRUCTURE = set('source_file mod_item declaration_list use_declaration scoped_use_list use_list '
                'use_as_clause use_wildcard struct_item enum_item type_item union_item '
                'field_declaration_list ordered_field_declaration_list field_declaration '
                'enum_variant_list enum_variant visibility_modifier identifier type_identifier '
                'field_identifier scoped_identifier scoped_type_identifier crate super self '
                'primitive_type generic_type type_arguments type_parameters type_parameter '
                'lifetime_parameter lifetime trait_bounds where_clause where_predicate '
                'reference_type pointer_type mutable_specifier tuple_type unit_type never_type '
                'slice_type bracketed_type qualified_type dynamic_type abstract_type '
                'function_type parameters parameter variadic_parameter higher_ranked_trait_bound '
                'for_lifetimes bounded_type associated_type type_binding '
                'line_comment block_comment doc_comment inner_doc_comment outer_doc_comment'.split())
LITERALS = {'string_literal', 'raw_string_literal', 'char_literal', 'integer_literal',
            'float_literal', 'boolean_literal', 'negative_literal'}
PATHS = {'identifier', 'scoped_identifier'}


def text(node):
    """Decode one node from the already validated UTF-8 source."""
    return node.text.decode('utf-8')


def descendants(node):
    """Visit every named node, including the root."""
    yield node
    for child in node.named_children:
        yield from descendants(child)


def imports(node, prefix=''):
    """Resolve ordinary use paths and aliases, without guessing glob bindings."""
    if node.type == 'use_declaration':
        return imports(node.child_by_field_name('argument'), prefix)
    if node.type == 'scoped_use_list':
        path = node.child_by_field_name('path')
        return imports(node.child_by_field_name('list'), prefix + text(path) + '::')
    if node.type == 'use_list':
        return [pair for child in node.named_children for pair in imports(child, prefix)]
    if node.type == 'use_as_clause':
        return [(text(node.child_by_field_name('alias')), prefix + text(node.child_by_field_name('path')))]
    if node.type == 'use_wildcard':
        return [('*', prefix + text(node))]
    return [(text(node).split('::')[-1], prefix + text(node))]


def classify(path):
    """Return (category, reason); a None category retains the absent-file failure."""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_rust
        source = Path(path).read_bytes()
        source.decode('utf-8')
        parser = Parser(Language(tree_sitter_rust.language()))
        root = parser.parse(source).root_node
        if root.has_error or any(n.is_missing for n in descendants(root)):
            return None, 'Rust syntax error or missing syntax node'
        nodes = list(descendants(root))
        bindings = {}
        for n in nodes:
            if n.type == 'use_declaration':
                for name, target in imports(n):
                    bindings.setdefault(name, []).append(target)
            if n.type in {'struct_item', 'enum_item', 'type_item', 'mod_item', 'union_item'}:
                name = n.child_by_field_name('name')
                if name:
                    bindings.setdefault(text(name), []).append('<local>')
        has_clap = False

        def derive_name(name):
            nonlocal has_clap
            if name.startswith('clap::') and name[6:] in CLAP and 'clap' not in bindings and '*' not in bindings:
                has_clap = True
                return True
            targets = bindings.get(name, [])
            if (len(targets) == 1 and targets[0] in {'clap::' + c for c in CLAP}
                    and '*' not in bindings and 'clap' not in bindings):
                has_clap = True
                return True
            return name in BUILTIN and not targets and '*' not in bindings

        def arguments(attr):
            args = attr.child_by_field_name('arguments')
            if args is None:
                return None
            parsed = parser.parse(b'fn __meta() { __values' + args.text + b'; }').root_node
            if parsed.has_error or any(n.is_missing for n in descendants(parsed)):
                return None
            calls = [n for n in descendants(parsed) if n.type == 'call_expression']
            return calls[0].child_by_field_name('arguments').named_children if calls else None

        def inert(node):
            if node.type in LITERALS:
                return True
            if node.type in PATHS:
                return all(c.type in PATHS | {'crate', 'super', 'self'} for c in descendants(node))
            if node.type == 'array_expression':
                return all(c.type in LITERALS for c in node.named_children)
            if node.type == 'range_expression':
                return all(c.type in {'integer_literal', 'negative_literal'} for c in node.named_children)
            return False

        attrs = [n for n in nodes if n.type in {'attribute_item', 'inner_attribute_item'}]
        # Resolve derives first, so helper attributes cannot introduce an unknown macro.
        for item in attrs:
            attr = item.named_children[0]
            name = text(attr.named_children[0])
            if name == 'derive':
                args = arguments(attr)
                if not args or not all(n.type in PATHS and derive_name(text(n)) for n in args):
                    return None, 'unknown or shadowed derive'
        for item in attrs:
            attr = item.named_children[0]
            name = text(attr.named_children[0])
            if name == 'derive':
                continue
            if name == 'doc':
                value = attr.child_by_field_name('value')
                if value is not None and value.type in {'string_literal', 'raw_string_literal'}:
                    continue
            if name in HELPERS and has_clap:
                args = arguments(attr)
                if args is not None:
                    good = True
                    for arg in args:
                        if arg.type == 'identifier':
                            good &= text(arg) in HELPERS[name]
                        elif arg.type == 'assignment_expression':
                            left, right = arg.child_by_field_name('left'), arg.child_by_field_name('right')
                            good &= left.type == 'identifier' and text(left) in HELPERS[name] and inert(right)
                        else:
                            good = False
                    if good:
                        continue
            return None, 'unknown or executable attribute'

        def structural(node):
            if node.type in {'attribute_item', 'inner_attribute_item', 'line_comment', 'block_comment'}:
                return True  # attributes were checked above, comments cannot contain code
            if node.type not in STRUCTURE:
                return False
            return all(structural(c) for c in node.named_children)

        if not structural(root):
            return None, 'handwritten runtime or unsupported syntax'
        substantive = [n for n in root.named_children if n.type not in {'line_comment', 'block_comment'}]
        return ('known Clap declarations' if has_clap else 'declarations' if substantive else 'empty/comment-only'), ''
    except Exception as exc:  # Parser/runtime failures must retain the absent-file finding.
        return None, f'Rust declaration parser unavailable or failed: {exc}'
