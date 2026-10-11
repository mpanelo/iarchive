# iarchive development runbook

The executable is `iarchive`. Read local planning instructions before changing scope. Internal planning documents are local-only: do not track or publish them or copy their roadmap into public documentation.

## Plan and permission

- Before editing files or implementing changes, explain the proposed plan, affected files, and relevant tradeoffs to the user, then ask for permission to proceed.
- Wait for explicit approval of that plan before making changes. Read-only inspection may be used to prepare the plan.
- If the scope or approach materially changes, explain the revised plan and obtain approval again.
- Obtain explicit permission before committing or pushing the current changes. Permission from an earlier task is not standing authorization for future tasks.

## Safety invariants

- Inventory and image review are read-only on media. Never read private archives during development; use synthetic fixtures and temporary directories.
- No cloud authentication, deletion, local purge, quarantine, or telemetry in the archive CLI. Individually approved cloud research remains separate. Do not invoke destructive icloudpd flags.
- Unknown, unreadable, partial, missing, or undecodable files are explicit findings, never deletion candidates. Preserve every file variant as a separate inventory entry. Pair confidence remains unknown until metadata proves a relationship.
- A single-root inventory does not prove replication. Deep replica checks report local byte equality separately from physical-drive independence, completed iPhone upload, and cloud coverage. Unknown guarantees remain NOT VERIFIED.
- State, manifests, and thumbnails stay outside archive roots. Never follow symlinks or Windows reparse points. Reject path traversal and unsafe output destinations.
- Scan cache is an optimization, never evidence for a future deletion gate. Replica verification must independently hash both roots without cached hashes. Never overwrite conflicting destination files or prune extra files; interrupted partials remain outside media and never count as verified.
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

## Tests

- Add tests for distinct behavior, meaningful edge cases, safety invariants, or regressions. Check existing coverage first; avoid tests for trivial edits or tests that merely mirror the implementation.
- Extend an existing parameterized test when the new case exercises the same behavior. Keep overlapping unit and integration tests only when they protect different boundaries or system wiring.
- Keep names concise, usually 3–6 words excluding `test_`; use module/class context and put scenario details in fixtures, assertions, or comments. Names must describe what the test actually proves.
- Assert observable outcomes and isolate failure causes so an earlier guard cannot mask the intended check. Negative tests should distinguish the intended rejection from unrelated failures.
- Test our dependency integration rather than third-party behavior alone. Run required tests with their dependencies in CI; do not silently skip them when those dependencies are absent.

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
uv run --locked --project tools/cloud-probe --python 3.12 python -m unittest discover -s tools/cloud-probe -v
```

CI runs formatting, Clippy, and the full Rust/Python suites on macOS and Windows, plus dependency policy checks on Linux. Do not claim Windows runtime verification without a Windows run. Keep Cargo.lock and image-worker/uv.lock committed alongside dependency changes; CI must use these locks rather than resolving new versions. Review cargo-deny findings and document any narrow exceptions in deny.toml. Rust uses one SQLite writer; do not run concurrent mutating commands against the same state directory.

Windows scripts keep project tools and temporary files in the checkout's `.windows-sandbox` directory and use process-only environment settings. Setup detects compatible prerequisites and installs missing ones through WinGet by default; prerequisite installers can change Windows outside the checkout. Use `-SkipPrerequisites` in preconfigured CI to check dependencies without running installers. Exercise discovery and simulated installation tests under PowerShell 7 and Windows PowerShell 5.1; never run real system installers during development on a user machine. Directory scoping is not OS isolation. External-drive tests require explicit drive arguments and use only fresh synthetic runs beneath a marked `iarchive-sandbox` directory. See [Windows setup and testing](docs/windows-testing.md).

See docs/CLI.md for a synthetic smoke test, docs/replication.md for copy/verify workflows, and docs/safety.md for scope limits.
