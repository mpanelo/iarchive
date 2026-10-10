# iarchive development runbook

The executable is `iarchive`. Read local planning instructions before changing scope. Internal planning documents are local-only: do not track or publish them or copy their roadmap into public documentation.

## Safety invariants

- Inventory and image review are read-only on media. Never read private archives during development; use synthetic fixtures and temporary directories.
- No cloud authentication, deletion, local purge, quarantine, replication, or telemetry in this phase. Do not invoke destructive icloudpd flags.
- Unknown, unreadable, partial, missing, or undecodable files are explicit findings, never deletion candidates. Preserve every file variant as a separate inventory entry. Pair confidence remains unknown until metadata proves a relationship.
- Local inventory and hashes do not prove replication, independent physical drives, completed iPhone upload, or cloud coverage. Reports must say NOT VERIFIED.
- State, manifests, and thumbnails stay outside archive roots. Never follow symlinks or Windows reparse points. Reject path traversal and unsafe output destinations.
- Scan cache is an optimization, never evidence for a future deletion gate. Future verification must independently hash both drives.
- Interrupted scans must retain per-file progress and invalidate completion; errors must not disappear behind successful counts.

## Commit messages

Use this pattern consistently:

```text
<Capitalized imperative verb> <specific change>

<explanation of what changed and why>
```

- Start the subject with a capitalized imperative verb, such as `Add`, `Fix`, `Update`, `Document`, `Refactor`, or `Remove`.
- Describe the resulting change concretely. Keep the subject at most 72 characters and omit a trailing period.
- Use plain subjects without Conventional Commit prefixes, ticket numbers, or internal planning labels. Preserve the usual casing of names such as `iarchive`, `SQLite`, and `SHA-256`.
- Do not include internal milestone identifiers or planning references in commit subjects or bodies. Describe concrete behavior and changes instead.
- Keep each commit focused on one coherent change. Put related issues and tradeoffs in the body when they help explain the change.
- Require a body for every nontrivial change. Omit it only for trivial changes, such as typo or formatting-only corrections. Explain what changed and why. Separate the body from the subject with a blank line and wrap prose at approximately 72 characters.
- Do not include validation sections, test results, or test/environment blockers in commit messages. Report checks and blockers accurately in PR descriptions or task summaries instead.
- Rewrite existing commit messages only when explicitly requested. Preserve configured signing and hooks; report blockers rather than silently bypassing them.

Subject examples (nontrivial changes also require a body):

```text
Add crash-safe replication to the secondary drive
Fix stale analysis after source hashes change
Document the replication workflow
Document the commit message convention
```

## Checks

Use rustfmt's standard Rust 2024 style and the Rust API Guidelines for public APIs. Run Clippy with warnings treated as errors; enable stricter lints selectively rather than enabling entire pedantic or restriction groups. See [development tooling](docs/development.md) for setup and editor integration.

Use workspace-local caches when sandboxed:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
IARCHIVE_REQUIRE_WORKER_TESTS=1 cargo test --workspace --all-features --locked
cargo deny --locked --all-features check
uv run --locked --project image-worker --group dev pytest image-worker/tests
uv run --locked --project image-worker python -m unittest discover -s tests -v
```

CI runs formatting, Clippy, and the full Rust/Python suites on macOS and Windows, plus dependency policy checks on Linux. Do not claim Windows runtime verification without a Windows run. Keep Cargo.lock and image-worker/uv.lock committed alongside dependency changes; CI must use these locks rather than resolving new versions. Review cargo-deny findings and document any narrow exceptions in deny.toml. Rust uses one SQLite writer; do not run concurrent mutating commands against the same state directory.

See docs/CLI.md for a synthetic smoke test and docs/safety.md for scope limits.
