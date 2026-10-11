# Build and test report

## Windows acceptance script checks — 2026-10-11

Replica database backups now retain their writable creation handle until SQLite
finishes, then flush that handle. The previous read-only reopen cannot satisfy
Windows' flush access requirements and is the likely cause of the two failed
replication tests in the published Windows run. Failed exit-code assertions now
include stdout JSON as well as stderr. A Windows rerun is still required to
confirm the reported CI failure is resolved.

The two-drive guide now uses `scripts/windows/test-two-drives.ps1` for synthetic
copying, independent hashes, bundle recovery and failure handling. Three shared
PowerShell regression cases passed locally with a temporary portable PowerShell
7.5.4 runtime on macOS: the acceptance workflow, same-volume topology refusal,
and interruption of only an owned child, including cleanup after a failed
observation. All Windows script files parsed successfully, and the CI workflow
YAML was checked. CI is configured to run the cases under both Windows shells.

After the backup fix, all 33 Rust tests passed with the Python worker required,
along with formatting, workspace Clippy, 28 image-worker tests, three repository
tests and 62 offline cloud-probe tests. The three PowerShell cases passed again
against the updated executable. cargo-deny passed after refreshing its public
advisory database; the existing duplicate `syn`/`winnow` warnings remain.

A separate generated 512 MiB file was interrupted during copying, resumed and
deeply verified. Source bytes/timestamps and the retained partial's digest stayed
unchanged. These local checks explicitly acknowledged same-volume byte testing;
they do not prove Windows runtime behavior, physical-drive independence or USB
disconnect/reconnect handling.

## Replication checks — 2026-10-11

The new raw-copy and deep-verification commands were exercised on macOS using
only synthetic files and temporary directories. All 33 Rust tests passed with
the Python worker required, including interruption/retry, root replacement,
capacity preflight, conflicts, changed source snapshots, incomplete publication,
schema migration and recovery from either drive's standalone database backup.
Formatting and workspace Clippy passed with warnings treated as errors.

All 28 image-worker tests, three repository tests (including the offline cloud
policy suite), and 62 offline cloud-probe tests passed. cargo-deny passed after
refreshing the public advisory database; the existing duplicate `syn`/`winnow`
warnings remain. Cargo.lock records the filesystem crate's added dependencies;
SQLite's backup feature is enabled without changing its resolved version.

The Windows filesystem crate and its test targets passed a cross-target compile
and Clippy check. This is **not Windows runtime verification**. PowerShell smoke
changes are prepared for both CI shells but have not run on this host. Windows
runtime, physical USB disconnect/reconnect and a large-library benchmark remain
outstanding. The [Windows two-drive acceptance plan](windows-two-drive-test.md)
provides the next manual checks; it has not been executed on Windows. See
[replication and recovery](replication.md) for command behavior and limits.

## Earlier inventory and image-audit checks

Date: 2026-10-10. Local host: macOS, Apple Silicon; Rust/Cargo 1.96.0, uv 0.12.3, Python 3.14.7. Development uses only synthetic media and temporary directories. No private archives or cloud accounts were accessed.

## Completed local checks

- Rust formatting and Clippy across all workspace targets/features passed with warnings treated as errors.
- All 21 Rust tests passed, including the required Python worker integration test, byte-count overflow/negative-value regression coverage, and normalized manifest-path restrictions.
- All 28 Python tests passed, including HEIC, every EXIF rotation/mirroring orientation in JPEG and HEIC, decoder failures, path restrictions, and unchanged source bytes/mtime.
- Both Python SQLite schema/query tests passed.
- cargo-deny passed advisory, license, dependency-version, and source checks. Duplicate transitive versions of `syn` and `winnow` produce warnings; no security advisories are ignored.
- Cargo.lock and image-worker/uv.lock contain the resolved dependency versions used by these checks.

The SQLite compiler failure was fixed with checked conversions between public unsigned byte counts and SQLite's signed integers. HEIC fixture metadata is serialized to EXIF bytes so the encoder writes the intended orientation property. Path expectations distinguish native Windows separators from forward-slash manifest paths; non-normalized manifest input is rejected on every platform.

## Continuous integration

The [checks workflow](https://github.com/mpanelo/iarchive/actions/workflows/ci.yml) runs formatting, Clippy, Rust tests, and Python/HEIC tests on macOS and Windows, with the worker required. A separate Linux job runs cargo-deny. CI uses the committed locks without dependency bootstrap or lockfile replacement. Consult the workflow run for a specific commit's platform results.

## Remaining limits

Synthetic checks do not prove behavior with real Windows external drives, a complete iCloud library, or a 200 GiB library. The declared Rust minimum version has not been independently tested. The archive CLI has no cloud authentication, deletion or quarantine. Replica commands now distinguish independently checked local bytes from OS-reported physical independence and unverified cloud coverage; saved status does not reverify files.
