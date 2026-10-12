# Replication and recovery

Use the [full workflow](workflows.md) for commands in order.

`replicate` copies every eligible raw file, including videos, edits, sidecars,
unknown formats and empty files. It never filters by review decisions, converts
originals, overwrites conflicts or prunes extras. It tries to preserve modification
times. Empty directories, ACLs, extended attributes and alternate data streams
are not archived.

## Paths

All media and artifact directories must already exist. Keep them separate from
app state, with no overlapping roots, symlinks or reparse points.

```text
primary/media/        unchanged source
primary/artifacts/    bundles
secondary/media/      replica
secondary/artifacts/  bundles and partials
app-state/            SQLite, previews and another bundle
```

Each artifact directory must be on its media directory's volume. Supply both
artifact paths every time. `--secondary` can use the path configured by `init`.
A disconnected media root is not recreated.

## Preview, copy and verify

- `--dry-run replicate`: hash and check paths, conflicts and capacity; write nothing.
  A passing preview still reports `complete: false`.
- `replicate`: copy missing files, rehash both roots and publish bundles.
- `verify --deep`: independently rehash both roots and refresh bundles; copy no media.

For two folders on one volume, use `--allow-unverified-devices` to acknowledge a
byte-only check. macOS also needs this flag because physical backing disks are
not resolved. It never turns physical independence into a verified result.

Windows requires distinct known backing disks. Unknown, pooled, virtual, network
or spanned storage remains **NOT VERIFIED**. An unavailable stable root/volume
identity blocks the operation even with the flag. Driver topology is not a
guarantee against inaccurate or virtualized hardware.

Use [Windows two-drive tests](windows-two-drive-test.md) to check synthetic copies
across physical drives. Physical USB disconnect/reconnect remains untested.

## Conflicts and interruptions

- Conflicts and extra destination files are retained and block completion.
- Missing, unreadable, partial or linked inputs and name collisions block completion.
  Undecodable media can still be backed up as bytes.
- New copies use `.partial` files in secondary artifacts. Only completed files
  are published, without overwriting an existing destination.
- Reruns rehash matching files and skip them. Unfinished files restart from zero;
  retained partials are never trusted.
- Changed source content or a replaced root blocks reuse of the frozen cycle.
  Investigate it; use a new cycle only for an intentionally changed snapshot.
- Capacity checks include the configured margin (default 15%) and an artifact
  reserve. Later disk exhaustion still leaves the operation incomplete.

Pause other programs writing to these roots. This is not an OS snapshot.
A new index, audit or replica operation invalidates saved replica completion.

## Results and recovery

| Field | Meaning |
| --- | --- |
| `local_replica: LOCAL BYTES VERIFIED` | Both local file sets matched for this operation. |
| `independent_physical_drives` | Windows topology result, or `NOT VERIFIED`. |
| `cloud_coverage: NOT VERIFIED` | Cloud completeness was not established. |
| `artifact_bundles` | Bundle paths in state and both artifact directories. |

Exit 0 means the operation completed. Exit 2 reports findings and invalidates
completion. Exit 1 is an invocation or state error.

Each bundle contains identical `manifest.jsonl`, `manifest.sqlite` and
`summary.json`. The summary records manifest/database SHA-256 digests. A bundle
is a historical observation; later changes or failed runs can invalidate it.

To recover:

1. Stop all iarchive processes.
2. Check the bundle's digests.
3. Copy `manifest.sqlite` into a new, empty state directory. Do not mix old WAL/SHM
   files with it or copy only the main file of a live database.
4. Use that directory with `--state-dir` and supply the original media/artifact paths.
5. Run `verify --deep` before relying on the restored state.

Backups capture `PUBLISHING`, before final publication. Restored state therefore
needs fresh verification. Relocated roots need explicit handling; reusing a drive
letter does not preserve identity.

`status` shows saved state and keeps local bytes **NOT VERIFIED** because it does
not read current media. See [safety](safety.md) for other limits.
