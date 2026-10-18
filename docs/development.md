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
The worker, pinned ExifTool and cloud probe dependencies are required; missing
dependencies must fail rather than skip integration checks.

Clippy warnings are errors. Add stricter lints only when useful; do not enable
whole pedantic or restriction groups.

### CI

CI runs Rust/Python suites on macOS and Windows and dependency checks on Linux.
Windows checks prerequisite discovery and simulated installation under PowerShell
5.1 and 7; setup uses `-SkipPrerequisites` to prevent real installers.
Same-volume smoke tests use `C:` and upload `windows-script-reports`. These do
not establish clean-machine installation or external-drive behavior.

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

Report checks in task summaries or PRs, not commit messages. Follow
[AGENTS.md](../AGENTS.md) for safety, tests and commits.

## Recorded manual checks

These historical observations used synthetic media. Use [CI](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml)
for routine checks on a particular commit.

### Windows two-drive run — 2026-10-11

PowerShell 5.1.26100.9444, commit `c3a418ffab1341ffe168989f525372a2b8e167d9`.
D: was NTFS; E: was exFAT. Windows reported distinct physical disks.

| Check | Result |
| --- | --- |
| Automated tests | Passed; smoke checks on C:, D: and E:. Individual counts were not retained. |
| Copy and rerun | 12 files verified; rerun copied zero. |
| Recovery | Both drive backups restored into separate state and verified all 12 files. |
| Corruption | One mismatch blocked completion; no conflict was overwritten. |
| Missing file | Rejected, then recopied and verified. |
| Unavailable root | Rejected. exFAT folder identity changed after restoration; the old cycle refused it and a fresh cycle verified all files. |
| Interruption | A 512 MiB copy stopped with a 10 MiB partial. Status stayed RUNNING/NOT VERIFIED. Resume and deep verification passed. |

Both drive summaries recorded `passed: true`; interruption also recorded
`interruption.status: PASSED`. Final reports had no issues, missing files or
mismatches. Source bytes/timestamps and retained partial hashes stayed unchanged.
Raw reports remain local and untracked:

```text
.smoke/windows-369ce8fb6d6740a68b9d5c77b5db9ad7/
.smoke/two-drive-b583cc97b7f846c19693aaabd4ca891a/
.smoke/two-drive-interruption-6b4e4d894808465bbd29f354844737cf/
```

### macOS gallery and script checks — 2026-10-11

Firefox displayed synthetic JPEG/HEIC previews side by side. Saving a flag updated
the Reviewed filter; corrupt HEIC and unknown files remained findings. All 11
source hashes/timestamps stayed unchanged. Ctrl+C stopped the server cleanly.

Portable PowerShell 7.5.4 passed three cases on macOS: workflow acceptance,
same-volume refusal and stopping only an owned child, including cleanup after
failed observation. A separate 512 MiB copy was stopped, resumed and verified
without changing source bytes/timestamps or the retained partial.

These runs did not establish Windows browser acceptance, PowerShell 7 runtime
on Windows, physical USB disconnects, graceful copy cancellation, large-library
performance, minimum-compiler compatibility or complete iCloud coverage.
Cloud experiments have [separate evidence](../tools/cloud-probe/README.md#recorded-observations).
