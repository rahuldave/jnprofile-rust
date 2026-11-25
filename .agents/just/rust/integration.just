# The integration tool of the rust profile: the test targets, such as the files of the folder tests, with cargo
# llvm-cov. just install integration adds it, and jnprofile-rust installs this file.

# Run the integration tests with coverage into target/llvm-cov/integration.lcov. Extra arguments go to cargo llvm-cov test: just integration parse. Alone, it saves JLOG_RUN
integration *args:
    {{rust_py}} {{rust_steps}} run integration "$@"

# Run, without coverage, the test targets that the change of the tree may break: those that read a changed file, by
# rustc's dep-info, those that name a changed module, such as app::store, and those that run a binary of a changed
# package. It prints the targets it runs and skips. It is no step of just check, so run just check on the final tree
integration-changed *args:
    {{rust_py}} {{rust_steps}} integration-changed "$@"
