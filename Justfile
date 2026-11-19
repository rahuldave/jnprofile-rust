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

# ruff, at a fixed version, lints the Python scripts and tests
ruff := "uvx --quiet ruff@0.17.5"

# Check the JSON, the Python with ruff, and the recipe files of the profile: no shebang, no - line, no [no-cd], and they parse
lint:
    {{ruff}} check .
    {{python}} -m json.tool profile.json > /dev/null
    {{python}} -m json.tool skill-package.json > /dev/null
    {{python}} -m py_compile .agents/just/rust/steps.py .agents/just/rust/api_rule.py lab/lint_just.py lab/prose_check.py lab/ci_programs.py scripts/release_tag.py tests/test_lab.py tests/test_release.py tests/test_steps.py tests/test_profile.py tests/test_api.py
    {{python}} lab/lint_just.py .agents/just/rust

# Run the tests and the lab. Extra arguments go to unittest: just test tests.test_steps. JNTERFACES names jnterfaces, else
# SKILL_PACKAGES_DIR/jnterfaces, else ../jnterfaces
test *args:
    {{python}} -m unittest {{ if args == "" { "discover -s tests -t ." } else { "" } }} "$@"

# Check the prose of the Markdown of this repository with nossaifd check.py, without the front matter
prose-check:
    {{python}} lab/prose_check.py {{quote(checker)}} README.md docs/index.md docs/tools.md docs/jnterfaces-entry.md .agents/skills/rust-profile/SKILL.md .agents/targets/rust/review.md .agents/targets/rust/test-review.md .agents/targets/rust/update-docs.md .agents/targets/rust/prose.md

# Everything a commit must pass
check: release-check lint test prose-check docs-check

# The release tags. scripts/release_tag.py is a copy of .agents/skills/gpa/scripts/release_tag.py of
# agent_gest_git_skills at the commit fbf0837. The job release of verify.yml runs just release on each push to main
release_tag := "uv run --no-project --quiet --no-python-downloads --python '>=3.9' python scripts/release_tag.py"

# Tag HEAD with the next release vX.Y.Z by semver, push the tag and check it. Takes --dry-run first
[positional-arguments]
release *args:
    @{{release_tag}} "$@"

# Print the next tag, or fail on a git error. It needs no network and no remote
release-check:
    {{release_tag}} --check

# The documentation site, from docs/, with smalldocs.
smalldocs := "uvx --quiet smalldocs@0.5.12"

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
