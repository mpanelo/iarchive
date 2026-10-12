# Full iarchive workflow

Run from the checkout. Choose Windows or macOS below. Run each command
separately and stop if it fails. Use the same state directory and cycle throughout.

```text
doctor
init
import
index
audit
report
export-manifest
replicate --dry-run
replicate
review
Ctrl+C to stop review
verify --deep
status
```

The demo creates fresh files under `.smoke/workflow-<id>`. Its two copies are on
one volume: it checks bytes, not independent physical backups. Source media,
replica media, app state, exports and backup bundles use separate folders.

## Windows

From a regular PowerShell window in the checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

Setup reuses installed tools and installs missing prerequisites. If it installs
tools, open a fresh terminal before testing. See [Windows setup](windows-testing.md).

### Prepare the demo

Keep these variables in the same terminal:

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
New-Item -ItemType Directory -Force -Path $secondary, $primaryArtifacts, $secondaryArtifacts, $exports
& $python .\fixtures\generate.py --output $primary --clean --with-heic
$replicaArgs = @('--cycle', $cycle, '--secondary', $secondary, '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts, '--allow-unverified-devices')
```

### Register, scan and export

```powershell
& $exe --state-dir $state doctor --primary $primary --secondary $secondary
& $exe --state-dir $state init --primary $primary --secondary $secondary
& $exe --state-dir $state import --source $primary --cycle $cycle
& $exe --state-dir $state index --cycle $cycle
& $exe --state-dir $state audit --cycle $cycle --worker-project $worker
& $exe --state-dir $state report --cycle $cycle
& $exe --state-dir $state export-manifest --cycle $cycle --output (Join-Path $exports 'inventory.jsonl')
```

### Preview and copy

Check the preview before running the copy:

```powershell
& $exe --state-dir $state --dry-run replicate @replicaArgs
& $exe --state-dir $state replicate @replicaArgs
```

### Review

```powershell
& $exe --state-dir $state review --cycle $cycle
```

Open the full printed URL. Save your notes, then press **Ctrl+C** in the terminal.
The server must stop before the next command.

### Verify and check status

```powershell
& $exe --state-dir $state verify --deep @replicaArgs
& $exe --state-dir $state status --cycle $cycle
```

## macOS

With Rust/Cargo and uv installed, build from the checkout:

```sh
export CARGO_HOME="$PWD/.cargo-home"
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --project image-worker --group dev --python 3.12
cargo build --release --locked
```

### Prepare the demo

Use zsh or Bash. Keep these variables in the same terminal:

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
replica_args=(--cycle "$cycle" --secondary "$secondary" --primary-artifacts "$primary_artifacts" --secondary-artifacts "$secondary_artifacts" --allow-unverified-devices)
```

### Register, scan and export

```sh
"$exe" --state-dir "$state" doctor --primary "$primary" --secondary "$secondary"
"$exe" --state-dir "$state" init --primary "$primary" --secondary "$secondary"
"$exe" --state-dir "$state" import --source "$primary" --cycle "$cycle"
"$exe" --state-dir "$state" index --cycle "$cycle"
"$exe" --state-dir "$state" audit --cycle "$cycle" --worker-project "$worker"
"$exe" --state-dir "$state" report --cycle "$cycle"
"$exe" --state-dir "$state" export-manifest --cycle "$cycle" --output "$exports/inventory.jsonl"
```

### Preview and copy

Check the preview before running the copy:

```sh
"$exe" --state-dir "$state" --dry-run replicate "${replica_args[@]}"
"$exe" --state-dir "$state" replicate "${replica_args[@]}"
```

### Review

```sh
"$exe" --state-dir "$state" review --cycle "$cycle"
```

Open the full printed URL. Save your notes, then press **Ctrl+C** in the terminal.
The server must stop before the next command.

### Verify and check status

```sh
"$exe" --state-dir "$state" verify --deep "${replica_args[@]}"
"$exe" --state-dir "$state" status --cycle "$cycle"
```

## What to expect

- The clean demo reaches `AUDITED` with no file or decode errors.
- A successful copy or deep check reports `complete: true` and
  `local_replica: LOCAL BYTES VERIFIED`.
- A preview reports `complete: false` because it has not copied anything.
- `status` shows saved `replica_state: VERIFIED`, but its local byte status stays
  `NOT VERIFIED`: status does not read current media.

Final verification saves fresh bundles with your review notes. Keep `$run` and
its reports. Each bundle contains `manifest.jsonl`, `manifest.sqlite` and
`summary.json`. Cloud coverage remains **NOT VERIFIED**.

## Existing archives

After the demo passes, replace its paths with your archive paths. Skip fixture
generation. Use an existing source and replica directory, artifact directories
on their respective volumes, state outside media, and a cycle ID for that snapshot.
Pause downloaders and other programs writing to those folders.

For two Windows drives, remove `--allow-unverified-devices` and require
`independent_physical_drives: VERIFIED BY OS TOPOLOGY`. If that check fails, stop
and inspect the finding. macOS currently needs the flag and does not prove
physical independence. See [two-drive tests](windows-two-drive-test.md).

## Reruns and failures

- Exit 1: fix the reported error before continuing.
- Exit 2: inspect the findings. Videos or decode errors can leave visual audit
  incomplete while raw-byte copying remains possible. Missing, partial,
  unreadable or conflicting files block verification.
- Rerun replication on unchanged input: matching files are rehashed and skipped.
  Conflicts and extras are kept, never overwritten or pruned.
- After interruption: reconnect the drive and rerun the same copy command.
  Unfinished files restart from zero; old partials remain outside media.
- A new index or audit invalidates saved verification and current review decisions.
- Use fresh demo paths and export filenames. For intentionally changed source
  content or root identity, use a new cycle after inspecting the change.

[CLI options](CLI.md) · [Review controls](review.md) · [Recovery](replication.md#results-and-recovery) · [Safety](safety.md)
