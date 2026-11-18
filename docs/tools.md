---
title: The tools
---

Each tool of the profile brings its recipes. No tool adds a crate to `Cargo.toml`. The tools use `cargo`, `uv`, and the programs that the key `programs` of `profile.json` names. The installer installs each of those with rustup or cargo.

The recipes have no shebang line, so each line of a recipe runs through the shell of its module, `.agents/just/rust.just`. When that module sets `jlog` as its shell, the run log has each line. A `set shell` in the root Justfile does not reach the recipes of a module.

The steps run cargo with `--locked`, so a step never makes or changes `Cargo.lock`. Run `just setup` to make the file or to bring it up to date, and commit it. The steps also run with `--all-features`, so the code behind each feature counts. Code behind a feature that cannot be on together with another feature, or code for another platform, such as `#[cfg(windows)]` on macOS, does not count.

## The base

Every project of the profile gets the base, even a project with no tool.

| Recipe | What it does |
|---|---|
| `just setup` | Downloads every dependency with `cargo fetch`, and makes or updates `Cargo.lock` |
| `just build` | Builds every package of the workspace. Extra arguments go to cargo: `just build --release` |
| `just version` | Prints the version of the project, `X.Y.Z`, from `[package]` or `[workspace.package]` of the root `Cargo.toml`, for the release tag. It prints nothing when there is none. It exits with 2 when the file cannot be read, or when the version is not semver. It is not a step of `just check` |

The base adds `/target/` and `*.profraw` to `.gitignore`, and `/target/` to `.worktreedisposable`. The leading `/` keeps a module folder such as `src/target/` in git. That file lists the ignored files that a command can make again. Every report of a step goes to `target/`, so a step leaves no file that git sees.

## toml

`toml` is not a default tool. Install it with `just install toml`. It needs `taplo` and `cargo-sort`, and the installer installs both with `cargo +stable install --locked`.

| Recipe | What it does |
|---|---|
| `just toml-fmt` | Sorts the tables of each `Cargo.toml` of the workspace with `cargo sort --workspace --no-format`, then formats the TOML files with `taplo fmt` |
| `just toml-check` | Checks both, and changes no file |

`toml-check` is not a step of the commit gates. So `just verify` runs it, and `just check` and the commit gate do not. A CI job that wants it runs `just rust toml-check`.

`cargo-sort` reads its config `tomlfmt.toml` from the top of the project. When that file is missing and `.config/tomlfmt.toml` exists, the recipe passes that file with `--config`. taplo gets only the TOML files that git tracks or does not ignore, so it never reads `target/`. cargo-sort only sorts, because its format of arrays differs from that of taplo.

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
| `just test` | Runs the unit tests, those of the libraries and the binaries, with `cargo llvm-cov test`. It writes the coverage to `target/llvm-cov/test.lcov`. Extra arguments go to cargo llvm-cov test, so `just test parse` runs the unit tests whose names hold `parse` |
| `just coverage-check` | Fails on each changed line of code that no test runs. A line that `test` or `integration` runs passes. It runs each of them first, unless it wrote its report in the same run |
| `just test-paths [PATH...]` | Runs the tests of some files in the folder `tests`, with `cargo test` and without coverage |

`cargo llvm-cov` writes the coverage as an LCOV file, a text format that lists how often each line ran. `diff-cover` is a Python program that compares such a file with the diff of git.

`coverage-check` runs `diff-cover` against `HEAD`, with a limit of 100%. The diff holds the staged, the unstaged and the untracked changes.

The step merges the report of `test` and that of `integration` into one report, and gives it to `diff-cover`. A line that one of them runs passes. A line that one report measures and no report runs fails, also when the other report does not measure it, as for code under `#[cfg(not(test))]`. It reads each report only for the same run ID and the same worktree tree.

On the first commit of a repository, `diff-cover` has no parent to compare with. Then the step checks each line of the merged report, and it names each line that no test runs.

The report of `integration` counts only when `integration` is in the steps of `just check`. The installer of jnterfaces writes those steps into the key `steps` of `.agents/stack.json`. The setup answer that runs `integration` only in `just verify` takes it out of that list. Then a line that only an integration test runs fails `coverage-check`. Give it a unit test, or exclude its file. With no `.agents/stack.json`, the report counts, as the spec says by default.

`diff-cover` counts only the files that are in the report. `cargo llvm-cov` leaves out a file that no test target compiles, such as a file that no `mod` line names, or a file in `examples/`. It also leaves out each file in a folder named `tests`, `examples` or `benches`, at any depth, for example `src/tests/mod.rs`.

So `coverage-check` also fails on each changed `.rs` file that is not in the report. The exceptions are the files in the folder `tests` or `benches` of a package, and the files of the two lists. On the first commit, the step checks only the files in the reports. Workspace build scripts may be in the report. Dependency reporting scope and copied source paths can omit files that execute. Absence alone does not prove that no test runs a file.

LLVM counts the code of a `macro_rules!` macro at the line that calls the macro. So a branch of a macro that no test runs does not fail `coverage-check`, and the `test-review` target checks it.

## The run ID

The root Justfile of a project has the line `export JLOG_RUN := env("JLOG_RUN", uuid())`. So each run of `just`, such as `just check`, has one run ID, and each inner call of `just` gets the same ID. A module must not have the line, because a second line would make a second ID.

A full run of `just test`, with no arguments, takes the worktree tree before the tests run. The worktree tree is the tree hash of every file that git does not ignore, staged or not, without the folder `target`. When the tests pass and the tree has not changed, `test` saves `JLOG_RUN` and the tree in `target/llvm-cov/test.run.json`. A file that changed while the tests ran drops the run, because the report may come from the old code.

`coverage-check` reads a report only when both match the current run and the current tree. So in `just check`, which runs `test`, then `integration`, then `coverage-check`, the tests run once. Alone, `just coverage-check` runs the tests itself, and so does a run after `just test parse`. `integration` saves its run in the same way, in `target/llvm-cov/integration.run.json`.

## integration

| Recipe | What it does |
|---|---|
| `just integration` | Runs the integration tests with `cargo llvm-cov test`: each test target, and each example or bench that sets `test = true`. It writes the coverage to `target/llvm-cov/integration.lcov`. Extra arguments go to cargo llvm-cov test, as for `test` |

An integration test is a test target, such as a file `tests/NAME.rs`, or a `[[test]]` of `Cargo.toml`. It uses the crate from outside, as a user does. A workspace with no test target runs no test, and its report is empty.

Each step reads the targets from `cargo metadata`, and names each target whose key `test` is true, with `--bin`, `--test`, `--example` or `--bench`. So a `[[test]]` with `test = false`, such as a slow suite, does not run, as with a plain `cargo test`. A library has no such name, so `test` gives `--lib`. cargo looks for a name in each package. So when a library sets `test = false`, or two packages have a target of the same name and only one tests it, each package runs in a `cargo llvm-cov` of its own, and the step merges the reports.

An extra argument that chooses targets or packages, such as `--test api` or `-p core`, takes the place of the choice of the step. The step adds `--workspace` only when the arguments choose no package. So `just integration --test slow` runs that suite, and `just test --test api` runs only `tests/api.rs`. An argument after `--` goes to the tests. The doctests, the tests in doc comments, run in neither step. `cargo llvm-cov test` leaves them out unless it gets `--doctests`, and that option needs nightly Rust.

## test-paths

`test-paths` takes the files that the `test-parent` recipe of the tasker role gives it. `test-parent` gives it only the changed files that match the key `tests` of `profile.json`, `tests/**`. When no file matches, as in a change with unit tests only, `test-parent` does not call `test-paths`. A unit test lives in the file of its code, such as `src/lib.rs`, and a copy of that file in the parent commit would bring the new code too.

A file `tests/NAME.rs` runs as its test target. Any other file in the folder `tests`, such as `tests/common/mod.rs`, runs every test target of that folder. When called by hand with a file of code, `test-paths` skips the file and says so.

The exit code of `test-paths` tells `test-parent` what happened. Each profile gives the same four codes:

- **0** means the tests ran and passed.
- **1** means a test failed or did not build. A new test of new code often does not build on the parent, and that is a failure of the test.
- **5** means no test ran. The files may hold only code and no test file, the test targets may hold no test, or the folder `tests` may have no test target. A test with `#[ignore]` does not run, so it does not count. With no path, `test-paths` runs no test and exits with 5. pytest gives the same code when it collects no test.
- **2** means the run itself failed. cargo may fail, for example on a stale `Cargo.lock` or on a path that it cannot run. Or a test may end the run, as with `std::process::exit` in a test, before its binary gives a result.

`test-paths` runs the tests of each package, and then gives one code. A failed test in any package gives 1, even when another package's run ended early.

A dependency whose build script fails for an outside reason, such as a missing system library, gives 1, not 2. cargo prints the same lines as for a test that does not build. The spec lists this as a limit of the Rust profile.

cargo exits with 101 both when a test fails or does not build and when it fails itself. So `test-paths` reads the error lines of cargo to tell them apart.

`test-paths` reads the output of cargo to count the tests that ran. So it runs cargo with no colors and with neither quiet nor verbose output, whatever the settings of `CARGO_TERM_COLOR`, `CARGO_TERM_QUIET` and `CARGO_TERM_VERBOSE`. A target with `harness = false` prints no count, so its run counts as a test that ran.

`test-paths` reads the name of each test target from `cargo metadata`. So a `[[test]]` with a name other than its file still runs. A file in `tests` that is no test target, as with `autotests = false`, does not run, and `test-paths` says so.

A test binary that names its tests and then stops with no result ends the run early. That happens on `std::process::exit(0)` or `std::process::exit(1)` in a test. `test-paths` then gives 2, as the spec says for every language, and not 1. A test that libtest names as failed before the stop still gives 1. A stop also ends the rest of the run of its package, so a test that would fail in a later binary of that package does not run.

The key `tests` of `profile.json` names `tests/**`, the folder `tests` at the top of the project. A glob cannot name only the folder `tests` of each package of a workspace, because `**/tests/**` also takes a module such as `src/x/tests/mod.rs`, whose new code would reach the parent. So the tests of a member package are not copied to the parent, and `test-review` judges them from the diff.

## docstring

| Recipe | What it does |
|---|---|
| `just docstring-check` | Fails on each item with no doc comment, public or private, and names its file and line |

The step runs `cargo clippy` with the lints `missing_docs` and `clippy::missing_docs_in_private_items`, with every feature. It checks the code of the libraries and the binaries, and not the tests. As the spec says, the step gives the two lints with `-W`, not `-D`, and reads the JSON messages of clippy. It has two reasons. An error in a library stops clippy from checking the binaries that use it, and a generated file must not fail the step.

The step also passes `--cap-lints warn`. So a crate that sets `#![deny(missing_docs)]` still gets the list of items, and a generated file in such a crate still passes. An item in a file that git ignores, such as code that `build.rs` writes into `target/`, counts as generated. An `#[allow(missing_docs)]` in the code wins over the lints of the command line, so it hides its items from the step. The `review` target checks each new `#[allow]`. Each line of its output has the form `src/lib.rs:18:4: missing documentation for a function`.

## api

| Recipe | What it does |
|---|---|
| `just api-check` | Fails on each public item, parameter and return with no comment for the API pages, then runs `api-prose-check` |
| `just api-prose-check [--base REF]` | Checks the API page of each changed file with the checker of nossaifd |

The recipes have the names and the form of the `api` tools of the Python and TypeScript profiles. The step `docs-check` of the docs role runs `api-check`, and so does `just verify`.

`api-check` runs `smalldocs api check` of smalldocs 0.5.12 with `uv`. smalldocs reads the API with `cargo +nightly-2026-11-12 rustdoc`, which writes the JSON format 61. smalldocs reads only the formats 57 and 61, so the profile pins the date. The program `nightly` of the tool installs that toolchain with `rustup toolchain install nightly-2026-11-12 --profile minimal`. Its check is `rustup run nightly-2026-11-12 rustc --version`, which never downloads a toolchain. `rustup toolchain list` and `rustup show active-toolchain` would install a missing pin of the project.

The step fails on three things:

- A public item with no doc comment.
- A parameter with no comment after it on its line, and no line in an `# Arguments` section.
- A return other than `()` with no `# Returns` section.

When `docs/_smalldocs.yml` has the key `api` with a section that says `language: rust`, the step checks only those sections. A section with no `toolchain` gets the pin. Otherwise the step checks each package of the workspace that has a library or a binary. A crate with only a binary gets the pages of its private items, as smalldocs does.

`api-prose-check` builds the API pages into a temporary folder, outside the project. It then runs the checker of nossaifd on the page of each changed API file and on the module comment of that file, the `//!` lines at its top. A changed file changed since `HEAD`. In CI, or with `--base REF`, it changed since the base that `check-changed` of nossaifd takes. Without the prose role, which installs the checker, the step passes. A run that passed is not run again in the same run of `just`, on the same tree and with the same base.

The run files of `api-prose-check` go into `.jnprofile-rust/`, as those of the Python and TypeScript profiles go into `.jnprofile-python/` and `.jnprofile-typescript/`. The tool adds the folder to `.gitignore` and `.worktreedisposable`.

`.agents/just/rust/api_rule.py` holds the rule of the commit gate for the API files. Its function `is_api(path)` is true for a file that rustdoc reads for the pages. It is false in a project without the tool `api` or without the docs role, as `.agents/stack.json` records them. When the rule cannot read the api settings, such as a key `api` written on one line, it is true for each Rust file outside the folders of the tools and `.agents/generated.txt`. So the gate stays on, as with the rules of the Python and TypeScript profiles.

With the key `api` in `docs/_smalldocs.yml`, the crates are those of its Rust sections. A `manifest` names a crate from `docs/`, and a section with no `manifest` takes the `Cargo.toml` of its root, as smalldocs does. Without the key, the crate of a file is the nearest folder above it with a `Cargo.toml`.

In a crate with a library, the files are `src/lib.rs` and the modules under `src/`. In a crate with only a binary, they are `src/main.rs` and its modules. The binaries in `src/bin/`, the tests, the examples, `build.rs` and the files of `.agents/generated.txt` are not API files. The rule reads no TOML, so a library at another path, with `path` in `[lib]`, does not count. `api-prose-check` takes its changed files by the same rule, without the check of the tool and the role.

## Reviewed coverage exclusions

Stable Rust has no pragma that leaves out one line from the coverage. So the profile reads two lists of files:

- **`.agents/coverage-exclude.txt`** lists the files that `coverage-check` leaves out.
- **`.agents/generated.txt`** lists the generated files. Both `coverage-check` and `docstring-check` leave them out.

Each line of a list is a path or a glob, with the rules of git pathspecs. A `*` does not match a `/`, `**` matches any number of folders, and a path that ends with `/` names a folder. A line that starts with `#` is a comment. Write the reason for each entry in a comment above it.

The steps read both lists from the parent commit, with `git show HEAD:./PATH`, which names the path from the top of the project. So a new entry counts only after it is in a commit of its own, and a commit cannot exempt its own code. With no parent, or when the parent has no such file, the list is empty. The `test-review` target judges each new entry.

Independent review may also accept exact `coverage-exclude.txt` paths for unchanged source from a pinned
third-party release. Review must precede the import, and the entries need a separate prior commit.
Each comment must name the release, reason and review evidence. No glob or folder entry qualifies.

The review requires the complete archive digest and inventory, accounts for every imported path and local
change, and proves byte identity for each excluded file. The project must always check complete source and
provenance in `just check` and CI. A staged commit guard must check the inventory and bytes to be committed.

The profile does not implement these project checks or infer exemptions from dependency paths.

Keep meaningful caller behavior and regression tests. Integrity replaces first-import line coverage only;
it never qualifies runtime behavior. Each release or byte change needs new independent review.
First-party and locally adapted Rust files cannot use this exception.

Handwritten upstream build scripts
and helpers remain reachable logic. Actual generated bindings belong in `generated.txt` with their reason.
The shipped Rust `test-review` rules give the full review conditions.

## The helper script

The recipes of `coverage-check`, `docstring-check`, `test-paths` and `api-check` run `.agents/just/rust/steps.py` with `uv`. The script needs Python 3.11 or later and no package. The api commands run it again under smalldocs, whose PyYAML reads `docs/_smalldocs.yml`. It reads the lists from the parent commit, and it leaves the excluded files out of a copy of the coverage report, in `target/jnprofile-rust/`. It also reads the JSON messages of clippy, and prints each item with no doc. `coverage-check` runs `diff-cover` 11.0.1 with `uv run --with`.

## Recommendations

Prefer tests and coverage for reachable code. For an unchanged upstream import, review the exact exclusions
and the project source checks first, then commit the exclusions before the import.
