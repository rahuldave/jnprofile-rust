# The test tool of the rust profile: cargo llvm-cov and diff-cover. just install test adds it, and jnprofile-rust
# installs this file.

# The tests with coverage, with every feature, into target/llvm-cov/lcov.info
rust_cov := "cargo llvm-cov test --workspace --all-features --locked --include-build-script --lcov --output-path target/llvm-cov/lcov.info"

# Run the tests with coverage. Extra arguments go to cargo llvm-cov test: just test parse. Alone, it saves JLOG_RUN
test *args:
    rm -f target/llvm-cov/lcov.info
    mkdir -p target/llvm-cov
    {{rust_py}} {{rust_steps}} begin "$@"
    {{rust_cov}} "$@"
    {{rust_py}} {{rust_steps}} stamp

# Fail on each changed line of code that no test runs, and on the first commit on each line. It reads the report of
# test only from the same run of just, JLOG_RUN, and the same tree. Otherwise it runs the tests first
coverage-check:
    {{rust_py}} {{rust_steps}} fresh || { rm -f target/llvm-cov/lcov.info && mkdir -p target/llvm-cov && {{rust_py}} {{rust_steps}} begin && {{rust_cov}} && {{rust_py}} {{rust_steps}} stamp; }
    {{rust_py}} {{rust_steps}} coverage target/llvm-cov/lcov.info

# Run the tests of some files in the folder tests, without coverage: just test-paths tests/parse.rs
test-paths *paths:
    {{rust_py}} {{rust_steps}} test-paths "$@"
