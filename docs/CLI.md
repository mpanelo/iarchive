# CLI reference and developer commands

For the complete command chain, start with the [end-to-end workflow](workflows.md).
This page is the option and developer-check reference.

Run from the repository root. Requirements: stable Rust/Cargo, uv, Python
3.12–3.14 managed by uv, and internet access for initial dependency installation.
ffprobe and icloudpd are optional for the current commands.

## Bootstrap and checks on macOS

Use the committed lockfiles. On a network-enabled development host:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
rustup component add rustfmt clippy
cargo install --locked cargo-deny --version 0.20.2
uv sync --locked --project image-worker --group dev
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
IARCHIVE_REQUIRE_WORKER_TESTS=1 cargo test --workspace --all-features --locked
cargo deny --locked --all-features check
uv run --locked --project image-worker --group dev pytest image-worker/tests
uv run --locked --project image-worker python -m unittest discover -s tests -v
uv run --locked --project tools/cloud-probe --python 3.12 python -m unittest discover -s tools/cloud-probe -v
cargo build --release --locked
```

Update lockfiles deliberately alongside dependency changes; see [development tooling](development.md). If Python dependencies are absent, raw import/index/export still work. The Rust worker integration test requires the uv venv; CI sets `IARCHIVE_REQUIRE_WORKER_TESTS=1` to prevent skipping it.

## Synthetic examples

The [end-to-end workflow](workflows.md#2-create-a-fresh-demo-and-define-paths)
uses fresh paths for one synthetic cycle on both platforms. It includes valid
PNG/JPEG/HEIC fixtures, inventory, audit, export, copying, browser review and
final deep verification. Use that guide's variables consistently rather than
mixing state paths from different demos.

The generator's `--clean` option selects valid images. Omitting it includes
intentionally corrupt/unsupported files, dummy video/RAW bytes, an empty JPEG
and a `.partial` file; inventory/audit then report incomplete results and exit 2.
These are synthetic bytes, not genuine videos or RAW images. Existing fixture
directories and manifest filenames are never overwritten.

## Options and exit codes

All commands have `--help`, global `--state-dir`, `--json`, and `--dry-run`. Dry runs preview writes without changing media or state. Replica dry runs still read and hash media for preflight; inventory/audit dry runs do not scan or invoke the image worker. Doctor is already diagnostic and performs local capability checks. Normal output is readable JSON; `--json` emits compact JSON reports and structured stderr logs. `RUST_LOG=warn` suppresses routine progress logs.

- `init --primary PATH [--secondary PATH]`: create TOML config/DB in app state; primary must exist. An unavailable secondary is allowed in config, with readiness unverified. A changed existing config is not silently overwritten.
- `import --source PATH --cycle ID`: register the canonical source root. Rerunning for the same root/ID is idempotent. Cycle IDs are ASCII letters, digits, `_`, `-` (1–80 characters).
- `index --cycle ID [--deep]`: hash all regular files; resume unchanged records using metadata unless `--deep`. Corrupt image bytes may still be hashable: run audit for decode validity.
- `audit --cycle ID [--categories exact,similar,dark] [--worker-project PATH] [--timeout-seconds 60] [--cache-limit-mib 1024]`: local candidate generation; no file actions. `--categories exact` needs no Python and deliberately reports an incomplete visual audit.
- `export-manifest --cycle ID --output PATH`: atomic, non-overwriting JSONL export outside media/app state. Partial/error inventories remain exportable but exit 2.
- `replicate --cycle ID --secondary PATH --primary-artifacts PATH --secondary-artifacts PATH`: non-overwriting raw copy and independent uncached hashing of both roots. Existing roots and separate artifact directories are required; see [replication](replication.md).
- `verify --cycle ID --deep --secondary PATH --primary-artifacts PATH --secondary-artifacts PATH`: independently rehash the frozen file set without copying media. Both commands require `--allow-unverified-devices` when physical independence cannot be proved; acknowledgment does not verify independence.
- `status --cycle ID`, `report --cycle ID`: view recorded state/results; no fresh verification.
- `review --cycle ID [--port 0]`: serve saved candidates/previews on `127.0.0.1` and record review notes in SQLite. Open the complete printed URL and stop with Ctrl+C. The state lock stays held until shutdown; stop review before running another state-writing command. A dry run opens no listener and saves no decisions.
- `doctor [--primary PATH] [--secondary PATH]`: free space, availability, optional uv/ffprobe/icloudpd checks and default worker probe. It does not install anything or prove physical disks distinct.

Exit 0 = requested operation completed (not a backup-safety claim). Exit 1 = invocation/config/state/process error. Exit 2 = completed report with incomplete inventory/visual audit/replica checks or unavailable requested drive. A mixed library with videos can be indexed successfully while its visual audit remains incomplete.

Default state comes from `directories::ProjectDirs`: `%LOCALAPPDATA%\iarchive\data` on Windows and `~/Library/Application Support/iarchive` on macOS. Use an explicit state path when diagnosing configuration. The worker defaults to `image-worker` under the current working directory; pass `--worker-project` when running the executable elsewhere. The worker currently requires a local checkout and its Python environment.

## Windows 11 x64 / PowerShell

Use the [end-to-end workflow](workflows.md#1-set-up-the-checkout) for setup,
automated checks and the complete command sequence. The
[Windows setup guide](windows-testing.md) explains prerequisite detection,
project-local tool storage, process-only execution policies and troubleshooting.
The [two-drive acceptance guide](windows-two-drive-test.md) tests fresh synthetic
files on selected physical drives; it never scans existing archives.

The [existing-archive section](workflows.md#use-the-workflow-with-an-existing-archive)
explains source, replica, state and artifact paths and when to require physical
independence. Local inventory counts and successful byte checks do not prove
complete iCloud coverage.
