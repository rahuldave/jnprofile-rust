---
title: jnprofile-rust
---

jnprofile-rust is the Rust profile of [jnterfaces](https://rahuldave.github.io/jnterfaces/). A profile sets up a project in one language. It has tools, for example `lint`, and each tool brings its recipes. A recipe is a command of the project's Justfile, such as `just lint`.

The profile is for a Rust project with Cargo. The installer finds such a project by its `Cargo.toml`. The profile gives the project the real steps of the commit gates. A step is one recipe that `just check` runs, and it fails when the change breaks a rule.

## Install

jnterfaces has the rust profile since jnterfaces#34. [The jnterfaces entry](jnterfaces-entry.md) keeps the entry and the change to the installer that it took. Run this command in a project that has the jnterfaces installer:

```bash
just install --profile rust
```

In a folder with no `Cargo.toml`, the command first runs `cargo init --vcs none`. It then installs the four tools `lint`, `typecheck`, `test` and `docstring`. If you do not choose other roles, it also turns on the default roles of the profile, `tasker` and `prose`. The section [The roles](https://rahuldave.github.io/jnterfaces/#roles) of the jnterfaces guide says what each role installs.

The machine needs these programs:

| Program | Use | Install |
|---|---|---|
| `cargo` | Every step, with the rustup components `clippy` and `rustfmt` | https://rustup.rs/ |
| `cargo-llvm-cov` | The coverage of `test` and `coverage-check` | `cargo install cargo-llvm-cov`, and `rustup component add llvm-tools-preview` |
| `just` | Every recipe | `cargo install just` |
| `uv` | `diff-cover` and the helper script of the steps | https://docs.astral.sh/uv/ |

## The steps

| Step | Tool | What fails it |
|---|---|---|
| `fmt-check` | `lint` | Code that rustfmt formats in another way |
| `lint` | `lint` | Each warning of clippy |
| `typecheck` | `typecheck` | A type error in the code or in the tests |
| `test` | `test` | A test that fails |
| `coverage-check` | `test` | A changed line of code that no test runs |
| `docstring-check` | `docstring` | An item with no doc comment, public or private |

[The tools](tools.md) gives each recipe, and the rules for the lines that no test can run. The profile has no `api` tool yet. That tool waits for the release of smalldocs that reads rustdoc.

## The other sites

- [jnterfaces](https://rahuldave.github.io/jnterfaces/): the installer, and the `just`, `jagt-protocol` and `cx` skills.
- [jnprofile-python](https://rahuldave.github.io/jnprofile-python/): the Python profile.
- [jnprofile-typescript](https://rahuldave.github.io/jnprofile-typescript/): the TypeScript profile.
- [agent_gest_git_skills](https://rahuldave.github.io/agent_gest_git_skills/): the tasker role and its g skills, the skills whose names start with g.
