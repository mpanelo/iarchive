# Architecture

## Components

| Component | Responsibility |
| --- | --- |
| `iarchive` | CLI, configuration, orchestration and embedded gallery. |
| `iarchive-core` | Domain types, path rules and analysis interfaces. |
| `iarchive-store` | SQLite state, migrations, candidates and review notes. |
| `iarchive-fs` | Inventory, hashes, copying and drive checks. |
| `image-worker` | Local image decoding and measurements, managed by uv. |

Rust uses edition 2024 with a declared minimum of 1.85. That minimum is not yet
independently tested. Dependencies include bundled rusqlite, clap, serde/TOML,
SHA-256, walkdir and fs2. Cloud operations are unavailable; the supplied inventory
provider returns an error and exposes no deletion method.

## State

SQLite uses WAL, foreign keys and one writer. A state-directory lock serializes
CLI operations. Newer database schemas are rejected.

JSONL remains version 1. Files use a cycle ID and normalized relative path;
absolute roots are mount hints, not permanent identities.

Import records `IMPORTED`, not completed cloud download. Scans commit per-file
progress, invalidate prior completion and mark missing files only after complete
traversal. Changed hashes invalidate analysis and related candidates.

## Paths and hashes

Links/reparse points, traversal and unsafe output paths are refused. Non-Unicode
or non-normalized names stop traversal. Reserved/long Windows names are warnings;
case/NFC collisions block completion. NFC plus Unicode lowercase is an
approximation of filesystem name equivalence.

Hashing streams bytes with a 1 MiB reader and 64 KiB blocks, checking size/mtime
before and after. Scan caches use path, size and mtime; `index --deep` bypasses
that cache. Replica checks always independently hash both roots.

Exact duplicates mean equal bytes, not equal cloud assets. Inventory classification
uses extensions. Metadata-proven local relationships are recorded separately;
filenames never prove variant links or cloud identity. Cross-cycle facts are
saved observations.

## Copying and bundles

Preflight freezes the full raw inventory, binds root/volume identities, checks
capacity and hashes existing targets. Conflicts, extras, partials, collisions and
traversal errors block it. Visual candidates never filter the raw archive.

New copies use retained `.partial` files outside media, sync bytes/mtime and
publish through `tempfile::persist_noclobber`. Final inventories rehash both roots.
Resume restarts unfinished files from zero; it never trusts old partial bytes.

Success publishes matching manifest/database/summary bundles in app state and
both artifact directories. [SQLite backup](https://docs.rs/rusqlite/0.40.2/rusqlite/backup/index.html)
includes WAL contents. Publication is not a cross-volume transaction; failed runs
invalidate completion and retain historical artifacts. See [recovery](replication.md#results-and-recovery).

Windows combines volume GUIDs, root identities,
[disk extents](https://learn.microsoft.com/windows/win32/api/winioctl/ni-winioctl-ioctl_volume_get_volume_disk_extents)
and [storage queries](https://learn.microsoft.com/windows/win32/api/winioctl/ni-winioctl-ioctl_storage_query_property).
Unix binds device/inode identities but does not resolve physical disks.

## Image worker

Python 3.12–3.14 uses Pillow, pillow-heif, ImageHash and NumPy from uv.lock.
Rust launches the venv's Python directly so timeouts stop the actual decoder.
Each invocation handles one file; JSONL supports batches up to 64. Per-file
startup isolates crashes but costs throughput.

Protocol 1 carries request/file IDs, input, authorized roots and requested
algorithms. The worker limits decoding to 64 million pixels. Rust validates
versions, IDs, metrics and thumbnail containment. A failed file produces a
finding while other files continue.

EXIF orientation and color conversion affect previews only. Features include
64-bit pHash, brightness percentiles, near-black share, edge energy and sharpness.
Dark score ≥0.65 proposes review. Similarity uses distance ≤6, a 10% aspect-ratio
limit, at most 64 neighbors and one pair per image. Dark/uniform images are
excluded. These heuristics are not exhaustive; crops can be missed.

Audit hashes images before and after analysis. Cache keys include source hash
and algorithm version. Missing previews trigger analysis; a full cache reports
an error. Default capacity is 1 GiB, with a 1 MiB reserve per new preview.
Videos, RAW, unknown files and decode failures prevent a complete visual audit;
multi-frame images are rejected.

## Gallery

Axum 0.8 and a Tokio current-thread runtime serve embedded HTML/CSS/JavaScript
on IPv4 loopback. A random 256-bit token in the URL fragment authorizes API and
thumbnail requests. Host/Origin checks, no CORS, no-store responses, CSP and a
16 KiB body limit protect the interface. See Axum's
[serve API](https://docs.rs/axum/0.8.9/axum/fn.serve.html) and
[body limit](https://docs.rs/axum/0.8.9/axum/extract/struct.DefaultBodyLimit.html).

Review holds the state lock. It loads saved groups/findings and shows 12 groups
per page. Allowlisted thumbnail IDs can include explicit cross-cycle members.
Preview reads check containment and links, use no-follow leaf opens, cap bytes
at 8 MiB and check JPEG markers. Originals are never served.

Keep, flag, defer and reset append notes. Fingerprints bind the cycle, scans,
group ID/type/version/explanation and recorded member metadata/features.
Regenerated groups cannot inherit old notes through reused IDs. Reset clears
the current note while retaining history. Notes do not change replica completion;
future whole-database backups include them.

## Limits

- Doctor checks availability and tool readiness, not physical independence or
  cloud completeness. uvx-only icloudpd installations remain unverified.
- File-size memory is bounded; lists still grow with file count. Large-library
  throughput and gallery performance need benchmarks.
- Directories are not OS snapshots. Pause other writers. Path checks do not
  isolate the app from hostile local processes racing directory changes.
- Real USB disconnect/reconnect and minimum-compiler compatibility remain untested.

See [safety](safety.md) and [recorded checks](development.md#recorded-manual-checks).
