# The recipes of the rust profile that every Rust project has. jnprofile-rust installs this file.

# uv runs the helper script of the steps, steps.py, which needs Python 3.11 or later and no package
rust_py := "uv run --no-project --quiet --python '>=3.11' python"
rust_steps := quote(source_directory() / "steps.py")

# Download every dependency, and make or update Cargo.lock. The steps run with --locked, so commit Cargo.lock
setup:
    cargo fetch

# Build the code of every package of the workspace. Extra arguments go to cargo: just build --release
build *args:
    cargo build --workspace "$@"
