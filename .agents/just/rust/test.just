# The test tool of the rust profile: cargo llvm-cov and diff-cover. just install test adds it, and jnprofile-rust
# installs this file.

# Run the tests with coverage, with every feature, into target/llvm-cov/lcov.info. Extra arguments go to cargo llvm-cov
# test: just test parse
test *args:
    rm -f target/llvm-cov/lcov.info
    mkdir -p target/llvm-cov
    cargo llvm-cov test --workspace --all-features --locked --include-build-script --lcov --output-path target/llvm-cov/lcov.info "$@"

# Run the tests again, then fail on each changed line of code that no test runs. On the first commit, on each line
coverage-check: test
    {{rust_py}} {{rust_steps}} coverage target/llvm-cov/lcov.info

# Run the tests of some files only, without coverage: just test-paths tests/parse.rs src/lib.rs
test-paths +paths:
    {{rust_py}} {{rust_steps}} test-paths "$@"
