---
title: The tools
---

Each tool of the profile brings its recipes. No tool adds a crate to `Cargo.toml`. The tools are the programs `cargo`, `cargo-llvm-cov` and `uv` of the machine. The recipes have no shebang line, so each line of a recipe runs through the shell of its module, `.agents/just/rust.just`. When that module sets `jlog` as its shell, the run log has each line. A `set shell` in the root Justfile does not reach the recipes of a module.

The steps run cargo with `--locked`, so a step never makes or changes `Cargo.lock`. Run `just setup` to make the file or to bring it up to date, and commit it. The steps also run with `--all-features`, so the code behind each feature counts. Code behind a feature that cannot be on together with another feature, or code for another platform, such as `#[cfg(windows)]` on macOS, does not count.

## The base

Every project of the profile gets the base, even a project with no tool.

| Recipe | What it does |
|---|---|
| `just setup` | Downloads every dependency with `cargo fetch`, and makes or updates `Cargo.lock` |
| `just build` | Builds every package of the workspace. Extra arguments go to cargo: `just build --release` |

The base adds `/target/` and `*.profraw` to `.gitignore`, and `/target/` to `.worktreedisposable`. The leading `/` keeps a module folder such as `src/target/` in git. That file lists the ignored files that a command can make again. Every report of a step goes to `target/`, so a step leaves no file that git sees.

## lint

| Recipe | What it does |
|---|---|
| `just fmt` | Formats the code of every package with `cargo fmt --all` |
| `just fmt-check` | Checks the format with `cargo fmt --all --check`, and changes no file |
| `just lint` | Runs `cargo clippy --workspace --all-targets --all-features --locked -- -D warnings`. Extra arguments go to cargo: `just lint -p app` |

## typecheck

| Recipe | What it does |
|---|---|
| `just typecheck` | Checks the types of the code and the tests with `cargo check --workspace --all-targets --all-features --locked` |

## test

| Recipe | What it does |
|---|---|
| `just test` | Runs the tests with `cargo llvm-cov test`, and writes the coverage to `target/llvm-cov/lcov.info`. Extra arguments go to cargo llvm-cov test, so `just test parse` runs the tests whose names hold `parse` |
| `just coverage-check` | Fails on each changed line of code that no test runs. It runs the tests first, unless `test` of the same run wrote the report |
| `just test-paths [PATH...]` | Runs the tests of some files in the folder `tests`, with `cargo test` and without coverage |

`cargo llvm-cov` writes the coverage as an LCOV file, a text format that lists how often each line ran. `diff-cover` is a Python program that compares such a file with the diff of git.

`coverage-check` runs `diff-cover` against `HEAD`, with a limit of 100%. The diff holds the staged, the unstaged and the untracked changes. It reads the report of `test` only for the same run ID and the same worktree tree. On the first commit of a repository, `diff-cover` has no parent to compare with. Then the step runs `cargo llvm-cov report --fail-under-lines 100`, which checks every line of the code.

`diff-cover` counts only the files that are in the report. `cargo llvm-cov` leaves out a file that no test target compiles, such as a file that no `mod` line names, or a file in `examples/`. It also leaves out each file in a folder named `tests`, `examples` or `benches`, at any depth, for example `src/tests/mod.rs`.

So `coverage-check` also fails on each changed `.rs` file that is not in the report. The exceptions are the files in the folder `tests` or `benches` of a package, and the files of the two lists. On the first commit, `cargo llvm-cov report` checks only the files in the report. The build script `build.rs` is in the report.

LLVM counts the code of a `macro_rules!` macro at the line that calls the macro. So a branch of a macro that no test runs does not fail `coverage-check`, and the `test-review` target checks it.

## The run ID

The root Justfile of a project has the line `export JLOG_RUN := env("JLOG_RUN", uuid())`. So each run of `just`, such as `just check`, has one run ID, and each inner call of `just` gets the same ID. A module must not have the line, because a second line would make a second ID.

A full run of `just test`, with no arguments, takes the worktree tree before the tests run. The worktree tree is the tree hash of every file that git does not ignore, staged or not, without the folder `target`. When the tests pass and the tree has not changed, `test` saves `JLOG_RUN` and the tree in `target/llvm-cov/run.json`. A file that changed while the tests ran drops the run, because the report may come from the old code.

`coverage-check` reads the report only when both match the current run and the current tree. So in `just check` the tests run once. Alone, `just coverage-check` runs the tests itself, and so does a run after `just test parse`.

## test-paths

`test-paths` takes the files that the `test-parent` recipe of the tasker role gives it. A file `tests/NAME.rs` runs as the test target `NAME`. Any other file in the folder `tests`, such as `tests/common/mod.rs`, runs every test target of that folder. A unit test lives in the file of its code, such as `src/lib.rs`. A copy of that file in the parent commit brings the new code too, so `test-paths` skips the file and says so.

The exit code of `test-paths` tells `test-parent` what happened. Each profile gives the same four codes:

- **0** means the tests ran and passed.
- **1** means a test failed or did not build. A new test of new code often does not build on the parent, and that is a failure of the test.
- **5** means no test ran. The change may add only unit tests, the test targets may hold no test, or the folder `tests` may have no test target. A test with `#[ignore]` does not run, so it does not count. With no path, `test-paths` runs no test and exits with 5. pytest gives the same code when it collects no test.
- **2** means cargo itself failed, for example on a stale `Cargo.lock` or on a path that it cannot run.

cargo exits with 101 both when a test fails or does not build and when it fails itself. So `test-paths` reads the error lines of cargo to tell them apart.

`test-paths` reads the output of cargo to count the tests that ran. So it runs cargo with no colors and with neither quiet nor verbose output, whatever the settings of `CARGO_TERM_COLOR`, `CARGO_TERM_QUIET` and `CARGO_TERM_VERBOSE`. A target with `harness = false` prints no count, so its run counts as a test that ran.

`test-paths` reads the name of each test target from `cargo metadata`. So a `[[test]]` with a name other than its file still runs. A file in `tests` that is no test target, as with `autotests = false`, does not run, and `test-paths` says so. A test binary that names its tests and then stops with no result, as on `std::process::exit(0)` in a test, counts as a failure.

The key `tests` of `profile.json` names `tests/**`, the folder `tests` at the top of the project. A glob cannot name only the folder `tests` of each package of a workspace, because `**/tests/**` also takes a module such as `src/x/tests/mod.rs`, whose new code would reach the parent. So the tests of a member package are not copied to the parent, and `test-review` judges them from the diff.

## docstring

| Recipe | What it does |
|---|---|
| `just docstring-check` | Fails on each item with no doc comment, public or private, and names its file and line |

The step runs `cargo clippy` with the lints `missing_docs` and `clippy::missing_docs_in_private_items`, with every feature. It checks the code of the libraries and the binaries, and not the tests. As the spec says, the step gives the two lints with `-W`, not `-D`, and reads the JSON messages of clippy. It has two reasons. An error in a library stops clippy from checking the binaries that use it, and a generated file must not fail the step.

The step also passes `--cap-lints warn`. So a crate that sets `#![deny(missing_docs)]` still gets the list of items, and a generated file in such a crate still passes. An item in a file that git ignores, such as code that `build.rs` writes into `target/`, counts as generated. An `#[allow(missing_docs)]` in the code wins over the lints of the command line, so it hides its items from the step. The `review` target checks each new `#[allow]`. Each line of its output has the form `src/lib.rs:18:4: missing documentation for a function`.

## Lines that no test can run

Stable Rust has no pragma that leaves out one line from the coverage. So the profile reads two lists of files:

- **`.agents/coverage-exclude.txt`** lists the files that `coverage-check` leaves out.
- **`.agents/generated.txt`** lists the generated files. Both `coverage-check` and `docstring-check` leave them out.

Each line of a list is a path or a glob, with the rules of git pathspecs. A `*` does not match a `/`, `**` matches any number of folders, and a path that ends with `/` names a folder. A line that starts with `#` is a comment. Write the reason for each entry in a comment above it.

The steps read both lists from the parent commit, with `git show HEAD:./PATH`, which names the path from the top of the project. So a new entry counts only after it is in a commit of its own, and a commit cannot exempt its own code. With no parent, both lists are empty. The `test-review` target judges each new entry.

## The helper script

The recipes of `coverage-check`, `docstring-check` and `test-paths` run `.agents/just/rust/steps.py` with `uv`. The script needs Python 3.11 or later and no package. It reads the lists from the parent commit, and it leaves the excluded files out of a copy of the coverage report, in `target/jnprofile-rust/`. It also reads the JSON messages of clippy, and prints each item with no doc. `coverage-check` runs `diff-cover` 11.0.1 with `uv run --with`.
