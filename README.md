# jnprofile-rust

jnprofile-rust is the Rust profile of
[jnterfaces](https://github.com/rahuldave/jnterfaces). A profile sets up a
project in one language. It has tools, for example `lint`, and each tool
brings its recipes. A recipe is a command of the project's Justfile, such as
`just lint`.

The profile gives a Cargo project the real steps of the commit gates. A step is
one recipe that `just check` runs. The steps of the profile are `fmt-check`,
`lint`, `typecheck`, `test`, `coverage-check` and `docstring-check`. The spec
of the commit gates is in [docs/spec.md](docs/spec.md).

The documentation site is https://rahuldave.github.io/jnprofile-rust/. Its
pages are in `docs/`. `just docs` shows a preview of the site, and
`just docs-publish` publishes it to the branch `gh-pages`.

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

The machine needs Rust from rustup, and `just`, `uv`, `git` and jlog v0.1.0
for jnterfaces. The profile installs `rustfmt`, `clippy`,
`llvm-tools-preview` and `cargo-llvm-cov` with `rustup component add` and
`cargo +stable install --locked`. It installs `taplo` and `cargo-sort` the same way
for the tool `toml`. The
[install table](docs/index.md#install) gives the line for each program.

## What is here

| Path | What it holds |
|---|---|
| `profile.json` | The profile: how to find a Rust project, its tools, its lines for `.gitignore` and `.worktreedisposable`, the `@code` globs, and the `tests` globs. |
| `.agents/just/rust/` | One recipe file for each tool, and `steps.py`, the helper script of three steps. The installer copies the folder into the project. |
| `.agents/targets/rust/` | The Rust rules for the agentic targets `review`, `test-review`, `docs` and `prose`. |
| `.agents/skills/rust-profile/` | The skill that tells an agent how to use the recipes. |
| `lab/` | The lint of the recipe files and the check of the prose. |
| `tests/` | The lab, and the tests of `steps.py` and of the files of the profile. |

## The lab

The lab is a test that installs the profile into a temporary Cargo project and
runs each step. It needs a clone of jnterfaces with commit `f9e66f7`, next to
this repository or in the folder that the variable `JNTERFACES` names. The lab
copies jnterfaces at that commit, and leaves the clone alone. Run the lab and
the other tests with this command:

```bash
just test
```

`just check` runs the lint, the tests, the check of the prose and the check of
the site.

## License

The license is MIT. See [LICENSE](LICENSE).
