# jnprofile-rust

jnprofile-rust is the Rust profile of
[jnterfaces](https://github.com/rahuldave/jnterfaces). A profile sets up a
project in one language. It has tools, for example `lint`, and each tool
brings its recipes. A recipe is a command of the project's Justfile, such as
`just lint`.

The profile gives a Cargo project the real steps of the commit gates. A step is
one recipe that `just check` runs. The steps of the profile are `fmt-check`,
`lint`, `typecheck`, `test`, `integration`, `coverage-check`,
`docstring-check` and `api-check`. The spec
of the commit gates is in [docs/spec.md](docs/spec.md).

The documentation site is https://rahuldave.github.io/jnprofile-rust/. Its
pages are in `docs/`. `just docs` shows a preview of the site, and
`just docs-publish` publishes it to the branch `gh-pages`. On each push to
`main`, the job `docs` of `.github/workflows/verify.yml` runs
`just docs-publish` after the job `verify` passes, so the site follows `main`.

## Install

jnterfaces has the rust profile since jnterfaces#34, and installs its programs
since jnterfaces#39. Run this command in a project that has the jnterfaces
installer:

```bash
just install --profile rust
```

The installer also finds the profile by the file `Cargo.toml`. Then add or
remove a tool:

```bash
just uninstall docstring
just install docstring
```

The machine needs Rust from rustup, and `just`, `uv`, `git` and jlog v0.3.0
for jnterfaces. This command installs the release binary of jlog into
`~/.local/bin`, which must be on `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/rahuldave/jlog/v0.3.0/scripts/install.sh | sh -s -- --tag v0.3.0
```

The profile installs `rustfmt`, `clippy`,
`llvm-tools-preview` and `cargo-llvm-cov` with `rustup component add` and
`cargo +stable install --locked`. It installs `taplo` and `cargo-sort` the same way
for the tool `toml`. For the tool `api`, it installs the nightly toolchain
`nightly-2026-11-12` with `rustup toolchain install`. The
[install table](docs/index.md#install) gives the line for each program.

## What is here

| Path | What it holds |
|---|---|
| `profile.json` | The profile: how to find a Rust project, its tools, its lines for `.gitignore` and `.worktreedisposable`, the `@code` globs, and the `tests` globs. |
| `.agents/just/rust/` | One recipe file for each tool, `steps.py`, the helper script of the steps, and `api_rule.py`, the rule of the commit gate for the API files. The installer copies the folder into the project. |
| `.agents/targets/rust/` | The Rust rules for the agentic targets `review`, `test-review`, `update-docs` and `prose`. |
| `.agents/skills/rust-profile/` | The skill that tells an agent how to use the recipes. |
| `lab/` | The lint of the recipe files and the check of the prose. |
| `tests/` | The lab, and the tests of `steps.py` and of the files of the profile. |

## The lab

The lab is a test that installs the profile into a temporary Cargo project and
runs each step. It needs a clone of jnterfaces with commit `99b12a5`, next to
this repository or in the folder that the variable `JNTERFACES` names. A
worktree finds the clone next to its main clone. The lab
copies jnterfaces at that commit, and leaves the clone alone. Run the lab and
the other tests with this command:

```bash
just test
```

`just check` runs `release-check`, the lint, the tests, the check of the prose
and the check of the site. `just release-check` prints the next tag, and needs
no network. This repository has no `Cargo.toml`, so the tag comes from the
commits since the last tag. `just release` tags HEAD with the next release
`vX.Y.Z`, pushes the tag and checks it. `just release --dry-run` prints the tag
and changes nothing.

CI runs `just check` on each push and each pull request, with the workflow
`.github/workflows/verify.yml`. It installs the programs of `profile.json` with
their own install lines, so CI and the profile have the same pins. It clones
jnterfaces at the commit of the lab into the folder that `SKILL_PACKAGES_DIR`
names, and nossaifd at a fixed commit. In CI, a lab that cannot run fails, and
does not skip.

On each push to `main`, the job `release` of `.github/workflows/verify.yml`
runs `just release` after the job `verify` passes. Only that job and the job
`docs` can write to the repository, to push the tag and the site. So nobody tags
by hand. The script is
`scripts/release_tag.py`, a copy of the script of the gpa skill of
agent_gest_git_skills. The section "Release tags" of its
`docs/integration_delivery_workflow.md` gives the rules.

## License

The license is MIT. See [LICENSE](LICENSE).
