# The recipes of the rust profile that every Rust project has. jnprofile-rust installs this file.

# uv runs the helper script of the steps, steps.py, with pinned tree-sitter and Rust grammar packages
rust_py := "uv run --no-project --quiet --python '>=3.11' --with tree-sitter==0.25.2 --with tree-sitter-rust==0.24.0 python"
rust_steps := quote(source_directory() / "steps.py")

# Download every dependency, and make or update Cargo.lock. The steps run with --locked, so commit Cargo.lock
setup:
    cargo fetch

# Build the code of every package of the workspace. Extra arguments go to cargo: just build --release
build *args:
    cargo build --workspace "$@"

# Print the version of the project, X.Y.Z, from Cargo.toml, for the release tag. It prints nothing when there is none
version:
    {{rust_py}} {{rust_steps}} version

# Run the quick checks of a fix round, without coverage: fmt-check, lint, typecheck, and the unit tests of the
# libraries and the binaries with a plain cargo test, whose build stays warm. Each runs only when its tool is
# installed. The tests take the targets of just test. Extra arguments go to cargo test: just rust::quick -p core.
# Run just check once on the final tree
quick *args:
    if [ -f {{ quote(source_directory() / "lint.just") }} ]; then cargo fmt --all --check && cargo clippy --workspace --all-targets --all-features --locked -- -D warnings; fi
    if [ -f {{ quote(source_directory() / "typecheck.just") }} ]; then cargo check --workspace --all-targets --all-features --locked; fi
    if [ -f {{ quote(source_directory() / "test.just") }} ]; then {{rust_py}} {{rust_steps}} quick-test "$@"; fi
