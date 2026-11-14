# jnprofile-rust: every task is a recipe here. `just` lists them.

# Each word after a recipe's name reaches its commands whole, as "$1", "$2" and so on; "$@" is all of them
set positional-arguments

[private]
default:
    @just --list --unsorted

# Python comes from uv. The lab runs the installer of jnterfaces, which needs tomlkit and ruamel.yaml
python := "uv run --no-project --quiet --python '>=3.11' --with tomlkit==0.15.2 --with ruamel.yaml==0.19.1 python"

# The checker of the nossaifd writing guide. Set NOSSAIFD_CHECK to use another copy
checker := env("NOSSAIFD_CHECK", home_directory() / "Projects/nossaifd/check.py")

# Check the JSON, the Python, and the recipe files of the profile: no shebang, no - line, no [no-cd], and they parse
lint:
    {{python}} -m json.tool profile.json > /dev/null
    {{python}} -m json.tool skill-package.json > /dev/null
    {{python}} -m py_compile .agents/just/rust/steps.py lab/lint_just.py tests/test_lab.py tests/test_steps.py tests/test_profile.py
    {{python}} lab/lint_just.py .agents/just/rust

# Run the tests and the lab. Extra arguments go to unittest: just test tests.test_steps. JNTERFACES names jnterfaces
test *args:
    {{python}} -m unittest {{ if args == "" { "discover -s tests -t ." } else { "" } }} "$@"

# Check the prose of the Markdown of this repository with nossaifd check.py, without the front matter
prose-check:
    {{python}} lab/prose_check.py {{quote(checker)}} README.md docs/index.md docs/tools.md docs/jnterfaces-entry.md .agents/skills/rust-profile/SKILL.md .agents/targets/rust/review.md .agents/targets/rust/test-review.md .agents/targets/rust/docs.md .agents/targets/rust/prose.md

# Everything a commit must pass
check: lint test prose-check docs-check

# The documentation site, from docs/, with smalldocs.
smalldocs := "uvx --quiet smalldocs@0.5.5"

# Check the site: broken links, and options that smalldocs ignores
docs-check:
    {{smalldocs}} check docs

# Preview the site, and build it again on each change
docs:
    {{smalldocs}} preview docs

# Build the site into docs/_site
docs-build:
    {{smalldocs}} render docs --strict

# Build the site in strict mode, then push it to the branch gh-pages
docs-publish: docs-build
    {{smalldocs}} publish gh-pages docs --no-render
