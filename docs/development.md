# Development

Use the [Rust Style Guide](https://doc.rust-lang.org/style-guide/) and
[Rust API Guidelines](https://rust-lang.github.io/api-guidelines/).
Rustfmt uses Rust 2024 style with a 100-character width. VS Code recommends
[rust-analyzer](https://rust-analyzer.github.io/book/) and runs Clippy on save.

## Set up

From the checkout:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
rustup component add rustfmt clippy
cargo install --locked cargo-deny --version 0.20.2
uv sync --locked --project image-worker --group dev
uv run --locked --project image-worker python scripts/setup_metadata.py
uv run --locked --project image-worker python scripts/check_secrets.py setup
```

Add `$CARGO_HOME/bin` to PATH if cargo-deny is not found.
Windows users can use the [setup and test scripts](windows-testing.md).

## Check changes

```sh
uv run --locked --project image-worker python scripts/check_secrets.py check
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
IARCHIVE_REQUIRE_WORKER_TESTS=1 cargo test --workspace --all-features --locked
cargo deny --locked --all-features check
uv run --locked --project image-worker --group dev pytest image-worker/tests
uv run --locked --project image-worker python -m unittest discover -s tests -v
uv run --locked --project tools/cloud-probe --python 3.12 python -m unittest discover -s tools/cloud-probe -v
```

In PowerShell, set `$env:IARCHIVE_REQUIRE_WORKER_TESTS = '1'` before Cargo tests.
Install the worker first. CI requires it and the cloud probe's pinned environment;
missing dependencies must fail rather than skip those checks.

Clippy warnings are errors. Add stricter lints only when useful; do not enable
whole pedantic or restriction groups.

## Secrets

Setup installs checksum-pinned [Gitleaks](https://github.com/gitleaks/gitleaks)
inside the checkout and enables its pre-push guard. Windows setup does this too.
No system installation or cloud credential verification is used.

The guard checks the full history of every pushed branch or commit tag, even
if a secret was removed in a later commit. It also blocks private research
folders and local planning files. Missing or broken tooling blocks the push.
`check` additionally scans staged and working files, including untracked files
that Git does not ignore. Ignored local archives and sessions are never scanned.
Findings show locations and rule names, with secret values redacted.

Each clone needs setup. Existing custom hooks are preserved; setup stops if
they need manual integration. Do not bypass the guard or add broad exceptions.
Review any synthetic-fixture exceptions narrowly. Rotate exposed credentials;
removing a file does not revoke them. CI checks history again after upload;
local hooks can be bypassed and scanners cannot recognize every secret.

## Dependencies

Keep Cargo.lock and both uv.lock files with dependency changes. Update deliberately
with `cargo update` or `uv lock --project <project>`; CI uses the committed locks.

`deny.toml` checks advisories, licenses, versions and sources. No advisories are
ignored. Duplicate `syn`/`winnow` versions are warnings. Crate-specific license
allowances cover `option-ext` (MPL-2.0) and Axum's `matchit` (BSD-3-Clause).

CI runs Rust/Python suites on macOS and Windows and dependency checks on Linux.
Report checks in task summaries or PRs, not commit messages. Follow
[AGENTS.md](../AGENTS.md) for safety, tests and commits.
