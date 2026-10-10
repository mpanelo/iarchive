# Build and test report

Date: 2026-10-10. Host: macOS, Apple Silicon. Rust 1.96.0, Cargo 1.96.0, uv 0.12.3, system Python 3.14.5. Development used only synthetic media and temporary directories; no private user archives or cloud accounts were accessed. The supplied directory had no Git metadata or application code.

## Implementation delivered

| Area | Source implementation | Acceptance status |
|---|---|---|
| Foundation | Four-crate Rust workspace; CLI/config; versioned transactional SQLite schema; import/init/status/doctor; safety runbook; macOS/Windows CI | Pending full build, lint, platform tests and initial dependency locks |
| Inventory | Streaming read-only inventory/SHA-256; exact byte groups with provenance; per-file resume/cache; missing/error/link/partial/collision findings; atomic JSONL export | Pending cargo integration tests and large-library benchmark |
| Image audit | uv Python project; HEIC/Pillow decoder; EXIF previews; pHash/dark/sharpness metrics; bounded/versioned worker IPC; timed per-file Rust adapter; cached read-only audit and reports | Pending dependencies, real image tests, end-to-end Rust/worker tests and HEIC on both platforms |

These implementation areas remain unverified until their pending checks pass. No cloud/local deletion, quarantine, auth, replication, or verified-backup claim is available.

## Checks completed in the sandbox

- `cargo fmt --check`: passed after formatting.
- `cargo metadata --no-deps --offline`: passed; all four manifests parsed.
- Standalone Rust similarity tests compiled directly with rustc: 2 passed (BK-tree equivalence and bounded homogeneous-hash output).
- Python standard-library protocol tests: 4 passed (error isolation/no metrics, authorized roots, traversal/cache restrictions, oversized request handling).
- Python SQLite schema/query tests: 2 passed (all embedded SQL compiled, unique constraints/foreign keys/candidate cascade). These use Python's SQLite and do not replace rusqlite tests.
- Python syntax compilation: passed for worker, fixture generator, and tests.
- Synthetic PNG/error fixture generation: passed. Source code includes JPEG/HEIC/orientation generation, but that dependency-requiring path was not exercised.

## Blocked checks

`cargo test --workspace` failed before compilation because index.crates.io could not resolve. Offline `cargo test` and `cargo clippy --workspace --all-targets --all-features -- -D warnings` also could not resolve `anyhow`; the local cache did not contain the required dependency set. This is not a compiler/test pass.

`uv lock --project image-worker` failed resolving pypi.org/simple/imagehash/. An offline `uv run --locked ... pytest` then failed because uv.lock does not exist; full Python pytest/HEIC tests could not install dependencies. Cargo.lock and image-worker/uv.lock have deliberately not been fabricated. On a network-enabled host, generate locks, sync the worker, run all checks in CLI.md, and commit the validated locks. CI conditionally bootstraps missing locks and uploads them; remove that bootstrap once the initial locks are committed.

macOS/Windows CI is configured but has not run: no remote repository/Actions run was available. Windows physical-drive smoke tests, independent-device identity and 200 GiB performance are unverified. Cross-platform test code covers Windows junction escape, mixed-case and Unicode filenames, read-only input, missing/partial files, resume, source changes, decoder failure and original preservation.

## Pending checks

Dependency access, resolved lockfiles, full build and test checks, and CI platform results remain pending. No cloud deletion work is authorized for this build.
