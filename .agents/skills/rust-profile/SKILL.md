---
name: rust-profile
description: Use when you run, add or remove the recipes of a Rust project that jnterfaces set up, such as just lint, just coverage-check, just rust test or just install docstring. It says which tool gives which recipe, what each step checks, and where the files are.
---

# The rust profile

The rust profile of jnterfaces sets up a Rust project with Cargo. A tool is
one unit of the setup, for example `lint`. `just install lint` adds the
recipes of the tool, and `just uninstall lint` takes them out. The tools of
this profile add no crate to `Cargo.toml`. They use `cargo` with the rustup
components `clippy` and `rustfmt`, `cargo-llvm-cov` with the rustup component
`llvm-tools-preview`, and `uv`.

## The tools and their recipes

| Tool | Recipes | What the steps check |
|---|---|---|
| none, always there | `setup`, `build` | nothing |
| `lint` | `fmt`, `fmt-check`, `lint` | the format with rustfmt, and each clippy warning as an error |
| `typecheck` | `typecheck` | the types of the code and the tests, with `cargo check` |
| `test` | `test`, `coverage-check`, `test-paths` | the tests, and that the tests run each changed line |
| `docstring` | `docstring-check` | a doc comment on each item, public or private |

All four tools are the default tools. The base adds `/target/` and
`*.profraw` to `.gitignore`, and `/target/` to `.worktreedisposable`. Every
report of a step goes to `target/`, so no step leaves a file that git sees.

The steps run cargo with `--locked` and `--all-features`. So a step never
makes or changes `Cargo.lock`. Run `just setup` to make or update the file,
and commit it. Code behind each feature counts, but code for another platform
does not.

## What each step does

- **`fmt-check`** runs `cargo fmt --all --check`. It changes no file. To
  format the code, run `just fmt`.
- **`lint`** runs `cargo clippy --workspace --all-targets --all-features
  --locked -- -D warnings`.
- **`typecheck`** runs `cargo check --workspace --all-targets --all-features
  --locked`.
- **`test`** runs the tests with `cargo llvm-cov test`, and writes the coverage to
  `target/llvm-cov/lcov.info`.
- **`coverage-check`** runs `diff-cover` against `HEAD`. It reads the report
  of `test` only when `test` of the same run of `just`, `JLOG_RUN`, wrote it
  for the same tree. Otherwise it runs the tests first.
  `diff-cover` compares the coverage report, an LCOV file, with the diff of
  git. The step fails on each changed line, staged, unstaged or untracked,
  that no test runs. It also fails on each changed `.rs` file that is not in
  the report, such as a file in `examples/` or `src/tests/`. The files in the
  folders `tests` and `benches` of a package do not count. On the first commit of a
  repository, it runs `cargo llvm-cov report --fail-under-lines 100` over all
  the code.
- **`docstring-check`** runs `cargo clippy` with the lints `missing_docs` and
  `clippy::missing_docs_in_private_items`. It prints each item with no doc as
  `FILE:LINE:COLUMN: missing documentation for ...`. It checks the code of
  the libraries and the binaries, and not the tests. It gives the lints with
  `-W` and `--cap-lints warn`, and reads the messages of clippy, so a
  generated file can be left out. A file that git ignores, such as code that
  `build.rs` writes into `target/`, counts as generated.
- **`test-paths PATH...`** runs the tests of some files with `cargo test`.
  A file `tests/NAME.rs` runs the test target `NAME`. Any other file in the
  folder `tests` runs every test target of that folder. It skips a file of
  code, such as `src/lib.rs`, because its unit tests live with the new code.
  It exits with 0 when the tests pass, and with 5 when no test ran, such as
  when the test targets hold only ignored tests. Any other code means a test failed or
  cargo could not build the tests.

## Lines that no test can run

Stable Rust has no pragma that leaves out one line from the coverage. So the
profile reads a list of files instead:

- **`.agents/coverage-exclude.txt`** lists the files that `coverage-check`
  leaves out. Each line is a path or a glob. A `*` does not match a `/`, `**`
  matches any number of folders, and a path that ends with `/` names a folder.
- **`.agents/generated.txt`** lists the generated files. Both
  `coverage-check` and `docstring-check` leave them out.
- **Both lists come from the parent commit**, with `git show HEAD:./PATH`. A new
  entry counts only after it is in a commit of its own. So a commit cannot
  exempt its own code.
- **Each new entry needs a reason** in a comment line above it, for example
  `# the main function only parses the arguments`. The `test-review` target
  judges each new entry.

## How to run a recipe

- **`just rust lint`** runs the recipe of the rust profile.
- **`just lint`** runs the `lint` of each language of the project.
- **`just verify`** runs every installed check of each language, then `git
  diff --check`.
- **Extra arguments** go to the tool, for example `just test parse` runs the
  tests whose names hold `parse`. A filtered run writes a partial coverage
  report and saves no run ID. So `coverage-check` then runs all the tests.

## Where the files are

- **`.agents/just/rust/`** holds the recipe files and `steps.py`, the helper
  script of `coverage-check`, `docstring-check` and `test-paths`. The installer
  writes them. Do not edit them, because the next `just install` names an
  edited file and does not update it. Put your own recipes in the Justfile.
- **`.agents/targets/rust/`** holds the Rust rules for the agentic targets
  `review`, `test-review`, `docs` and `prose`. `jagt render` adds them to the
  packet of each target.

## Rules for agents

- Run the recipes, not the tools. Write `just test`, not `cargo test`.
- Write a doc comment for each item that you add, and a test for each line.
  Then run `docstring-check` and `coverage-check`, and fix what they list.
- Do not add `#[allow(missing_docs)]` to pass `docstring-check`. Write the doc.
