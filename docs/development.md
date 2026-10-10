# Development tooling

Use [the official Rust Style Guide](https://doc.rust-lang.org/style-guide/) for formatting and [the Rust API Guidelines](https://rust-lang.github.io/api-guidelines/) for naming, documentation, traits, and public API design. `rustfmt.toml` selects standard Rust 2024 formatting with a 100-character line width.

Install the compiler components and the dependency checker:

```sh
rustup component add rustfmt clippy
cargo install --locked cargo-deny --version 0.20.2
uv sync --locked --project image-worker --group dev
```

For sandboxed work, use the workspace-local Cargo and uv caches described in [AGENTS.md](../AGENTS.md). Install cargo-deny with that same `CARGO_HOME` and add its `bin` directory to your shell's `PATH` if needed.

## Editor feedback

The VS Code workspace recommends [rust-analyzer](https://rust-analyzer.github.io/book/), formats Rust files on save, and uses Clippy for on-save diagnostics. Other editors can use the same language server. Editor feedback supplements the complete CI checks.

## Required checks

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
IARCHIVE_REQUIRE_WORKER_TESTS=1 cargo test --workspace --all-features --locked
cargo deny --locked --all-features check
uv run --locked --project image-worker --group dev pytest image-worker/tests
uv run --locked --project image-worker python -m unittest discover -s tests -v
```

On PowerShell, set `$env:IARCHIVE_REQUIRE_WORKER_TESTS = '1'` before running `cargo test`. The worker must be installed before the required Rust integration test runs.

Clippy's default lints are enforced as errors in CI. Add stricter lints individually when they catch a relevant risk, with narrowly scoped explanations for exceptions. Do not enable the entire `restriction` group, whose lints can conflict.

`deny.toml` checks advisory, license, dependency-version, and source policies across the supported macOS and Windows targets. It rejects security and unsoundness advisories, unmaintained dependencies, yanked versions, wildcard requirements, and unknown sources. Multiple transitive versions are warnings: the current graph needs both major versions of `syn` and `winnow`. The existing `directories` dependency uses MPL-2.0-licensed `option-ext`, which has an explicit crate-specific license allowance. No advisories are ignored.

The dependency check runs in a separate Linux CI job. Rust formatting, Clippy, Rust integration/unit tests, HEIC/Python tests, and SQLite schema tests run on macOS and Windows. Cargo.lock and image-worker/uv.lock are committed, and CI fails rather than replacing them with newly resolved dependencies.

## Updating dependencies

Resolve changes intentionally with `cargo update` or `uv lock --project image-worker`, then run the required checks and review the lockfile diff. Keep dependency and lockfile changes together. Record results and blockers in PR descriptions or task summaries; commit messages describe what changed and why.
