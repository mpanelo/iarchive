# Replication and deep verification

`replicate` copies every readable raw file, including videos, edits, sidecars,
unknown formats and zero-byte files. It preserves relative filenames and attempts
to preserve modification times. It does not decode, convert, rename or filter
files based on image-review candidates. Empty directories and filesystem metadata
such as ACLs, extended attributes and alternate data streams are not archived.

**Experimental:** start with synthetic files. Windows runtime and real USB
disconnect/reconnect behavior still require testing for this implementation.
These commands never approve cloud removal.

## Directory layout

All four directories must already exist. Media and artifact directories must be
separate, with no overlapping roots, symlinks or Windows reparse points. App state
must also be outside them. Each artifact directory must be on the same volume as
its corresponding media directory. State and artifact directories must not sit
inside another imported or configured archive.

```text
primary volume/
  media/                      original source, read-only
  artifacts/                  manifests, summaries and SQLite backups
secondary volume/
  media/                      destination, never overwritten or pruned
  artifacts/                  backup bundles and retained copy partials
app state/                    SQLite progress and another backup bundle
```

Use `--secondary PATH`, or the secondary configured with `init`. Both artifact
paths are explicit on every invocation. A missing root is treated as unavailable;
the command does not recreate a disconnected media root.

## Try local synthetic copies on macOS

Run from the checkout after `cargo build --locked`. These are synthetic byte
files; image decoding is not part of replication.

```sh
mkdir -p .smoke
replica_demo=$(mktemp -d "$PWD/.smoke/replica-demo-XXXXXX")
mkdir "$replica_demo/source" "$replica_demo/secondary" \
  "$replica_demo/primary-artifacts" "$replica_demo/secondary-artifacts"
mkdir "$replica_demo/source/nested"
printf 'synthetic original\n' > "$replica_demo/source/photo.bin"
printf 'synthetic metadata\n' > "$replica_demo/source/nested/photo.xmp"

target/debug/iarchive --state-dir "$replica_demo/state" import \
  --source "$replica_demo/source" --cycle demo

target/debug/iarchive --state-dir "$replica_demo/state" --dry-run replicate \
  --cycle demo --secondary "$replica_demo/secondary" \
  --primary-artifacts "$replica_demo/primary-artifacts" \
  --secondary-artifacts "$replica_demo/secondary-artifacts" \
  --allow-unverified-devices

target/debug/iarchive --state-dir "$replica_demo/state" replicate \
  --cycle demo --secondary "$replica_demo/secondary" \
  --primary-artifacts "$replica_demo/primary-artifacts" \
  --secondary-artifacts "$replica_demo/secondary-artifacts" \
  --allow-unverified-devices

target/debug/iarchive --state-dir "$replica_demo/state" verify --deep \
  --cycle demo --secondary "$replica_demo/secondary" \
  --primary-artifacts "$replica_demo/primary-artifacts" \
  --secondary-artifacts "$replica_demo/secondary-artifacts" \
  --allow-unverified-devices
```

The dry run reads and hashes files to calculate missing bytes and detect conflicts;
it writes no media, state, progress or reports. Read results from the console.
It is a preview, so `complete` remains false even when preflight passes. Repeat
the same `replicate` command to exercise resuming: matching files are rehashed,
then skipped rather than copied again.

`--allow-unverified-devices` explicitly acknowledges a byte-only check. It is
necessary for these two folders on one volume and for macOS, where this build
binds filesystem device/inode identities but does not resolve physical backing
disks. It never changes `independent_physical_drives` into a verified result.

## Windows synthetic tests

The [Windows test script](windows-testing.md) runs the same copy/verify workflow
in fresh generated directories, checks safe reruns and backup-bundle digests, and
deliberately corrupts one generated destination file to check invalidation.
Its source snapshots must remain unchanged. Each selected external drive gets
its own same-volume synthetic source and replica; this exercises byte copying,
not independence between drives.

After those tests pass, follow the [Windows two-drive acceptance plan](windows-two-drive-test.md).
Its script creates a fresh synthetic source on one drive and an empty replica on
the other, checks independent PowerShell hashes and restores state from each
drive's backup. It also checks conflicts, missing files and unavailable roots,
with an optional process-interruption/resume check. Do not reuse the test script's
completed fixture: it includes deliberately invalid files.

Windows binds volume GUIDs and root file identities. Native read-only queries
accept distinct disk extents only when drivers report ATA, USB, SATA or NVMe
backing. Pooled, virtual, network, spanned and unavailable device information
retain **NOT VERIFIED**. A refusal does not imply a drive is broken. Adding
`--allow-unverified-devices` permits byte-only verification, but does not establish
physical independence. OS-reported topology is not a guarantee against inaccurate
drivers or virtualized hardware.
If a stable volume/root identity cannot be obtained, the command stops even with
that acknowledgment.

## Conflicts, interruption and capacity

- Existing destination files are independently hashed. A mismatch blocks the
  whole preflight; no conflict is overwritten or renamed automatically. Extra
  destination entries are reported and retained, and prevent completion.
- Linked entries, partial source files, traversal errors, unreadable files,
  non-portable Windows names and case/Unicode collisions prevent completion.
  Unknown file types and undecodable images remain eligible for byte copying.
- New copies stream into uniquely named `.partial` files beneath the secondary
  artifact directory. They are synced and published without clobbering a target.
  Failed/interrupted partials remain outside media and never count as verified.
- Rerun the same command after restoring availability. Completed targets are
  rehashed; unfinished files start again from byte zero. This is resume by file,
  not resume within a partially copied file. Old partials remain for inspection.
- A frozen binding records the initial source paths, lengths, timestamps and
  hashes plus the secondary root/volume identity. Changed source content or a
  replaced root blocks reuse of the cycle. Restore the frozen input or explicitly
  register a new cycle for a deliberately changed archive; reusing a drive letter
  alone cannot establish identity.
- Preflight checks currently available space with the configured margin
  (`free_space_margin`, default 15%) and an additional artifact reserve. Concurrent
  disk usage can still exhaust space afterward; such failures remain incomplete.

Each completed file and copy progress are recorded by one SQLite writer.
Starting another replica operation invalidates previous completion; an unfinished
run is recorded as interrupted on the next invocation. `index` also invalidates
saved verification. `verify --deep` always rehashes both roots, including existing
targets, and performs final inventories to detect changes during the operation.
Pause other programs writing to these directories: this is not an atomic OS
snapshot or a defense against a hostile process racing directory replacements.

Automatic tests inject copy interruption, capacity failure, root replacement,
source edits and destination conflicts. A folder replacement is not a physical
USB disconnect. Real disconnect/reconnect testing should use disposable drives
and synthetic files, then confirm a rerun recovers without treating missing or
partial files as verified. That manual hardware check remains outstanding.

## Results and recovery

Exit 0 means the requested operation completed. Exit 2 includes a report with
findings and invalidates saved replica completion. Exit 1 is an invocation or
state error. Successful replication and verification reports distinguish:

| Field | Meaning |
| --- | --- |
| `local_replica: LOCAL BYTES VERIFIED` | Both local file sets were independently read and matched for this operation. |
| `independent_physical_drives` | OS topology result, or `NOT VERIFIED`; separate from byte equality. |
| `cloud_coverage: NOT VERIFIED` | Nothing here proves all cloud assets or iPhone uploads are captured. |
| `artifact_bundles` | Three completed bundle locations: state, primary artifacts and secondary artifacts. |

Bundles contain identical `manifest.jsonl`, a standalone SQLite backup made through
SQLite's backup API, and `summary.json`. The summary records the manifest and
database SHA-256 digests. The manifest preserves every file separately; it does
not infer cloud IDs or Live Photo/edited-photo pairing. Publication failures
invalidate live SQLite completion and retain partial artifacts for inspection.
The standalone database snapshot captures fully verified per-file progress in
`PUBLISHING`, before the final publication record. Live state becomes `VERIFIED`
only after all bundles have been saved. Restored snapshots therefore require a
new deep verification; they do not claim that publication previously finished.
Already published bundles remain historical byte observations, not evidence that
a later run completed or that files are still intact.

To recover state, verify a bundle's digests and copy its `manifest.sqlite` into a
**new, empty app-state directory**. Do not copy a live SQLite main file by itself,
or mix restored state with old WAL/SHM files. Stop all iarchive processes first.
Run with `--state-dir` pointing to the recovered directory and supply the media
and artifact paths again. Backup roots are hints for their original locations;
relocated mounts need explicit handling rather than silently rebinding them.
Run `verify --deep` before relying on recovered state. Recovery from either
standalone drive backup is covered by synthetic CLI tests.

`status` shows the last `replica_state` and run ID, while its `local_replica` remains
**NOT VERIFIED** because it performs no current filesystem reads. Image decoding,
offsite protection and cloud coverage are separate from local byte verification.
