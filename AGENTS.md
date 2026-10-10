# iarchive development runbook

The executable is `iarchive`. Read local planning instructions before changing scope.

## Safety invariants

- Inventory and image review are read-only on media. Never read private archives during development; use synthetic fixtures and temporary directories.
- No cloud authentication, deletion, local purge, quarantine, replication, or telemetry in this phase. Do not invoke destructive icloudpd flags.
- Unknown, unreadable, partial, missing, or undecodable files are explicit findings, never deletion candidates. Preserve every file variant as a separate inventory entry. Pair confidence remains unknown until metadata proves a relationship.
- Local inventory and hashes do not prove replication, independent physical drives, completed iPhone upload, or cloud coverage. Reports must say NOT VERIFIED.
- State, manifests, and thumbnails stay outside archive roots. Never follow symlinks or Windows reparse points. Reject path traversal and unsafe output destinations.
- Scan cache is an optimization, never evidence for a future deletion gate. Future verification must independently hash both drives.
- Interrupted scans must retain per-file progress and invalidate completion; errors must not disappear behind successful counts.

## Checks

Use workspace-local caches when sandboxed:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
cargo fmt --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace
uv run --locked --project image-worker --group dev pytest image-worker/tests
```

CI runs these checks on macOS and Windows. Do not claim Windows runtime verification without a Windows run. Keep Cargo.lock and image-worker/uv.lock committed alongside dependency changes. Rust uses one SQLite writer; do not run concurrent mutating commands against the same state directory.

See docs/CLI.md for a synthetic smoke test and docs/safety.md for scope limits.
