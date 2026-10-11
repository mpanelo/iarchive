# CLI and developer commands

Run from the repository root. Requirements: stable Rust/Cargo, uv, Python 3.12–3.14 managed by uv, and network access for the initial dependency installation. ffprobe and icloudpd are optional for the current commands.

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
cargo build --release --locked
```

Update lockfiles deliberately alongside dependency changes; see [development tooling](development.md). If Python dependencies are absent, raw import/index/export still work. The Rust worker integration test requires the uv venv; CI sets `IARCHIVE_REQUIRE_WORKER_TESTS=1` to prevent skipping it.

## Synthetic scan and reports

```sh
python3 fixtures/generate.py --output .smoke/clean-media --clean
target/release/iarchive --state-dir .smoke/state doctor --primary .smoke/clean-media
target/release/iarchive --state-dir .smoke/state --dry-run import --source .smoke/clean-media --cycle demo
target/release/iarchive --state-dir .smoke/state import --source .smoke/clean-media --cycle demo
target/release/iarchive --state-dir .smoke/state index --cycle demo
target/release/iarchive --state-dir .smoke/state audit --cycle demo
target/release/iarchive --state-dir .smoke/state --json report --cycle demo
target/release/iarchive --state-dir .smoke/state export-manifest --cycle demo --output .smoke/manifest.jsonl
```

To include synthetic JPEG/HEIC and rotated HEIC, generate a fresh directory with:

```sh
uv run --locked --project image-worker python fixtures/generate.py --output .smoke/heic-media --clean --with-heic
```

Omitting `--clean` includes intentionally corrupt HEIC, unsupported RAW, unknown bytes, video dummy bytes, an empty JPEG, and a `.partial` file. Inventory/audit then report incomplete results and exit 2 as expected. These bytes are synthetic, not genuine videos/RAW. Existing fixture directories and manifest filenames are never overwritten.

`report` shows saved candidate groups, per-image measurements and preview paths, decode/file errors, and status. Open preview JPEGs from the reported thumbnail cache with a local image viewer. There is no browser gallery in this build.

## Options and exit codes

All commands have `--help`, global `--state-dir`, `--json`, and `--dry-run`. Dry runs describe operations without hashing or invoking the image worker. Doctor is already diagnostic and performs local capability checks. Normal output is readable JSON; `--json` emits compact JSON reports and structured stderr logs. `RUST_LOG=warn` suppresses routine progress logs.

- `init --primary PATH [--secondary PATH]`: create TOML config/DB in app state; primary must exist. An unavailable secondary is allowed in config, with readiness unverified. A changed existing config is not silently overwritten.
- `import --source PATH --cycle ID`: register the canonical source root. Rerunning for the same root/ID is idempotent. Cycle IDs are ASCII letters, digits, `_`, `-` (1–80 characters).
- `index --cycle ID [--deep]`: hash all regular files; resume unchanged records using metadata unless `--deep`. Corrupt image bytes may still be hashable: run audit for decode validity.
- `audit --cycle ID [--categories exact,similar,dark] [--worker-project PATH] [--timeout-seconds 60] [--cache-limit-mib 1024]`: local candidate generation; no file actions. `--categories exact` needs no Python and deliberately reports an incomplete visual audit.
- `export-manifest --cycle ID --output PATH`: atomic, non-overwriting JSONL export outside media/app state. Partial/error inventories remain exportable but exit 2.
- `status --cycle ID`, `report --cycle ID`: view recorded state/results; no fresh verification.
- `doctor [--primary PATH] [--secondary PATH]`: free space, availability, optional uv/ffprobe/icloudpd checks and default worker probe. It does not install anything or prove physical disks distinct.

Exit 0 = requested operation completed (not a backup-safety claim). Exit 1 = invocation/config/state/process error. Exit 2 = completed report with incomplete inventory/visual audit or unavailable requested drive. A mixed library with videos can be indexed successfully while its visual audit remains incomplete.

Default state comes from `directories::ProjectDirs`: `%LOCALAPPDATA%\iarchive\data` on Windows and `~/Library/Application Support/iarchive` on macOS. Use an explicit state path when diagnosing configuration. The worker defaults to `image-worker` under the current working directory; pass `--worker-project` when running the executable elsewhere. The worker currently requires a local checkout and its Python environment.

## Windows 11 x64 / PowerShell

For automated setup, quality checks, synthetic smoke tests, and optional external-drive tests, use the [Windows setup and test scripts](windows-testing.md):

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1 -ExternalDrives 'D:', 'E:'
```

Omit `-ExternalDrives` for local tests only. Clone in a directory of your choice, then run the scripts from the resulting `iarchive` checkout; tools and caches stay inside it. Setup automatically reuses compatible prerequisites and installs missing ones through WinGet; prerequisite installers can change Windows outside the checkout. Use `-SkipPrerequisites` to check without installing. This layout works on Windows Home; directory scoping does not provide OS isolation. External tests write generated fixtures only under each selected volume's `iarchive-sandbox\run-<id>` directory; they never scan your archive.

Install stable Rust with the MSVC build tools and uv, then build and validate the checkout. Windows checks run in CI; real external-drive workflows still need separate runtime checks.

```powershell
uv sync --locked --project image-worker --group dev --python 3.12
cargo build --release --locked
uv run --locked --project image-worker python fixtures/generate.py --output .smoke\windows-media --clean --with-heic
$iarchiveExe = Join-Path $PWD 'target\release\iarchive.exe'
& $iarchiveExe --state-dir .smoke\windows-state import --source .smoke\windows-media --cycle demo
& $iarchiveExe --state-dir .smoke\windows-state index --cycle demo
& $iarchiveExe --state-dir .smoke\windows-state audit --cycle demo
& $iarchiveExe --state-dir .smoke\windows-state report --cycle demo
```

Only after dependency/platform checks and synthetic dry runs pass, these are the intended read-only runtime commands for the existing archive:

```powershell
& $iarchiveExe doctor --primary 'D:\iCloud Photos' --secondary 'E:\iCloud Photos Backup'
& $iarchiveExe init --primary 'D:\iCloud Photos' --secondary 'E:\iCloud Photos Backup'
& $iarchiveExe --dry-run import --source 'D:\iCloud Photos' --cycle 2026-10-10
& $iarchiveExe import --source 'D:\iCloud Photos' --cycle 2026-10-10
& $iarchiveExe index --cycle 2026-10-10
& $iarchiveExe audit --cycle 2026-10-10 --worker-project (Join-Path $PWD 'image-worker')
& $iarchiveExe report --cycle 2026-10-10
```

No E: bytes are copied, verified, or pruned by these commands. Do not interpret local counts as remote library coverage.
