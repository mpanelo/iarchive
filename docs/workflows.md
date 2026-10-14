# iarchive walkthrough

Run from the checkout. Start with the demo, then use the commands below for an
existing archive.

## Windows demo

Allow scripts in this session, run setup, then start the guided demo:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\windows\setup.ps1
.\scripts\windows\demo.ps1
```

The policy lasts only for this PowerShell session. Repeat the policy command in
each new window or tab. If setup installs tools, open a fresh terminal before
starting the demo.
See [Windows setup](windows-testing.md) for installer details and automated tests.

## macOS demo

With Rust/Cargo and uv installed:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --project image-worker --group dev --python 3.12
cargo build --release --locked
sh scripts/demo.sh
```

## What the demo does

The script creates fresh synthetic images and separate media, state and report
folders under `.smoke/workflow-<id>`. It prints that path and retains all results.
It stops on a failed command.

```text
doctor
init
import
index
audit
report
export-manifest
replicate --dry-run
Confirm the copy
replicate
review
Ctrl+C to finish review
verify --deep
status
```

Check the replication preview and answer `y` to copy. Open the gallery's printed
URL, save your notes, then press **Ctrl+C** in the terminal. The script waits for
the gallery to stop before verifying. Ctrl+C elsewhere cancels the demo.

Both copies are on one volume. This demo verifies local bytes; physical backup
independence and cloud coverage remain **NOT VERIFIED**.

## Existing Windows archives

These examples use an existing source on D: and a replica on E:. Change the paths
and cycle ID to match your archive. Keep state and artifacts outside media. Pause
any downloaders or other programs writing to these folders.

Create the replica and artifact folders if needed:

```powershell
New-Item -ItemType Directory -Force 'E:\iCloud Photos Backup', 'D:\iarchive-artifacts', 'E:\iarchive-artifacts'
```

Run each command separately and stop if it fails. These commands use the default
app state directory. Add `--state-dir PATH` to every command to choose another.

```powershell
.\target\release\iarchive.exe doctor --primary 'D:\iCloud Photos' --secondary 'E:\iCloud Photos Backup'
.\target\release\iarchive.exe init --primary 'D:\iCloud Photos' --secondary 'E:\iCloud Photos Backup'
.\target\release\iarchive.exe import --source 'D:\iCloud Photos' --cycle archive-2026-10-13
.\target\release\iarchive.exe index --cycle archive-2026-10-13
.\target\release\iarchive.exe audit --cycle archive-2026-10-13
.\target\release\iarchive.exe report --cycle archive-2026-10-13
.\target\release\iarchive.exe export-manifest --cycle archive-2026-10-13 --output 'D:\iarchive-artifacts\inventory-2026-10-13.jsonl'
.\target\release\iarchive.exe --dry-run replicate --cycle archive-2026-10-13 --primary-artifacts 'D:\iarchive-artifacts' --secondary-artifacts 'E:\iarchive-artifacts'
```

Check the preview before copying:

```powershell
.\target\release\iarchive.exe replicate --cycle archive-2026-10-13 --primary-artifacts 'D:\iarchive-artifacts' --secondary-artifacts 'E:\iarchive-artifacts'
.\target\release\iarchive.exe review --cycle archive-2026-10-13
```

Open the printed URL and save your notes. Press **Ctrl+C** to stop the gallery,
then run:

```powershell
.\target\release\iarchive.exe verify --deep --cycle archive-2026-10-13 --primary-artifacts 'D:\iarchive-artifacts' --secondary-artifacts 'E:\iarchive-artifacts'
.\target\release\iarchive.exe status --cycle archive-2026-10-13
```

For two physical Windows drives, require
`independent_physical_drives: VERIFIED BY OS TOPOLOGY`. Stop if that check fails.
Use the [two-drive tests](windows-two-drive-test.md) before trusting real archives.

On macOS, use the same command options with `./target/release/iarchive` and your
volume paths. Replication and deep verification need `--allow-unverified-devices`;
macOS currently cannot prove physical-drive independence.

## Results and reruns

- A successful copy or deep check reports `complete: true` and
  `local_replica: LOCAL BYTES VERIFIED`.
- A preview reports `complete: false` because it has not copied anything.
- `status` shows saved state, not a fresh byte check. Its local byte status remains
  `NOT VERIFIED` even after successful verification.
- Final verification saves fresh bundles with your review notes. Each contains
  `manifest.jsonl`, `manifest.sqlite` and `summary.json`.
- Exit 1 means an error; exit 2 means findings or incomplete checks. Inspect the
  report before continuing. Videos can remain visually unaudited while raw-byte
  copying succeeds.
- After interruption, reconnect the drive and rerun the same copy command.
  Matching files are rehashed and skipped; conflicts and extras are kept.
- A new index or audit invalidates saved verification and current review decisions.
- Use a new cycle for an intentionally changed snapshot and a fresh filename for
  each inventory export. Demo scripts always create a fresh run.

[CLI options](CLI.md) · [Review controls](review.md) · [Recovery](replication.md#results-and-recovery) · [Safety](safety.md)
