---
title: jnprofile-rust
---

jnprofile-rust is the Rust profile of [jnterfaces](https://rahuldave.github.io/jnterfaces/). A profile sets up a project in one language. It has tools, for example `lint`, and each tool brings its recipes. A recipe is a command of the project's Justfile, such as `just lint`.

The profile is for a Rust project with Cargo. The installer finds such a project by its `Cargo.toml`. The profile gives the project the real steps of the commit gates. A step is one recipe that `just check` runs, and it fails when the change breaks a rule.

## Install

jnterfaces has the rust profile since jnterfaces#34, and installs its programs since jnterfaces#39. [The jnterfaces entry](jnterfaces-entry.md) keeps the entry and the change to the installer that it took. Run this command in a project that has the jnterfaces installer:

```bash
just install --profile rust
```

In a folder with no `Cargo.toml`, the command first runs `cargo init --vcs none`. It then installs the four tools `lint`, `typecheck`, `test` and `docstring`. If you do not choose other roles, it also turns on the default roles of the profile, `tasker` and `prose`. The section [The roles](https://rahuldave.github.io/jnterfaces/#roles) of the jnterfaces guide says what each role installs.

The machine needs Rust from rustup, and the programs that jnterfaces needs. The profile installs the rest with rustup and cargo. The key `programs` of `profile.json` names each program, how the installer checks it, and how it installs it.

The installer checks the programs of each tool that it installs, in the project folder. It runs `cargo +stable install` in the home folder, so the stable toolchain builds the programs, whatever the project pins, and also when rustup has no default toolchain. The profile installs stable with the minimal profile when it is missing. The checks run in the project folder, so rustup reads the `rust-toolchain.toml` of the project, and a component goes to the toolchain that the project pins.

| Program | Use | Install |
|---|---|---|
| `rustup` | Every step | https://rustup.rs/. The installer stops if it is missing. |
| The toolchain, with `cargo` | Every step | The profile runs `rustup toolchain install` when the toolchain that `rust-toolchain.toml` or `rust-toolchain` pins is missing. With no pin and no default toolchain, run `rustup default stable` yourself |
| `rustfmt` and `clippy` | `fmt`, `fmt-check`, `lint` and `docstring-check` | The profile runs `rustup component add` |
| `llvm-tools-preview` and `cargo-llvm-cov` | The coverage of `test` and `coverage-check` | The profile runs `rustup component add llvm-tools-preview` and `cargo +stable install --locked cargo-llvm-cov@0.9.1` |
| `taplo` and `cargo-sort` | The tool `toml`, if you install it | The profile runs `cargo +stable install --locked taplo-cli@0.10.0` and `cargo +stable install --locked cargo-sort@2.1.4` |
| `just` | Every recipe | `cargo install just` |
| `uv` | `diff-cover` and the helper script of the steps | https://docs.astral.sh/uv/ |
| `jlog` v0.1.0 | `just install` of jnterfaces, which runs each step through it | Clone https://github.com/rahuldave/jlog, run `git checkout v0.1.0`, then `just install` there |

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
