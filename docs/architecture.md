# Architecture and implementation decisions

## Scope and crate boundaries

The implementation uses four workspace crates. The binary package is `iarchive` in `crates/iarchive-cli`; libraries are `iarchive-core`, `iarchive-store`, and `iarchive-fs`. Cloud operations and review decisions are not implemented. `CloudProvider` exposes inventory only and its supplied implementation returns an error. There is no destructive provider method or CLI route.

Rust 2024 with a declared minimum Rust 1.85, bundled rusqlite, serde/TOML, clap, tracing, walkdir, fs2, SHA-256, and Unicode NFC normalization are the selected libraries. These selections have not yet been dependency-resolved or fully compiled in this sandbox. The minimum compiler version also needs a CI check after resolution; current CI validates stable Rust.

`StorageProvider` streams file facts to one SQLite writer. `ImageAnalyzer` isolates a replaceable worker. Paths are canonical root hints plus cycle ID and slash-separated relative paths. Absolute paths are hints for the current mount, not permanent drive identity. Windows reparse points and Unix symlinks are explicit errors and are never descended. Non-Unicode/non-portable colon/backslash components stop traversal with a scan issue rather than being silently normalized to another name. Windows reserved names/long paths generate portability warnings; case/NFC collisions block inventory completion.

## State and resuming

SQLite schema version 1 creates cycles, scan runs, file facts, analysis, candidates, memberships, issues, and transactional transition history. Foreign keys and WAL are enabled. An advisory lock serializes CLI writers. A newer DB schema is rejected. Status shows last recorded state, not live filesystem assurance.

Existing downloads enter `IMPORTED`, not `DOWNLOADED`: this app has no evidence that iPhone sync or remote download coverage completed. A scan first records `INDEXING` and invalidates prior completion/candidates. Each completed file is committed independently, so an interrupted run can reuse unchanged path/size/nanosecond-mtime hashes. Missing records are marked only after a complete traversal; interrupted traversals do not invent missing files. Errors and retained missing records prevent completion. Restoring a missing file and rerunning can recover completion; accepting a changed snapshot requires a separate cycle, not silently dropping its history.

Hashing uses a 1 MiB reader and 64 KiB blocks, with size/mtime checks before and after. Every regular file is hashed, including unknown formats and sidecars. Exact equality means byte equality only; filename/stem never proves equivalence or a cloud asset ID. Known decode failures are excluded from audit candidates. Duplicate results include cross-cycle provenance, although other cycles' cached facts are last-scan records, not current verification. Hash/source changes invalidate associated analysis and candidates. Mtime caching can miss deliberately preserved metadata changes: `index --deep` bypasses it. Independent replica verification is not implemented.

The JSONL export streams from SQLite, includes a versioned summary and all file records (including missing/error records), and uses a temporary file, fsync, and a no-clobber persist. Explicit exports cannot write inside media or app state. They are local inventory manifests, not verified backup manifests. The app does not create independently verified manifests or state backups on two drives.

## Worker and audit

Python requires 3.12–3.14 and uses Pillow, pillow-heif, ImageHash, and NumPy. Dependencies are declared in a uv project. The initial uv.lock is pending dependency access; do not invent hashes or claim compatibility has been validated. [pillow-heif's official plugin documentation](https://pillow-heif.readthedocs.io/en/latest/pillow-plugin.html) documents registration and HEIF image opening.

Create the venv with uv, then Rust executes that venv's Python directly from the worker project. This is a deliberate execution detail: killing `uv run` alone can leave a child decoder alive on Windows. Direct execution gives a timeout for the actual decoder. No shell strings are interpolated. Each invocation processes one file; the Python JSONL service supports bounded batches up to 64. Per-file startup is slower than a resident worker, but isolates native decoder crashes and caps memory. A persistent bounded-batch adapter is a future performance improvement.

Protocol version 1 carries request/file IDs, immutable absolute input, authorized archive/cache roots, four requested algorithms, and a preview dimension cap. Process arguments establish root authority; JSON cannot change it. The worker rejects traversal/reparse points, limits input line length and batch size, bounds decoding to 64 million pixels, and returns no features on failure. Rust checks response ID/version, finite metric ranges, hash width, and canonical thumbnail containment. A process timeout, invalid response, or failed decode is an error for that file; other files continue.

Pillow EXIF transpose is applied to preview pixels only. Previews use RGB, white compositing for transparency, no intentional source mutation, and atomic cache replacement. pHash is 64 bits; dark scoring combines luminance p95, the fraction below 16, and low edge energy. Sharpness is luminance Laplacian variance and is descriptive only. A score ≥0.65 proposes review, never deletion. The exact thresholds are initial heuristics, not empirically validated judgments about nighttime photos.

Rust uses a BK-tree with distance ≤6 and a 10% aspect-ratio restriction. Uniform/dark images are excluded from similarity matching. Candidate generation is deliberately bounded to at most 64 matching neighbors and one pair suggestion per image, so homogeneous hashes do not create quadratic output. This is a non-exhaustive candidate list, explicitly labeled in reports. Cropped variants with substantial aspect changes can be missed; there is no trusted capture-time narrowing yet.

Audit refreshes membership before analysis and deeply hashes every image before/after consuming cached or new features. Analysis cache keys include source SHA-256 and algorithm version; missing/invalid previews cause a rescan. Bump the algorithm version when changing feature definitions or resolved worker dependencies. Thumbnails default to a 1 GiB cache capacity, with a 1 MiB reserve per new preview. Cache exhaustion reports an error, never evicts originals or quietly omits analysis. Videos, RAW, unknown files, partials, and decode failures cannot produce a full `AUDITED` claim. Sidecars are preserved as bytes and are not visual media.

## Known limits and unverified work

- Cargo.lock/uv.lock, full clippy/tests, HEIC wheels, and Windows/macOS CI results are pending network-enabled validation. CI currently bootstraps missing locks and uploads them; commit validated locks and remove bootstrap before release.
- Doctor reports free space, availability, permission metadata, optional tool checks, and worker readiness. It does not prove separate physical disks or exercise archive writes. `icloudpd` installations available only through uvx remain unverified; no upstream install/download/auth is launched by doctor. Version 1.32.3 is a candidate pin from the handoff, not a compatibility claim.
- No native volume/filesystem identity API or swapped-drive binding is implemented. Drive identity, capacity guards, and independent physical disks are not proven by the current commands.
- Classification uses extension, not MIME/content metadata. Capture timestamps, ContentIdentifier, original/edited labels and Live Photo/RAW relationships remain unknown. Each file variant stays independent.
- Memory is bounded with respect to file byte size. Candidate/path/feature lists use memory proportional to file count. Per-file SQLite durability and Python startup favor resume correctness over HDD throughput; a 200 GiB benchmark remains unrun.
- Source directories are not OS snapshots. Pause concurrent downloader/file edits for a stable scan. Checks resist ordinary path escapes, but do not claim a sandbox against an adversarial process racing directory replacements. Future high-risk actions require stronger stable handles and revalidation.
- The collision key uses NFC plus Unicode lowercase, an approximation of native filesystem equivalence. Additional platform-specific name equivalences are not fully modeled. Doctor/audit refuse a media root containing the system temp directory, where subprocess capture files would otherwise touch that root.
- Multi-frame images are not yet fully analyzed; the decoder rejects them rather than claiming the whole container was visually scanned.
