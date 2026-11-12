You are starting the `jnprofile-rust` project in ~/Projects/jnprofile-rust. It is the jnterfaces profile for Rust projects. The folder has `docs/spec.md` and this brief, on branch `main`, with no commits yet.

## Read first

1. `docs/spec.md`. It is the full design of the commit gates, and this profile is one part of it. Read these sections closely:
   - "Who calls what"
   - "The steps", in particular `coverage-check` and `docstring-check`
   - "Skills and targets"
   - "Agentic targets", the language layer
   - "Gates", the `@code` globs
   - "Where each part lives"
   - "Acceptance Criteria", the profile parts
2. The two profiles that exist, as models for layout, `profile.json`, `skill-package.json`, the `.agents/just/<language>/` modules, the profile skill and the docs site:
   - ~/Projects/jnprofile-typescript
   - ~/Projects/jnprofile-python
3. ~/Projects/jnterfaces, which installs profiles. Read these parts:
   - `stack.json`, the key `profiles`
   - the installer
   - `scripts/shared_files.py` and `scripts/formats.json`
   - `tests/test_install_stack.py`, which shows how the tests make profile packages
   - the `just` skill and its command contract
4. ~/Projects/nossaifd, the writing guide `GUIDE.md` and `check.py`.

Treat the other repos as read-only. Make no edits and no git writes, and do not run `git fetch`.

## What to build

The Rust profile, with the real steps for a Cargo project:

- **`profile.json`.** It detects a Rust project by `Cargo.toml`. It gives the tools, the lines for `.gitignore` and `.worktreedisposable` (for example `target/`), and the `@code` globs (`**/*.rs`).
- **The modules** in `.agents/just/rust/`. They fill `fmt-check`, `lint`, `typecheck`, `test`, `coverage-check` and `docstring-check`, as the spec says:
  - `fmt-check` runs `cargo fmt --check`.
  - `lint` runs `cargo clippy` with `-D warnings`.
  - `docstring-check` uses `-D missing_docs` and `-D clippy::missing_docs_in_private_items`.
  - `coverage-check` uses `cargo llvm-cov` and `diff-cover`.
  - No recipe has a shebang line, so each line runs through `jlog`.
- **The language layer** in `.agents/targets/rust/`. It holds short Rust rules for the `review`, `test-review`, `docs` and `prose` targets, for example the `# Examples` and `# Errors` sections of a doc comment.
- **A skill `rust-profile`**, like `typescript-profile`.
- **A `skill-package.json`.** Its required executables are `cargo`, `cargo-llvm-cov` and `just`.
- **A Justfile and a docs site** with smalldocs, like the other profiles.
- **No `api` tool yet.** It waits for the smalldocs release that reads rustdoc.

Test the profile with a lab. The lab makes a temporary Cargo project and installs the profile with a local copy of the jnterfaces installer, in the way that `tests/test_install_stack.py` does it. jnterfaces has no entry for `rust` in `stack.json` yet. Do not edit jnterfaces. Write the entry that it needs in your report, and in a file `docs/jnterfaces-entry.md`.

`jlog` is being built now in ~/Projects/jlog. If it is not on `PATH`, test the recipes with the default shell, and say so in your report.

## GitHub

You may create the public repo `rahuldave/jnprofile-rust` and push to `main`. Then make a GitHub issue in it, "Commit gates: the Rust profile". Its acceptance list holds the profile criteria of the spec. Link it to rahuldave/agent_gest_git_skills#65. Put `Refs #<issue>` in each commit message.

## Rules of this user

- **Commit messages** end with the line "Coded using Claude". Add no Claude attribution lines.
- **Before each commit,** have an adversarial reviewer try to break the change. Use a subagent on the strongest model. Fix what it finds, with a test for each finding.
- **Use `just` for every operation,** and run the recipes yourself, not raw `cargo` or `uv`.
- **Write each doc** (README, docs, skill) by ~/Projects/nossaifd/GUIDE.md, and check it with ~/Projects/nossaifd/check.py.
- **Some folders are off limits.** Do not touch ~/.claude, the jj fork or the folder of any private project. Read ~/.codex only in read-only mode. Opening its SQLite files counts as a write, so do not open them.
- **Never print the values of environment variables.** This machine has secrets in its environment.
- **If the spec is wrong or unclear** for this profile, write the problem down in your report. Do not change `docs/spec.md` on your own.
- **End each report with four headings, in short lines.** They are Done, Not done, Issues and For you.
