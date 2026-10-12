# End-to-end iarchive workflow

This is the main guide for chaining the available commands. It starts with a
fresh synthetic archive and finishes with a raw replica, saved review decisions
and fresh byte verification. Choose the commands for your platform and keep the
same paths, state directory and cycle throughout the run.

```text
doctor → init → import → index → audit → report → export-manifest
       → replication preview → replicate → review → stop review
       → verify --deep → status
```

Source media stays unchanged. Replication writes to the selected secondary media
directory; review writes notes to app state. Cloud downloads/authentication,
quarantine, restore and deletion are unavailable in the archive CLI.

## 1. Set up the checkout

Run everything from the repository root in a regular terminal. If you already
have a checkout, open it; otherwise clone in your current directory:

```sh
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
```

Choose a writable checkout outside photo archives and synced folders. Initial
setup needs internet access. Run commands separately and inspect their results;
stop on an unexpected failure. The clean demo below should complete each step
with exit 0. See [failures and reruns](#failures-and-reruns) for exit 2 findings.

### Windows PowerShell

Windows 11 x64, including Home, is supported. Use Windows PowerShell 5.1 or
PowerShell 7. Setup detects compatible prerequisites and installs missing ones:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stop: setup failed.' }
```

Setup builds the executable and creates the image-worker environment. Project
tools stay in the checkout, but prerequisite installers can change Windows
outside it. If setup installed prerequisites, open a fresh terminal in the
checkout before continuing. See [Windows setup](windows-testing.md) for details.

Run the full automated checks if they have not passed for this checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stop: automated checks failed.' }
```

These scripts use process-only execution policies. The remaining PowerShell
commands invoke executables directly and do not require changing script policy.

### macOS shell

Install Rust/Cargo and [uv](https://docs.astral.sh/uv/) before this step. Use the
committed dependency locks and keep caches in the checkout:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --project image-worker --group dev --python 3.12
cargo build --release --locked
```

For the automated quality checks, see [development tooling](development.md).

## 2. Create a fresh demo and define paths

The demo uses two directories on one volume. It exercises byte copying and
creates recoverable bundles; **physical-drive independence remains NOT VERIFIED**. Its
replica arguments explicitly acknowledge that limit. For two Windows drives,
use the [physical-drive acceptance guide](windows-two-drive-test.md).

The layout keeps media, artifacts, exports and app state separate:

```text
.smoke/workflow-<unique-id>/
  primary/media/             generated source files
  primary/artifacts/         primary backup bundles
  secondary/media/           copied files
  secondary/artifacts/       secondary bundles and retained partials
  state/                     configuration, SQLite, previews and state bundles
  exports/                   explicit JSONL inventory exports
```

### Windows PowerShell

Keep these variables in the same terminal for the remaining steps. Use the
Python environment created by setup, without installing a system Python:

```powershell
$exe = Join-Path $PWD 'target\release\iarchive.exe'
$worker = Join-Path $PWD 'image-worker'
$python = Join-Path $worker '.venv\Scripts\python.exe'
$run = Join-Path $PWD ('.smoke\workflow-' + [guid]::NewGuid().ToString('N'))
$cycle = 'demo'
$state = Join-Path $run 'state'
$primary = Join-Path $run 'primary\media'
$secondary = Join-Path $run 'secondary\media'
$primaryArtifacts = Join-Path $run 'primary\artifacts'
$secondaryArtifacts = Join-Path $run 'secondary\artifacts'
$exports = Join-Path $run 'exports'

New-Item -ItemType Directory -Force -Path $secondary, $primaryArtifacts, $secondaryArtifacts, $exports | Out-Null
& $python .\fixtures\generate.py --output $primary --clean --with-heic
if ($LASTEXITCODE -ne 0) { throw 'Stop: fixture generation failed.' }

$replicaArgs = @('--cycle', $cycle, '--secondary', $secondary,
    '--primary-artifacts', $primaryArtifacts,
    '--secondary-artifacts', $secondaryArtifacts, '--allow-unverified-devices')
Write-Host "Keep this run: $run"
```

### macOS shell

These commands work in zsh or Bash. Keep the variables in the same terminal:

```sh
exe="$PWD/target/release/iarchive"
worker="$PWD/image-worker"
mkdir -p .smoke
run=$(mktemp -d "$PWD/.smoke/workflow-XXXXXX")
cycle=demo
state="$run/state"
primary="$run/primary/media"
secondary="$run/secondary/media"
primary_artifacts="$run/primary/artifacts"
secondary_artifacts="$run/secondary/artifacts"
exports="$run/exports"

mkdir -p "$secondary" "$primary_artifacts" "$secondary_artifacts" "$exports"
uv run --locked --project image-worker python fixtures/generate.py --output "$primary" --clean --with-heic

replica_args=(--cycle "$cycle" --secondary "$secondary"
  --primary-artifacts "$primary_artifacts"
  --secondary-artifacts "$secondary_artifacts" --allow-unverified-devices)
printf 'Keep this run: %s\n' "$run"
```

The generator refuses to overwrite an existing source. Starting this section
again creates a new run instead of changing previous results. Do not reuse the
intentionally corrupted fixtures left by the Windows test scripts.

## 3. Check tools, configure and register the source

`doctor` checks availability, free space and local tools. The image worker should
be available for this visual demo. Missing optional icloudpd or ffprobe does not
block the local workflow; doctor does not install tools or authenticate.

`init` creates configuration and state outside media. `import` registers the
source under a cycle ID; it does not download files or establish cloud coverage.

**Windows:**

```powershell
& $exe --state-dir $state doctor --primary $primary --secondary $secondary
& $exe --state-dir $state init --primary $primary --secondary $secondary
& $exe --state-dir $state import --source $primary --cycle $cycle
```

**macOS:**

```sh
"$exe" --state-dir "$state" doctor --primary "$primary" --secondary "$secondary"
"$exe" --state-dir "$state" init --primary "$primary" --secondary "$secondary"
"$exe" --state-dir "$state" import --source "$primary" --cycle "$cycle"
```

Expect the imported cycle to report `IMPORTED`. At this point no media has been
copied or visually analyzed. `init` can be omitted when you supply explicit paths
to every command; it is included here to show the complete configuration flow.

## 4. Index, audit and inspect the report

`index` inventories and hashes files. `audit` refreshes that inventory, reuses
eligible cached work and generates previews and candidate groups. Run both
before replication: a new scan invalidates saved replica completion and requires
fresh review decisions. `report` displays the recorded candidates and findings.

**Windows:**

```powershell
& $exe --state-dir $state index --cycle $cycle
& $exe --state-dir $state audit --cycle $cycle --worker-project $worker
& $exe --state-dir $state report --cycle $cycle
```

**macOS:**

```sh
"$exe" --state-dir "$state" index --cycle "$cycle"
"$exe" --state-dir "$state" audit --cycle "$cycle" --worker-project "$worker"
"$exe" --state-dir "$state" report --cycle "$cycle"
```

The clean image demo should reach `AUDITED` with exact, similar and dark
candidates, without file or decode errors. Dark and similar candidates are
suggestions for human review. They do not identify files to remove.

## 5. Export the recorded inventory

This produces a versioned JSONL summary and individual file records, outside
media and app state. It is an inventory export, not a verified two-drive backup
or an export of review decisions. Existing output filenames are refused.

**Windows:**

```powershell
& $exe --state-dir $state export-manifest --cycle $cycle --output (Join-Path $exports 'inventory.jsonl')
```

**macOS:**

```sh
"$exe" --state-dir "$state" export-manifest --cycle "$cycle" --output "$exports/inventory.jsonl"
```

## 6. Preview replication, then copy

First inspect the dry-run report. It reads and hashes media to check conflicts,
capacity and roots, but writes no files or state. A successful preview exits 0
with `complete: false`: it previews work rather than certifying completion.

**Windows:**

```powershell
& $exe --state-dir $state --dry-run replicate @replicaArgs
```

**macOS:**

```sh
"$exe" --state-dir "$state" --dry-run replicate "${replica_args[@]}"
```

If preflight passes, run the same operation without `--dry-run`:

**Windows:**

```powershell
& $exe --state-dir $state replicate @replicaArgs
```

**macOS:**

```sh
"$exe" --state-dir "$state" replicate "${replica_args[@]}"
```

Replication copies every eligible raw file, independently hashes both roots and
publishes three backup bundles: app state, primary artifacts and secondary
artifacts. Expect `complete: true`, `local_replica: LOCAL BYTES VERIFIED`, and
`cloud_coverage: NOT VERIFIED`. Physical independence stays unverified in this
same-volume demo. Candidate flags never filter the copied files.

Each bundle contains `manifest.jsonl`, `manifest.sqlite` and `summary.json`.
Retain the printed `artifact_bundles` paths and summary digests. See
[replication and recovery](replication.md#results-and-recovery) for restoring a
standalone database backup into empty state.

## 7. Review and save decisions

**Windows:**

```powershell
& $exe --state-dir $state review --cycle $cycle
```

**macOS:**

```sh
"$exe" --state-dir "$state" review --cycle "$cycle"
```

Open the complete printed `review_url`, including its `#` fragment. The gallery
runs on `127.0.0.1` and serves saved previews. Use its filters to compare
candidates, then keep all, flag selected files for later, defer, or clear a note.
Decisions save immediately to SQLite; no media is moved or deleted.

**Stop the server with Ctrl+C before running the next command.** Review holds the
state lock while running. Reopening the same saved audit preserves current notes;
reindexing or auditing again leaves earlier notes as history. See
[browser review](review.md) for controls, missing previews and session details.

## 8. Deeply verify and inspect saved status

After review stops, rehash both media roots and publish fresh backup bundles.
These whole-database backups include the review notes saved before this check;
review alone does not refresh earlier bundles.

**Windows:**

```powershell
& $exe --state-dir $state verify --deep @replicaArgs
& $exe --state-dir $state status --cycle $cycle
```

**macOS:**

```sh
"$exe" --state-dir "$state" verify --deep "${replica_args[@]}"
"$exe" --state-dir "$state" status --cycle "$cycle"
```

Require a successful verification report with `complete: true`, no missing files,
extras or hash mismatches, and `local_replica: LOCAL BYTES VERIFIED`. Keep its
new bundle paths and digests along with the run directory.

`status` should show the last `replica_state` as `VERIFIED`. Its `local_replica`
field still says **NOT VERIFIED** because status reads saved state, not current
media. The preceding deep-verification report is the current byte observation;
a later status read cannot renew that observation. Cloud coverage, completed
iPhone uploads and offsite protection remain unverified.

## Use the workflow with an existing archive

First complete the synthetic checks on your platform. Use your own existing
source directory, an explicitly selected secondary media directory, separate
artifact directories on their corresponding volumes, state outside all media,
and a cycle ID for the intended snapshot. All media/artifact roots must exist.
Pause downloaders and other programs writing to those roots during the workflow.
There is no cloud download command; use an existing local download as the source.

Change the path variables from step 2 instead of regenerating fixtures over an
archive. For example, a Windows layout could use:

```text
D:\iCloud Photos\                 source media, unchanged
D:\iarchive-artifacts\            primary bundles
E:\iCloud Photos Backup\          selected replica destination
E:\iarchive-artifacts\            secondary bundles
checkout\.smoke\archive-state\    app state and previews, outside media
checkout\.smoke\archive-exports\  inventory exports, outside state/media
```

Use a fresh state directory if it would otherwise reuse a demo configuration.
Do not run the fixture generator on these archive paths. No folders are moved or
renamed automatically. Existing replica conflicts and extras remain untouched
and prevent completion; inspect them rather than deleting them to force a pass.

For distinct Windows drives, **remove `--allow-unverified-devices` from the
replica arguments** and require `independent_physical_drives: VERIFIED BY OS
TOPOLOGY`. Two drive letters alone do not prove two disks. If topology is unknown,
stop and review the finding before choosing a separately acknowledged byte-only
run. This build does not resolve physical backing disks on macOS, even when
volumes differ; the acknowledgment remains necessary and independence unverified.

Confirm iPhone uploads and Personal/Shared Library coverage separately. These
confirmations and local file counts do not establish complete cloud coverage.
Review notes give no permission to remove archive or cloud files.

## Failures and reruns

Use `--json` for machine-readable reports, or `--help` for any command's options.
Global `--dry-run` previews writes; replica previews still read and hash files.

| Result or situation | What to do |
| --- | --- |
| Exit 0 | The requested operation completed. Read the operation's report; this is not a cloud-safety claim. |
| Exit 1 | Stop and fix the invocation, unavailable tool, configuration or state-lock error. |
| Exit 2 from inventory/replication/verification | Keep the findings. Missing, partial, unreadable or conflicting files cannot count as verified. |
| Exit 2 from audit | Inspect the report. Videos, unsupported formats and decode failures keep visual audit incomplete. A visual finding alone does not prevent raw-byte backup; inventory and replica checks must still pass. |
| Exit 2 from export | The export retains incomplete inventory findings. Preserve it for inspection. |
| Repeat import/init with the same configuration | Existing source registration/configuration is reused; changing configured roots requires an explicit configuration change or fresh state. |
| Repeat index/audit | Eligible cached work can be reused, but a scan invalidates saved replica completion and current review decisions. |
| Repeat replicate on unchanged inputs | Matching destinations are independently rehashed and skipped. No conflicts are overwritten or extras pruned. |
| Interrupted copy | Restore availability and rerun the same replicate command. Incomplete files restart from byte zero; old partials stay outside media and never count as verified. |
| Changed source snapshot or replaced root identity | Keep the old record. Investigate unexpected changes; register a new cycle only for an intentionally changed snapshot. |
| Repeat export or demo generation | Choose a new export filename or fresh run directory; existing destinations are never overwritten. |

The [CLI reference](CLI.md) covers all options and exit codes. The
[replication guide](replication.md) explains capacity, physical identity,
conflicts and recovery. [Safety](safety.md) describes what remains unverified.
